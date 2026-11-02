"""The monitoring SQL run against a real Postgres: a throwaway database with the Lakebase schema and a
small fixture (2 databases on one server, a running run with unchanged, loaded, in-flight and failed
tables, a first load with per-table counts and one batched by the run).

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (a libpq URI to any database there) where
the test may create and drop its own database, for example a local pgserver."""
import json
import os
from datetime import datetime, timedelta, timezone
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
        schema = (REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()
        for statement in state_store.split_sql_statements(schema):
            conn.execute(statement)
        _fixture(conn)
        yield conn
    finally:
        conn.close()
        admin.execute(f"drop database {name}")
        admin.close()


def _fixture(conn):
    for db in ("db1", "db2"):
        conn.execute(
            "insert into lakefed_ingest.control (task_collection, src_host, src_database, src_schema, src_table, "
            "sink_catalog, sink_schema, sink_table, client_id, primary_key, select_list, source_instance_group, "
            "freshness_sla_minutes, write_mode, created_at) "
            "select 'c1', 'Host-A', %s, 'dbo', 't' || g, 'm', 'l', 't' || g, %s, 'id', 'id', 'srv1', 60, 'append', "
            "now() - interval '3 days' from generate_series(1, 10) g", (db, db))
    ids = {(d, t): i for i, d, t in conn.execute("select id, src_database, src_table from lakefed_ingest.control")}
    # db1: every table loaded; db2: t9 failing, t10 never loaded, t8 late (last success 2 h ago, SLA 60 min).
    conn.execute("insert into lakefed_ingest.checkpoint (control_id, src_database, src_schema, src_table, phase, "
                 "status, last_success_at) select id, src_database, src_schema, src_table, 'incremental', "
                 "case when src_database = 'db2' and src_table = 't9' then 'failed' else 'ok' end, "
                 "case when src_database = 'db2' and src_table = 't8' then now() - interval '2 hours' "
                 "else now() - interval '30 minutes' end "
                 "from lakefed_ingest.control where not (src_database = 'db2' and src_table = 't10')")
    # First load 3 days ago: db1 counted per table (10 x 100 rows), db2 batched (run total 500).
    conn.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, started_at, finished_at, status, "
                 "total, ok, failed, skipped, detail) values (1, 'c1', now() - interval '3 days', "
                 "now() - interval '3 days', 'completed', 20, 20, 0, 0, %s)",
                 (json.dumps({"batch_rows": 500, "batch_rows_unknown": 0}),))
    conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_database, "
                 "src_schema, src_table, action, status, started_at, finished_at, rows_merged, pool) "
                 "select 1, 'c1', id, src_database, src_schema, src_table, 'seed', 'ok', now() - interval '3 days', "
                 "now() - interval '3 days', case when src_database = 'db1' then 100 end, "
                 "case when src_database = 'db2' then 'batch9' end "
                 "from lakefed_ingest.control where src_table <> 't10' or src_database = 'db1'")
    # A run going now: started 5 minutes ago; db1 t1..t6 unchanged (stamped at planning), t7 loaded with 7
    # rows, t8 in flight; db2 t9 failed.
    conn.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, started_at, status) "
                 "values (2, 'c1', now() - interval '5 minutes', 'running')")
    conn.execute("update lakefed_ingest.checkpoint set last_success_at = now() - interval '4 minutes' "
                 "where src_database = 'db1' and src_table in ('t1','t2','t3','t4','t5','t6','t7')")
    for table, status, rows in (("t7", "ok", 7), ("t8", "running", None)):
        conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_database, "
                     "src_schema, src_table, action, status, started_at, finished_at, rows_merged) "
                     "values (2, 'c1', %s, 'db1', 'dbo', %s, 'increment', %s, now() - interval '2 minutes', "
                     "case when %s = 'ok' then now() - interval '1 minute' end, %s)",
                     (ids[("db1", table)], table, status, status, rows))
    conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_database, "
                 "src_schema, src_table, action, status, started_at, finished_at, error) "
                 "values (2, 'c1', %s, 'db2', 'dbo', 't9', 'increment', 'failed', "
                 "now() - interval '3 minutes', now() - interval '3 minutes', "
                 "'[FAILED_JDBC.CONNECTION] connection reset')", (ids[("db2", "t9")],))


# The server the fixture's rows reach: their host and SQL Server's default port, whatever their label (srv1).
A = "host-a:1433"


def rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def test_the_rows_written_in_a_window_count_its_table_loads_and_say_which_had_no_count(pg):
    day, = rows(pg, q.rows_written_since_sql(), (["c1"], 24))
    assert (day["rows_written"], day["uncounted"]) == (7, 0)       # the running run's t7; the failed t9 has no rows
    four_days, = rows(pg, q.rows_written_since_sql(), (["c1"], 96))
    # db2's first load was batched: its 9 loads are counted by the run's batch total, not uncounted.
    assert (four_days["rows_written"], four_days["uncounted"]) == (1007, 0)
    assert [r["run_id"] for r in rows(pg, q.run_batch_rows_sql(), (96,))] == [1]
    assert rows(pg, q.rows_written_since_sql(), (["other"], 96))[0]["rows_written"] == 0


def test_every_enabled_table_is_in_exactly_one_state(pg):
    c = rows(pg, q.collections_overview_sql())[0]
    states = (c["state_current"], c["state_late"], c["state_first_load"], c["state_failing"])
    assert states == (17, 1, 1, 1) and sum(states) == c["enabled_tables"] == 20
    assert (c["databases"], c["servers"], c["server"]) == (2, 1, A)
    assert c["rows_24h"] == 7


def test_running_run_progress_counts_tables_stamped_since_it_started(pg):
    r = rows(pg, q.running_runs_sql())[0]
    assert (r["run_id"], r["enabled_tables"], r["tables_checked"]) == (2, 20, 7)
    assert (r["tables_ok"], r["tables_running"], r["tables_failed"]) == (1, 1, 1)
    assert r["last_activity_at"] is not None and r["server"] == A


def test_a_work_list_runs_progress_is_over_its_planned_tables_and_counts_stamps(pg):
    # The run planned only db2 (10 tables); db2 was checked unchanged a minute ago at version 50, which
    # vouches for its tables at 50 (t1..t8; t9 and t10 have no position).
    with pg.transaction(force_rollback=True):   # the module's other tests share the fixture
        pg.execute("update lakefed_ingest.ingest_run set total = 10 where run_id = 2")
        pg.execute("update lakefed_ingest.checkpoint set strategy = 'ct', position = '50' "
                   "where src_database = 'db2' and src_table not in ('t9', 't10')")
        pg.execute("insert into lakefed_ingest.database_check (task_collection, source, src_database, checked_version, "
                   "tables, checked_at) values ('c1', %s, 'db2', 50, 10, now() - interval '1 minute')",
                   (q.database_source({"src_host": "Host-A", "src_port": None, "secret_key": None}),))
        r = rows(pg, q.running_runs_sql())[0]
        assert (r["enabled_tables"], r["tables_checked"]) == (10, 7 + 8)
        shared = rows(pg, q.running_progress_sql())[0]
        assert (shared["enabled_tables"], shared["tables_checked"]) == (10, 7 + 8)
        states = [s for s in rows(pg, q.database_states_sql()) if s["src_database"] == "db2"]
        assert states and all(s["newest_success_at"] is not None for s in states)
        db2 = {d["src_database"]: d for d in rows(pg, q.collection_databases_sql(), ("c1", "c1"))}["db2"]
        assert db2["checked_this_run"] == 8


def test_servers_roll_up_their_databases(pg):
    s = rows(pg, q.servers_sql())[0]
    assert (s["server"], s["databases"], s["tables"]) == (A, 2, 20)
    assert (s["collections"], s["running_collections"]) == (1, 1)
    assert (s["state_failing"], s["state_first_load"], s["state_late"], s["state_current"]) == (1, 1, 1, 17)


def test_a_collections_databases_show_this_runs_progress_and_their_rows(pg):
    dbs = {d["src_database"]: d for d in rows(pg, q.collection_databases_sql(), ("c1", "c1"))}
    assert (dbs["db1"]["tables"], dbs["db1"]["checked_this_run"], dbs["db1"]["state_current"]) == (10, 7, 10)
    assert (dbs["db2"]["checked_this_run"], dbs["db2"]["state_failing"], dbs["db2"]["state_first_load"]) == (0, 1, 1)
    written = {d["src_database"]: d for d in rows(pg, q.collection_database_rows_sql(), ("c1",))}
    assert (written["db1"]["rows_first_loads"], written["db1"]["first_loads_uncounted"]) == (1000, 0)
    db1 = written["db1"]
    assert (db1["rows_changes_24h"], db1["loaded_this_run"], db1["in_flight"]) == (7, 1, 1)
    assert dbs["db1"]["server"] == A
    assert (written["db2"]["rows_first_loads"], written["db2"]["first_loads_uncounted"]) == (None, 9)
    assert written["db2"]["failed_this_run"] == 1
    batch = rows(pg, q.collection_batch_runs_sql(), ("c1", "c1"))
    assert [(b["run_id"], b["databases"]) for b in batch] == [(1, ["db2"])]
    assert q.batch_rows(batch[0]["detail"]) == (500, 0)


def test_a_runs_databases_and_rows(pg):
    by_db = {d["src_database"]: d for d in rows(pg, q.run_databases_sql(), (2,))}
    assert (by_db["db1"]["loaded"], by_db["db1"]["in_flight"], by_db["db1"]["rows_written"]) == (1, 1, 7)
    assert by_db["db2"]["failed"] == 1
    per_run = {r["run_id"]: r for r in rows(pg, q.run_work_sql(), ([1, 2],))}
    assert (per_run[1]["rows_written"], per_run[1]["uncounted"]) == (1000, 0)
    assert {r["run_id"] for r in rows(pg, q.run_batch_rows_sql(), (24 * 7,))} == {1}


def test_connection_databases_match_by_name_or_host_case_insensitively(pg):
    found = rows(pg, q.connection_databases_sql(), ("no_such_connection", "host-a"))
    assert [(d["src_database"], d["tables"], d["collections"]) for d in found] == [("db1", 10, "c1"), ("db2", 10, "c1")]


def test_failed_tables_are_one_row_per_table_with_its_failures(pg):
    found = rows(pg, q.failed_tables_sql(), (24, 10))
    assert [(r["src_database"], r["src_table"], r["failures"]) for r in found] == [("db2", "t9", 1)]
    assert found[0]["error"].startswith("[FAILED_JDBC.CONNECTION]") and found[0]["first_at"] == found[0]["last_at"]
    assert rows(pg, q.failed_tables_sql(), (24, 0)) == []


def test_the_shared_per_database_pass_adds_up_to_the_overview_and_servers(pg):
    import rollup
    with pg.transaction():
        states = [rollup.from_scan(r) for r in rows(pg, q.database_states_sql())]
        old_groups = {r["task_collection"]: r for r in rows(pg, q.collections_overview_sql())}
        old_servers = rows(pg, q.servers_sql())
    # The rollup counts a late table's database as loaded with its table, so loaded is current plus late.
    new_groups = rollup.groups(states)
    assert set(new_groups) == set(old_groups)
    for name, old in old_groups.items():
        new = new_groups[name]
        assert new["state_loaded"] == old["state_current"] + old["state_late"], name
        for k in ("enabled_tables", "no_sla_tables", "databases", "servers", "server", "state_first_load",
                  "state_failing"):
            assert new[k] == old[k], (name, k)
    new_servers = rollup.servers(states)
    assert [s["server"] for s in new_servers] == [s["server"] for s in old_servers]
    for new, old in zip(new_servers, old_servers):
        assert new["state_loaded"] == old["state_current"] + old["state_late"], old["server"]
        for k in ("databases", "tables", "collections", "oldest_success_at", "newest_success_at", "state_first_load",
                  "state_failing"):
            assert new[k] == old[k], (old["server"], k)


def test_the_running_pass_counts_what_the_old_running_query_counts(pg):
    with pg.transaction():
        new = {r["run_id"]: r for r in rows(pg, q.running_progress_sql())}
        old = {r["run_id"]: r for r in rows(pg, q.running_runs_sql())}
    assert set(new) == set(old) and new
    for run_id, o in old.items():
        for k in ("enabled_tables", "tables_checked", "tables_ok", "tables_failed", "tables_running"):
            assert new[run_id][k] == o[k], (run_id, k)
        assert "p95_s" in new[run_id] and new[run_id]["ok_last_5m"] == 1


def test_the_in_flight_list_is_running_tables_longest_first(pg):
    found = rows(pg, q.in_flight_sql(), (2, 50))
    assert [(r["src_database"], r["src_table"]) for r in found] == [("db1", "t8")]
    assert found[0]["running_s"] >= 60 and found[0]["retry_count"] == 0
    assert rows(pg, q.in_flight_count_sql(), (2,))[0]["tables"] == 1
    assert rows(pg, q.in_flight_sql(), (1, 50)) == []


def test_a_connections_databases_from_the_shared_pass_equal_the_old_query_with_a_disabled_table(pg):
    import rollup
    with pg.transaction(force_rollback=True):
        pg.execute("update lakefed_ingest.control set task_enabled = false where src_database = 'db1' and src_table = 't1'")
        pg.execute("update lakefed_ingest.checkpoint set last_success_at = now() + interval '1 hour' "
                   "where src_database = 'db1' and src_table = 't1'")
        old = rows(pg, q.connection_databases_sql(), ("no_such_connection", "HOST-A"))
        new = rollup.connection_databases(rows(pg, q.database_states_sql()), "no_such_connection", "HOST-A")
    keys = ("src_database", "collections", "tables", "newest_success_at")
    assert [tuple(d[k] for k in keys) for d in new] == [tuple(d[k] for k in keys) for d in old]


def test_the_typical_run_leaves_out_runs_another_run_closed_and_reseeds_queued_are_counted(pg):
    with pg.transaction(force_rollback=True):
        pg.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, started_at, finished_at, status, detail) "
                   "values (90, 'c1', now() - interval '2 days', now(), 'failed', '{\"closed_by_run\": 2}')")
        pg.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_database, src_schema, "
                   "src_table, action, status, started_at, finished_at) select 2, 'c1', id, src_database, src_schema, "
                   "src_table, 'reseed', 'skipped', now() - interval '1 minute', now() - interval '1 minute' "
                   "from lakefed_ingest.control where src_database = 'db2' and src_table = 't10'")
        found = {r["run_id"]: r for r in rows(pg, q.running_progress_sql())}[2]
    assert found["p50_s"] == 0 and found["tables_skipped"] == 1


def test_the_check_jobs_own_records_are_not_group_runs(pg):
    # The check job records each check as a run of the group: one running now (marked by its work_id), and an
    # earlier finished one from before that mark, known by its detail. Neither is the group's run, its last run
    # or part of its typical run time.
    with pg.transaction(force_rollback=True):
        pg.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, started_at, status, work_id) "
                   "values (91, 'c1', now() - interval '1 minute', 'running', 'check-77')")
        pg.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, started_at, finished_at, status, "
                   "total, ok, failed, skipped, detail) values (92, 'c1', now() - interval '2 days', "
                   "now() - interval '2 days' + interval '9 seconds', 'completed', 20, 20, 0, 20, %s)",
                   (json.dumps({"check": {"work": [], "unchanged_tables": 20}}),))
        progress = rows(pg, q.running_progress_sql())
        last = {r["task_collection"]: r["run_id"] for r in rows(pg, q.last_runs_sql())}
        stats = rows(pg, q.collection_run_stats_sql(), (["c1"],) * 4)[0]
        first = rows(pg, q.first_started_sql(), ("check-77", "check-77:%"))
        assert first == rows(pg, "select task_collection, started_at from lakefed_ingest.ingest_run where run_id = 91")
    assert [r["run_id"] for r in progress] == [2] and progress[0]["p50_s"] == 0
    assert last == {"c1": 2} and stats["last_run_id"] == 2
    assert (stats["runs_24h"], stats["p50_s"]) == (1, 0)


def test_a_job_runs_databases_list_failing_first_and_keep_two_servers_apart(pg):
    with pg.transaction(force_rollback=True):
        pg.execute("update lakefed_ingest.control set src_host = 'Host-B' where src_database = 'db1' "
                   "and src_table = 't1'")
        pg.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_database, "
                   "src_schema, src_table, action, status, started_at, finished_at, rows_merged) select 1, 'c1', id, "
                   "src_database, src_schema, src_table, 'increment', 'ok', now(), now(), 3 "
                   "from lakefed_ingest.control where src_database = 'db1' and src_table = 't1'")
        found = rows(pg, q.job_run_databases_sql(), ([1, 2],))
        failures = rows(pg, q.run_failures_sql(), ([1, 2],))
    assert (found[0]["src_database"], found[0]["failed"]) == ("db2", 1)
    assert sorted((d["server"], d["task_collection"]) for d in found if d["src_database"] == "db1") == [(A, "c1"), ("host-b:1433", "c1")]
    assert [(f["src_table"], f["server"]) for f in failures] == [("t9", A)]


def test_group_runs_in_a_window_oldest_first(pg):
    found = rows(pg, q.group_runs_sql(), (["c1"], datetime.now(timezone.utc) - timedelta(days=4),
                                          datetime.now(timezone.utc)))
    assert [r["run_id"] for r in found] == [1, 2] and "cluster_id" in found[0]


def test_not_ingested_counts_discovered_tables_with_no_control_row_from_the_latest_discovery(pg):
    with pg.transaction(force_rollback=True):
        pg.execute("insert into lakefed_ingest.discovery (discovery_id, src_host, src_database, status, started_at, finished_at) "
                   "values ('d0', 'HOST-A', 'db1', 'ok', now() - interval '2 days', now() - interval '2 days'), "
                   "('d1', 'HOST-A', 'db1', 'ok', now() - interval '1 hour', now() - interval '1 hour')")
        pg.execute("insert into lakefed_ingest.discovered_table (discovery_id, src_database, src_schema, src_table, size_mb) "
                   "values ('d0', 'db1', 'dbo', 'only_in_the_old_one', 1), ('d1', 'db1', 'DBO', 'T1', 5), "
                   "('d1', 'db1', 'dbo', 'new_a', 7), ('d1', 'db1', 'dbo', 'new_b', 3)")
        found = rows(pg, q.not_ingested_sql())
    assert [(r["src_host"], r["src_database"], r["discovered"], r["not_registered"], r["largest_not_registered"])
            for r in found] == [("HOST-A", "db1", 3, 2, "dbo.new_a")]


def test_a_databases_tables_come_failing_first_with_their_part_in_the_running_run(pg):
    found = rows(pg, q.database_tables_sql(), ("c1", A, "db2", 2, "%", 100, 0))
    assert len(found) == 10 and (found[0]["src_table"], found[0]["run_status"]) == ("t9", "failed")
    assert rows(pg, q.database_tables_sql(), ("c1", A, "db1", 2, "%t8%", 100, 0))[0]["run_status"] == "running"
    assert [r["src_table"] for r in rows(pg, q.database_tables_sql(), ("c1", A, "db1", None, "%t1%", 100, 0))] == ["t1", "t10"]
    assert rows(pg, q.database_tables_count_sql(), ("c1", A, "db1", "%"))[0]["tables"] == 10


def test_tables_page_by_keyset_and_filter_one_database_exactly(pg):
    sql, params = q.tables_sql("c1", None, "all", 4, database="db2")
    first = rows(pg, sql, params)
    cursor = [first[-1][k] for k in ("task_collection", "src_database", "src_schema", "src_table", "id")]
    sql, params = q.tables_sql("c1", None, "all", 4, database="db2", after=cursor)
    second = rows(pg, sql, params)
    assert {r["src_database"] for r in first + second} == {"db2"} and len(first) == len(second) == 4
    assert not {r["id"] for r in first} & {r["id"] for r in second}
    assert [r["src_table"] for r in first + second] == sorted(r["src_table"] for r in first + second)


def test_a_jobs_last_ran_group_runs_are_each_members_newest_finished_one_while_a_member(pg):
    # Job 1's c1 ran 3 days ago (run 1) and is running now (run 2, not finished); a skipped run and a run from
    # before c1 joined the job are not its job's. One row per member: its newest.
    with pg.transaction(force_rollback=True):
        pg.execute("insert into lakefed_ingest.schedule (id, name, cadence_minutes) values (1, 'hourly', 60)")
        pg.execute("insert into lakefed_ingest.schedule_member (schedule_id, task_collection, added_at) "
                   "values (1, 'c1', now() - interval '4 days')")
        pg.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, started_at, status) values "
                   "(80, 'c1', now() - interval '5 days', 'completed'), (81, 'c1', now() - interval '1 day', 'skipped')")
        found = rows(pg, q.last_ran_group_runs_sql(), ([1],))
    assert [(r["run_id"], r["schedule_id"]) for r in found] == [(1, 1)]
    now = datetime.now(timezone.utc)
    starts, ends = [now - timedelta(days=4), now - timedelta(minutes=6)], [now - timedelta(days=2), now]
    windows = rows(pg, q.group_runs_in_windows_sql(), (["c1", "c1"], starts, ends))
    assert sorted(r["run_id"] for r in windows) == [1, 2]


def test_two_labels_on_one_connection_are_one_server_and_the_rows_keep_their_labels(pg):
    # db1 read through the UC connection Conn_A, three of its tables labelled lab-b: one server, conn_a, with one
    # database; the labels come with the rows so a page can say where they differ.
    with pg.transaction(force_rollback=True):
        pg.execute("update lakefed_ingest.control set src_connection = 'Conn_A', use_remote_query = true, src_host = null "
                   "where src_database = 'db1'")
        pg.execute("update lakefed_ingest.control set source_instance_group = 'lab-b' where src_database = 'db1' "
                   "and src_table in ('t1', 't2', 't3')")
        states = [s for s in rows(pg, q.database_states_sql()) if s["src_database"] == "db1"]
        servers = {s["server"]: s for s in rows(pg, q.servers_sql())}
    assert {s["server"] for s in states} == {"conn_a"}
    assert sorted((s["instance_label"], s["tables"]) for s in states) == [("lab-b", 3), ("srv1", 7)]
    assert servers["conn_a"]["databases"] == 1 and servers[A]["databases"] == 1


def test_a_port_of_zero_is_no_port_as_the_engine_reads_it(pg):
    with pg.transaction(force_rollback=True):
        pg.execute("update lakefed_ingest.control set src_port = 0 where src_database = 'db1' and src_table = 't1'")
        servers = {s["server"] for s in rows(pg, q.database_states_sql())}
    assert servers == {A}


def test_windows_that_overlap_read_a_run_once(pg):
    now = datetime.now(timezone.utc)
    twice = rows(pg, q.group_runs_in_windows_sql(), (["c1", "c1"], [now - timedelta(days=4)] * 2, [now] * 2))
    assert sorted(r["run_id"] for r in twice) == [1, 2]
