"""Per-database views against a real Postgres, on a collection whose two servers each have a database
named Sales: a database is its server and its name, never its name alone. Also: a table's rows in first
loads are its latest first load's, so a reseed does not add to them.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
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
        _fixture(conn)
        _batched_changes(conn)
        _batched_full_reloads(conn)
        yield conn
    finally:
        conn.close()
        admin.execute(f"drop database {name}")
        admin.close()


# A server is the one a row reaches (queries.SERVER): its host and port here. The rows' instance-group labels
# (srvX, srvY, srvZ) do not decide it.
X, Y, Z = "host-x:1433", "host-y:1433", "host-z:1433"


def _fixture(conn):
    # Two servers, each with a database called Sales of three tables, in one collection.
    for server, host in (("srvX", "host-x"), ("srvY", "host-y")):
        conn.execute(
            "insert into lakefed_ingest.control (task_collection, src_host, src_database, src_schema, src_table, "
            "sink_catalog, sink_schema, sink_table, primary_key, select_list, source_instance_group, created_at) "
            "select 'c2', %s, 'Sales', 'dbo', 't' || g, 'm', %s, 't' || g, 'id', 'id', %s, now() - interval '3 days' "
            "from generate_series(1, 3) g", (host, server, server))
    conn.execute("insert into lakefed_ingest.checkpoint (control_id, src_database, src_schema, src_table, phase, status, "
                 "last_success_at) select id, src_database, src_schema, src_table, 'incremental', 'ok', "
                 "now() - interval '10 minutes' from lakefed_ingest.control")
    # Run 10 seeded everything (100 rows a table on srvX, 200 on srvY); run 12 reseeded srvX (150 a table);
    # run 13 is going now and has loaded one srvY table with 5 changed rows.
    conn.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, started_at, finished_at, status) values "
                 "(10, 'c2', now() - interval '2 days', now() - interval '2 days', 'completed'), "
                 "(12, 'c2', now() - interval '1 day', now() - interval '1 day', 'completed'), "
                 "(13, 'c2', now() - interval '5 minutes', null, 'running')")
    insert = ("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_database, src_schema, "
              "src_table, action, status, started_at, finished_at, rows_merged) "
              "select %s, 'c2', id, src_database, src_schema, src_table, %s, 'ok', %s::timestamptz, %s::timestamptz, %s "
              "from lakefed_ingest.control where source_instance_group = any(%s)")
    two_days, one_day = "now() - interval '2 days'", "now() - interval '1 day'"
    for run, action, at, rows_each, servers in ((10, "seed", two_days, 100, ["srvX"]), (10, "seed", two_days, 200, ["srvY"]),
                                                (12, "reseed", one_day, 150, ["srvX"])):
        when = conn.execute(f"select {at}").fetchone()[0]
        conn.execute(insert, (run, action, when, when, rows_each, servers))
    conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_database, src_schema, "
                 "src_table, action, status, started_at, finished_at, rows_merged) "
                 "select 13, 'c2', id, src_database, src_schema, src_table, 'increment', 'ok', now() - interval '2 minutes', "
                 "now() - interval '1 minute', 5 from lakefed_ingest.control "
                 "where source_instance_group = 'srvY' and src_table = 't1'")


def _batched_changes(conn):
    # Run 14 appended srvX's changes in a batch: one total on the run, no count on its events.
    conn.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, started_at, finished_at, status, detail) "
                 "values (14, 'c2', now() - interval '1 hour', now() - interval '1 hour', 'completed', "
                 "'{\"batch_rows\": 999, \"batch_rows_unknown\": 0}')")
    conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_database, src_schema, "
                 "src_table, action, status, started_at, finished_at, rows_merged) "
                 "select 14, 'c2', id, src_database, src_schema, src_table, 'increment', 'ok', now() - interval '1 hour', "
                 "now() - interval '1 hour', null from lakefed_ingest.control where source_instance_group = 'srvX'")


def _batched_full_reloads(conn):
    # Collection c3: one table reloaded in full by a batched append twice (200 rows, then 240), and a
    # table that failed first on a connection and then on a missing column.
    conn.execute("insert into lakefed_ingest.control (task_collection, src_host, src_database, src_schema, src_table, "
                 "sink_catalog, sink_schema, sink_table, primary_key, select_list, source_instance_group, load_type) "
                 "values ('c3', 'host-z', 'Shop', 'dbo', 'prices', 'm', 'z', 'prices', 'id', 'id', 'srvZ', 'full'), "
                 "('c3', 'host-z', 'Shop', 'dbo', 'orders', 'm', 'z', 'orders', 'id', 'id', 'srvZ', 'ct')")
    for run, rows_, ago in ((20, 200, "3 hours"), (21, 240, "2 hours")):
        conn.execute(f"insert into lakefed_ingest.ingest_run (run_id, task_collection, started_at, finished_at, status, "
                     f"detail) values ({run}, 'c3', now() - interval '{ago}', now() - interval '{ago}', 'completed', "
                     f"'{{\"batch_rows\": {rows_}, \"batch_rows_unknown\": 0}}')")
        conn.execute(f"insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_database, "
                     f"src_schema, src_table, action, status, started_at, finished_at, rows_merged) select {run}, 'c3', id, "
                     f"src_database, src_schema, src_table, 'full', 'ok', now() - interval '{ago}', now() - interval '{ago}', "
                     f"null from lakefed_ingest.control where task_collection = 'c3' and src_table = 'prices'")
    for run, error, ago in ((20, "connection reset", "3 hours"), (21, "Invalid column name 'qty'.", "2 hours")):
        conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_database, "
                     "src_schema, src_table, action, status, started_at, finished_at, error) select %s, 'c3', id, "
                     "src_database, src_schema, src_table, 'increment', 'failed', now() - %s::interval, "
                     "now() - %s::interval, %s from lakefed_ingest.control where task_collection = 'c3' "
                     "and src_table = 'orders'", (run, ago, ago, error))


def rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def test_two_servers_databases_of_the_same_name_are_two_databases(pg):
    c = {r["task_collection"]: r for r in rows(pg, q.collections_overview_sql())}["c2"]
    assert (c["databases"], c["servers"]) == (2, 2)
    dbs = rows(pg, q.collection_databases_sql(), ("c2", "c2"))
    assert sorted((d["server"], d["src_database"], d["tables"]) for d in dbs) == [(X, "Sales", 3), (Y, "Sales", 3)]
    written = {(d["server"], d["src_database"]): d for d in rows(pg, q.collection_database_rows_sql(), ("c2",))}
    assert set(written) == {(X, "Sales"), (Y, "Sales")}
    assert written[(Y, "Sales")]["loaded_this_run"] == 1 and written[(X, "Sales")]["loaded_this_run"] == 0
    by_run = {(d["server"], d["src_database"]): d for d in rows(pg, q.run_databases_sql(), (10,))}
    assert {k: v["rows_written"] for k, v in by_run.items()} == {(X, "Sales"): 300, (Y, "Sales"): 600}


def test_rows_in_first_loads_are_each_tables_latest_first_load(pg):
    written = {(d["server"], d["src_database"]): d for d in rows(pg, q.collection_database_rows_sql(), ("c2",))}
    assert (written[(X, "Sales")]["rows_first_loads"], written[(X, "Sales")]["first_loads"]) == (450, 3)
    assert (written[(Y, "Sales")]["rows_first_loads"], written[(Y, "Sales")]["first_loads"]) == (600, 3)


def test_a_batch_of_changes_is_not_placed_under_rows_in_first_loads(pg):
    assert [b["run_id"] for b in rows(pg, q.collection_batch_runs_sql(), ("c2", "c2"))] == []


def test_a_table_reloaded_in_batches_twice_counts_only_its_latest_batch(pg):
    assert [b["run_id"] for b in rows(pg, q.collection_batch_runs_sql(), ("c3", "c3"))] == [21]


def test_failed_tables_give_each_cause_of_a_table_and_its_server(pg):
    found = [r for r in rows(pg, q.failed_tables_sql(), (24, 10)) if r["task_collection"] == "c3"]
    assert sorted((r["src_table"], r["server"], r["error"]) for r in found) == [
        ("orders", Z, "Invalid column name 'qty'."), ("orders", Z, "connection reset")]


def test_a_table_registered_at_one_port_is_not_registered_at_another(pg):
    with pg.transaction(force_rollback=True):
        for discovery, port in (("d1", None), ("d2", 1500)):
            pg.execute("insert into lakefed_ingest.discovery (discovery_id, src_type, src_host, src_port, src_database, "
                       "status, finished_at) values (%s, 'sqlserver', 'host-x', %s, 'Sales', 'ok', now())", (discovery, port))
            pg.execute("insert into lakefed_ingest.discovered_table (discovery_id, src_database, src_schema, src_table) "
                       "values (%s, 'Sales', 'dbo', 't1')", (discovery,))
        found = {r["endpoint"]: r for r in rows(pg, q.not_ingested_sql())}
    assert (found[X]["not_registered"], found["host-x:1500"]["not_registered"]) == (0, 1)
