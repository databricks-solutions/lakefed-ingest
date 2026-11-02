"""A refusal for databases whose tables sit in more than one ingestion group, against Postgres as the app's own role:
the split check and the lookup of the live jobs holding those groups (queries.clash_jobs_sql), whose ids and names the
409 carries. A deleted job's groups are not named.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see app_role_pg_test.py)."""
import os

import pytest

pytest.importorskip("fastapi")
from app_role_pg_test import client  # noqa: E402,F401  (the app-role fixture: Lakebase reached as the role)
from delete_job_pg_test import _job, _row  # noqa: E402

import main  # noqa: E402

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")

SOURCE_ROW = {"src_type": "sqlserver", "src_host": "h", "src_port": 1433, "src_connection": None, "secret_key": "k1",
              "src_database": "db"}      # the source database the rows below are in


def test_a_database_whose_groups_a_live_job_holds_is_refused_with_that_job_named_and_its_id(client):
    sid = _job(client.owner, "Sales", ["clash_a"])
    _row(client.owner, "clash_a", "t1")
    _row(client.owner, "clash_b", "t2")
    with pytest.raises(main.HTTPException) as refused:
        _refuse([SOURCE_ROW])
    assert refused.value.status_code == 409
    assert refused.value.detail["jobs"] == [{"id": sid, "name": "Sales"}]
    assert "Some of these groups are in ingestion job Sales." in refused.value.detail["message"]


def test_a_group_only_a_deleted_job_held_names_no_job(client):
    _job(client.owner, "Old", ["clash_c"], deleted=True)
    _row(client.owner, "clash_c", "t1")
    _row(client.owner, "clash_d", "t2")
    with pytest.raises(main.HTTPException) as refused:
        _refuse([SOURCE_ROW])
    assert refused.value.detail["jobs"] == []
    assert "Some of these groups" not in refused.value.detail["message"]


def _refuse(rows):
    conn = main.db.connect()
    try:
        main._refuse_split_databases(conn.cursor(), rows)
    finally:
        conn.close()
