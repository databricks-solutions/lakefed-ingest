"""The job page's failing tables, shaped for the page: a table's class and kind, the message it shows, when its failing
began and how many runs failed it. The SQL that reads them runs against Postgres in failing_tables_pg_test.py."""
from datetime import datetime, timezone

import queries as q


def _row(**changes):
    row = {"control_id": 7, "task_collection": "sales", "server": "host:1433", "src_database": "db1",
           "src_schema": "dbo", "src_table": "orders", "checkpoint_detail": None, "failing_since": None,
           "runs": 0, "failures": 0, "error": None, "error_kind": None, "error_class": None}
    row.update(changes)
    return row


def test_a_table_shows_the_engines_class_and_kind_with_its_latest_message_and_counts():
    since = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
    [shaped] = q.failing_table_rows([_row(error="Login failed for user 'lakefed'", error_class="FAILED_JDBC.CONNECTION",
                                          error_kind="transient", failing_since=since, runs=3, failures=5)])
    assert shaped["table"] == "dbo.orders"
    assert shaped["error_class"] == "FAILED_JDBC.CONNECTION"
    assert shaped["error_kind"] == "transient"
    assert shaped["message"] == "Login failed for user 'lakefed'"
    assert shaped["failing_since"] == "2026-10-27T08:00:00+00:00"
    assert (shaped["runs"], shaped["failures"]) == (3, 5)
    assert (shaped["control_id"], shaped["server"], shaped["src_database"]) == (7, "host:1433", "db1")


def test_a_failure_recorded_before_schema_14_has_the_apps_word_for_its_cause_and_no_kind():
    [shaped] = q.failing_table_rows([_row(error="Query timed out after 600 seconds")])
    assert shaped["error_class"] == "Timed out"
    assert shaped["error_kind"] is None


def test_a_table_with_no_event_in_the_window_shows_its_checkpoints_note():
    [shaped] = q.failing_table_rows([_row(checkpoint_detail="reseed requested", runs=0, failures=0)])
    assert shaped["message"] == "reseed requested"
    assert shaped["failing_since"] is None
    assert (shaped["runs"], shaped["failures"]) == (0, 0)


def test_a_long_message_is_cut_to_2000_characters():
    [shaped] = q.failing_table_rows([_row(error="x" * 5000)])
    assert len(shaped["message"]) == 2000


def test_the_rows_keep_their_order_and_each_table_is_one_row():
    rows = q.failing_table_rows([_row(control_id=1, src_table="a"), _row(control_id=2, src_table="b")])
    assert [(r["control_id"], r["table"]) for r in rows] == [(1, "dbo.a"), (2, "dbo.b")]
