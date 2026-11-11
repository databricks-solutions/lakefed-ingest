"""One source database whose tables sit in two ingestion groups, against a real Postgres with the Lakebase schema: the
state the engine keeps for it is kept per group. Each group reads only its own rows, gets its own check stamp and work
list, moves only its own checkpoints, locks the database under its own key (so the two groups' runs go at once), and
counts only its own tables in the monitoring rollups.

split_database_test.py runs the same split through the check and the ingest planning; this file proves the Lakebase
side of it. Skipped unless LAKEFED_TEST_PG names a Postgres server (see state_store_pg_test.py)."""
import os

import pytest
import state_store as ss
from state_store_pg_test import sessions  # noqa: F401  (the throwaway database fixture)

URI = os.environ.get("LAKEFED_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_TEST_PG not set")

# sales: t1 and t2 in g1, t3 and t4 in g2. state_store.database_source of these rows (SQL Server's default port).
TABLES = {"t1": "g1", "t2": "g1", "t3": "g2", "t4": "g2"}
SOURCE = "sql1|1433|k1"


def _split_database(conn, positions):
    """The four control rows and their checkpoints, incremental at ``positions`` ({table: CT version}). Returns
    {table: control id}."""
    ids = {}
    for table, group in TABLES.items():
        ids[table] = conn.execute(
            "insert into lakefed_ingest.control (task_collection, src_host, src_port, secret_key, src_database, "
            "src_schema, src_table, sink_catalog, sink_schema, sink_table, primary_key, select_list) values "
            "(%s, 'sql1', 1433, 'k1', 'sales', 'dbo', %s, 'main', 'bronze', %s, 'id', 'id, v') returning id",
            (group, table, f"sales_{table}")).fetchone()[0]
        conn.execute("insert into lakefed_ingest.checkpoint (control_id, src_database, src_schema, src_table, "
                     "strategy, position, phase, status) values (%s, 'sales', 'dbo', %s, 'ct', %s, 'incremental', "
                     "'ok')", (ids[table], table, str(positions[table])))
    return ids


def _positions(conn, ids):
    rows = conn.execute("select control_id, position from lakefed_ingest.checkpoint").fetchall()
    by_id = {control_id: int(position) for control_id, position in rows}
    return {table: by_id[control_id] for table, control_id in ids.items()}


def test_each_group_reads_only_its_own_tables_of_the_split_database(sessions):
    conn = sessions()
    _split_database(conn, {table: 20 for table in TABLES})
    store = ss.StateStore(conn)
    assert sorted(c["src_table"] for c in store.list_task_configs("g1")) == ["t1", "t2"]
    assert sorted(c["src_table"] for c in store.list_task_configs("g2")) == ["t3", "t4"]
    assert {ss.database_source(c) for c in store.list_task_configs("g1")} == {SOURCE}


def test_each_group_keeps_its_own_check_stamp_for_the_split_database(sessions):
    store = ss.StateStore(sessions())
    store.stamp_databases("g1", [(SOURCE, "sales", 20, 2)], None)
    store.stamp_databases("g2", [(SOURCE, "sales", 31, 2)], None)
    assert {key: stamp["version"] for key, stamp in store.get_database_checks("g1").items()} == {(SOURCE, "sales"): 20}
    assert {key: stamp["version"] for key, stamp in store.get_database_checks("g2").items()} == {(SOURCE, "sales"): 31}
    store.stamp_databases("g1", [(SOURCE, "sales", 25, 2)], None)                    # a later check of g1 only
    assert store.get_database_checks("g2")[(SOURCE, "sales")]["version"] == 31


def test_moving_one_groups_unchanged_tables_leaves_the_other_groups_checkpoints(sessions):
    conn = sessions()
    ids = _split_database(conn, {table: 20 for table in TABLES})
    ss.StateStore(conn).bulk_advance_unchanged([(ids["t1"], 31, None), (ids["t2"], 31, None)])
    assert _positions(conn, ids) == {"t1": 31, "t2": 31, "t3": 20, "t4": 20}


def test_the_two_groups_runs_hold_the_split_database_at_the_same_time(sessions):
    g1_run, g2_run, g1_second = (ss.StateStore(sessions()) for _ in range(3))
    g1_key, g2_key = (ss.database_lock_key(group, SOURCE, "sales") for group in ("g1", "g2"))
    assert g1_run.try_lock_collection_shared("g1") and g2_run.try_lock_collection_shared("g2")
    assert g1_run.try_lock_databases([g1_key]) == {g1_key}
    assert g2_run.try_lock_databases([g2_key]) == {g2_key}
    assert g1_second.try_lock_databases([g1_key]) == set()          # within a group, still one run per database


def test_each_groups_work_list_names_only_its_part_of_the_split_database(sessions):
    store = ss.StateStore(sessions())
    store.record_check_work("L:run1", "g2", [(SOURCE, "sales")])
    assert store.get_check_work("L:run1", "g1") == set()
    assert store.get_check_work("L:run1", "g2") == {(SOURCE, "sales")}
    assert store.get_check_work_all("L:run1") == {("g2", SOURCE, "sales")}


def test_each_groups_rollup_counts_its_own_tables_and_reads_its_own_stamp(sessions):
    conn = sessions()
    _split_database(conn, {"t1": 20, "t2": 20, "t3": 25, "t4": 25})
    store = ss.StateStore(conn)
    store.stamp_databases("g1", [(SOURCE, "sales", 20, 2)], None)          # vouches for t1 and t2 (at 20)
    store.stamp_databases("g2", [(SOURCE, "sales", 30, 2)], None)          # t3 and t4 are behind it: no vouch
    store.rewrite_rollups("g1", ["sales"])
    store.rewrite_rollups("g2", ["sales"])
    rows = conn.execute("select task_collection, tables, loaded, newest_success_at is not null "
                        "from lakefed_ingest.rollup_database where src_database = 'sales' "
                        "order by task_collection").fetchall()
    assert rows == [("g1", 2, 2, True), ("g2", 2, 2, False)]
    groups = conn.execute("select task_collection, databases, tables from lakefed_ingest.rollup_group "
                          "order by task_collection").fetchall()
    assert groups == [("g1", 1, 2), ("g2", 1, 2)]
