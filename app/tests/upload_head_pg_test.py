"""The import's preview read against Postgres as the app's own role (select, insert and update only, as
app_role_pg_test.py grants it): an upload posted through the API, then its first lines read back in file order, at most
20.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see app_role_pg_test.py)."""
import os

import pytest

pytest.importorskip("fastapi")
from app_role_pg_test import client  # noqa: E402,F401  (the app-role fixture: Lakebase reached as the role)

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
CSV = "src_database,src_schema,src_table\n" + "".join(f"eng_{n:03d},dbo,t{n}\n" for n in range(1, 26))


def test_the_first_five_lines_come_back_under_the_header_as_the_file_gave_them(client):
    upload_id = client.post("/api/uploads", json={"csv": CSV, "connection": "sql_a", "file_name": "f.csv"}).json()["upload_id"]
    body = client.get(f"/api/uploads/{upload_id}/lines").json()
    assert body["columns"] == ["src_database", "src_schema", "src_table"]
    assert [line["line"] for line in body["lines"]] == [2, 3, 4, 5, 6]
    assert body["lines"][0]["fields"]["src_table"] == "t1" and body["lines"][4]["fields"]["src_database"] == "eng_005"


def test_a_limit_above_twenty_gives_at_most_twenty_lines_in_order(client):
    upload_id = client.post("/api/uploads", json={"csv": CSV, "connection": "sql_a", "file_name": "f.csv"}).json()["upload_id"]
    body = client.get(f"/api/uploads/{upload_id}/lines?limit=50").json()
    assert [line["line"] for line in body["lines"]] == list(range(2, 22))


def test_an_upload_that_was_never_posted_is_not_found(client):
    assert client.get("/api/uploads/no-such-upload/lines").status_code == 404
