"""lakebase_comments.sql describes every table and column of lakebase_schema.sql, against a real Postgres: applied by
StateStore.apply_comments after the schema, every base table and every column has a description, and applying it
again changes nothing. A Lakebase database registered in Unity Catalog shows these as the tables' and columns'
comments, for Genie (verified on a Lakebase catalog, 2026-11-10).

Skipped unless LAKEFED_TEST_PG names a Postgres server (see state_store_pg_test.py)."""
import os
import uuid
from pathlib import Path

import pytest
import state_store as ss

URI = os.environ.get("LAKEFED_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_TEST_PG not set")
HERE = Path(__file__).resolve().parents[1] / "src" / "lakefed_ingest"


@pytest.fixture
def conn():
    psycopg = pytest.importorskip("psycopg")
    name = f"lakefed_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    db = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=name), autocommit=True)
    try:
        for statement in ss.split_sql_statements((HERE / "lakebase_schema.sql").read_text()):
            db.execute(statement)
        yield db
    finally:
        db.close()
        admin.execute(f"drop database {name}")
        admin.close()


UNDESCRIBED = """
    select c.relname as table_name, a.attname as column_name
    from pg_class c join pg_namespace n on n.oid = c.relnamespace
    left join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
    where n.nspname = 'lakefed_ingest' and c.relkind = 'r'
      and coalesce(col_description(c.oid, a.attnum), '') = ''
    union all
    select c.relname, null from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'lakefed_ingest' and c.relkind = 'r' and coalesce(obj_description(c.oid, 'pg_class'), '') = ''
    order by 1, 2"""


def test_every_table_and_column_has_a_description(conn):
    store = ss.StateStore(conn)
    applied = store.apply_comments((HERE / "lakebase_comments.sql").read_text())
    tables, columns = conn.execute(
        "select count(distinct c.oid), count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped "
        "where n.nspname = 'lakefed_ingest' and c.relkind = 'r'").fetchone()
    assert conn.execute(UNDESCRIBED).fetchall() == []
    assert applied == tables + columns                      # one each, nothing for an object the schema lacks
    assert store.apply_comments((HERE / "lakebase_comments.sql").read_text()) == applied   # re-applying is safe


def test_a_comment_on_a_column_the_schema_lacks_fails_and_applies_nothing(conn):
    store = ss.StateStore(conn)
    sql = ("comment on table lakefed_ingest.control is 'first';\n"
           "comment on column lakefed_ingest.control.no_such_column is 'second';\n")
    with pytest.raises(Exception, match="no_such_column"):
        store.apply_comments(sql)
    assert conn.execute("select obj_description('lakefed_ingest.control'::regclass, 'pg_class')").fetchone()[0] is None
