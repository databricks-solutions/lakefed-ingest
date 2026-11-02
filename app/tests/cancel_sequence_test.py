"""A Cancel run played through with fakes: Databricks (its Jobs API listing and its runs), Lakebase (the poller's table,
the group run and its events) and the cluster's late writes. At every step the page is read the way the job page reads
it (the job's phase from the active Databricks runs, the group run's row), and the two must agree: Cancelling from the
Cancel until Databricks ends the run, then Cancelled. The run ends with no failed and no running event.

The late writes go through the engine's own rules (state_store.failure_status and run_is_stopping), which the engine's
tests cover; this file only runs them with the poller's table and the app's readers."""
import contextlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import ingestion_jobs as ij
import jobs
import queries as q
import run_poller as rp

sys.path.append(str(Path(__file__).resolve().parents[2] / "src" / "lakefed_ingest"))
import state_store  # noqa: E402
from sql_parameter_types_test import untyped_parameters  # noqa: E402

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
CLOCK = {"now": NOW}     # what now() is in the statements the fake runs
STARTED = "2026-10-30T11:00:00+00:00"
JOB_ID, JOB_RUN, GROUP = 10, 500, "c1"


def _run(state="RUNNING", result=None, ended=None):
    """A group run as the Jobs API lists it (jobs.run_row's shape)."""
    return {"run_id": JOB_RUN, "job_id": JOB_ID, "parameters": {"task_collection": GROUP}, "task_collection": GROUP,
            "state": state, "result": result, "message": None, "started": STARTED, "ended": ended,
            "trigger": "RUN_JOB_TASK", "url": f"https://h/jobs/{JOB_ID}/runs/{JOB_RUN}"}


class Lakebase:
    """The poller's table (databricks_run), the group run (ingest_run) and its events (ingest_event), kept as the
    statements in these tests write them."""

    def __init__(self):
        self.databricks, self.runs, self.events, self.listing, self.jobs_api_down = {}, {}, [], [], False
        self.runs[3] = {"run_id": 3, "task_collection": GROUP, "job_run_id": str(JOB_RUN), "status": "running",
                        "started_at": STARTED, "finished_at": None, "detail": None, "total": 3, "ok": None,
                        "failed": None, "skipped": None, "reseeded": None, "parallelism": 4, "cluster_id": "c-1",
                        "work_id": None}
        self.events += [_event(1, "orders", "running"), _event(2, "lines", "running"), _event(3, "customers", "ok")]

    def query(self, sql, params=()):
        if sql == q.databricks_run_table_sql():
            return [{"ready": True}]
        if sql == q.open_runs_sql():
            return [{"run_id": r["run_id"], "task_collection": r["task_collection"], "job_run_id": r["job_run_id"],
                     "started_at": r["started_at"]} for r in self.runs.values() if r["status"] == "running"]
        if sql == q.databricks_run_states_sql():
            return [dict(self.databricks[i]) for i in params[0] if i in self.databricks]
        if sql == q.active_databricks_runs_sql():
            return [dict(r) for r in self.databricks.values() if r["life_cycle_state"] in params[0]]
        raise AssertionError(f"unexpected read: {sql[:80]}")

    @contextlib.contextmanager
    def transaction(self):
        yield _Cursor(self)

    def upsert(self, row):
        stored = self.databricks.get(row["run_id"], {})
        state = row["life_cycle_state"]
        if stored.get("life_cycle_state") == "TERMINATING" and state in q.NOT_YET_STOPPING_STATES:
            state = "TERMINATING"
        self.databricks[row["run_id"]] = dict(row, life_cycle_state=state,
                                              cancel_requested_at=stored.get("cancel_requested_at"))

    def mark_cancel_requested(self, run_ids, active_states, when):
        for run_id in run_ids:
            row = self.databricks.get(run_id)
            if row and row["life_cycle_state"] in active_states:
                row["life_cycle_state"] = "TERMINATING"
                row["cancel_requested_at"] = row["cancel_requested_at"] or when

    def close(self, run_id, detail, status, finished_at, reason, now):
        """What close_cancelled_run_sql and close_ended_run_sql do to one run still running and its events."""
        run = self.runs[run_id]
        if run["status"] != "running":
            return
        run["status"], run["detail"] = status, detail
        run["finished_at"] = finished_at or now.isoformat()
        for event in self.events:
            if event["run_id"] == run_id and event["status"] == "running":
                event.update(status=status, finished_at=event["finished_at"] or run["finished_at"],
                             error=event["error"] or reason)

    def cluster_write(self, event_id, error, now):
        """The engine's record of a table that failed: its status by state_store.failure_status, from the run's own
        record and its Databricks run as the poller has them."""
        event = next(e for e in self.events if e["id"] == event_id)
        run = self.runs[event["run_id"]]
        stopping = state_store.run_is_stopping(run["status"], self.databricks.get(int(run["job_run_id"])))
        event.update(status=state_store.failure_status(error, stopping), error=error, finished_at=now.isoformat())


class _Cursor:
    def __init__(self, lakebase):
        self.lakebase = lakebase

    def executemany(self, sql, rows):
        assert sql == q.databricks_run_upsert_sql()
        for row in rows:
            self.lakebase.upsert(row)

    def execute(self, sql, params=()):
        if sql == q.close_cancelled_run_sql():
            finished_at, detail, run_id, reason = params
            self.lakebase.close(run_id, detail, "cancelled", finished_at, reason, CLOCK["now"])
        elif sql == q.close_ended_run_sql():
            detail, run_id, reason, _ = params
            self.lakebase.close(run_id, detail, "failed", None, reason, CLOCK["now"])
        elif sql == q.mark_cancel_requested_sql():
            run_ids, active_states = params
            self.lakebase.mark_cancel_requested(run_ids, active_states, CLOCK["now"].isoformat())
        else:
            raise AssertionError(f"unexpected write: {sql[:80]}")


def _event(event_id, table, status):
    return {"id": event_id, "run_id": 3, "control_id": event_id, "table": table, "status": status,
            "started_at": "2026-10-30T11:01:00+00:00", "finished_at": None, "duration_ms": None, "error": None}


@pytest.fixture
def lakebase(monkeypatch):
    database = Lakebase()
    monkeypatch.setattr(rp.db, "query", database.query)
    monkeypatch.setattr(rp.db, "transaction", database.transaction)
    monkeypatch.setattr(rp, "table_ready", lambda: True)
    monkeypatch.setattr(rp, "tracked_jobs", lambda: {JOB_ID: ("group", None)})
    monkeypatch.setattr(jobs, "is_not_found", lambda error: isinstance(error, LookupError))
    monkeypatch.setattr(jobs, "run_row", lambda run: run)

    def runs(job_id, active_only=False, limit=25):
        return [dict(r) for r in database.listing if int(r["job_id"]) == int(job_id)]

    def get_run(run_id):
        if database.jobs_api_down:
            raise RuntimeError("the Jobs API is down")
        raise LookupError("no such run")
    monkeypatch.setattr(jobs, "runs", runs)
    monkeypatch.setattr(jobs, "get_run", get_run)
    return database


def _set_clock(when):
    CLOCK["now"] = when


def _cancel(monkeypatch, lakebase, when):
    """Cancel run as the app does it: the Jobs API is asked (jobs.cancel), then the poller's table is marked."""
    asked = []

    class Workspace:
        class jobs:
            @staticmethod
            def cancel_run(run_id):
                asked.append(run_id)
    monkeypatch.setattr(jobs, "workspace", lambda: Workspace)
    _set_clock(when)
    jobs.cancel(JOB_RUN)
    assert asked == [JOB_RUN]


def _poll(when):
    _set_clock(when)
    return rp.Poller().poll_once(when)


def _page(lakebase, when):
    """What the job page shows: its phase (ingestion_jobs.run_phase over the active Databricks runs) and the group
    run's row (ingestion_jobs.member_views, with the terminating runs the page reads)."""
    active = [rp.view(r) for r in lakebase.databricks.values() if r["life_cycle_state"] in rp.ACTIVE_STATES]
    terminating = [str(r["run_id"]) for r in lakebase.databricks.values() if r["life_cycle_state"] == "TERMINATING"]
    record = dict(lakebase.runs[3])
    run = dict(record, job_run_id=str(JOB_RUN), duration_s=None, work_started_at=None, work_finished_at=None)
    member = {"task_collection": GROUP, "offset_minutes": 0, "fire_at": when.isoformat(), "state": "running",
              "run": run, "extra": []}
    rows = ij.member_views({"members": [member], "checks": []}, {}, {GROUP: 10}, when, terminating)
    return {"phase": ij.run_phase(active, when)["phase"], "group": rows[0]["status"]["label"]}


def _statuses(lakebase):
    return sorted({e["status"] for e in lakebase.events})


def test_a_cancel_plays_through_with_one_phase_on_the_page_and_no_failed_or_running_event(lakebase, monkeypatch):
    lakebase.listing = [_run("RUNNING")]
    _poll(NOW - timedelta(minutes=1))
    assert _page(lakebase, NOW - timedelta(minutes=1)) == {"phase": "running", "group": "Running"}

    _cancel(monkeypatch, lakebase, NOW)
    assert _page(lakebase, NOW) == {"phase": "cancelling", "group": "Cancelling"}
    assert lakebase.runs[3]["status"] == "running"

    late = NOW + timedelta(seconds=10)
    lakebase.cluster_write(2, "SPARK_JOB_CANCELLED: Job 88 cancelled part of cancelled stage", late)
    lakebase.cluster_write(1, "connection reset by the source", late)
    assert [e["status"] for e in lakebase.events if e["id"] in (1, 2)] == ["cancelled", "cancelled"]
    assert _page(lakebase, late) == {"phase": "cancelling", "group": "Cancelling"}

    during = NOW + timedelta(seconds=20)
    lakebase.listing = [_run("RUNNING")]
    _poll(during)
    assert lakebase.databricks[JOB_RUN]["life_cycle_state"] == "TERMINATING"
    assert _page(lakebase, during) == {"phase": "cancelling", "group": "Cancelling"}

    ended = NOW + timedelta(seconds=30)
    lakebase.listing = [_run("TERMINATED", "CANCELED", ended=ended.isoformat())]
    _poll(ended)
    assert _page(lakebase, ended) == {"phase": "idle", "group": "Cancelled"}
    assert lakebase.runs[3]["status"] == "cancelled" and lakebase.runs[3]["finished_at"] == ended.isoformat()
    assert _statuses(lakebase) == ["cancelled", "ok"]

    later = ended + timedelta(seconds=5)
    lakebase.cluster_write(2, "Job aborted due to cancellation", later)
    assert _statuses(lakebase) == ["cancelled", "ok"]
    assert lakebase.runs[3]["status"] == "cancelled"


def test_a_cancel_whose_databricks_run_never_answers_closes_two_minutes_later_as_cancelled_with_no_failed_event(
        lakebase, monkeypatch):
    lakebase.listing = [_run("RUNNING")]
    _poll(NOW - timedelta(minutes=1))
    _cancel(monkeypatch, lakebase, NOW)
    lakebase.listing, lakebase.jobs_api_down = [], True
    lakebase.cluster_write(1, "connection reset by the source", NOW + timedelta(seconds=5))

    _poll(NOW + timedelta(minutes=1, seconds=59))
    assert lakebase.runs[3]["status"] == "running"
    assert _page(lakebase, NOW + timedelta(minutes=1, seconds=59)) == {"phase": "cancelling", "group": "Cancelling"}

    after = NOW + timedelta(minutes=2, seconds=1)
    _poll(after)
    assert lakebase.runs[3]["status"] == "cancelled" and lakebase.runs[3]["finished_at"] == after.isoformat()
    assert lakebase.databricks[JOB_RUN]["life_cycle_state"] == "TERMINATING"
    assert _statuses(lakebase) == ["cancelled", "ok"]
    assert _page(lakebase, after) == {"phase": "cancelling", "group": "Cancelling"}
    assert json.loads(lakebase.runs[3]["detail"])["reason"] == \
        "cancelled from the app, and Databricks had not ended its run 2 minutes after the cancel"


def test_the_close_statements_the_sequence_runs_are_ones_postgres_can_type():
    for sql in (q.close_cancelled_run_sql(), q.close_ended_run_sql(), q.mark_cancel_requested_sql()):
        assert untyped_parameters(sql) == []
