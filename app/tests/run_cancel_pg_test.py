"""Cancel run against a real Postgres: the app marks the run TERMINATING (queries.mark_cancel_requested_sql), and the poller's
next upsert (queries.databricks_run_upsert_sql) must keep that while the Jobs API still says the run is going, as it
does right after jobs.cancel, and take any ended state the API reports after. A throwaway database with the Lakebase
schema.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
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
ACTIVE = ["QUEUED", "PENDING", "RUNNING", "TERMINATING", "BLOCKED", "WAITING_FOR_RETRY"]


@pytest.fixture
def pg():
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_app_cancel_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    conn = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=name), autocommit=True)
    try:
        for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
            conn.execute(statement)
        yield conn
    finally:
        conn.close()
        admin.execute(f"drop database {name}")
        admin.close()


def _seen(conn, run_id, state, result=None, ended=None):
    """The poller recording one run as the Jobs API lists it."""
    conn.cursor().executemany(q.databricks_run_upsert_sql(), [q.databricks_run_params({
        "run_id": run_id, "job_id": 10, "kind": "group", "schedule_id": None, "task_collection": "lab_c01",
        "trigger": "ONE_TIME", "parameters": json.dumps({"task_collection": "lab_c01"}), "life_cycle_state": state,
        "result_state": result, "state_message": None, "run_page_url": None,
        "started_at": "2026-10-30T10:00:00+00:00", "ended_at": ended})])


def _state(conn, run_id):
    return conn.execute("select life_cycle_state, result_state from lakefed_ingest.databricks_run where run_id = %s",
                        (run_id,)).fetchone()


def test_a_cancelled_run_stays_terminating_while_the_api_still_says_it_is_going(pg):
    _seen(pg, 7, "RUNNING")
    pg.execute(q.mark_cancel_requested_sql(), ([7], ACTIVE))
    for still_going in ("RUNNING", "PENDING", "QUEUED", "BLOCKED", "WAITING_FOR_RETRY"):
        _seen(pg, 7, still_going)
        assert _state(pg, 7) == ("TERMINATING", None), still_going


def test_an_ended_state_replaces_terminating_and_a_run_not_cancelled_takes_the_api_state(pg):
    _seen(pg, 8, "RUNNING")
    pg.execute(q.mark_cancel_requested_sql(), ([8], ACTIVE))
    _seen(pg, 8, "TERMINATED", result="CANCELED", ended="2026-10-30T10:05:00+00:00")
    assert _state(pg, 8) == ("TERMINATED", "CANCELED")
    _seen(pg, 9, "PENDING")
    _seen(pg, 9, "RUNNING")
    assert _state(pg, 9) == ("RUNNING", None)
