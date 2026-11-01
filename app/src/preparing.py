"""A new ingestion job's preparation: the state a job is in until it can run, what Create was asked for, and the
databases set aside on the way, as the job page, the jobs list and the server's checks say them.

A job goes through three states, kept on its record in the job's options (OPTION):
- Preparing: its chosen databases' tables are read (reading), then its tables are written (writing), then its
  Databricks jobs are made (syncing);
- Needs you: some of its databases are set aside, each with why (it couldn't be read, or its tables clash with another
  job's or with each other), until someone reads them again, leaves them out, or changes the job's target names;
- Ready: it runs as any job does.

A job with no record is Ready: every job made before the record. The record also keeps what Create was asked for (the
settings, the sizing, the secret scope and the source) and who asked (the maker), so the write, which may run later on
a thread with no request, is held to the job as it was made and to its maker as they are then. Only the app's own steps
write it (main.MANAGED_OPTIONS). While a job isn't Ready, nothing starts a run of it, and its schedule stays off until it
is: the schedule Create was asked for is kept as "turn on when ready". Pure, so the rules are tested without Lakebase or
Databricks."""
import json
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set

import rediscovery

OPTION = "prepare"
PREPARING, NEEDS_YOU, READY = "preparing", "needs_you", "ready"
STATES = (PREPARING, NEEDS_YOU, READY)
READING, WRITING, SYNCING = "reading", "writing", "syncing"
STAGES = (READING, WRITING, SYNCING)
STAGE_WORDS = {READING: "reading tables", WRITING: "writing the job's tables",
               SYNCING: "making the job's Databricks jobs"}
# Why a database is set aside. One that couldn't be read, or whose tables clash, waits for someone's choice. One the
# login can't open in a whole-server choice is only left out, as the whole-server read always has, and listed.
UNREADABLE, CLASH, CLOSED = "unreadable", "clash", "closed"
KINDS = (UNREADABLE, CLASH, CLOSED)
NEEDS_ACTION = (UNREADABLE, CLASH)
# The job parameters that say which secret scope a job reads its logins from, and whether it trusts the server's
# certificate: part of the source Create checked, so they are kept as asked while the job prepares.
SCOPE_PARAMETERS = ("secret_scope", "trust_server_certificate")
# The target name templates that "Change the job's target names" changes in the record's settings.
TARGET_FIELDS = ("sink_catalog", "sink_schema", "sink_table")
# The most set-aside or left-out databases the job page lists at once; the counts are always whole.
LISTED = 200
# The reason a refusal's detail carries when the job isn't Ready, so a caller reads it from the detail, not its text.
NOT_READY_REASON = "not_ready"

# The minutes left while reading: the databases left over the recent rate (those finished in the last RATE_WINDOW_S),
# shown only once there is something to go on (ESTIMATE_AFTER_DATABASES finished, or ESTIMATE_AFTER_S of reading). The
# recent rate weighs RECENT_WEIGHT against the rate since the reading began, so one slow minute doesn't make it jump,
# and the minutes are rounded more coarsely the more there are.
RATE_WINDOW_S = 120
ESTIMATE_AFTER_DATABASES = 20
ESTIMATE_AFTER_S = 30
RECENT_WEIGHT = 0.75
ROUND_TO_FIVE_FROM = 15
ROUND_TO_TEN_FROM = 60


def _now(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def record_of(options: Optional[dict]) -> Optional[dict]:
    """The job's record, or None for a job made before it (which is Ready)."""
    record = (options or {}).get(OPTION)
    return dict(record) if isinstance(record, dict) else None


def state_of(options: Optional[dict]) -> str:
    record = record_of(options)
    return record.get("state") if record and record.get("state") in STATES else READY


def is_ready(options: Optional[dict]) -> bool:
    return state_of(options) == READY


def stage_of(options: Optional[dict]) -> Optional[str]:
    record = record_of(options)
    return record.get("stage") if record and state_of(options) != READY else None


def new_record(maker: str, settings: dict, sizing: dict, parameters: Optional[dict], source: dict, turn_on: bool,
               now: Optional[datetime] = None, state: str = READY, stage: Optional[str] = None) -> dict:
    """The record Create writes with the job, in the job's own transaction: what it was asked for and by whom. settings
    are the job's settings as asked (new_job.settings_of), sizing its tables per group and groups per server, parameters
    its run parameters as checked (only the secret scope and trust are kept), source the upload or discoveries it was
    chosen from ({kind, source_id}; the parts are the job's job_selection rows), and turn_on whether its schedule is to
    be on once it is ready."""
    at = _now(now)
    return {"state": state, "stage": stage if state != READY else None, "writing_started": False,
            "maker": maker or None, "made_at": at, "updated_at": at,
            "settings": dict(settings or {}),
            "sizing": {"tables_per_group": (sizing or {}).get("tables_per_group"),
                       "groups_per_server": (sizing or {}).get("groups_per_server")},
            "scope": {k: (parameters or {})[k] for k in SCOPE_PARAMETERS if k in (parameters or {})},
            "source": {"kind": (source or {}).get("kind"), "source_id": (source or {}).get("source_id")},
            "turn_on": bool(turn_on), "turn_on_by": (maker or None) if turn_on else None,
            "set_aside": {}, "left_out": {}, "progress": {}}


def moved(record: dict, state: Optional[str] = None, stage: Optional[str] = None, progress: Optional[dict] = None,
          now: Optional[datetime] = None) -> dict:
    """The record moved on: to a state and stage (each left as it is when None), with the progress the prepare task
    reports (merged over the last). Once the write has begun it stays begun, so the table settings wait for Ready."""
    out = dict(record)
    if state is not None:
        if state not in STATES:
            raise ValueError(f"not a state: {state}")
        out["state"] = state
    if stage is not None:
        if stage not in STAGES:
            raise ValueError(f"not a stage: {stage}")
        out["stage"] = stage
    if out.get("stage") in (WRITING, SYNCING):
        out["writing_started"] = True
    if progress:
        out["progress"] = {**(out.get("progress") or {}), **progress}
    if out["state"] == READY:
        out["stage"] = None
    out["updated_at"] = _now(now)
    return out


def ready(record: dict, kept_paused: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """The record once the job is Ready. kept_paused says why its schedule stayed off although it was to turn on (whoever
    asked for it may no longer run what it starts)."""
    out = moved(record, state=READY, now=now)
    out["ready_at"] = out["updated_at"]
    out["kept_paused"] = kept_paused
    return out


def with_turn_on(record: dict, turn_on: bool, user: str, now: Optional[datetime] = None) -> dict:
    """The record with the schedule's wish for Ready changed: Pause while preparing keeps it off once ready."""
    out = dict(record, turn_on=bool(turn_on), turn_on_by=(user or None) if turn_on else None)
    out["updated_at"] = _now(now)
    return out


def words(options: Optional[dict]) -> str:
    """Where the job stands, in a few words for a refusal: "preparing: reading tables", "needs you: 3 databases set
    aside", or "ready"."""
    state = state_of(options)
    if state == READY:
        return "ready"
    if state == NEEDS_YOU:
        waiting = len(waiting_entries(record_of(options)))
        return f"needs you: {waiting:,} {'database' if waiting == 1 else 'databases'} set aside"
    return f"preparing: {STAGE_WORDS.get(stage_of(options) or READING, STAGE_WORDS[READING])}"


def run_refusal(options: Optional[dict]) -> Optional[str]:
    """Why nothing may start a run of the job now, or None once it is Ready."""
    if is_ready(options):
        return None
    return f"the job isn't ready yet ({words(options)}): Run now is available when the job is ready"


def turn_on_refusal(options: Optional[dict]) -> Optional[str]:
    """Why the job's schedule may not be turned on now, or None once it is Ready."""
    if is_ready(options):
        return None
    return (f"the job isn't ready yet ({words(options)}): its schedule is available when the job is ready, and turns "
            "on then if it was asked to")


def tables_refusal(options: Optional[dict]) -> Optional[str]:
    """Why what the job ingests (its tables, databases and groups) may not be changed now, or None once it is Ready."""
    if is_ready(options):
        return None
    return f"the job isn't ready yet ({words(options)}): what it ingests can be changed when the job is ready"


def settings_refusal(options: Optional[dict]) -> Optional[str]:
    """Why the job's table settings (targets, load type, fan-in, write modes, identity columns) and its tables per group
    may not be changed now. While its tables are still being read they may: they are the record's, and the write takes
    them. Once the write has begun they wait for Ready, so one job never has two rules."""
    record = record_of(options)
    if is_ready(options) or record is None or not record.get("writing_started"):
        return None
    return (f"the job isn't ready yet ({words(options)}): its table settings are kept as Create was asked while its "
            "tables are written, and are editable when the job is ready")


def scope_changed(before: Optional[dict], after: Optional[dict]) -> List[str]:
    """The scope parameters an edit changes (SCOPE_PARAMETERS), by name."""
    return [k for k in SCOPE_PARAMETERS if str((before or {}).get(k) or "") != str((after or {}).get(k) or "")]


def scope_refusal(options: Optional[dict], changed: List[str]) -> Optional[str]:
    """Why an edit may not change the job's secret scope or trust now, or None."""
    if is_ready(options) or not changed:
        return None
    return (f"the job isn't ready yet ({words(options)}): {', '.join(changed)} {'is' if len(changed) == 1 else 'are'} "
            "kept as Create checked them until the job is ready")


def database_key(server: str, database: str) -> str:
    """A database's key in the record: its server as the job keeps it (rediscovery.server_key) and its name, in lower
    case, as JSON text, for an object key."""
    return json.dumps([rediscovery.server_key(server), str(database or "").lower()])


def entry(server: str, database: str, kind: str, why: str, target: Optional[str] = None, job: Optional[str] = None,
          now: Optional[datetime] = None) -> dict:
    """One database set aside: where it is, why (the server's message, or what its tables clash with), and for a clash
    the target and the other job, when there is one."""
    if kind not in KINDS:
        raise ValueError(f"not a kind of set-aside database: {kind}")
    return {"server": server, "database": database, "kind": kind, "why": str(why or "")[:2000], "target": target,
            "job": job, "at": _now(now)}


def waiting_entries(record: Optional[dict]) -> List[dict]:
    """The set-aside databases that wait for someone's choice: couldn't be read, or clash, and not yet marked to be read
    again or planned again."""
    return [e for e in ((record or {}).get("set_aside") or {}).values()
            if e.get("kind") in NEEDS_ACTION and not e.get("again") and not e.get("replan")]


def refreshed(record: dict, now: Optional[datetime] = None) -> dict:
    """The record's state after its set-aside databases changed: Needs you while one waits for a choice, else Preparing
    again (the prepare task goes on: it reads, plans again, or makes the job Ready). A Ready job stays Ready."""
    out = dict(record)
    if out.get("state") == READY:
        return out
    out["state"] = NEEDS_YOU if waiting_entries(out) else PREPARING
    out["updated_at"] = _now(now)
    return out


def with_set_aside(record: dict, entries: Iterable[dict], now: Optional[datetime] = None) -> dict:
    """The record with these databases set aside (each replacing an earlier one of the same database), and its state
    refreshed. A database already left out stays out."""
    set_aside = dict(record.get("set_aside") or {})
    left_out = record.get("left_out") or {}
    for item in entries:
        key = database_key(item["server"], item["database"])
        if key not in left_out:
            set_aside[key] = dict(item)
    return refreshed(dict(record, set_aside=set_aside), now)


def cleared(record: dict, keys: Iterable[str], now: Optional[datetime] = None) -> dict:
    """The record once the prepare task has read or planned these databases again and taken them: off the list."""
    set_aside = {k: v for k, v in (record.get("set_aside") or {}).items() if k not in set(keys)}
    return refreshed(dict(record, set_aside=set_aside), now)


def _chosen(record: dict, keys: Optional[Iterable[str]], kinds: Iterable[str]) -> List[str]:
    """The set-aside databases an action names (all of its kinds when keys is None) that are there and of those kinds."""
    set_aside = record.get("set_aside") or {}
    wanted = set_aside if keys is None else [k for k in dict.fromkeys(keys) if k in set_aside]
    return [k for k in wanted if set_aside[k].get("kind") in set(kinds)]


def read_again(record: dict, keys: Optional[Iterable[str]], user: str,
               now: Optional[datetime] = None) -> tuple:
    """Read them again: the databases that couldn't be read (those named, or all of them) are marked to be read again,
    and the job is Preparing again when none waits for a choice. The read itself is the prepare task's. Returns
    (the record, how many were marked)."""
    chosen = _chosen(record, keys, (UNREADABLE,))
    at = _now(now)
    set_aside = dict(record.get("set_aside") or {})
    for key in chosen:
        set_aside[key] = dict(set_aside[key], again={"by": user or None, "at": at})
    out = refreshed(dict(record, set_aside=set_aside), now)
    if chosen and out["state"] == PREPARING:
        out["stage"] = READING
    return out, len(chosen)


def leave_out(record: dict, keys: Optional[Iterable[str]], user: str, now: Optional[datetime] = None) -> tuple:
    """Leave them out: the set-aside databases named (or all that wait for a choice) come out of the job's choice: off
    the set-aside list, onto the left-out one, which the write and a later re-discovery leave out. The job goes on.
    Returns (the record, how many were left out)."""
    chosen = _chosen(record, keys, NEEDS_ACTION) if keys is not None else [
        database_key(e["server"], e["database"]) for e in waiting_entries(record)]
    at = _now(now)
    set_aside = dict(record.get("set_aside") or {})
    left_out = dict(record.get("left_out") or {})
    for key in chosen:
        left_out[key] = dict(set_aside.pop(key), left_out={"by": user or None, "at": at})
    return refreshed(dict(record, set_aside=set_aside, left_out=left_out), now), len(chosen)


def change_targets(record: dict, templates: dict, user: str, now: Optional[datetime] = None) -> tuple:
    """Change the job's target names: only the target templates given (TARGET_FIELDS) change in the record's settings,
    and the databases set aside for a clash are marked to be planned again with them, by the prepare task. Returns
    (the record, how many were marked)."""
    at = _now(now)
    settings = dict(record.get("settings") or {})
    settings.update({k: templates[k] for k in TARGET_FIELDS if k in templates})
    set_aside = dict(record.get("set_aside") or {})
    marked = 0
    for key, item in set_aside.items():
        if item.get("kind") == CLASH:
            set_aside[key] = dict(item, replan={"by": user or None, "at": at})
            marked += 1
    out = dict(record, settings=settings, set_aside=set_aside, targets_changed={"by": user or None, "at": at})
    out = refreshed(out, now)
    if marked and out["state"] == PREPARING:
        out["stage"] = WRITING
    return out, marked


def left_out_keys(options: Optional[dict]) -> Set[str]:
    """The databases the job's choice no longer takes (Leave them out), by database_key."""
    return set(((record_of(options) or {}).get("left_out") or {}).keys())


def without_left_out(rows: List[dict], options: Optional[dict]) -> List[dict]:
    """These rows (each with its server and src_database) less the databases the job left out, for a re-discovery: a
    database left out is no longer the job's, so it isn't added again as new."""
    keys = left_out_keys(options)
    if not keys:
        return list(rows)
    return [r for r in rows if database_key(r["server"], r["src_database"]) not in keys]


def rounded_minutes(minutes: float) -> int:
    """Whole minutes, at least one; to the nearest 5 from ROUND_TO_FIVE_FROM, and the nearest 10 from
    ROUND_TO_TEN_FROM."""
    if minutes < 1:
        return 1
    if minutes < ROUND_TO_FIVE_FROM:
        return int(minutes + 0.5)
    step = 5 if minutes < ROUND_TO_TEN_FROM else 10
    return step * int(minutes / step + 0.5)


def minutes_left(left: int, finished: int, finished_recently: int, elapsed_s: float,
                 window_s: float = RATE_WINDOW_S) -> Optional[int]:
    """About how many minutes the reading has left: the databases left over the rate, or None while there is nothing to
    go on (fewer than ESTIMATE_AFTER_DATABASES finished and less than ESTIMATE_AFTER_S of reading), nothing has
    finished, or nothing is left. The rate is the recent one (finished_recently over the last window_s, or over the
    reading so far while it is shorter) blended with the rate since the reading began."""
    if left <= 0 or finished <= 0 or elapsed_s <= 0:
        return None
    if finished < ESTIMATE_AFTER_DATABASES and elapsed_s < ESTIMATE_AFTER_S:
        return None
    recent = finished_recently / min(window_s, elapsed_s)
    overall = finished / elapsed_s
    rate = RECENT_WEIGHT * recent + (1 - RECENT_WEIGHT) * overall
    if rate <= 0:
        return None
    return rounded_minutes(left / rate / 60)


def percent(done: int, total: int) -> int:
    """Whole percent done, 100 only when all is."""
    if total <= 0:
        return 0
    return 100 if done >= total else min(99, 100 * done // total)


def reading_view(reads: Optional[dict]) -> Optional[dict]:
    """Where the reading of the job's databases stands, from its claimed draft rows (queries.job_reads_sql): of how
    many, how many are read (done or failed), the tables so far, the percent and the minutes left."""
    if not reads:
        return None
    count = lambda key: int(reads.get(key) or 0)
    databases, done, failed, pending = count("databases"), count("done"), count("failed"), count("pending")
    finished = done + failed
    return {"databases": databases, "read": finished, "done": done, "failed": failed, "pending": pending,
            "tables": count("tables"), "percent": percent(finished, databases),
            "minutes_left": minutes_left(pending, finished, count("recent"), float(reads.get("elapsed_s") or 0))}


def _listed(entries: Iterable[dict]) -> List[dict]:
    return sorted(entries, key=lambda e: (str(e.get("server") or "").lower(), str(e.get("database") or "").lower()))[:LISTED]


def view(options: Optional[dict], reads: Optional[dict] = None) -> dict:
    """What the job page shows: the state and stage with their words, whether its schedule turns on when it is ready,
    the reading's progress (reads, from queries.job_reads_sql) and the write's (as the prepare task reports it), and
    the set-aside and left-out databases (LISTED of each, with the counts)."""
    record = record_of(options) or {}
    state = state_of(options)
    set_aside = list((record.get("set_aside") or {}).values())
    left_out = list((record.get("left_out") or {}).values())
    waiting = waiting_entries(record)
    by_kind = {kind: sum(1 for e in set_aside if e.get("kind") == kind) for kind in KINDS}
    waiting_by_kind = {f"waiting_{kind}": sum(1 for e in waiting if e.get("kind") == kind) for kind in NEEDS_ACTION}
    settings = record.get("settings") or {}
    return {"state": state, "stage": stage_of(options), "words": words(options),
            "recorded": bool(record), "made_at": record.get("made_at"), "ready_at": record.get("ready_at"),
            "turn_on": bool(record.get("turn_on")), "kept_paused": record.get("kept_paused"),
            "writing_started": bool(record.get("writing_started")),
            "targets": {k: settings.get(k) for k in TARGET_FIELDS},
            "reading": reading_view(reads) if state != READY else None,
            "progress": record.get("progress") or {},
            "set_aside": _listed(set_aside), "left_out": _listed(left_out),
            "counts": {"set_aside": len(set_aside), "waiting": len(waiting), **waiting_by_kind, **by_kind,
                       "left_out": len(left_out)}}


def summary(options: Optional[dict]) -> dict:
    """The few words the job's detail carries for its header and buttons, with no read of its rows: its state, stage,
    words, whether its schedule turns on when it is ready, and how many databases wait for a choice."""
    record = record_of(options) or {}
    return {"state": state_of(options), "stage": stage_of(options), "words": words(options),
            "turn_on": bool(record.get("turn_on")), "waiting": len(waiting_entries(record))}


def status_cell(options: Optional[dict]) -> Optional[dict]:
    """The jobs list's Status cell while the job isn't Ready (None once it is): Preparing with its stage and, as the
    prepare task last reported them, its percent and minutes left; or Needs you with how many databases wait."""
    state = state_of(options)
    if state == READY:
        return None
    record = record_of(options) or {}
    if state == NEEDS_YOU:
        waiting = len(waiting_entries(record))
        return {"label": "Needs you", "tone": "warn", "cause": None,
                "detail": [f"{waiting:,} {'database' if waiting == 1 else 'databases'} set aside"]}
    progress = record.get("progress") or {}
    detail = [STAGE_WORDS.get(stage_of(options) or READING, STAGE_WORDS[READING])]
    if progress.get("percent") is not None:
        detail.append(f"{int(progress['percent'])}%")
    if progress.get("minutes_left"):
        detail.append(f"about {int(progress['minutes_left'])} min left")
    return {"label": "Preparing", "tone": "run", "detail": detail, "cause": None}
