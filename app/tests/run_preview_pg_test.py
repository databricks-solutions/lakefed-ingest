"""What Run now's confirmation reads (queries.run_preview_sql): a job's enabled tables, how many servers and databases
they are on, and the first few names in name order, against a real Postgres.

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


@pytest.fixture
def pg():
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_app_preview_{uuid.uuid4().hex[:8]}"
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


def test_one_database_gives_its_server_and_database_and_the_first_names_in_name_order(pg):
    for table in ("zebra", "Apple", "mango", "banana"):
        _add(pg, "sales", "host-a", "db1", table)
    row = _read(pg, ["sales"])
    assert row["tables"] == 4 and row["databases"] == 1 and row["servers"] == 1
    assert row["database"] == "db1" and "host-a" in row["server"]
    assert row["names"] == ["dbo.Apple", "dbo.banana", "dbo.mango"]


def test_several_databases_and_servers_are_counted_without_naming_them(pg):
    _add(pg, "sales", "host-a", "db1", "orders")
    _add(pg, "sales", "host-a", "db2", "orders")
    _add(pg, "sales", "host-b", "db1", "orders")
    row = _read(pg, ["sales"])
    assert (row["tables"], row["databases"], row["servers"]) == (3, 3, 2)


def test_disabled_tables_and_other_groups_are_not_counted(pg):
    _add(pg, "sales", "host-a", "db1", "orders")
    _add(pg, "sales", "host-a", "db1", "parked", enabled=False)
    _add(pg, "marketing", "host-a", "db1", "leads")
    row = _read(pg, ["sales"])
    assert row["tables"] == 1 and row["names"] == ["dbo.orders"]


def test_a_job_with_nothing_enabled_reads_as_zero_tables_and_no_names(pg):
    _add(pg, "sales", "host-a", "db1", "parked", enabled=False)
    row = _read(pg, ["sales"])
    assert row["tables"] == 0 and row["names"] == []


def _read(conn, groups):
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(q.run_preview_sql(), (groups, 3))
        return cur.fetchone()


def _add(conn, collection, host, database, table, enabled=True):
    conn.execute("insert into lakefed_ingest.control (task_collection, src_host, src_port, secret_key, src_database, "
                 "src_schema, src_table, sink_catalog, sink_schema, sink_table, primary_key, select_list, task_enabled) "
                 "values (%s, %s, 1433, 'k1', %s, 'dbo', %s, 'm', 'l', %s, 'id', 'id', %s)",
                 (collection, host, database, table, f"{collection}_{host}_{database}_{table}".lower(), enabled))
