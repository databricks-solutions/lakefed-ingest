"""The monitoring rollups' SQL, checked without a database: the app's rewrite statements are the engine's text, the
reads name the columns the DDL defines, and the scan for an unwritten group is limited to its groups."""
import re
import sys
from pathlib import Path

import queries as q

REPO = Path(__file__).resolve().parents[2]
ROLLUP_COLUMNS = ("task_collection", "src_database", "server", "src_connection", "src_host", "instance_label", "tables",
                  "no_sla_tables", "failing", "first_load", "loaded", "oldest_success_at", "newest_success_at",
                  "newest_any_success_at", "earliest_due_at", "control_rows", "updated_at")


def _engine():
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    return state_store


def test_the_app_rewrites_are_the_engine_statements():
    state_store = _engine()
    assert q.rollup_database_rewrite_sql() == state_store.rollup_database_rewrite_sql()
    assert q.rollup_group_rewrite_sql() == state_store.rollup_group_rewrite_sql()
    assert q.ROLLUP_CHUNK == state_store.ROLLUP_CHUNK


def test_the_rollup_reads_use_only_columns_the_ddl_defines():
    ddl = (REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()
    table = ddl[ddl.index("create table if not exists lakefed_ingest.rollup_database"):]
    table = table[:table.index("\n);")]
    for column in ROLLUP_COLUMNS:
        assert re.search(rf"^\s+{column}\s+", table, re.MULTILINE), column


def test_the_database_read_is_one_row_per_grain_with_late_as_a_flag():
    sql = q.rollup_states_sql()
    assert "from lakefed_ingest.rollup_database r" in sql and "where r.control_rows > 0" in sql
    assert "coalesce(r.earliest_due_at < now(), false) as late_database" in sql
    assert "nullif(r.src_connection, '') as src_connection" in sql
    assert "nullif(r.instance_label, '') as instance_label" in sql
    assert "r.loaded as state_loaded" in sql and "%s" not in sql


def test_the_scan_for_unwritten_groups_is_limited_to_them_and_the_all_groups_scan_is_not():
    assert "where t.task_collection = any(%s)" in q.database_states_sql(only_groups=True)
    assert "any(%s)" not in q.database_states_sql()
    assert "from lakefed_ingest.control t" in q.database_states_for_groups_sql()


def test_the_live_database_counts_match_the_state_columns_of_one_database():
    sql = q.database_counts_sql()
    for column in ("control_rows", "tables", "no_sla_tables", "state_current", "state_late", "state_first_load",
                   "state_failing"):
        assert f"as {column}" in sql, column
    assert sql.count("%s") == 3


def test_the_databases_a_write_touched_are_found_by_id_or_by_group():
    by_ids = q.control_databases_sql("ids")
    by_groups = q.control_databases_sql("groups")
    assert "t.id = any(%s)" in by_ids and "t.task_collection = any(%s)" not in by_ids
    assert "t.task_collection = any(%s)" in by_groups and "t.id = any(%s)" not in by_groups
    assert "coalesce(t.src_database, '')" in by_ids
