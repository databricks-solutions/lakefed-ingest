"""A new job whose database is bigger than a group, created through the API against Postgres as the app's own role:
the app spreads the database over the groups it needs, each table written once, the parts even, and the Create's
answer warns that the database is split (placement_split_test.py has the placement itself).

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see app_role_pg_test.py)."""
import os

import pytest

pytest.importorskip("fastapi")
from app_role_pg_test import SETTINGS, client  # noqa: E402,F401  (the app-role fixture: Lakebase reached as the role)

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
HEAD = "src_connection,src_database,src_schema,src_table,select_list,primary_key\n"


def test_creating_a_job_spreads_a_database_bigger_than_a_group_and_warns_about_it(client):
    csv = HEAD + "".join(f'sql_a,{name},dbo,t{t:04d},"id",id\n' for name, tables in (("huge", 250), ("small", 20))
                         for t in range(1, tables + 1))
    made = client.post("/api/ingestion-jobs", json={"name": "Big", "settings": SETTINGS, "csv": csv,
                                                    "tables_per_group": 100, "dry_run": False, "request_id": "big-1"})
    assert made.status_code in (200, 202), made.text
    answer = made.json()
    if "task" in answer:
        answer = client.get(f"/api/tasks/{answer['task']['id']}").json()["result"]
    parts = client.sql("select src_database, task_collection, count(*) from lakefed_ingest.control "
                       "group by 1, 2 order by 1, 2")
    huge = sorted(n for database, _, n in parts if database == "huge")
    assert huge == [83, 83, 84]
    assert [n for database, _, n in parts if database == "small"] == [20]
    assert client.sql("select count(*), count(distinct (src_database, src_table)) from lakefed_ingest.control") == \
        [(270, 270)]
    [warning] = answer["warnings"]
    assert [item["database"] for item in warning["databases"]] == ["huge"]
    assert len(warning["databases"][0]["groups"]) == 3
