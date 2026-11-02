"""A job's failing tables, read from its checkpoints and failure events, and the work list a Retry now records, against a
real Postgres.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server where the test may create and drop its own database."""
import os
import sys
import uuid
from pathlib import Path

import pytest
import queries as q

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]
SERVER = "host-a:1433"


@pytest.fixture
def pg():
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_app_failing_{uuid.uuid4().hex[:8]}"
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


def test_a_table_is_failing_since_its_first_failure_after_its_last_success_and_its_runs_are_counted(pg):
    table = _add(pg, "sales", "db1", "orders")
    _checkpoint(pg, table, "failed", "old note")
    _event(pg, table, "failed", 5, run_id=100, error="stale")
    _event(pg, table, "ok", 4, run_id=101)
    _event(pg, table, "failed", 2, run_id=102, error="Login failed", kind="transient", cls="FAILED_JDBC.CONNECTION")
    _event(pg, table, "failed", 1, run_id=103, error="Login failed again", kind="transient", cls="FAILED_JDBC.CONNECTION")
    [row] = q.failing_table_rows(_rows(pg, q.failing_tables_sql(), (["sales"],)))
    assert row["table"] == "dbo.orders"
    assert row["message"] == "Login failed again"
    assert (row["error_class"], row["error_kind"]) == ("FAILED_JDBC.CONNECTION", "transient")
    assert (row["runs"], row["failures"]) == (2, 2)
    assert row["failing_since"] is not None


def test_a_table_whose_checkpoint_is_not_failed_is_not_listed(pg):
    table = _add(pg, "sales", "db1", "orders")
    _checkpoint(pg, table, "ok")
    _event(pg, table, "failed", 1, run_id=102, error="earlier")
    assert _rows(pg, q.failing_tables_sql(), (["sales"],)) == []


def test_a_disabled_failing_table_is_not_listed(pg):
    table = _add(pg, "sales", "db1", "orders", enabled=False)
    _checkpoint(pg, table, "failed")
    assert _rows(pg, q.failing_tables_sql(), (["sales"],)) == []


def test_only_the_groups_asked_for_are_listed(pg):
    mine = _add(pg, "sales", "db1", "orders")
    other = _add(pg, "marketing", "db1", "leads")
    _checkpoint(pg, mine, "failed")
    _checkpoint(pg, other, "failed")
    assert [r["task_collection"] for r in _rows(pg, q.failing_tables_sql(), (["sales"],))] == ["sales"]


def test_retry_now_records_the_enabled_tables_of_that_database_only(pg):
    import state_store
    one = _add(pg, "sales", "db1", "orders")
    _add(pg, "sales", "db1", "items")
    _add(pg, "sales", "db2", "orders")
    _add(pg, "sales", "db1", "parked", enabled=False)
    assert _rows(pg, q.retry_database_sql(), ("retry-1:sales", "sales", SERVER, "db1")) == [{"src_database": "db1"}]
    stored = _rows(pg, "select task_collection, source, src_database from lakefed_ingest.check_work "
                       "where work_id = 'retry-1:sales'", ())
    assert stored == [{"task_collection": "sales", "source": state_store.database_source(one), "src_database": "db1"}]


def test_retry_now_records_nothing_for_a_database_with_no_enabled_tables(pg):
    _add(pg, "sales", "db1", "parked", enabled=False)
    assert _rows(pg, q.retry_database_sql(), ("retry-2:sales", "sales", SERVER, "db1")) == []


def test_the_failing_tables_read_can_use_the_control_and_started_at_index(pg):
    _add(pg, "sales", "db1", "orders")
    plan = _plan(pg, q.failing_tables_sql(), (["sales"],))
    assert any("ingest_event_control_started_idx" in line for line in plan), "\n".join(plan)


def _plan(conn, sql, parameter_values):
    with conn.transaction():
        conn.execute("set local enable_seqscan = off")
        return [row[0] for row in conn.execute(f"explain {sql}", parameter_values).fetchall()]


def _rows(conn, sql, parameter_values):
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, parameter_values)
        return cur.fetchall() if cur.description else []


def _add(conn, collection, database, table, enabled=True):
    host = "Host-A"
    conn.execute("insert into lakefed_ingest.control (task_collection, src_host, src_port, secret_key, src_database, "
                 "src_schema, src_table, sink_catalog, sink_schema, sink_table, primary_key, select_list, task_enabled) "
                 "values (%s, %s, 1433, 'k1', %s, 'dbo', %s, 'm', 'l', %s, 'id', 'id', %s)",
                 (collection, host, database, table, f"{collection}_{database}_{table}".lower(), enabled))
    row = conn.execute("select id from lakefed_ingest.control where task_collection = %s and src_database = %s "
                       "and src_table = %s", (collection, database, table)).fetchone()
    return {"id": row[0], "task_collection": collection, "src_host": host, "src_port": 1433, "secret_key": "k1",
            "src_database": database, "src_table": table, "src_type": None, "src_connection": None}


def _checkpoint(conn, table, status, detail=None):
    conn.execute("insert into lakefed_ingest.checkpoint (control_id, src_database, src_schema, src_table, status, detail) "
                 "values (%s, %s, 'dbo', %s, %s, %s)",
                 (table["id"], table["src_database"], table["src_table"], status, detail))


def _event(conn, table, status, days_ago, run_id, error=None, kind=None, cls=None):
    conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_database, src_schema, "
                 "src_table, status, started_at, finished_at, error, error_kind, error_class) values (%s, %s, %s, %s, "
                 "'dbo', %s, %s, now() - make_interval(days => %s::int), now() - make_interval(days => %s::int), %s, %s, %s)",
                 (run_id, table["task_collection"], table["id"], table["src_database"], table["src_table"], status,
                  days_ago, days_ago, error, kind, cls))
