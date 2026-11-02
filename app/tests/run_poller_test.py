"""The run poller: the runs it records, the ingest runs it closes, and what the readers answer from its table.
The database and the Jobs API are fakes here, so these tests need neither."""
import contextlib
import json
from datetime import datetime, timedelta, timezone

import pytest

import jobs
import queries as q
import run_poller as rp

REAL_TABLE_READY = rp.table_ready
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
ACTIVE = ("RUNNING",)


def _run(run_id, job_id, state="RUNNING", result=None, started="2026-10-29T11:00:00+00:00", ended=None,
         trigger="RUN_JOB_TASK", task_collection="c1"):
    """A run as jobs.run_row reads it."""
    return {"run_id": run_id, "job_id": job_id, "parameters": {"task_collection": task_collection},
            "task_collection": task_collection, "state": state, "result": result, "message": None,
            "started": started, "ended": ended, "trigger": trigger, "url": f"https://h/jobs/{job_id}/runs/{run_id}"}


class FakeCursor:
    def __init__(self, database):
        self.database = database

    def executemany(self, sql, rows):
        assert sql == q.databricks_run_upsert_sql()
        for row in rows:
            self.database.runs[row["run_id"]] = dict(row)
            self.database.writes.append(row["run_id"])

    def execute(self, sql, params=()):
        if sql == q.close_ended_run_sql():
            self.database.closes.append((params[1], json.loads(params[0]), "failed"))
        elif sql == q.close_cancelled_run_sql():
            self.database.closes.append((params[2], json.loads(params[1]), "cancelled"))
            self.database.close_times[params[2]] = params[0]
        elif sql == q.mark_cancel_requested_sql():
            for run_id in params[0]:
                if self.database.runs[run_id]["life_cycle_state"] in params[1]:
                    self.database.runs[run_id]["life_cycle_state"] = "TERMINATING"
                    self.database.runs[run_id]["cancel_requested_at"] = self.database.runs[run_id].get(
                        "cancel_requested_at") or self.database.clock
        else:
            raise AssertionError(f"unexpected write: {sql}")


class FakeDatabase:
    """The databricks_run table, the ingest runs still running, and the schedules, answered as Lakebase would."""

    def __init__(self):
        self.runs, self.open_runs, self.writes, self.closes = {}, [], [], []
        self.close_times, self.clock = {}, NOW.isoformat()
        self.schedules, self.members = [], []

    def query(self, sql, params=()):
        if sql == q.schedules_sql():
            return list(self.schedules)
        if sql == q.schedule_members_sql():
            return list(self.members)
        if sql == q.open_runs_sql():
            return list(self.open_runs)
        if sql == q.active_databricks_runs_sql():
            return [dict(r) for r in self.runs.values() if r["life_cycle_state"] in params[0]]
        if sql == q.databricks_run_states_sql():
            return [dict(self.runs[i]) for i in params[0] if i in self.runs]
        if sql == q.databricks_run_table_sql():
            return [{"ready": True}]
        if sql == q.databricks_runs_of_job_sql():
            rows = [r for r in self.runs.values() if r["job_id"] == params["job_id"]
                    and (not params["active_only"] or r["life_cycle_state"] in params["active_states"])]
            return sorted(rows, key=lambda r: r["run_id"], reverse=True)[:params["limit"]]
        return []

    @contextlib.contextmanager
    def transaction(self):
        yield FakeCursor(self)


@pytest.fixture
def lakebase(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr(rp.db, "query", database.query)
    monkeypatch.setattr(rp.db, "transaction", database.transaction)
    monkeypatch.setattr(rp, "table_ready", lambda: True)
    monkeypatch.setattr(jobs, "ingest_variants", lambda: {"default": "10"})
    for name in ("CHECK_JOB_ID", "SETUP_JOB_ID", "DISCOVER_JOB_ID", "DELETE_JOB_ID", "CLUSTER_COPIES_JOB_ID"):
        monkeypatch.setattr(jobs, name, None)
    monkeypatch.setattr(jobs, "is_not_found", lambda error: isinstance(error, LookupError))
    monkeypatch.setattr(jobs, "run_row", lambda run: run)
    database.listing, database.by_id, database.listed_with = {}, {}, []

    def runs(job_id, active_only=False, limit=25):
        database.listed_with.append((int(job_id), active_only, limit))
        if int(job_id) in database.failing:
            raise RuntimeError("the Jobs API is down")
        rows = [r for r in database.listing.get(int(job_id), []) if not active_only or r["state"] in ACTIVE]
        return rows[:limit]

    def get_run(run_id):
        value = database.by_id[run_id]
        if isinstance(value, Exception):
            raise value
        return value

    database.failing = set()
    monkeypatch.setattr(jobs, "runs", runs)
    monkeypatch.setattr(jobs, "get_run", get_run)
    database.members = []
    database.schedules = []
    return database


def _poll(database, now=NOW):
    return rp.Poller().poll_once(now)


def test_the_first_poll_records_the_recent_runs_of_every_job_and_later_polls_record_only_changes(lakebase):
    lakebase.listing = {10: [_run(100, 10, "TERMINATED", "SUCCESS", ended="2026-10-29T11:30:00+00:00")],
                        30: [_run(300, 30, "SKIPPED", trigger="PERIODIC")]}
    lakebase.schedules = [{"id": 1, "options": {}}]
    lakebase.members = [{"schedule_id": 1, "job_id": 30, "task_collection": "c1"}]
    poller = rp.Poller()
    assert poller.poll_once(NOW) == {"recorded": 2, "closed": 0}
    assert lakebase.runs[100]["life_cycle_state"] == "TERMINATED" and lakebase.runs[300]["kind"] == "wrapper"
    assert lakebase.runs[300]["schedule_id"] == 1
    assert (10, False, rp.GROUP_LIMIT) in lakebase.listed_with
    assert (30, False, rp.WRAPPER_SEED_LIMIT) in lakebase.listed_with

    lakebase.writes.clear()
    assert poller.poll_once(NOW) == {"recorded": 0, "closed": 0}
    assert lakebase.writes == []


def test_a_group_run_that_leaves_its_listing_is_looked_up_once_and_recorded_as_it_ended(lakebase):
    lakebase.listing = {10: [_run(101, 10)]}
    poller = rp.Poller()
    poller.poll_once(NOW)
    lakebase.listing = {10: []}
    lakebase.by_id[101] = _run(101, 10, "TERMINATED", "SUCCESS", ended="2026-10-29T11:50:00+00:00")
    assert poller.poll_once(NOW) == {"recorded": 1, "closed": 0}
    assert lakebase.runs[101]["life_cycle_state"] == "TERMINATED" and lakebase.runs[101]["result_state"] == "SUCCESS"


def test_a_run_the_jobs_api_no_longer_has_is_recorded_not_found_and_keeps_its_start(lakebase):
    lakebase.listing = {10: [_run(102, 10)]}
    poller = rp.Poller()
    poller.poll_once(NOW)
    lakebase.listing = {10: []}
    lakebase.by_id[102] = LookupError("Run 102 does not exist")
    poller.poll_once(NOW)
    assert lakebase.runs[102]["life_cycle_state"] == rp.NOT_FOUND
    assert lakebase.runs[102]["started_at"] == "2026-10-29T11:00:00+00:00"


def test_a_run_that_cannot_be_read_is_left_as_it_is_and_a_job_whose_listing_fails_is_not_looked_into(lakebase):
    lakebase.listing = {10: [_run(103, 10)]}
    poller = rp.Poller()
    poller.poll_once(NOW)
    lakebase.listing = {10: []}
    lakebase.by_id[103] = RuntimeError("timed out")
    lakebase.writes.clear()
    assert poller.poll_once(NOW) == {"recorded": 0, "closed": 0}
    assert lakebase.runs[103]["life_cycle_state"] == "RUNNING" and lakebase.writes == []

    lakebase.failing = {10}
    lakebase.by_id[103] = AssertionError("should not be asked: its job's listing failed")
    poller.poll_once(NOW)
    assert lakebase.runs[103]["life_cycle_state"] == "RUNNING"


def test_an_ingest_run_whose_databricks_run_ended_is_closed_and_one_still_going_is_not(lakebase):
    lakebase.listing = {10: [_run(104, 10, "TERMINATED", "CANCELED", ended="2026-10-29T11:40:00+00:00"),
                             _run(105, 10)]}
    started = "2026-10-29T11:00:00+00:00"
    lakebase.open_runs = [{"run_id": 7, "task_collection": "c1", "job_run_id": "104", "started_at": started},
                          {"run_id": 8, "task_collection": "c1", "job_run_id": "105", "started_at": started}]
    assert rp.Poller().poll_once(NOW) == {"recorded": 2, "closed": 1}
    [(run_id, detail, status)] = lakebase.closes
    assert run_id == 7 and detail["closed_by_app"] == "reconcile" and detail["databricks_run"] == 104
    assert detail["databricks_result"] == "CANCELED" and status == "cancelled"


def test_a_missing_run_closes_only_after_a_day(lakebase):
    recent, old = (NOW - timedelta(hours=2)).isoformat(), (NOW - timedelta(days=2)).isoformat()
    lakebase.open_runs = [{"run_id": 1, "task_collection": "c1", "job_run_id": "201", "started_at": recent},
                          {"run_id": 2, "task_collection": "c1", "job_run_id": "202", "started_at": old}]
    lakebase.by_id = {201: LookupError("gone"), 202: LookupError("gone")}
    assert rp.Poller().poll_once(NOW) == {"recorded": 0, "closed": 1}
    assert [(run_id, status) for run_id, _, status in lakebase.closes] == [(2, "failed")]


def test_wrappers_are_listed_newest_first_every_poll_and_others_only_while_active(lakebase):
    lakebase.schedules = [{"id": 1, "options": {}}]
    lakebase.members = [{"schedule_id": 1, "job_id": 30, "task_collection": "c1"}]
    poller = rp.Poller()
    poller.poll_once(NOW)
    lakebase.listed_with.clear()
    poller.poll_once(NOW)
    assert (30, False, rp.WRAPPER_LIMIT) in lakebase.listed_with
    assert (10, True, rp.GROUP_LIMIT) in lakebase.listed_with


def test_a_cancel_marks_the_active_runs_it_asked_to_stop_as_terminating_and_keeps_when_it_was_asked(lakebase):
    lakebase.runs[9] = {"run_id": 9, "job_id": 10, "life_cycle_state": "RUNNING", "result_state": None,
                        "state_message": None, "started_at": None, "ended_at": None, "parameters": "{}",
                        "kind": "group",
                        "schedule_id": None, "task_collection": None, "trigger": None, "run_page_url": None}
    rp.mark_stopping([9, 10])
    assert lakebase.runs[9]["life_cycle_state"] == "TERMINATING"
    asked = lakebase.runs[9]["cancel_requested_at"]
    lakebase.clock = "2026-10-29T13:00:00+00:00"
    rp.mark_stopping([9])
    assert lakebase.runs[9]["cancel_requested_at"] == asked


def test_the_cancel_mark_is_one_statement_that_sets_the_terminating_state_and_the_time_it_was_asked():
    sql = q.mark_cancel_requested_sql()
    assert "life_cycle_state = 'TERMINATING'" in sql
    assert "cancel_requested_at = coalesce(cancel_requested_at, now())" in sql
    assert "where run_id = any(%s) and life_cycle_state = any(%s)" in sql


def test_a_run_the_app_cancelled_closes_as_cancelled_with_databricks_end_time_whatever_the_result():
    started = "2026-10-29T11:00:00+00:00"
    requested = (NOW - timedelta(seconds=30)).isoformat()
    ended = {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS", "state_message": None,
             "ended_at": "2026-10-29T11:59:00+00:00", "cancel_requested_at": requested}
    closed = rp.closure(ended, started, 1, NOW)
    assert closed["status"] == "cancelled" and closed["finished_at"] == "2026-10-29T11:59:00+00:00"
    assert closed["reason"] == "cancelled, and its Databricks run ended SUCCESS at 2026-10-29T11:59:00+00:00"


def test_a_cancelled_run_databricks_has_not_ended_closes_only_two_minutes_after_the_cancel():
    started = "2026-10-29T11:00:00+00:00"
    going = {"life_cycle_state": "TERMINATING", "result_state": None, "state_message": None, "ended_at": None,
             "cancel_requested_at": (NOW - timedelta(minutes=1, seconds=59)).isoformat()}
    assert rp.closure(going, started, 1, NOW) is None
    later = dict(going, cancel_requested_at=(NOW - timedelta(minutes=2, seconds=1)).isoformat())
    closed = rp.closure(later, started, 1, NOW)
    assert closed["status"] == "cancelled" and closed["finished_at"] is None
    assert closed["reason"] == "cancelled from the app, and Databricks had not ended its run 2 minutes after the cancel"
    assert json.loads(closed["detail"])["databricks_state"] == "TERMINATING"


def test_a_run_whose_databricks_run_failed_without_a_cancel_closes_as_failed():
    ended = {"life_cycle_state": "TERMINATED", "result_state": "FAILED", "state_message": None, "ended_at": None,
             "cancel_requested_at": None}
    assert rp.closure(ended, "2026-10-29T11:00:00+00:00", 1, NOW)["status"] == "failed"


def test_the_poller_keeps_a_cancelled_run_terminating_until_databricks_says_it_ended():
    # Right after jobs.cancel the API still says RUNNING; only an ended state replaces the app's TERMINATING.
    assert set(q.NOT_YET_STOPPING_STATES) == set(rp.ACTIVE_STATES) - {"TERMINATING"}
    sql = q.databricks_run_upsert_sql()
    assert "insert into lakefed_ingest.databricks_run as stored (" in sql
    assert ("life_cycle_state = case when stored.life_cycle_state = 'TERMINATING' and excluded.life_cycle_state in "
            "('QUEUED', 'PENDING', 'RUNNING', 'BLOCKED', 'WAITING_FOR_RETRY') then 'TERMINATING' "
            "else excluded.life_cycle_state end") in sql
    assert "result_state = excluded.result_state" in sql and "ended_at = excluded.ended_at" in sql


def test_a_new_run_is_recorded_as_pending_when_the_table_exists_and_never_raises_without_it(lakebase, monkeypatch):
    rp.note_started(10, 500, {"task_collection": "c2"})
    assert lakebase.runs[500]["life_cycle_state"] == "PENDING" and lakebase.runs[500]["task_collection"] == "c2"

    def broken():
        raise RuntimeError("no table")
    monkeypatch.setattr(rp.db, "transaction", broken)
    rp.note_started(10, 501, {})
    rp.mark_stopping([500])
    monkeypatch.setattr(rp, "table_ready", lambda: False)
    rp.note_started(10, 502, {})
    assert 502 not in lakebase.runs


def test_only_the_poller_holding_the_advisory_lock_polls(monkeypatch):
    class Connection:
        def __init__(self, granted):
            self.granted, self.closed, self.statements = granted, False, []

        def execute(self, sql, params=()):
            self.statements.append(sql)
            return self

        def fetchone(self):
            return (self.granted,)

        def close(self):
            self.closed = True
    other = Connection(granted=False)
    monkeypatch.setattr(rp.db, "connect", lambda: other)
    poller = rp.Poller()
    assert poller._leads() is False and other.closed

    leader = Connection(granted=True)
    monkeypatch.setattr(rp.db, "connect", lambda: leader)
    assert poller._leads() is True
    assert poller._leads() is True and leader.statements[-1] == "select 1"


def test_the_table_is_looked_for_again_only_every_recheck_interval(monkeypatch):
    calls = []

    def query(sql, params=()):
        calls.append(sql)
        raise RuntimeError("no such table")
    monkeypatch.setattr(rp, "table_ready", REAL_TABLE_READY)
    monkeypatch.setattr(rp.db, "query", query)
    rp._ready.update(yes=False, checked=None)
    assert rp.table_ready() is False and rp.table_ready() is False
    assert len(calls) == 1
    rp._ready.update(checked=0.0)
    rp.table_ready()
    assert len(calls) == 2
    rp._ready.update(yes=False, checked=None)


def test_the_readers_answer_from_the_table_and_fall_back_to_the_jobs_api_for_what_it_lacks(lakebase, monkeypatch):
    lakebase.runs[11] = {"run_id": 11, "job_id": 10, "kind": "group", "schedule_id": None, "task_collection": "c1",
                         "trigger": "RUN_JOB_TASK", "parameters": json.dumps({"task_collection": "c1"}),
                         "life_cycle_state": "RUNNING", "result_state": None, "state_message": None,
                         "run_page_url": "https://h/run/11", "started_at": "2026-10-29T11:00:00+00:00",
                         "ended_at": None}
    lakebase.runs[12] = dict(lakebase.runs[11], run_id=12, life_cycle_state="TERMINATED", result_state="FAILED",
                             ended_at="2026-10-29T11:30:00+00:00")
    monkeypatch.setattr(jobs, "run_state", lambda run_id: {"run_id": run_id, "state": "PENDING", "url": "api"})
    monkeypatch.setattr(jobs, "run_ended", lambda run_id: None)

    [listed] = rp.runs_of(10, active_only=True, limit=5)
    assert listed["run_id"] == 11 and listed["task_collection"] == "c1" and listed["state"] == "RUNNING"
    assert rp.run_state_of(12)["result"] == "FAILED"
    assert rp.run_state_of(99)["url"] == "api"
    assert rp.ended_of(11) is None
    assert rp.ended_of(12)["result"] == "FAILED"


def test_a_closure_records_the_reason_and_a_run_still_going_is_not_closed():
    still_going = {"life_cycle_state": "RUNNING"}
    assert rp.closure(still_going, "2026-10-29T11:00:00+00:00", 1, NOW) is None
    ended = {"life_cycle_state": "INTERNAL_ERROR", "result_state": None, "state_message": "lost", "ended_at": None}
    closed = rp.closure(ended, "2026-10-29T11:00:00+00:00", 1, NOW)
    assert closed["reason"] == "its Databricks run ended INTERNAL_ERROR without finishing (lost)"
    assert json.loads(closed["detail"])["databricks_run"] == 1
    missing = {"life_cycle_state": rp.NOT_FOUND}
    assert rp.closure(missing, (NOW - timedelta(hours=1)).isoformat(), 1, NOW) is None
    reason = rp.closure(missing, (NOW - timedelta(days=2)).isoformat(), 1, NOW)["reason"]
    assert reason.startswith("its Databricks run no")


def test_the_drafts_purge_is_asked_for_at_most_hourly_and_only_when_a_draft_is_due(monkeypatch):
    asked, started = [], []
    due = [[{"discovery_id": "d1"}], []]
    monkeypatch.setattr(jobs, "DELETE_JOB_ID", "9")
    monkeypatch.setattr(rp.db, "query", lambda sql, params=(): asked.append((sql, params)) or due.pop(0))
    monkeypatch.setattr(jobs, "start_draft_purge", started.append)
    assert rp.purge_drafts_if_due(None, 100.0) == 100.0 and started == [rp.DRAFT_TTL_S]
    assert rp.purge_drafts_if_due(100.0, 100.0 + rp.PURGE_EVERY_S - 1) == 100.0 and len(asked) == 1
    assert rp.purge_drafts_if_due(100.0, 100.0 + rp.PURGE_EVERY_S) == 100.0 + rp.PURGE_EVERY_S
    assert asked == [(q.drafts_to_purge_sql(), (rp.DRAFT_TTL_S,))] * 2 and len(started) == 1     # none due: no run
    monkeypatch.setattr(jobs, "DELETE_JOB_ID", None)
    assert rp.purge_drafts_if_due(None, 0.0) is None and len(asked) == 2


def test_the_purge_run_takes_the_drafts_mode_and_its_time_limit(monkeypatch):
    sent = []
    monkeypatch.setattr(jobs, "DELETE_JOB_ID", "9")
    monkeypatch.setattr(jobs, "run_now", lambda job_id, parameters: sent.append((job_id, parameters)) or 5)
    assert jobs.start_draft_purge(7200) == 5 and sent == [("9", {"purge_drafts": "true", "draft_ttl_s": "7200"})]
