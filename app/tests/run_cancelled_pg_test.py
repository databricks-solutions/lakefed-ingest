"""Cancelled runs against a real Postgres, as the app's role: the poller's close (queries.close_cancelled_run_sql), the
mark that shows a cancel as Cancelling (queries.mark_cancel_requested_sql) and the failure list that must not count a
cancelled run's events (queries.run_failures_sql). The role gets what the app is granted (select, insert and update;
the app has no DELETE), and each statement runs under SET ROLE, so one that needs a delete fails here. The seed rows
are written as the owner, as the engine writes them.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
import contextlib
import json
import os
import sys
import uuid
from pathlib import Path

import pytest
import queries as q

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]
ENDED = "2026-10-30T10:05:00+00:00"
REASON = "cancelled, and its Databricks run ended CANCELED at 2026-10-30T10:05:00+00:00"
ACTIVE_STATES = ["QUEUED", "PENDING", "RUNNING", "BLOCKED", "WAITING_FOR_RETRY"]


@pytest.fixture
def pg():
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_app_cancelled_{uuid.uuid4().hex[:8]}"
    role = f"lakefed_app_{uuid.uuid4().hex[:8]}"
    schema_sql = (REPO / "src" / "lakefed_ingest" / "lakebase_schema.sql").read_text()
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    conn = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=name), autocommit=True)
    try:
        for statement in state_store.split_sql_statements(schema_sql):
            conn.execute(statement)
        conn.execute(f"create role {role} nologin")
        conn.execute(f"grant usage on schema lakefed_ingest to {role}")
        conn.execute(f"grant select, insert, update on all tables in schema lakefed_ingest to {role}")
        conn.execute(f"grant usage, select on all sequences in schema lakefed_ingest to {role}")
        conn.execute(f"grant {role} to current_user")
        yield conn, role
    finally:
        conn.close()
        admin.execute(f"drop database {name}")
        admin.execute(f"drop role if exists {role}")
        admin.close()


@contextlib.contextmanager
def _as_app(conn, role):
    conn.execute(f"set role {role}")
    try:
        yield conn
    finally:
        conn.execute("reset role")


def test_a_cancelled_run_closes_as_cancelled_at_databricks_end_time_with_no_failed_or_running_event(pg):
    conn, role = pg
    _seed_group_run(conn, 701)
    assert _close(conn, role, 701, ENDED) == (1, 2)
    assert conn.execute("select status, finished_at = %s::timestamptz from lakefed_ingest.ingest_run "
                        "where run_id = 701", (ENDED,)).fetchone() == ("cancelled", True)
    assert _statuses(conn, 701) == ["cancelled", "cancelled", "ok"]
    assert _count_events(conn, 701, ["failed", "running"]) == 0


def test_a_cancelled_run_with_no_databricks_end_time_closes_now_and_a_second_close_changes_nothing(pg):
    conn, role = pg
    _seed_group_run(conn, 702)
    assert _close(conn, role, 702, None) == (1, 2)
    assert conn.execute("select finished_at is not null from lakefed_ingest.ingest_run "
                        "where run_id = 702").fetchone() == (True,)
    assert _close(conn, role, 702, None) == (0, 0)


def test_a_late_failure_on_a_cancelled_run_is_not_listed_as_a_failed_table(pg):
    conn, role = pg
    _seed_group_run(conn, 703)
    # The engine writes a table that fails after the cancel as cancelled (state_store.failure_status, stopping run).
    conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_table, status, "
                 "started_at, error) values (703, 'lab_c01', 4, 'items', 'cancelled', "
                 "'2026-10-30T10:01:00+00:00', 'boom')")
    _close(conn, role, 703, ENDED)
    with _as_app(conn, role) as app:
        assert app.execute(q.run_failures_sql(), ([703],)).fetchall() == []
    assert _count_events(conn, 703, ["failed"]) == 0


def test_a_cancel_marks_only_active_databricks_runs_terminating_and_keeps_the_first_request_time(pg):
    conn, role = pg
    conn.execute("insert into lakefed_ingest.databricks_run (run_id, job_id, kind, task_collection, life_cycle_state, "
                 "started_at) values (500, 10, 'group', 'lab_c01', 'RUNNING', '2026-10-30T10:00:00+00:00'), "
                 "(501, 10, 'group', 'lab_c02', 'TERMINATED', '2026-10-30T10:00:00+00:00')")
    with _as_app(conn, role) as app:
        app.execute(q.mark_cancel_requested_sql(), ([500, 501], ACTIVE_STATES))
    first = _databricks_run(conn, 500)
    assert first[0] == "TERMINATING" and first[1] is not None
    assert _databricks_run(conn, 501) == ("TERMINATED", None)
    with _as_app(conn, role) as app:
        app.execute(q.mark_cancel_requested_sql(), ([500, 501], ACTIVE_STATES))
    assert _databricks_run(conn, 500) == first


def _seed_group_run(conn, run_id):
    conn.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, job_run_id, status, started_at) "
                 "values (%s, 'lab_c01', '9001', 'running', '2026-10-30T10:00:00+00:00')", (run_id,))
    events = [(1, "orders", "running", None), (2, "lines", "retried", "timed out"), (3, "customers", "ok", None)]
    for control_id, table, status, error in events:
        conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_table, status, "
                     "started_at, error) values (%s, 'lab_c01', %s, %s, %s, '2026-10-30T10:01:00+00:00', %s)",
                     (run_id, control_id, table, status, error))


def _close(conn, role, run_id, finished_at):
    detail = json.dumps({"reason": REASON, "databricks_run": 9001, "databricks_state": "TERMINATED",
                         "databricks_result": "CANCELED"})
    with _as_app(conn, role) as app:
        return app.execute(q.close_cancelled_run_sql(), (finished_at, detail, run_id, REASON)).fetchone()


def _statuses(conn, run_id):
    return [row[0] for row in conn.execute(
        "select status from lakefed_ingest.ingest_event where run_id = %s order by control_id", (run_id,)).fetchall()]


def _count_events(conn, run_id, statuses):
    return conn.execute("select count(*) from lakefed_ingest.ingest_event where run_id = %s and status = any(%s)",
                        (run_id, statuses)).fetchone()[0]


def _databricks_run(conn, run_id):
    return conn.execute("select life_cycle_state, cancel_requested_at from lakefed_ingest.databricks_run "
                        "where run_id = %s", (run_id,)).fetchone()
