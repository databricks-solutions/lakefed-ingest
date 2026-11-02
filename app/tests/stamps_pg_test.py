"""A table's last success in the app is its own, or its database's check stamp when that stamp vouches
for it (planner.stamp_vouches in the engine): run against a real Postgres.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (a libpq URI to any database there) where
the test may create and drop its own database."""
import os
import sys
import uuid
from pathlib import Path

import pytest
import queries as q

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def pg():
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_app_stamps_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    conn = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=name), autocommit=True)
    try:
        for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
            conn.execute(statement)
        # One database, three ct tables, SLA 60 min, each table's own success 2 hours ago (late on its own).
        # The database was checked 5 minutes ago at version 100: a, at 100, is vouched for; b, at 90, changed
        # since and is behind; c is re-seeding.
        conn.execute("insert into lakefed_ingest.control (task_collection, src_host, src_port, secret_key, src_database, "
                     "src_schema, src_table, sink_catalog, sink_schema, sink_table, primary_key, select_list, "
                     "freshness_sla_minutes) select 'c1', 'Host-A', 1433, 'k1', 'db1', 'dbo', x, 'm', 'l', x, 'id', 'id', 60 "
                     "from unnest(array['a', 'b', 'c']) x")
        conn.execute("insert into lakefed_ingest.checkpoint (control_id, src_database, src_schema, src_table, strategy, "
                     "position, phase, status, last_success_at) select id, src_database, src_schema, src_table, 'ct', "
                     "case src_table when 'a' then '100' when 'b' then '90' else '100' end, "
                     "case src_table when 'c' then 'seeding' else 'incremental' end, 'ok', now() - interval '2 hours' "
                     "from lakefed_ingest.control")
        source = state_store.database_source({"src_host": "Host-A", "src_port": 1433, "secret_key": "k1"})
        conn.execute("insert into lakefed_ingest.database_check (task_collection, source, src_database, "
                     "checked_version, tables, checked_at) values ('c1', %s, 'db1', 100, 3, now() - interval '5 minutes')",
                     (source,))
        yield conn
    finally:
        conn.close()
        admin.execute(f"drop database {name}")
        admin.close()


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    names = [d.name for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def test_a_vouching_stamp_is_the_tables_last_success(pg):
    sql, params = q.tables_sql("c1", None, "all", 100)
    by = {r["src_table"]: r for r in _rows(pg, sql, params)}
    assert by["a"]["lag_min"] == 5 and by["a"]["sla_breached"] is False
    assert by["b"]["lag_min"] == 120 and by["b"]["sla_breached"] is True      # changed after the check
    assert by["c"]["lag_min"] == 120 and by["c"]["sla_breached"] is True      # re-seeding: not vouched for


def test_the_overview_counts_late_tables_with_the_stamps(pg):
    row, = _rows(pg, q.collections_overview_sql())
    assert row["sla_breached_tables"] == 2
    sql, params = q.tables_sql("c1", None, "sla_breached", 100)
    assert sorted(r["src_table"] for r in _rows(pg, sql, params)) == ["b", "c"]


def test_the_edit_pages_last_success_counts_the_stamp(pg):
    ids = dict(pg.execute("select src_table, id from lakefed_ingest.control").fetchall())
    stamped, = _rows(pg, q.checkpoint_for_sql(), (ids["a"],))
    behind, = _rows(pg, q.checkpoint_for_sql(), (ids["b"],))
    assert stamped["last_success_at"] > behind["last_success_at"]
