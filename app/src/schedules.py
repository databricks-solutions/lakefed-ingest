"""Schedules: run collections at a fixed cadence through small Databricks "wrapper" jobs.

A schedule has a cadence (minutes, dividing a day), a time zone, ingestion job parameters and one or
more member collections. For each member the app creates a wrapper job: a Quartz cron schedule
(shifted by the member's offset when the schedule staggers its members) and one ``run_job`` task that
starts the ingestion job with ``task_collection`` set. Wrapper jobs allow one run at a time and don't
queue, so a run that is still going when the next trigger fires makes Databricks skip that trigger;
the collection's run lock stays the backstop for manual runs.

A schedule of the ``check`` variant has one wrapper job for all its members instead: it starts the
check job (``lakefed_ingest_check``) with the members as ``task_collections``, and the check starts the
ingestion job only for the members with work. Every member records that one job.

Wrapper jobs carry tags naming their schedule, collection and app (the Lakebase database whose
schedules they implement), so ``sync`` reconciles by tag: it adopts a job whose id was never recorded
and deletes jobs no active member owns, whatever left them behind.

Pure helpers (cron, offsets, next runs, job settings, drift, warnings) are unit-tested; ``sync`` and
``check`` take a ``JobsClient`` so they can be tested with a fake.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CADENCES = (5, 10, 15, 20, 30, 60, 120, 180, 240, 360, 720, 1440)   # minutes; each divides a day
_whole = lambda lo, hi: (lambda v: v.isdigit() and lo <= int(v) <= hi)
_flag = lambda v: v in ("true", "false")
# Ingestion job parameters a schedule may set (strings, as job parameters are), checked the way
# ingest.ipynb parses them; bounds only catch typos. The job's own defaults apply to the rest.
PARAMETERS = {
    "parallelism": _whole(1, 256),
    "detection_chunk": _whole(1, 5000),
    "trust_server_certificate": _flag,
    "secret_scope": lambda v: bool(re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", v)),
    "snapshot_isolation": _flag,
    "reseed_on_new_columns": _flag,
    "append_batch_size": _whole(1, 10000),
    "max_seeds_per_run": _whole(0, 100000),
    "freshness_interval_s": lambda v: bool(re.fullmatch(r"\d{1,6}(\.\d+)?", v)) and float(v) <= 86400,
    "max_queries_per_source": _whole(0, 1024),
    "table_retries": _whole(0, 10),
    "table_retry_budget": _whole(0, 100000),
    "fail_on_table_errors": _flag,
}
FLAGS = [k for k, check in PARAMETERS.items() if check is _flag]
# Keys of schedule.options the app sets: start_minute = a set start time for daily and every 2 to 12 hours
# schedules; cluster = the job's own cluster ({driver_node_type_id, node_type_id, num_workers}, run by a copy of the
# ingestion job); cluster_copy_run_id = the copies job's run applying it. ingest_copy_job_id (that copy) is written
# only by the copies job (cluster_copies.ipynb), never taken from a request. rediscover_hour = the UTC hour a job adds
# its selection's new databases and tables (rediscovery.py); absent when off.
OPTIONS = ("start_minute", "cluster", "cluster_copy_run_id", "rediscover_hour")
COPY_OPTION = "ingest_copy_job_id"
CLUSTER_KEYS = ("driver_node_type_id", "node_type_id", "num_workers")
# Workers an app job may ask for: enough for the largest tested shape, small enough to bound a mistake's cost.
MAX_WORKERS = 8
_NODE_TYPE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
TAG_SCHEDULE = "lakefed_ingest_schedule"
TAG_COLLECTION = "lakefed_ingest_collection"
TAG_OWNER = "lakefed_ingest_app"     # instance/database: apps of other deployments in the workspace keep out
JOB_NAME_PREFIX = "lakefed_ingest schedule"
CHECK_VARIANT = "check"        # one wrapper per schedule, starting the check job (jobs.CHECK_JOB_ID)
GROUP_TAG = "all-members"      # TAG_COLLECTION of that wrapper: it starts every member
MAX_PARAMETER_CHARS = 10_000   # the Jobs API's limit on a job parameter value
# Earlier wrappers carried "*", which AWS workspaces refuse in a tag value: still recognised, and re-tagged on sync.
OLD_GROUP_TAGS = ("*",)


def is_group_tag(value: Optional[str]) -> bool:
    return value == GROUP_TAG or value in OLD_GROUP_TAGS
_EMAIL = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.,:()-]{0,79}")


# ---- validation ---------------------------------------------------------------------------------

def emails(text: Optional[str]) -> List[str]:
    return [e.strip() for e in (text or "").replace(";", ",").split(",") if e.strip()]


def validate(body: dict, known_collections: Iterable[str], variants: Iterable[str]) -> List[str]:
    """Errors in a schedule body (empty list = valid)."""
    errs = []
    if not _NAME.fullmatch(str(body.get("name") or "")):
        errs.append("name: 1-80 characters (letters, digits, space, _ . , : ( ) -), starting with a letter or digit")
    if body.get("cadence_minutes") not in CADENCES:
        errs.append(f"cadence_minutes must be one of {', '.join(map(str, CADENCES))}")
    try:
        ZoneInfo(str(body.get("timezone") or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        errs.append(f"timezone: unknown time zone '{body.get('timezone')}'")
    params = body.get("parameters") or {}
    if not isinstance(params, dict):
        errs.append("parameters must be an object")
    else:
        for k, v in params.items():
            if k not in PARAMETERS:
                errs.append(f"parameters: '{k}' isn't a schedulable parameter ({', '.join(PARAMETERS)})")
            elif not PARAMETERS[k](str(v)):
                errs.append(f"parameters.{k}: invalid value '{v}'")
    if (body.get("job_variant") or "default") not in set(variants):
        errs.append(f"job_variant must be one of {', '.join(sorted(variants))}")
    members = body.get("members") or []
    known = set(known_collections)
    if not members:
        errs.append("members: at least one ingestion group")
    if len(set(members)) != len(members):
        errs.append("members: an ingestion group is listed twice")
    unknown = [m for m in members if m not in known]
    if unknown:
        errs.append(f"members: unknown ingestion groups {', '.join(sorted(unknown))}")
    bad = [e for e in emails(body.get("notify_emails")) if not _EMAIL.fullmatch(e)]
    if bad:
        errs.append(f"notify_emails: invalid address {bad[0]}")
    options = body.get("options") or {}
    if not isinstance(options, dict):
        errs.append("options must be an object")
    else:
        errs += [f"options: unknown key {k}" for k in options if k not in OPTIONS]
        start = options.get("start_minute")
        if start is not None:
            if not isinstance(start, int) or isinstance(start, bool) or not 0 <= start < 1440:
                errs.append("options.start_minute: a minute of the day, 0 to 1439")
            elif int(body.get("cadence_minutes") or 0) <= 60:
                errs.append("options.start_minute: only daily and every 2 to 12 hours schedules start at a set time")
        hour = options.get("rediscover_hour")
        if hour is not None and (not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23):
            errs.append("options.rediscover_hour: an hour of the day in UTC, 0 to 23")
        errs += cluster_problems(options.get("cluster"), body.get("job_variant") or "default")
    return errs


def cluster_problems(cluster, variant: str) -> List[str]:
    """What's wrong with a job's own cluster (options.cluster), if anything. Only the default and check variants
    run the bundle's ingestion job, so only they can have a copy of it with another cluster."""
    if not cluster:
        return []
    if not isinstance(cluster, dict):
        return ["options.cluster must be an object"]
    errs = [f"options.cluster: unknown key {k}" for k in cluster if k not in CLUSTER_KEYS]
    if variant not in ("default", CHECK_VARIANT):
        errs.append(f"options.cluster: the {variant} variant runs its own Databricks job, whose cluster is set there")
    for key in ("driver_node_type_id", "node_type_id"):
        value = cluster.get(key)
        if value not in (None, "") and not (isinstance(value, str) and _NODE_TYPE.fullmatch(value)):
            errs.append(f"options.cluster.{key}: a node type name, such as Standard_D16ds_v5")
    workers = cluster.get("num_workers")
    if workers not in (None, "") and (not isinstance(workers, int) or isinstance(workers, bool)
                                      or not 0 <= workers <= MAX_WORKERS):
        errs.append(f"options.cluster.num_workers: 0 to {MAX_WORKERS} workers")
    return errs


def options_of(schedule: dict) -> dict:
    """A schedule's options as a dict (stored as JSON)."""
    options = schedule.get("options") or {}
    return json.loads(options) if isinstance(options, str) else options


def own_cluster(schedule: dict) -> Optional[dict]:
    """The cluster a job asks for, without blanks (None: the bundle's ingestion job's own)."""
    cluster = options_of(schedule).get("cluster") or {}
    found = {k: cluster[k] for k in CLUSTER_KEYS if cluster.get(k) not in (None, "")}
    return found or None


def copy_job_id(schedule: dict) -> Optional[int]:
    """The copy of the ingestion job this job runs (made by the copies job for its own cluster), or None for the
    bundle's job: no own cluster, or its copy isn't made yet."""
    recorded = options_of(schedule).get(COPY_OPTION)
    return int(recorded) if recorded and own_cluster(schedule) else None


# ---- cadence, offsets, cron, next runs ----------------------------------------------------------

def timezone_for(cadence: int, tz: Optional[str]) -> str:
    """The zone a schedule runs in. Every 5 to 60 minutes: UTC, since London (like most zones) is a whole hour
    off UTC, so the runs fall on the same local minutes, and in UTC none is skipped or late on a clock-change
    night (Databricks' advice for hourly runs). Daily or every 2 to 12 hours is tied to a time of day: its zone."""
    return "UTC" if int(cadence) <= 60 else (str(tz or "").strip() or "UTC")


def updated_by(schedule: dict) -> Optional[str]:
    """Who last changed the job or its groups' tables in the app (options.updated_by). A job never changed since it
    was created was last updated by its creator, as the Databricks Jobs list shows it. None: not recorded (jobs
    last changed before the app recorded it)."""
    recorded = options_of(schedule).get("updated_by")
    if recorded:
        return recorded
    never_changed = schedule.get("updated_at") is not None and schedule.get("updated_at") == schedule.get("created_at")
    return schedule.get("created_by") if never_changed else None


def start_minute(schedule: dict) -> int:
    """A schedule's set start time, minutes after local midnight (schedule.options.start_minute; 0 when unset or
    for a minute or hourly schedule)."""
    return int(options_of(schedule).get("start_minute") or 0) if int(schedule.get("cadence_minutes") or 0) > 60 else 0


def offsets(collections: Iterable[str], cadence: int, stagger: bool, start_minute: int = 0) -> Dict[str, int]:
    """Start offset (minutes within the cadence) per collection: the schedule's start time, plus a spread in name
    order when staggering (stable as members come and go). For cadences over an hour the spread stays within the
    first hour after the start, so staggered members start near it. A minute or hourly schedule has no start."""
    names = sorted(collections)
    span = min(cadence, 60)
    start = start_minute if cadence > 60 else 0
    return {c: (start + (i * span // len(names) if stagger else 0)) % cadence for i, c in enumerate(names)}


def member_start(cadence: int, offset: int) -> str:
    """When a member starts, as its page shows it: a clock time for a set-time schedule, minutes past otherwise."""
    return f"{offset // 60:02d}:{offset % 60:02d}" if cadence > 60 else _past_the_hour(offset % 60)


def stagger_of(offset: int, cadence: int, start_minute: int) -> int:
    """The part of a member's offset that is its stagger: how long after its job run's start it fires."""
    return (int(offset) - (start_minute if cadence > 60 else 0)) % cadence


def quartz_cron(cadence: int, offset: int) -> str:
    """Quartz expression firing when minutes-since-midnight is congruent to ``offset`` modulo ``cadence``."""
    if cadence not in CADENCES or not 0 <= offset < cadence:
        raise ValueError(f"bad cadence/offset {cadence}/{offset}")
    if cadence < 60:
        return f"0 {offset}/{cadence} * * * ?"
    hours, first_hour, minute = cadence // 60, offset // 60, offset % 60
    if cadence == 60:
        return f"0 {minute} * * * ?"
    if cadence == 1440:
        return f"0 {minute} {first_hour} * * ?"
    return f"0 {minute} {first_hour}/{hours} * * ?"


def _past_the_hour(minutes: int) -> str:
    return "on the hour" if not minutes else f"{minutes} minute{'' if minutes == 1 else 's'} past the hour"


def describe(cadence: int, offset: int = 0, tz: str = "UTC") -> str:
    """A schedule in plain words, as cronstrue reads a cron expression ("Every 30 minutes", "at 15 minutes past the
    hour"), with its time zone in brackets as the Databricks Jobs UI shows it. Minute and hourly schedules run in UTC."""
    clock = f"{offset // 60:02d}:{offset % 60:02d}"
    if cadence < 60:
        return f"Every {cadence} minutes" + (f", starting at {_past_the_hour(offset)}" if offset else "") + " (UTC)"
    if cadence == 60:
        return f"Every hour, {'on the hour' if not offset else 'at ' + _past_the_hour(offset)} (UTC)"
    if cadence == 1440:
        return f"Every day at {clock} ({tz or 'UTC'})"
    return f"Every {cadence // 60} hours, starting at {clock} ({tz or 'UTC'})"


def fire_times(cadence: int, offset: int, tz: str, start: datetime, end: datetime) -> List[datetime]:
    """Trigger times in [start, end), as UTC datetimes."""
    zone = ZoneInfo(tz or "UTC")
    local = start.astimezone(zone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    out, day = [], midnight
    while day.astimezone(timezone.utc) < end:
        for m in range(offset, 1440, cadence):
            wall = day + timedelta(minutes=m)
            t = wall.astimezone(timezone.utc)
            # A wall-clock time the clocks skip that night (inside the spring-forward gap) never fires: Quartz
            # skips it, so it is not a trigger here either.
            if t.astimezone(zone).replace(tzinfo=None) != wall.replace(tzinfo=None):
                continue
            if start <= t < end:
                out.append(t)
        day = (day + timedelta(days=1, hours=2)).replace(hour=0, minute=0)   # safe across DST changes
    return out


def next_run(cadence: int, offset: int, tz: str, after: datetime) -> datetime:
    return fire_times(cadence, offset, tz, after, after + timedelta(days=2))[0]


# ---- job runs: a schedule's group runs, placed by cadence slot ------------------------------------

def _when(value) -> Optional[datetime]:
    """A timestamp as an aware datetime: lakebase.query sends them as ISO text."""
    return datetime.fromisoformat(value) if isinstance(value, str) else value


# The check job's own record of checking a collection: an ingest_run row whose work_id is "check-<job run>" (from the
# start), or whose detail holds the check's summary (records written before the marker).
CHECK_PREFIX = "check-"


def _detail(run: dict) -> dict:
    detail = run.get("detail")
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            return {}
    return detail if isinstance(detail, dict) else {}


def is_check(run: dict) -> bool:
    """Whether a run row is the check's own record, not a group run."""
    return str(run.get("work_id") or "").startswith(CHECK_PREFIX) or "check" in _detail(run)


def check_work(run: dict) -> Optional[list]:
    """A finished check's databases with work for its collection, or None while it runs or when it failed."""
    check = _detail(run).get("check")
    return check.get("work") if isinstance(check, dict) and "error" not in check else None


def launch_of(run: dict) -> Optional[str]:
    """The check job's launch a group run belongs to: the launch id in its work_id ("<launch_id>:<collection>"),
    or None for a run started otherwise (and for the check's own record)."""
    work_id = run.get("work_id")
    return str(work_id).split(":", 1)[0] if work_id and not is_check(run) else None


def slot_of(fire: datetime, offset: int, tz: str) -> datetime:
    """The job run a member's trigger belongs to: its fire time less the member's offset, on the wall clock
    of the schedule's time zone, so every member of one trigger shares it across a daylight saving change."""
    local = fire.astimezone(ZoneInfo(tz or "UTC"))
    return (local - timedelta(minutes=offset)).astimezone(timezone.utc)


# How long a member's trigger shows as starting before it counts as "no run": a job cluster takes minutes to start.
STARTING_MINUTES = 15


def _check_job_run(check: dict) -> str:
    """The check job run a check record belongs to: every collection it checked shares it."""
    work_id = str(check.get("work_id") or "")
    return work_id if work_id.startswith(CHECK_PREFIX) else f"{CHECK_PREFIX}{check.get('job_run_id')}"


def job_runs(cadence: int, tz: str, members: List[dict], runs: List[dict], start: datetime, end: datetime,
             now: datetime, start_minute: int = 0, check_variant: bool = False) -> List[dict]:
    """A schedule's job runs whose triggers fall in [start, end), newest first, each with one entry per
    member then: its trigger time, its group run and any extra runs, and its state.

    A group run belongs to its member's latest trigger at or before its start, or with a launch id to its
    launch's job run (launch_of); the first in a slot is the
    slot's run and later ones are extra (started by hand, most likely). A member with no run is waiting when
    its trigger is still ahead, skipped when its group's previous run was still going at the trigger (wrapper
    jobs allow one run and don't queue), starting for STARTING_MINUTES after its trigger (its cluster may still
    be starting), else "no run". The check job's own records (is_check) are not group runs: each job run lists
    them as its checks, and a member whose check is going is "checking", whose check failed is "check failed", and
    whose check found nothing to load is "unchanged". Checks count only on a check_variant schedule, whose one
    wrapper fires every member at the slot (stored offsets aside); one check job run's checks all belong to the
    slot it started in. Only triggers and runs while the member belonged to the schedule count (a check: at
    its trigger). `runs` may start before `start`: earlier runs are only read for skipped triggers."""
    by_group: Dict[str, List[dict]] = {}
    checks: List[dict] = []
    for r in sorted(runs, key=lambda r: _when(r["started_at"])):
        if not is_check(r):
            by_group.setdefault(r["task_collection"], []).append(r)
        elif check_variant:
            checks.append(r)
    start_offset = (start_minute if cadence > 60 else 0) % cadence
    offset_of = (lambda m: start_offset) if check_variant else (lambda m: int(m["offset_minutes"]))
    # A slot is the job run's start: a member's fire time less its stagger (its offset past the start time).
    slot_at = lambda fire, offset: slot_of(fire, stagger_of(offset, cadence, start_minute), tz)
    check_slot: Dict[str, datetime] = {}
    for c in checks:
        started = _when(c["started_at"])
        before = fire_times(cadence, start_offset, tz, started - timedelta(minutes=cadence + 1),
                            started + timedelta(seconds=1))
        if before:
            check_slot.setdefault(_check_job_run(c), slot_of(before[-1], 0, tz))   # checks come oldest first
    checks_at: Dict[tuple, List[dict]] = {}
    for c in checks:
        if _check_job_run(c) in check_slot:
            checks_at.setdefault((check_slot[_check_job_run(c)], c["task_collection"]), []).append(c)
    slot_checks: Dict[datetime, List[dict]] = {}
    # Group runs the check job started together share a launch id: they are one job run, the slot of the
    # earliest of them, however late the others start.
    launch_slot: Dict[str, datetime] = {}
    for m in members:
        offset = offset_of(m)
        for r in by_group.get(m["task_collection"], []):
            launch = launch_of(r)
            if launch is None:
                continue
            started = _when(r["started_at"])
            before = fire_times(cadence, offset, tz, started - timedelta(minutes=cadence + 1), started + timedelta(seconds=1))
            if before:
                slot = slot_at(before[-1], offset)
                launch_slot[launch] = min(launch_slot.get(launch, slot), slot)
    slots: Dict[datetime, List[dict]] = {}
    for m in members:
        group, offset = m["task_collection"], offset_of(m)
        added, removed = _when(m.get("added_at")), _when(m.get("removed_at"))
        fires = fire_times(cadence, offset, tz, start, end)
        if not fires:
            continue
        fires.append(next_run(cadence, offset, tz, fires[-1] + timedelta(seconds=1)))
        history = by_group.get(group, [])
        # On a check schedule a launched run belongs to its launch's trigger, checked against membership below.
        member_runs = [r for r in history if (check_variant and launch_of(r) in launch_slot)
                       or ((added is None or _when(r["started_at"]) >= added)
                           and (removed is None or _when(r["started_at"]) < removed))]
        for fire, following in zip(fires, fires[1:]):
            if (added is not None and fire < added) or (removed is not None and fire >= removed):
                continue
            slot = slot_at(fire, offset)
            in_slot = [r for r in member_runs
                       if (launch_slot.get(launch_of(r)) == slot if launch_of(r) in launch_slot
                           else fire <= _when(r["started_at"]) < following)]
            checked = checks_at.get((slot, group), [])
            slot_checks.setdefault(slot, []).extend(checked)
            last_check = checked[-1] if checked else None
            if in_slot:
                state = "run"
            elif fire > now:
                state = "waiting"
            elif last_check and last_check.get("status") == "running":
                state = "checking"
            elif last_check and last_check.get("status") == "failed":
                state = "check failed"
            elif last_check and check_work(last_check) == []:
                state = "unchanged"
            elif any(_when(r["started_at"]) < fire and r.get("status") != "skipped"
                     and (r.get("finished_at") is None or _when(r["finished_at"]) > fire) for r in history):
                state = "skipped"
            elif now < min(following, fire + timedelta(minutes=STARTING_MINUTES)):
                state = "starting"
            else:
                state = "no run"
            slots.setdefault(slot, []).append(
                {"task_collection": group, "offset_minutes": offset, "fire_at": fire, "state": state,
                 "run": in_slot[0] if in_slot else None, "extra": in_slot[1:]})
    outside = _outside_job_runs(members, by_group, slots, start, end, offset_of)
    slots.update(outside)
    return [{"slot": slot, "members": sorted(entries, key=lambda e: (e["offset_minutes"], e["task_collection"])),
             "checks": sorted(slot_checks.get(slot, []), key=lambda c: (_when(c["started_at"]), c["task_collection"])),
             "outside": slot in outside}
            for slot, entries in sorted(slots.items(), reverse=True)]


# Group runs started outside every trigger within this long of each other are one job run: a Run now starts each
# member's wrapper job at once, and their clusters come up a few minutes apart.
OUTSIDE_SPAN = timedelta(minutes=STARTING_MINUTES)


def _outside_job_runs(members: List[dict], by_group: Dict[str, List[dict]], slots: Dict[datetime, List[dict]],
                      start: datetime, end: datetime, offset_of) -> Dict[datetime, List[dict]]:
    """The members' group runs that started in [start, end) while they belonged to the schedule but fall in no
    trigger's job run: a Run now before the job's first trigger, a run started from a group's page or the Jobs page,
    or by the ingestion job's own schedule. Each becomes part of a job run of its own, slotted at its first run's
    start, so the job's runs list every run it made. Runs starting within OUTSIDE_SPAN of that first one join it."""
    placed = {id(r) for entries in slots.values() for e in entries for r in ([e["run"]] if e["run"] else []) + e["extra"]}
    found = []
    for m in members:
        added, removed = _when(m.get("added_at")), _when(m.get("removed_at"))
        for r in by_group.get(m["task_collection"], []):
            started = _when(r["started_at"])
            if (id(r) not in placed and r.get("status") != "skipped" and start <= started < end
                    and (added is None or started >= added) and (removed is None or started < removed)):
                found.append((started, m, r))
    out: Dict[datetime, List[dict]] = {}
    slot = None
    for started, m, r in sorted(found, key=lambda f: (f[0], f[1]["task_collection"])):
        if slot is None or started - slot > OUTSIDE_SPAN or slot in slots:
            slot = started
        entries = out.setdefault(slot, [])
        same = next((e for e in entries if e["task_collection"] == m["task_collection"]), None)
        if same:
            same["extra"].append(r)
        else:
            entries.append({"task_collection": m["task_collection"], "offset_minutes": offset_of(m), "fire_at": started,
                            "state": "run", "run": r, "extra": []})
    return out


# ---- wrapper job settings and drift -------------------------------------------------------------

def job_name(schedule_name: str, collection: str) -> str:
    return f"{JOB_NAME_PREFIX}: {schedule_name} / {collection}"


def run_parameters(schedule: dict, collection: str) -> dict:
    """The ingestion job parameters a run of one collection takes from its schedule: the collection and every
    tuning value the schedule stores. A value it doesn't store is left out, so the ingestion job's default applies."""
    return {"task_collection": collection,
            **{k: str(v) for k, v in sorted((schedule.get("parameters") or {}).items())}}


def job_settings(schedule: dict, collection: str, offset: int, ingest_job_id: int,
                 view_group: Optional[str] = None, owner: Optional[str] = None) -> dict:
    """Jobs API 2.2 settings of a member's wrapper job."""
    params = run_parameters(schedule, collection)
    s = {
        "name": job_name(schedule["name"], collection),
        "description": (f"Managed by the lakefed_ingest operations app (schedule '{schedule['name']}'). "
                        "Edit the schedule in the app: changes made here are reported as drift and undone "
                        "by the next sync."),
        "schedule": {"quartz_cron_expression": quartz_cron(int(schedule["cadence_minutes"]), offset),
                     "timezone_id": schedule.get("timezone") or "UTC",
                     "pause_status": "PAUSED" if schedule.get("paused") else "UNPAUSED"},
        "max_concurrent_runs": 1,
        "queue": {"enabled": False},
        "tasks": [{"task_key": "ingest",
                   "run_job_task": {"job_id": int(ingest_job_id), "job_parameters": params}}],
        "tags": {TAG_SCHEDULE: str(schedule["id"]), TAG_COLLECTION: collection[:255],
                 **({TAG_OWNER: owner[:255]} if owner else {})},
        # Skipped triggers are how an overrun is absorbed, not failures: don't email them.
        "notification_settings": {"no_alert_for_skipped_runs": True},
    }
    addrs = emails(schedule.get("notify_emails"))
    if addrs:
        s["email_notifications"] = {"on_failure": addrs}
    if view_group:
        s["access_control_list"] = [{"group_name": view_group, "permission_level": "CAN_VIEW"}]
    return s


def group_job_name(schedule_name: str) -> str:
    return f"{JOB_NAME_PREFIX}: {schedule_name} / check"


def group_job_settings(schedule: dict, collections: Iterable[str], check_job_id: int,
                       view_group: Optional[str] = None, owner: Optional[str] = None) -> dict:
    """Jobs API 2.2 settings of a ``check`` schedule's one wrapper job: at the cadence, it starts the check
    job over every member. Members are not staggered: the check takes them in one go."""
    s = job_settings(schedule, "", start_minute(schedule) % int(schedule["cadence_minutes"]), check_job_id,
                     view_group, owner)
    members = ",".join(sorted(collections))
    # The check reads the members from Lakebase by the schedule's id; the list rides along while it fits a job
    # parameter value (at most 10,000 characters), so a check deployed before schedule_id still checks them.
    params = {"schedule_id": str(schedule["id"]),
              "task_collections": members if len(members) <= MAX_PARAMETER_CHARS else "",
              **{k: str(v) for k, v in sorted((schedule.get("parameters") or {}).items())}}
    if copy_job_id(schedule):     # the check starts the job's own copy of the ingestion job
        params["ingest_job_id"] = str(copy_job_id(schedule))
    s["name"] = group_job_name(schedule["name"])
    s["tasks"] = [{"task_key": "check", "run_job_task": {"job_id": int(check_job_id), "job_parameters": params}}]
    s["tags"][TAG_COLLECTION] = GROUP_TAG
    return s


def _comparable(s: dict) -> dict:
    """The parts of job settings the app owns, normalized for comparison."""
    task = ((s.get("tasks") or [{}])[0]) or {}
    rj = task.get("run_job_task") or {}
    sched = s.get("schedule") or {}
    return {
        "name": s.get("name"),
        "cron": sched.get("quartz_cron_expression"),
        "timezone": sched.get("timezone_id"),
        "paused": sched.get("pause_status") == "PAUSED",
        "max_concurrent_runs": s.get("max_concurrent_runs", 1),
        "queue": bool((s.get("queue") or {}).get("enabled")),
        "tasks": len(s.get("tasks") or []),
        "target_job_id": rj.get("job_id"),
        "job_parameters": {k: str(v) for k, v in (rj.get("job_parameters") or {}).items()},
        "tags": {k: v for k, v in (s.get("tags") or {}).items() if k in (TAG_SCHEDULE, TAG_COLLECTION, TAG_OWNER)},
        "notify": sorted((s.get("email_notifications") or {}).get("on_failure") or []),
        "no_alert_for_skipped_runs": bool((s.get("notification_settings") or {}).get("no_alert_for_skipped_runs")),
    }


def drift(actual: dict, desired: dict) -> List[str]:
    """Names of the app-owned settings that differ between the job and the schedule."""
    a, d = _comparable(actual), _comparable(desired)
    return [k for k in d if a.get(k) != d.get(k)]


# ---- warnings -----------------------------------------------------------------------------------

def warnings(cadence: int, p95_s: Optional[float], min_sla_minutes: Optional[float],
             skipped: int = 0) -> List[str]:
    """Overrun and SLA warnings for one member."""
    out = []
    period = cadence * 60
    if p95_s is not None and p95_s >= period:
        out.append(f"overrun: p95 run time {round(p95_s / 60, 1)} min is longer than the {cadence}-minute cadence, "
                   "so triggers are skipped")
    elif p95_s is not None and p95_s >= 0.8 * period:
        out.append(f"near overrun: p95 run time {round(p95_s / 60, 1)} min is over 80% of the cadence")
    if skipped:
        out.append(f"{skipped} trigger(s) skipped in the last 24 hours (the previous run was still going)")
    if min_sla_minutes is not None and p95_s is not None and cadence + p95_s / 60 > min_sla_minutes:
        out.append(f"can't meet the freshness target: cadence + p95 run time ({round(cadence + p95_s / 60)} min) is longer "
                   f"than the strictest table's target ({int(min_sla_minutes)} min)")
    return out


# ---- sync with the Jobs API ---------------------------------------------------------------------

class JobsClient:
    """The Jobs API calls sync needs. Settings stay plain JSON (the SDK's raw API client for get, create,
    reset and delete; its paging iterators, as dicts, for listing jobs and runs)."""

    def __init__(self, w):
        self.w = w
        self.api = w.api_client

    def get(self, job_id: int) -> Optional[dict]:
        try:
            return self.api.do("GET", "/api/2.2/jobs/get", query={"job_id": int(job_id)})
        except Exception as e:      # deleted outside the app
            if "does not exist" in str(e) or "RESOURCE_DOES_NOT_EXIST" in str(e) or "not found" in str(e).lower():
                return None
            raise

    def create(self, settings: dict) -> int:
        return int(self.api.do("POST", "/api/2.2/jobs/create", body=settings)["job_id"])

    def reset(self, job_id: int, settings: dict) -> None:
        body = {k: v for k, v in settings.items() if k != "access_control_list"}
        self.api.do("POST", "/api/2.2/jobs/reset", body={"job_id": int(job_id), "new_settings": body})

    def delete(self, job_id: int) -> None:
        try:
            self.api.do("POST", "/api/2.2/jobs/delete", body={"job_id": int(job_id)})
        except Exception as e:
            if "does not exist" not in str(e) and "RESOURCE_DOES_NOT_EXIST" not in str(e):
                raise

    def tagged(self, schedule_id: int, owner: Optional[str]) -> Dict[int, str]:
        """The schedule's wrapper jobs, found by tag: {job_id: collection}. Lists every job the app can see
        (the Jobs API can't filter by tag), so it runs once per sync, not per poll."""
        out = {}
        for j in self.w.jobs.list(limit=100):
            tags = (j.settings.tags if j.settings else None) or {}
            if tags.get(TAG_SCHEDULE) == str(schedule_id) and owner and tags.get(TAG_OWNER) == owner[:255]:
                out[int(j.job_id)] = tags.get(TAG_COLLECTION) or ""
        return out

    def runs(self, job_id: int, since_ms: int, until_ms: Optional[int] = None) -> List[dict]:
        """Every run of the job that started in [since_ms, until_ms) (no cap: the window bounds it)."""
        return [r.as_dict() for r in self.w.jobs.list_runs(job_id=int(job_id), start_time_from=int(since_ms),
                                                            start_time_to=int(until_ms) if until_ms else None)]


@dataclass
class MemberResult:
    task_collection: str
    job_id: Optional[int]
    action: str            # created | adopted | updated | unchanged | deleted | kept | error
    detail: str = ""
    member_id: Optional[int] = None     # None: a job no member owns (an untracked job deleted, or a listing error)


def sync(schedule: dict, members: List[dict], removed: List[dict], client: JobsClient,
         ingest_job_id: Optional[int], view_group: Optional[str] = None,
         owner: Optional[str] = None) -> List[MemberResult]:
    """Make each active member's wrapper job match the schedule (create, adopt an unrecorded tagged job, or
    reset when it drifted); delete the jobs of removed members (or of every member when the schedule is
    deleted), then any other job tagged with the schedule. Callers serialize syncs of one schedule.
    ``ingest_job_id`` None (its job variant is no longer configured): active members report an error,
    deletes still happen."""
    out: List[MemberResult] = []
    deleting = schedule.get("deleted_at") is not None
    try:
        tagged: Optional[Dict[int, str]] = client.tagged(schedule["id"], owner)
    except Exception as e:      # without the listing, sync by the recorded ids and delete no untracked job
        tagged = None
        out.append(MemberResult("", None, "error", f"listing the schedule's jobs: {e}"[:500]))
    claimed = set()             # job ids an active member keeps
    if schedule.get("job_variant") == CHECK_VARIANT and not deleting:
        out += _sync_group(schedule, members, client, ingest_job_id, tagged, claimed, view_group, owner)
        members = []            # the per-member loop below has nothing left to do; deletes go on as usual
    taken = set()               # job ids given to a member in this sync: a job left by a check schedule
    for m in (members if not deleting else []):     # is recorded on every member, and only one may keep it
        c = m["task_collection"]
        if m.get("job_id") and int(m["job_id"]) in taken:
            m = dict(m, job_id=None)
        if m.get("job_id"):
            claimed.add(int(m["job_id"]))
        if ingest_job_id is None:       # keep its jobs: only the target is unknown
            claimed.update(t for t, tc in (tagged or {}).items() if tc == c[:255])
            out.append(MemberResult(c, m.get("job_id"), "error",
                                    f"no ingestion job configured for job variant '{schedule.get('job_variant')}'",
                                    m.get("id")))
            continue
        desired = job_settings(schedule, c, int(m["offset_minutes"]), ingest_job_id, view_group, owner)
        try:
            jid, job, action = m.get("job_id"), None, "updated"
            if jid:
                job = client.get(jid)
            if job is None:     # never recorded, or deleted outside the app: adopt a tagged job before creating
                for t, tc in sorted((tagged or {}).items()):
                    if tc == c[:255] and t not in claimed:
                        job = client.get(t)
                        if job is not None:
                            jid, action = t, "adopted"
                            break
            if job is None:
                jid = client.create(desired)
                claimed.add(jid)
                taken.add(jid)
                out.append(MemberResult(c, jid, "created", member_id=m.get("id")))
                continue
            claimed.add(int(jid))
            taken.add(int(jid))
            diff = drift(job.get("settings") or {}, desired)
            if diff:
                client.reset(jid, desired)
            if diff or action == "adopted":
                out.append(MemberResult(c, jid, action, ", ".join(diff), m.get("id")))
            else:
                out.append(MemberResult(c, jid, "unchanged", member_id=m.get("id")))
        except Exception as e:
            out.append(MemberResult(c, m.get("job_id"), "error", str(e)[:500], m.get("id")))
    deleted = set()
    for m in removed + (members if deleting else []):
        if not m.get("job_id"):
            continue
        if int(m["job_id"]) in claimed:     # the collection was added back and its member adopted the job
            out.append(MemberResult(m["task_collection"], m["job_id"], "kept", "adopted by the active member",
                                    m.get("id")))
            continue
        if int(m["job_id"]) in deleted:     # a check schedule's members share one wrapper job, gone with the first
            out.append(MemberResult(m["task_collection"], m["job_id"], "deleted", "shared job, already deleted",
                                    m.get("id")))
            continue
        try:
            client.delete(m["job_id"])
            deleted.add(int(m["job_id"]))
            out.append(MemberResult(m["task_collection"], m["job_id"], "deleted", member_id=m.get("id")))
        except Exception as e:
            out.append(MemberResult(m["task_collection"], m["job_id"], "error", str(e)[:500], m.get("id")))
    for t, tc in sorted((tagged or {}).items()):
        if t in claimed or t in deleted:
            continue
        try:
            client.delete(t)
            out.append(MemberResult(tc, t, "deleted", "untracked job"))
        except Exception as e:
            out.append(MemberResult(tc, t, "error", f"deleting untracked job: {e}"[:500]))
    return out


def _sync_group(schedule: dict, members: List[dict], client: JobsClient, check_job_id: Optional[int],
                tagged: Optional[Dict[int, str]], claimed: set, view_group: Optional[str],
                owner: Optional[str]) -> List[MemberResult]:
    """A ``check`` schedule's one wrapper job: find it (a member's recorded job, or a tagged one), create
    it, or reset it when it drifted; every member records it. The members' own wrapper jobs from another
    variant are left unclaimed, so the caller deletes them as untracked."""
    if check_job_id is None:
        claimed.update(t for t, tc in (tagged or {}).items() if is_group_tag(tc))
        return [MemberResult(m["task_collection"], m.get("job_id"), "error",
                             "no check job configured for the check variant", m.get("id")) for m in members]
    desired = group_job_settings(schedule, [m["task_collection"] for m in members], check_job_id, view_group, owner)
    try:
        jid, job, action = None, None, "updated"
        candidates = [int(m["job_id"]) for m in members if m.get("job_id")]
        candidates += sorted(t for t, tc in (tagged or {}).items() if is_group_tag(tc))
        for candidate in candidates:
            found = client.get(candidate)
            if found is not None and is_group_tag(((found.get("settings") or {}).get("tags") or {}).get(TAG_COLLECTION)):
                jid, job = candidate, found
                action = "updated" if candidate in [int(m["job_id"]) for m in members if m.get("job_id")] else "adopted"
                break
        if job is None:
            jid, action, diff = client.create(desired), "created", []
        else:
            diff = drift(job.get("settings") or {}, desired)
            if diff:
                client.reset(jid, desired)
            elif action == "updated":
                action = "unchanged"
        claimed.add(int(jid))
    except Exception as e:
        return [MemberResult(m["task_collection"], m.get("job_id"), "error", str(e)[:500], m.get("id")) for m in members]
    detail = ", ".join(diff)
    return [MemberResult(m["task_collection"], jid, action if i == 0 else "unchanged", detail if i == 0 else "",
                         m.get("id")) for i, m in enumerate(sorted(members, key=lambda m: m["task_collection"]))]


def check(schedule: dict, members: List[dict], client: JobsClient, ingest_job_id: int,
          owner: Optional[str] = None) -> Dict[str, dict]:
    """Per member: in_sync | drifted (with the fields) | missing | not_synced. Changes nothing."""
    out = {}
    group = schedule.get("job_variant") == CHECK_VARIANT
    for m in members:
        if not m.get("job_id"):
            out[m["task_collection"]] = {"status": "not_synced"}
            continue
        job = client.get(m["job_id"])
        if job is None:
            out[m["task_collection"]] = {"status": "missing"}
            continue
        desired = (group_job_settings(schedule, [x["task_collection"] for x in members], ingest_job_id, owner=owner)
                   if group else
                   job_settings(schedule, m["task_collection"], int(m["offset_minutes"]), ingest_job_id, owner=owner))
        diff = drift(job.get("settings") or {}, desired)
        out[m["task_collection"]] = {"status": "drifted", "fields": diff} if diff else {"status": "in_sync"}
    return out


def is_skipped(r: dict) -> bool:
    """A wrapper run Databricks skipped because the previous run was still going."""
    st = r.get("state") or {}
    return (st.get("life_cycle_state") == "SKIPPED" or st.get("result_state") == "SKIPPED"
            or (r.get("status") or {}).get("state") == "SKIPPED")


class SkippedRuns:
    """Start times of the wrapper jobs' skipped runs, kept between requests. A job's runs are listed in full
    once per window; after that a refresh (at most every ``ttl_s``) lists only the runs that started since
    the previous refresh, so counts cover the whole window without paging a day of runs on every poll.
    A skipped run is final when it appears, so nothing listed needs listing again."""

    MARGIN_MS = 10 * 60 * 1000      # re-list a little overlap at each refresh (runs are de-duplicated by id)

    def __init__(self, ttl_s: int = 60, keep_hours: int = 8 * 24):
        self.ttl_ms, self.keep_ms = ttl_s * 1000, keep_hours * 3600 * 1000
        self._jobs: Dict[int, dict] = {}
        self._mu = threading.Lock()

    def starts(self, client: JobsClient, job_id: int, since_ms: int, now_ms: int) -> List[int]:
        """The job's skipped runs that started at or after ``since_ms`` (ms, ascending). Raises if the Jobs
        API does; what was cached stays."""
        job_id = int(job_id)
        with self._mu:
            e = self._jobs.get(job_id)
            if e is None:
                e = {"from": since_ms, "to": now_ms, "skipped": self._list(client, job_id, since_ms, None)}
                self._jobs[job_id] = e
            elif since_ms < e["from"]:          # a longer window than listed so far: list the earlier part
                e["skipped"].update(self._list(client, job_id, since_ms, e["from"]))
                e["from"] = since_ms
            if now_ms - e["to"] >= self.ttl_ms:
                e["skipped"].update(self._list(client, job_id, e["to"] - self.MARGIN_MS, None))
                e["to"] = now_ms
            floor = now_ms - self.keep_ms
            if e["from"] < floor:
                e["skipped"] = {r: t for r, t in e["skipped"].items() if t >= floor}
                e["from"] = floor
            return sorted(t for t in e["skipped"].values() if t >= since_ms)

    @staticmethod
    def _list(client, job_id, since_ms, until_ms) -> Dict[int, int]:
        return {int(r["run_id"]): int(r["start_time"]) for r in client.runs(job_id, since_ms, until_ms)
                if is_skipped(r) and r.get("start_time")}
