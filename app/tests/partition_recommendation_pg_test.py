"""queries.source_rows_sql gives a discovered table the partitioning Bulk import's review recommends, against a real
Postgres: at least DEFAULT_PARTITION_THRESHOLD_MB with a partition candidate is partitioned on it; anything else gets
no partition fields. Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
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
    name = f"lakefed_app_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    conn = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=name), autocommit=True)
    try:
        for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
            conn.execute(statement)
        conn.execute("insert into lakefed_ingest.discovery (discovery_id, src_type, src_connection, src_database, status, "
                     "finished_at) values ('d1', 'sqlserver', 'sql_a', 'sales', 'ok', now())")
        big, small = q.DEFAULT_PARTITION_THRESHOLD_MB, q.DEFAULT_PARTITION_THRESHOLD_MB - 1
        for table, size, candidate in (("big", big, "order_id"), ("big_no_column", big * 3, None),
                                       ("small", small, "id"), ("unknown_size", None, "id")):
            conn.execute("insert into lakefed_ingest.discovered_table (discovery_id, src_database, src_schema, src_table, "
                         "size_mb, partition_candidate, select_list, primary_key, load_type) "
                         "values ('d1', 'sales', 'dbo', %s, %s, %s, 'id', 'id', 'full')", (table, size, candidate))
        yield conn
    finally:
        conn.close()
        admin.execute(f"drop database {name}")
        admin.close()


def test_a_large_discovered_table_with_a_partition_column_carries_its_partitioning(pg):
    from psycopg.rows import dict_row
    with pg.cursor(row_factory=dict_row) as cur:
        cur.execute(q.source_rows_sql("discovery"), (["d1"],))
        found = {r["src_table"]: r["fields"] for r in cur.fetchall()}
    partitioning = {t: (f.get("load_partitioned"), f.get("partition_col"), f.get("partition_size_mb")) for t, f in found.items()}
    assert partitioning == {"big": ("true", "order_id", str(q.DEFAULT_PARTITION_SIZE_MB)),
                            "big_no_column": (None, None, None), "small": (None, None, None),
                            "unknown_size": (None, None, None)}
