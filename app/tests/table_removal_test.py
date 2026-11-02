"""Removing one table from a live job: what is refused, what is recorded, and how it shows on the Failing tables list."""
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import queries as q  # noqa: E402
import table_removal as tr  # noqa: E402


def test_a_removal_needs_the_user_to_confirm_it_first():
    assert tr.confirmation_missing(False) and "confirm" in tr.confirmation_missing(False)
    assert tr.confirmation_missing(True) is None


def test_a_removal_is_refused_for_a_deleted_job_a_shared_group_a_running_group_or_one_already_going():
    assert "deleted" in tr.refusal(True, [], False, False)
    assert "another live ingestion job" in tr.refusal(False, ["sales"], False, False)
    assert "sales" in tr.refusal(False, ["sales"], False, False)
    assert "going" in tr.refusal(False, [], True, False)
    assert "already going" in tr.refusal(False, [], False, True)
    assert tr.refusal(False, [], False, False) is None


def test_the_records_hold_the_whole_request_and_the_run_once_it_starts():
    record = tr.requests([10, 11], "op@example.com", "2026-10-30T00:00:00+00:00")
    assert record["10"] == {"run_id": None, "requested_at": "2026-10-30T00:00:00+00:00",
                            "requested_by": "op@example.com", "error": None}
    started = tr.requests([10], "op@example.com", "2026-10-30T00:00:00+00:00", run_id=77)
    assert started["10"]["run_id"] == 77 and started["10"]["requested_by"] == "op@example.com"


def test_a_recorded_removal_shows_removing_until_its_run_ends_and_failed_when_it_did_not_succeed():
    entry = {"run_id": 77, "error": None}
    assert tr.state_of(None, None) is None
    assert tr.state_of(entry, {"state": "RUNNING", "result": None}) == {"state": "removing", "message": None}
    assert tr.state_of(entry, {"state": "QUEUED", "result": None}) == {"state": "removing", "message": None}
    assert tr.state_of(entry, {"state": "TERMINATED", "result": "SUCCESS"}) is None
    failed = tr.state_of(entry, {"state": "TERMINATED", "result": "FAILED", "message": "a run of group sales is loading"})
    assert failed == {"state": "failed", "message": "a run of group sales is loading"}
    assert tr.state_of(entry, None) == {"state": "failed", "message": "the delete job's run can't be read"}
    assert tr.state_of({"run_id": None, "error": "the delete job didn't start: x"}, None)["state"] == "failed"


def test_the_removing_ids_are_the_ones_whose_run_has_not_ended():
    entries = {"10": {"run_id": 77, "error": None}, "11": {"run_id": 78, "error": None}}
    runs = {10: {"state": "RUNNING"}, 11: {"state": "TERMINATED", "result": "SUCCESS"}}
    assert tr.removing(entries, [10, 11, 12], runs) == [10]


LATER = datetime(2026, 10, 30, 0, 2, 1, tzinfo=timezone.utc)
RECORDED = "2026-10-30T00:00:00+00:00"


def test_a_removal_with_no_run_is_still_removing_for_two_minutes_then_failed_so_it_can_be_pressed_again():
    entry = {"run_id": None, "requested_at": RECORDED, "error": None}
    assert tr.state_of(entry, None) == {"state": "removing", "message": None}
    assert tr.state_of(entry, None, datetime(2026, 10, 30, 0, 1, 59, tzinfo=timezone.utc)) == {
        "state": "removing", "message": None}
    assert tr.state_of(entry, None, LATER) == {"state": "failed", "message": tr.NOT_STARTED}


def test_a_stalled_removal_with_no_run_no_longer_blocks_a_new_remove():
    entries = {"10": {"run_id": None, "requested_at": RECORDED, "error": None}}
    assert tr.removing(entries, [10], {10: None}) == [10]
    assert tr.removing(entries, [10], {10: None}, LATER) == []


def test_a_removal_without_a_readable_request_time_is_not_failed_early():
    entry = {"run_id": None, "requested_at": None, "error": None}
    assert tr.state_of(entry, None, LATER) == {"state": "removing", "message": None}
    dated = {"run_id": None, "requested_at": datetime(2026, 10, 30, tzinfo=timezone.utc), "error": None}
    assert tr.state_of(dated, None, LATER)["state"] == "failed"


def test_a_removed_table_is_recorded_under_the_key_its_rows_are_found_by():
    records = tr.exclusion_records([{"server": "H1:1433", "src_database": "Db1", "src_schema": "dbo",
                                     "src_table": "Orders"}], "op@example.com", RECORDED)
    [key] = records
    assert records[key] == {"removed_at": RECORDED, "removed_by": "op@example.com"}
    excluded = tr.exclusions_of({tr.EXCLUDED: records})
    assert excluded == {("h1", "db1", "dbo", "orders")}
    assert tr.is_excluded("h1", "DB1", "dbo", "orders", excluded)
    assert not tr.is_excluded("h1", "db1", "dbo", "orders2", excluded)


def test_leave_out_marks_only_the_valid_lines_of_removed_tables_and_counts_them():
    excluded = _excluded("orders")
    lines = [_line("orders"), _line("items"), _line("orders", status="in_job"), _line("orders", status="invalid")]
    assert tr.leave_out(lines, excluded) == 1
    assert [line["status"] for line in lines] == [tr.LEFT_OUT, "valid", "in_job", "invalid"]


def test_the_exclusions_taken_back_in_are_the_keys_of_the_valid_lines_only():
    lines = [_line("orders"), _line("items", status="in_job"), _line("bad", status="invalid"), _line("items")]
    assert tr.line_keys(lines) == sorted([tr.exclusion_key("h1", "db1", "dbo", "orders"),
                                          tr.exclusion_key("h1", "db1", "dbo", "items")])


def test_a_job_with_no_exclusions_excludes_nothing():
    assert tr.exclusions_of({}) == set() and tr.exclusions_of(None) == set()
    assert not tr.is_excluded("h1", "db1", "dbo", "orders", set())


def _line(table: str, status: str = "valid", host: str = "h1") -> dict:
    row = {"src_host": host, "src_connection": None, "src_database": "db1", "src_schema": "dbo", "src_table": table}
    return {"row": row, "status": status}


def _excluded(table: str) -> set:
    return tr.exclusions_of({tr.EXCLUDED: tr.exclusion_records(
        [{"server": "h1", "src_database": "db1", "src_schema": "dbo", "src_table": table}], None, RECORDED)})


class _Cursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))


def _job(**overrides):
    job = {"id": 1, "name": "Hourly", "deleted_at": None, "job_variant": "default", "options": {}}
    job.update(overrides)
    return job


def _members():
    return [{"schedule_id": 1, "task_collection": "sales", "removed_at": None}]


def _stub_endpoint(monkeypatch, *, job=None, running=False, started=None, failure=None):
    """The remove endpoint with Lakebase, the permission check, the lock and the delete job stubbed out."""
    cursor = _Cursor()
    rows = [{"id": 10, "task_collection": "sales", "server": "h1", "src_database": "db1", "src_schema": "dbo",
             "src_table": "orders"}]

    def query(sql, params=()):
        if sql == q.table_rows_by_ids_sql():
            return rows
        if sql == q.job_own_groups_sql():
            return [{"task_collection": "sales"}]
        return []

    @contextmanager
    def write():
        yield cursor

    def start(schedule_id, control_ids):
        if failure:
            raise RuntimeError(failure)
        return started

    monkeypatch.setattr(main, "_load_schedule", lambda sid, cur=None: (job or _job(), _members()))
    monkeypatch.setattr(main, "_refuse_job", lambda job, request, action: None)
    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main, "_write", write)
    monkeypatch.setattr(main, "_lock_job", lambda cur, job_id: [])
    monkeypatch.setattr(main, "_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_job_active_runs", lambda job, members: [{"run_id": 5}] if running else [])
    monkeypatch.setattr(main, "_recorded_running", lambda members: False)
    monkeypatch.setattr(main.jobs, "start_table_remove", start)
    monkeypatch.setattr(main.jobs, "workspace_url", lambda: "https://ws.example")
    monkeypatch.setattr(main.jobs, "DELETE_JOB_ID", "99")
    return cursor, TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})


def test_the_endpoint_refuses_a_removal_that_is_not_confirmed(monkeypatch):
    _, client = _stub_endpoint(monkeypatch, started=77)
    response = client.post("/api/ingestion-jobs/1/tables/remove", json={"control_ids": [10]})
    assert response.status_code == 422 and "confirm" in response.text


def test_the_endpoint_refuses_a_removal_with_no_delete_job_and_records_nothing(monkeypatch):
    started = []
    cursor, client = _stub_endpoint(monkeypatch, started=77)
    monkeypatch.setattr(main.jobs, "DELETE_JOB_ID", None)
    monkeypatch.setattr(main.jobs, "start_table_remove", lambda *args: started.append(args) or 77)
    response = client.post("/api/ingestion-jobs/1/tables/remove", json={"control_ids": [10], "confirmed": True})
    assert response.status_code == 409 and "deploy the engine bundle" in response.text
    assert started == [] and cursor.executed == []


def test_the_endpoint_refuses_while_a_run_of_the_group_is_going_and_starts_nothing(monkeypatch):
    started = []
    _, client = _stub_endpoint(monkeypatch, running=True, started=77)
    monkeypatch.setattr(main.jobs, "start_table_remove", lambda *args: started.append(args) or 77)
    response = client.post("/api/ingestion-jobs/1/tables/remove", json={"control_ids": [10], "confirmed": True})
    assert response.status_code == 409 and "going" in response.text
    assert started == []


def test_the_endpoint_records_the_request_then_its_run_and_starts_no_delete(monkeypatch):
    cursor, client = _stub_endpoint(monkeypatch, started=77)
    response = client.post("/api/ingestion-jobs/1/tables/remove", json={"control_ids": [10], "confirmed": True})
    assert response.status_code == 200, response.text
    assert response.json()["run"] == {"run_id": 77, "url": "https://ws.example/jobs/" + str(main.jobs.DELETE_JOB_ID)
                                      + "/runs/77"}
    writes = [(sql, params) for sql, params in cursor.executed if sql == q.set_table_remove_sql()]
    assert [json.loads(params[0])["10"]["run_id"] for _, params in writes] == [None, 77]
    assert all("delete from" not in sql.lower() for sql, _ in cursor.executed)


def test_the_endpoint_excludes_the_removed_table_in_the_same_write_as_its_request(monkeypatch):
    cursor, client = _stub_endpoint(monkeypatch, started=77)
    response = client.post("/api/ingestion-jobs/1/tables/remove", json={"control_ids": [10], "confirmed": True})
    assert response.status_code == 200, response.text
    [(_, params)] = [(sql, params) for sql, params in cursor.executed if sql == q.add_exclusions_sql()]
    assert list(json.loads(params[0])) == [tr.exclusion_key("h1", "db1", "dbo", "orders")]
    assert params[1] == 1
    assert [sql for sql, _ in cursor.executed][:2] == [q.set_table_remove_sql(), q.add_exclusions_sql()]


def test_a_delete_job_that_does_not_start_is_recorded_on_the_row_and_reported(monkeypatch):
    cursor, client = _stub_endpoint(monkeypatch, failure="no job. more")
    response = client.post("/api/ingestion-jobs/1/tables/remove", json={"control_ids": [10], "confirmed": True})
    assert response.json()["error"].startswith("the delete job didn't start")
    last = [params for sql, params in cursor.executed if sql == q.set_table_remove_sql()][-1]
    assert json.loads(last[0])["10"]["error"].startswith("the delete job didn't start")


def test_the_list_shows_a_removal_that_never_got_its_run_as_failed_with_the_reason(monkeypatch):
    stalled = {"10": {"run_id": None, "requested_at": "2020-01-01T00:00:00+00:00", "error": None}}
    job = _job(options={tr.OPTION: stalled})
    failing = [{"control_id": 10, "task_collection": "sales", "server": "h1", "src_database": "db1",
                "src_schema": "dbo", "src_table": "orders", "error": "boom", "error_kind": "transient",
                "error_class": None, "checkpoint_detail": None, "failing_since": None, "runs": 1, "failures": 1}]
    monkeypatch.setattr(main, "_load_schedule", lambda sid, cur=None: (job, _members()))
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): failing if sql == q.failing_tables_sql() else [])
    response = TestClient(main.app).get("/api/ingestion-jobs/1/failing-tables")
    [row] = response.json()["tables"]
    assert row["remove"] == {"state": "failed", "message": tr.NOT_STARTED}


def test_the_list_shows_the_source_message_and_a_removal_that_is_going(monkeypatch):
    job = _job(options={tr.OPTION: {"10": {"run_id": 77, "error": None}}})
    failing = [{"control_id": 10, "task_collection": "sales", "server": "h:1433", "src_database": "db1",
                "src_schema": "dbo", "src_table": "orders", "error": "Login failed for user 'svc'",
                "error_kind": "transient", "error_class": "FAILED_JDBC.CONNECTION", "checkpoint_detail": None,
                "failing_since": None, "runs": 1, "failures": 1}]
    monkeypatch.setattr(main, "_load_schedule", lambda sid, cur=None: (job, _members()))
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): failing if sql == q.failing_tables_sql() else [])
    monkeypatch.setattr(main.run_poller, "run_state_of", lambda run_id: {"state": "RUNNING", "result": None})
    response = TestClient(main.app).get("/api/ingestion-jobs/1/failing-tables")
    [row] = response.json()["tables"]
    assert row["message"] == "Login failed for user 'svc'"
    assert row["remove"] == {"state": "removing", "message": None}
