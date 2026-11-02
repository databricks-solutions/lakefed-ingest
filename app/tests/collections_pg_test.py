"""A database's tables share one collection: the app's check of that, against a real Postgres.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server where the test may create and drop its own
database."""
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
    name = f"lakefed_app_split_{uuid.uuid4().hex[:8]}"
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


def _add(conn, collection, database, table, host="Host-A"):
    conn.execute("insert into lakefed_ingest.control (task_collection, src_host, src_port, secret_key, src_database, "
                 "src_schema, src_table, sink_catalog, sink_schema, sink_table, primary_key, select_list) values "
                 "(%s, %s, 1433, 'k1', %s, 'dbo', %s, 'm', 'l', %s, 'id', 'id')",
                 (collection, host, database, table, f"{host}_{database}_{table}".lower().replace("-", "_")))
    return {"src_host": host, "src_port": 1433, "secret_key": "k1", "src_database": database}


def test_only_a_database_in_two_collections_is_reported(pg):
    one = _add(pg, "c1", "db1", "a")
    _add(pg, "c1", "db1", "b")
    two = _add(pg, "c1", "db2", "a")
    _add(pg, "c2", "db2", "b")
    same_name_elsewhere = _add(pg, "c3", "db1", "a", host="Host-B")    # another server's db1
    keys = [q.database_key(r) for r in (one, two, same_name_elsewhere)]
    rows = pg.execute(q.split_databases_sql(), (keys,)).fetchall()
    assert [(r[1], sorted(r[2])) for r in rows] == [("db2", ["c1", "c2"])]
