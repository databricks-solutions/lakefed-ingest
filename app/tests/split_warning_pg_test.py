"""The warning for databases whose tables sit in more than one ingestion group, against Postgres as the app's own role:
the split check and the lookup of the live jobs holding those groups (queries.clash_jobs_sql), whose ids and names the
warning carries. A deleted job's groups are not named. The write the warning comes back with is
job_settings_pg_test.py's.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see app_role_pg_test.py)."""
import os

import pytest

pytest.importorskip("fastapi")
from app_role_pg_test import client  # noqa: E402,F401  (the app-role fixture: Lakebase reached as the role)
from delete_job_pg_test import _job, _row  # noqa: E402

import main  # noqa: E402
import queries as q  # noqa: E402

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")

SOURCE_ROW = {"src_type": "sqlserver", "src_host": "h", "src_port": 1433, "src_connection": None, "secret_key": "k1",
              "src_database": "db"}      # the source database the rows below are in


def test_a_database_whose_groups_a_live_job_holds_is_warned_about_with_that_job_named_and_its_id(client):
    sid = _job(client.owner, "Sales", ["clash_a"])
    _row(client.owner, "clash_a", "t1")
    _row(client.owner, "clash_b", "t2")
    [warning] = _warnings([SOURCE_ROW])
    assert warning["databases"] == [{"database": "db", "groups": ["clash_a", "clash_b"]}]
    assert warning["jobs"] == [{"id": sid, "name": "Sales"}]
    assert "Some of these groups are in ingestion job Sales." in warning["message"]


def test_a_group_only_a_deleted_job_held_names_no_job(client):
    _job(client.owner, "Old", ["clash_c"], deleted=True)
    _row(client.owner, "clash_c", "t1")
    _row(client.owner, "clash_d", "t2")
    [warning] = _warnings([SOURCE_ROW])
    assert warning["jobs"] == []
    assert "Some of these groups" not in warning["message"]


def test_a_database_in_one_group_is_no_warning(client):
    _row(client.owner, "clash_e", "t1")
    _row(client.owner, "clash_e", "t2")
    assert _warnings([SOURCE_ROW]) == []


def test_a_database_that_was_split_before_the_write_is_found_and_left_out_of_the_warning(client):
    _row(client.owner, "clash_f", "t1")
    _row(client.owner, "clash_g", "t2")
    assert _split_keys([SOURCE_ROW]) == {q.database_key(SOURCE_ROW)}
    assert _warnings([SOURCE_ROW], already_split=_split_keys([SOURCE_ROW])) == []


def test_a_database_in_one_group_is_not_among_those_split_before_the_write(client):
    _row(client.owner, "clash_h", "t1")
    assert _split_keys([SOURCE_ROW]) == set()


def _split_keys(rows):
    conn = main.db.connect()
    try:
        return main._split_database_keys(conn.cursor(), rows)
    finally:
        conn.close()


def _warnings(rows, already_split=frozenset()):
    conn = main.db.connect()
    try:
        return main._split_database_warnings(conn.cursor(), rows, already_split)
    finally:
        conn.close()
