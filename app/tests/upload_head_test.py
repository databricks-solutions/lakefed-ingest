"""The import's preview reads the first few lines of an uploaded file back from Lakebase (GET /api/uploads/{id}/lines): the
header's columns, then each line's number and fields in file order, at most 20 however many are asked for. Plain: the
read is a fake that honours its limit, so no database is reached."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import queries as q  # noqa: E402

COLUMNS = ["src_database", "src_schema", "src_table"]


def test_the_first_five_lines_come_back_under_the_header_by_default(reads):
    body = TestClient(main.app).get("/api/uploads/u1/lines").json()
    assert body["columns"] == COLUMNS
    assert [line["line"] for line in body["lines"]] == [2, 3, 4, 5, 6]
    assert body["lines"][0]["fields"] == {"src_database": "eng_002", "src_schema": "dbo", "src_table": "t2"}
    assert reads["asked"] == [(q.upload_head_sql(), (5, "u1"))]


def test_a_limit_is_kept_between_one_and_twenty(reads):
    client = TestClient(main.app)
    assert client.get("/api/uploads/u1/lines?limit=50").status_code == 200
    assert client.get("/api/uploads/u1/lines?limit=0").status_code == 200
    assert [params[0] for _, params in reads["asked"]] == [20, 1]


def test_the_read_is_a_plain_select_that_takes_the_limit_before_the_upload_id():
    sql = q.upload_head_sql()
    assert sql.startswith("select ") and "order by line limit %s" in sql and sql.endswith("where u.upload_id = %s")
    assert not any(word in sql for word in ("insert ", "update ", "delete "))


def test_an_upload_with_no_lines_answers_its_header_and_no_lines(reads):
    reads["lines"] = 0
    assert TestClient(main.app).get("/api/uploads/u1/lines").json() == {"columns": COLUMNS, "lines": []}


def test_an_unknown_upload_is_not_found(reads):
    reads["upload"] = False
    r = TestClient(main.app).get("/api/uploads/nope/lines")
    assert r.status_code == 404 and r.json()["detail"] == "upload nope not found"


def test_reads_need_no_signed_in_user(reads):
    assert TestClient(main.app).get("/api/uploads/u1/lines").status_code == 200


@pytest.fixture
def reads(monkeypatch):
    """The fake database: an upload with seven lines (from line 2) unless a test changes it, and the asks it was sent."""
    state = {"upload": True, "lines": 7, "asked": []}

    def query(sql, params=()):
        state["asked"].append((sql, params))
        if not state["upload"]:
            return []
        if not state["lines"]:
            return [{"columns": COLUMNS, "line": None, "fields": None}]
        found = [{"columns": COLUMNS, "line": n, "fields": {"src_database": f"eng_{n:03d}", "src_schema": "dbo",
                                                           "src_table": f"t{n}"}} for n in range(2, 2 + state["lines"])]
        return found[:params[0]]

    monkeypatch.setattr(main.db, "query", query)
    return state
