"""The Databricks runs the app shows, kept in the `databricks_run` table by one poller that runs every POLL_S seconds.

Pages never ask the Jobs API about a run: they read the table. The poller lists each job the app shows runs of (the
ingestion job and its variants, the check job, the cluster copies, the schedules' copies and wrapper jobs, and the
setup, discovery and delete jobs) and writes a run only when its state changed. Its load is one listing per job per
poll, plus a get_run for each active run that dropped out of its job's listing. It doesn't grow with open tabs.

Only one instance polls. It takes a session-level advisory lock on a connection it keeps open while it leads, so the
lock lasts as long as that session. A transaction-level lock would be released at the end of each poll, and another
instance could poll at the same time. When the leader's session dies, the lock frees and another instance takes it.

The poller also closes the ingest runs whose Databricks run has ended (the pages used to do this on their reads). A
run is closed only when Databricks says TERMINATED, SKIPPED or INTERNAL_ERROR, or when the Jobs API no longer has a run
that began over NOT_FOUND_AFTER ago. It closes as cancelled when the app asked for the cancel (mark_stopping) or
Databricks says CANCELED, else as failed. A run the app cancelled that Databricks still has going CANCEL_CLOSE_AFTER
later closes as cancelled too. A queued, pending, running, terminating or unreadable run is otherwise never touched, and
the poller never cancels a run itself.

Until the table exists (the schema has not been migrated), the readers below fall back to the Jobs API.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

import jobs
import lakebase as db
import queries as q

POLL_S = 15
# The delete job purges the drafts of choices no job took (discarded, or not asked about for DRAFT_TTL_S): the poller
# asks for it at most every PURGE_EVERY_S, and only when there is one, so a quiet app starts no run.
PURGE_EVERY_S = 3600
DRAFT_TTL_S = int(os.environ.get("DRAFT_TTL_S", str(2 * 3600)))
# The poller's own advisory lock (its own class, so no collection's or schedule's lock can have the same key).
POLLER_LOCK = (0x6C667072, 0)
ACTIVE_STATES = ("QUEUED", "PENDING", "RUNNING", "TERMINATING", "BLOCKED", "WAITING_FOR_RETRY")
ENDED_STATES = ("TERMINATED", "SKIPPED", "INTERNAL_ERROR")
NOT_FOUND = "NOT_FOUND"
# A run the Jobs API doesn't know is closed only once it began this long ago: a run of another workspace sharing the
# state database, or one the API briefly can't find, is never taken for ended while it may still be going.
NOT_FOUND_AFTER = timedelta(hours=24)
# A run the app cancelled closes as cancelled this long after the cancel when Databricks still hasn't ended it, so a
# Databricks run that never answers doesn't keep its records running for ever.
CANCEL_CLOSE_AFTER = timedelta(minutes=2)
CANCELLED_RESULTS = ("CANCELED", "CANCELLED")
# Wrapper jobs are listed newest first on every poll, not only while active, so a SKIPPED trigger (a run Databricks
# never runs; it is over at once) is recorded and the schedules' skipped counts stay exact.
WRAPPER_LIMIT = 25
# The first poll after the lock is taken lists each wrapper back about a day (15-minute triggers), so the skipped
# counts start complete rather than from the poller's start.
WRAPPER_SEED_LIMIT = 300
# Other jobs are listed while active; the first poll after the lock is taken also records their recent runs.
GROUP_LIMIT = 100
# While the table is missing, how long to wait before looking for it again.
READY_RECHECK_S = 30

_ready: dict = {"yes": False, "checked": None}


def table_ready() -> bool:
    """Whether the poller's table exists yet. Until it does, the readers use the Jobs API as the app did before the
    poller. Looked for again at most every READY_RECHECK_S seconds while it is missing."""
    if _ready["yes"]:
        return True
    now = time.monotonic()
    if _ready["checked"] is not None and now - _ready["checked"] < READY_RECHECK_S:
        return False
    _ready["checked"] = now
    try:
        _ready["yes"] = bool(db.query(q.databricks_run_table_sql())[0]["ready"])
    except Exception:
        _ready["yes"] = False
    return _ready["yes"]


def ended_reason(ended: dict) -> str:
    """Why the app closed a run, in the run's own words: how its Databricks run ended, and when."""
    if ended["state"] == NOT_FOUND:
        return "its Databricks run no longer exists, so it ended without finishing"
    how = ended.get("result") or ended["state"]
    at = f" at {ended['ended']}" if ended.get("ended") else ""
    why = f" ({ended['message']})" if ended.get("message") else ""
    return f"its Databricks run ended {how}{at} without finishing{why}"[:1000]


def cancelled_reason(ended: dict) -> str:
    """Why the app closed a cancelled run, in the run's own words: how its Databricks run ended, and when."""
    if ended["state"] == NOT_FOUND:
        return "cancelled, and its Databricks run no longer exists"
    how = ended.get("result") or ended["state"]
    at = f" at {ended['ended']}" if ended.get("ended") else ""
    return f"cancelled, and its Databricks run ended {how}{at}"[:1000]


def _aware(value) -> datetime:
    """A timestamp as a timezone-aware datetime: lakebase.query sends them as ISO text, and a naive one is UTC."""
    when = q._when(value)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def should_close(state_row: dict, ingest_started_at, now: datetime) -> bool:
    """Whether a run recorded as running may be closed, given the recorded state of its Databricks run."""
    if state_row["life_cycle_state"] in ENDED_STATES:
        return True
    if state_row["life_cycle_state"] != NOT_FOUND or ingest_started_at is None:
        return False
    return _aware(ingest_started_at) < now - NOT_FOUND_AFTER


def closing_status(state_row: dict) -> str:
    """How a run whose Databricks run has ended is closed: cancelled when the app asked for the cancel or Databricks
    says it was cancelled, else failed."""
    if state_row.get("cancel_requested_at") or state_row.get("result_state") in CANCELLED_RESULTS:
        return "cancelled"
    return "failed"


def closure(state_row: dict, ingest_started_at, databricks_run: int, now: datetime) -> Optional[dict]:
    """How a run recorded as running is closed: its status, the time it finished (Databricks' end, or None for now), the
    detail it is closed with and the reason in it. None when the run stays as it is. A run the app cancelled that
    Databricks still hasn't ended CANCEL_CLOSE_AFTER after the cancel closes as cancelled, without a Databricks end."""
    if should_close(state_row, ingest_started_at, now):
        ended = {"state": state_row["life_cycle_state"], "result": state_row.get("result_state"),
                 "message": state_row.get("state_message"), "ended": state_row.get("ended_at")}
        status = closing_status(state_row)
        reason = ended_reason(ended) if status == "failed" else cancelled_reason(ended)
        finished_at = state_row.get("ended_at")
    elif state_row.get("cancel_requested_at") and now - _aware(state_row["cancel_requested_at"]) > CANCEL_CLOSE_AFTER:
        ended = {"state": state_row["life_cycle_state"], "result": None, "message": None, "ended": None}
        status, finished_at = "cancelled", None
        reason = "cancelled from the app, and Databricks had not ended its run 2 minutes after the cancel"
    else:
        return None
    detail = {"closed_by_app": "reconcile", "reason": reason, "databricks_run": databricks_run,
              "databricks_state": ended["state"], "databricks_result": ended["result"]}
    return {"status": status, "finished_at": finished_at, "detail": json.dumps(detail), "reason": reason}


def write_close(cur, run_id: int, close: dict) -> None:
    """Write one run's close (see closure) on an open transaction."""
    if close["status"] == "cancelled":
        cur.execute(q.close_cancelled_run_sql(), (close["finished_at"], close["detail"], run_id, close["reason"]))
    else:
        cur.execute(q.close_ended_run_sql(), (close["detail"], run_id, close["reason"], close["reason"]))


def row_from_run(run: dict, kind: str, schedule_id: Optional[int]) -> dict:
    """A run (as jobs.runs and jobs.run_row read it) as a databricks_run row."""
    parameter_values = run.get("parameters") or {}
    return {"run_id": int(run["run_id"]), "job_id": int(run["job_id"]), "kind": kind, "schedule_id": schedule_id,
            "task_collection": parameter_values.get("task_collection"), "trigger": run.get("trigger"),
            "parameters": json.dumps(parameter_values), "life_cycle_state": run.get("state") or NOT_FOUND,
            "result_state": run.get("result"), "state_message": run.get("message"), "run_page_url": run.get("url"),
            "started_at": run.get("started"), "ended_at": run.get("ended")}


def changed(row: dict, recorded: Optional[dict]) -> bool:
    """Whether a run's row differs from what the table holds, or the table has no row for it."""
    if recorded is None:
        return True
    return any((row.get(k) or None) != (recorded.get(k) or None)
               for k in ("kind", "schedule_id", "life_cycle_state", "result_state", "state_message", "ended_at"))


def _parameters(row: dict) -> dict:
    parameter_values = row.get("parameters") or {}
    return json.loads(parameter_values) if isinstance(parameter_values, str) else parameter_values


def view(row: dict) -> dict:
    """A databricks_run row in the shape the pages read a run in (the shape jobs.runs returns)."""
    parameter_values = _parameters(row)
    return {"run_id": row["run_id"], "job_id": row["job_id"], "parameters": parameter_values,
            "task_collection": parameter_values.get("task_collection"), "state": row["life_cycle_state"],
            "result": row.get("result_state"), "message": row.get("state_message"), "started": row.get("started_at"),
            "ended": row.get("ended_at"), "trigger": row.get("trigger"), "url": row.get("run_page_url"),
            "seen": row.get("seen_at")}


def state_view(row: dict) -> dict:
    """A run's state in the shape the run endpoints return (the shape jobs.run_state returns)."""
    return {"run_id": row["run_id"], "job_id": row["job_id"], "state": row["life_cycle_state"],
            "result": row.get("result_state"), "message": row.get("state_message"), "url": row.get("run_page_url"),
            "started": row.get("started_at")}


def runs_of(job_id, active_only: bool = False, limit: int = 25) -> List[dict]:
    """A job's runs, newest first, from the table (its active ones only when active_only), in the shape jobs.runs
    returns. Before the table exists, from the Jobs API."""
    if not table_ready():
        return jobs.runs(job_id, active_only=active_only, limit=limit)
    rows = db.query(q.databricks_runs_of_job_sql(), {"job_id": int(job_id), "active_only": bool(active_only),
                                                    "active_states": list(ACTIVE_STATES), "limit": int(limit)})
    return [view(r) for r in rows]


def run_state_of(run_id) -> dict:
    """A run's state (jobs.run_state's shape), from the table. A run the table hasn't recorded yet is read from the
    Jobs API once (the poller records it on its next poll); before the table exists, the Jobs API always answers."""
    if table_ready():
        rows = db.query(q.databricks_run_states_sql(), ([int(run_id)],))
        if rows:
            return state_view(rows[0])
    return jobs.run_state(run_id)


def ended_of(run_id) -> Optional[dict]:
    """How a Databricks run ended ({state, result, message, ended}), or None while it is going, from the table (the
    Jobs API for a run the table hasn't recorded yet). Raises what the Jobs API raises."""
    if table_ready():
        rows = db.query(q.databricks_run_states_sql(), ([int(run_id)],))
        if rows:
            row = rows[0]
            if row["life_cycle_state"] not in ENDED_STATES + (NOT_FOUND,):
                return None
            return {"state": row["life_cycle_state"], "result": row.get("result_state"),
                    "message": row.get("state_message"), "ended": row.get("ended_at")}
    return jobs.run_ended(run_id)


def skipped_starts_of(job_id, since: datetime, until: datetime) -> List[int]:
    """Start times (epoch ms, ascending) of a wrapper job's skipped runs in a window, from the table."""
    rows = db.query(q.skipped_databricks_runs_sql(), (int(job_id), since.isoformat(), until.isoformat()))
    return [int(q._when(r["started_at"]).timestamp() * 1000) for r in rows if r.get("started_at")]


def tracked_jobs() -> Dict[int, tuple]:
    """Every Databricks job the app shows runs of, as {job_id: (kind, schedule_id)}."""
    import schedules as sch
    out: Dict[int, tuple] = {}
    for job_id in jobs.ingest_variants().values():
        out[int(job_id)] = ("group", None)
    for kind, job_id in (("check", jobs.CHECK_JOB_ID), ("setup", jobs.SETUP_JOB_ID),
                         ("discover", jobs.DISCOVER_JOB_ID), ("delete", jobs.DELETE_JOB_ID),
                         ("copies", jobs.CLUSTER_COPIES_JOB_ID)):
        if job_id:
            out[int(job_id)] = (kind, None)
    schedules = db.query(q.schedules_sql())
    if schedules:
        for s in schedules:
            copy = sch.copy_job_id(s)
            if copy:
                out[int(copy)] = ("group", s["id"])
        for m in db.query(q.schedule_members_sql(), ([r["id"] for r in schedules],)):
            if m.get("job_id"):
                out[int(m["job_id"])] = ("wrapper", m["schedule_id"])
    return out


def purge_drafts_if_due(last: Optional[float], now_s: float) -> Optional[float]:
    """Start the delete job's drafts purge when an hour has gone since the last look and a draft is due. Returns when
    it last looked (``now_s`` when it did, else ``last``)."""
    if not jobs.DELETE_JOB_ID or (last is not None and now_s - last < PURGE_EVERY_S):
        return last
    if db.query(q.drafts_to_purge_sql(), (DRAFT_TTL_S,)):
        jobs.start_draft_purge(DRAFT_TTL_S)
    return now_s


class Poller:
    """One poller per process. ``start`` runs it in a daemon thread; ``wake`` polls at once."""

    def __init__(self):
        self._wake = threading.Event()
        self._lead_conn = None
        self._seeded = False
        self._on_change: Optional[Callable[[], None]] = None
        self._purge_looked: Optional[float] = None
        self.last_error: Optional[str] = None

    def start(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change
        threading.Thread(target=self._loop, name="run-poller", daemon=True).start()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while True:
            try:
                if table_ready() and self._leads():
                    self.poll_once(datetime.now(timezone.utc))
                    self._purge_looked = purge_drafts_if_due(self._purge_looked, time.monotonic())
                self.last_error = None
            except Exception as e:
                self.last_error = str(e).split(". ")[0][:300]
            self._wake.wait(POLL_S)
            self._wake.clear()

    def _leads(self) -> bool:
        """Whether this instance holds the poller's lock, taking it when it is free. A lock lasts as long as the
        connection it was taken on: when that connection has died, the lock is taken again."""
        if self._lead_conn is not None:
            try:
                self._lead_conn.execute("select 1")
                return True
            except Exception:
                self._drop_lead()
        conn = db.connect()
        try:
            got = conn.execute("select pg_try_advisory_lock(%s, %s)", POLLER_LOCK).fetchone()[0]
        except Exception:
            conn.close()
            raise
        if not got:
            conn.close()
            return False
        self._lead_conn, self._seeded = conn, False
        return True

    def _drop_lead(self) -> None:
        try:
            self._lead_conn.close()
        except Exception:
            pass
        self._lead_conn, self._seeded = None, False

    def poll_once(self, now: datetime) -> dict:
        """One poll: list each job, record the runs whose state changed, look up the active runs that dropped out of
        their listing, then close the ingest runs whose Databricks run has ended. Returns what it wrote."""
        tracked = tracked_jobs()
        seeding = not self._seeded
        listed: Dict[int, dict] = {}
        failed_jobs = set()
        for job_id, (kind, schedule_id) in tracked.items():
            if kind == "wrapper":
                limit, active_only = (WRAPPER_SEED_LIMIT if seeding else WRAPPER_LIMIT), False
            else:
                limit, active_only = GROUP_LIMIT, not seeding
            try:
                runs = jobs.runs(job_id, active_only=active_only, limit=limit)
            except Exception:
                failed_jobs.add(job_id)
                continue
            for run in runs:
                listed[int(run["run_id"])] = row_from_run(run, kind, schedule_id)
        self._seeded = True
        for r in db.query(q.active_databricks_runs_sql(), (list(ACTIVE_STATES),)):
            if int(r["run_id"]) in listed or int(r["job_id"]) in failed_jobs:
                continue
            looked_up = self._looked_up(r, tracked)
            if looked_up is not None:
                listed[int(r["run_id"])] = looked_up
        recorded = {int(r["run_id"]): r for r in db.query(q.databricks_run_states_sql(), (list(listed),))} \
            if listed else {}
        writes = [row for run_id, row in listed.items() if changed(row, recorded.get(run_id))]
        self._write_rows(writes)
        closed = self._reconcile(tracked, now)
        if writes or closed:
            self._changed()
        return {"recorded": len(writes), "closed": closed}

    def _looked_up(self, active_row: dict, tracked: Dict[int, tuple]) -> Optional[dict]:
        """An active run the job's listing no longer shows: its own state from Databricks, or NOT_FOUND when the
        Jobs API has no such run (closed later, see should_close). None when it can't be read now: left as it is."""
        kind, schedule_id = tracked.get(int(active_row["job_id"]), ("group", None))
        try:
            run = jobs.run_row(jobs.get_run(active_row["run_id"]))
        except Exception as e:
            if not jobs.is_not_found(e):
                return None
            run = {"run_id": active_row["run_id"], "job_id": active_row["job_id"], "state": NOT_FOUND,
                   "parameters": _parameters(active_row), "trigger": active_row.get("trigger"),
                   "started": active_row.get("started_at"), "ended": active_row.get("ended_at"),
                   "url": active_row.get("run_page_url"), "message": active_row.get("state_message")}
        return row_from_run(run, kind, schedule_id)

    def _write_rows(self, rows: List[dict]) -> None:
        if rows:
            with db.transaction() as cur:
                cur.executemany(q.databricks_run_upsert_sql(), [q.databricks_run_params(r) for r in rows])

    def _reconcile(self, tracked: Dict[int, tuple], now: datetime) -> int:
        """Close each ingest run recorded as running whose Databricks run has ended (see closure)."""
        open_runs = [r for r in db.query(q.open_runs_sql()) if str(r.get("job_run_id") or "").strip().isdigit()]
        if not open_runs:
            return 0
        ids = sorted({int(r["job_run_id"]) for r in open_runs})
        known = {int(r["run_id"]): r for r in db.query(q.databricks_run_states_sql(), (ids,))}
        closed = 0
        for r in open_runs:
            job_run = int(r["job_run_id"])
            state_row = known.get(job_run)
            if state_row is None:
                state_row = self._read_unrecorded(job_run, tracked)
                if state_row is None:
                    continue
            close = closure(state_row, r.get("started_at"), job_run, now)
            if close is None:
                continue
            with db.transaction() as cur:
                write_close(cur, int(r["run_id"]), close)
            closed += 1
        return closed

    def _read_unrecorded(self, job_run: int, tracked: Dict[int, tuple]) -> Optional[dict]:
        """The state of a run the table has no row for, read once from the Jobs API and recorded. None when it can't be
        read now (the run is left as it is)."""
        try:
            run = jobs.run_row(jobs.get_run(job_run))
        except Exception as e:
            if not jobs.is_not_found(e):
                return None
            return {"life_cycle_state": NOT_FOUND, "result_state": None, "state_message": None, "ended_at": None}
        row = row_from_run(run, *tracked.get(int(run["job_id"]), ("group", None)))
        self._write_rows([row])
        return row

    def _changed(self) -> None:
        if self._on_change:
            self._on_change()


POLLER = Poller()


def note_started(job_id, run_id, job_parameters: dict) -> None:
    """Record a run the app just started as pending, so the pages show it before the next poll records its real state.
    Does nothing (and never raises) while the table is missing: the start itself has already happened."""
    try:
        if not table_ready():
            return
        tracked = tracked_jobs()
        kind, schedule_id = tracked.get(int(job_id), ("group", None))
        parameter_values = dict(job_parameters or {})
        row = row_from_run({"run_id": run_id, "job_id": job_id, "parameters": parameter_values, "state": "PENDING",
                            "started": datetime.now(timezone.utc).isoformat()}, kind, schedule_id)
        POLLER._write_rows([row])
        POLLER.wake()
    except Exception:
        pass


def mark_stopping(run_ids) -> None:
    """Show runs the app just asked Databricks to cancel as terminating, from now until Databricks ends them (the page
    reads this as Cancelling). Never raises: the cancel has happened."""
    try:
        if run_ids and table_ready():
            with db.transaction() as cur:
                cur.execute(q.mark_cancel_requested_sql(), (list(run_ids), list(ACTIVE_STATES)))
            POLLER.wake()
    except Exception:
        pass
