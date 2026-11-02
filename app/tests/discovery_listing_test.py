"""A discovery's databases, paged from Lakebase: one page of 100 at a time with a filter, never the whole list."""
import pytest

import queries as q

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


def test_a_listing_page_is_read_with_the_filter_and_the_page_bounds(monkeypatch):
    asked = []

    def query(sql, params=()):
        asked.append((sql, params))
        if sql == q.discovery_totals_sql():
            return [{"total": 22000, "matched": 3}]
        return [{"src_database": f"eng_{i:03d}", "status": "ok", "error": None} for i in range(3)]

    monkeypatch.setattr(main.db, "query", query)
    r = TestClient(main.app).get("/api/discoveries/d1/listing", params={"filter": "ENG", "offset": 200, "limit": 100})
    body = r.json()
    assert r.status_code == 200 and body["total"] == 22000 and body["matched"] == 3 and body["limit"] == 100
    assert body["offset"] == 200 and len(body["databases"]) == 3
    assert asked[0] == (q.discovery_totals_sql(), ("ENG", "d1"))
    assert asked[1] == (q.discovery_listing_sql(), ("d1", "ENG", 100, 200))


def test_a_listing_page_is_at_most_500_and_the_offset_is_not_negative(monkeypatch):
    seen = []
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): seen.append(params) or [{"total": 0, "matched": 0}])
    TestClient(main.app).get("/api/discoveries/d1/listing", params={"offset": -5, "limit": 100000})
    assert seen[-1] == ("d1", "", 500, 0)
