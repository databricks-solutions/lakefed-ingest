"""Closing runs against a real Postgres, where parameters are typed by the server: Cancel run's close-out and the
run poller's reconcile run queries.close_ended_run_sql, which the unit tests' fakes never prepare. The sequence is the
one that left a job's runs 'running' on 29 Oct: eight group runs loading, Cancel run, then Databricks reporting each
one cancelled. A throwaway database with the Lakebase schema.

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
GROUPS = [f"lab_c{n:02d}" for n in range(1, 9)]


@pytest.fixture
def pg():
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_app_close_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    conn = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=name), autocommit=True)
    try:
        for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
            conn.execute(statement)
        for n, group in enumerate(GROUPS, start=1):
            conn.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, job_run_id, status) "
                         "values (%s, %s, %s, 'running')", (60 + n, group, str(9000 + n)))
            conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_table, "
                         "status, error) values (%s, %s, %s, 't1', 'running', null), (%s, %s, %s, 't2', 'retried', "
                         "'timed out')", (60 + n, group, 10 * n, 60 + n, group, 10 * n + 1))
        yield conn
    finally:
        conn.close()
        admin.execute(f"drop database {name} with (force)")
        admin.close()


def test_cancel_runs_close_out_closes_eight_loading_group_runs_and_their_events(pg):
    reason = "stopped from the app by op@example.com"
    detail = json.dumps({"closed_by_app": "op@example.com", "reason": reason})
    with pg.transaction():
        closed = [pg.execute(q.close_ended_run_sql(), (detail, 60 + n, reason, reason)).fetchone()
                  for n in range(1, len(GROUPS) + 1)]
    assert closed == [(1, 2)] * len(GROUPS)
    assert pg.execute("select count(*) from lakefed_ingest.ingest_run where status = 'running'").fetchone()[0] == 0
    errors = dict(pg.execute("select src_table, error from lakefed_ingest.ingest_event where run_id = 61").fetchall())
    assert errors == {"t1": reason, "t2": f"timed out | {reason}"}


def test_the_reconcile_closes_a_run_databricks_reports_cancelled_and_leaves_a_closed_one(pg):
    detail = json.dumps({"closed_by_app": "reconcile", "databricks_result": "CANCELED"})
    reason = "its Databricks run ended CANCELED without finishing"
    first = pg.execute(q.close_ended_run_sql(), (detail, 61, reason, reason)).fetchone()
    again = pg.execute(q.close_ended_run_sql(), (detail, 61, reason, reason)).fetchone()
    assert first == (1, 2) and again == (0, 0)
    assert json.loads(pg.execute("select detail from lakefed_ingest.ingest_run where run_id = 61").fetchone()[0]) == {
        "closed_by_app": "reconcile", "databricks_result": "CANCELED"}
