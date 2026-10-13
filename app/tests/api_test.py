"""FastAPI backend (main.py) with a fake database: bulk register merge / dry run, validation, SPA.

Skipped when FastAPI isn't installed (the repo's base test environment needs only pytest);
run with the app's requirements installed: `pip install -r app/src/requirements.txt httpx pytest`."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import queries as q  # noqa: E402

DISCOVERED = [
    dict(discovery_id="d1", src_database="db1", src_schema="dbo", src_table="orders", row_count=10, size_mb=2048.0,
         ct_enabled=True, primary_key="id", select_list="id, v", excluded_columns=None, rowversion_column=None,
         identity_column=None, load_type="ct", cursor_column=None, partition_candidate="id", notes=None,
         src_type="sqlserver", src_host="h", src_port=None, secret_key="k-db1", registered_id=None),
    dict(discovery_id="d1", src_database="db1", src_schema="dbo", src_table="lines", row_count=5, size_mb=1.0,
         ct_enabled=False, primary_key=None, select_list="a, b", excluded_columns=None, rowversion_column=None,
         identity_column=None, load_type="full", cursor_column=None, partition_candidate=None, notes=None,
         src_type="sqlserver", src_host="h", src_port=None, secret_key="k-db1", registered_id=None),
]


@pytest.fixture
def client(monkeypatch):
    inserted = []
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): DISCOVERED if "discovered_table" in sql else [])

    def returning(sql, params):
        inserted.append(dict(zip(q.CONTROL_INSERT_COLUMNS, params)))
        return None if params[q.CONTROL_INSERT_COLUMNS.index("src_table")] == "lines" else (len(inserted),)
    monkeypatch.setattr(main.db, "execute_returning", returning)
    c = TestClient(main.app)
    c.inserted = inserted
    return c


def test_review_hides_server_fields_and_partitions_large_tables(client):
    rows = client.post("/api/discoveries/d1/review", json={"shared": {"sink_schema": "bronze"}}).json()
    assert {r["src_table"]: r["load_partitioned"] for r in rows} == {"orders": True, "lines": False}
    assert not any(k.startswith("_") for r in rows for k in r)


def test_register_dry_run_inserts_nothing(client):
    r = client.post("/api/discoveries/d1/register", json={"task_collection": "c", "dry_run": True}).json()
    assert r["counts"] == {"valid": 2} and client.inserted == []


def test_register_applies_only_editable_fields_and_reports_skips(client):
    edits = [{"src_database": "db1", "src_schema": "dbo", "src_table": "orders", "include": True,
              "sink_table": "ord", "secret_key": "stolen", "_secret_key": "stolen"}]
    r = client.post("/api/discoveries/d1/register", json={"task_collection": "c", "edits": edits}).json()
    assert r["counts"] == {"registered": 1, "skipped": 1}
    orders = next(i for i in client.inserted if i["src_table"] == "orders")
    assert orders["sink_table"] == "ord" and orders["secret_key"] == "k-db1" and orders["load_partitioned"] is True


def test_register_requires_collection_and_reports_invalid_rows(client):
    assert client.post("/api/discoveries/d1/register", json={"task_collection": ""}).status_code == 422
    edits = [{"src_database": "db1", "src_schema": "dbo", "src_table": "orders", "load_type": "cursor"}]
    r = client.post("/api/discoveries/d1/register", json={"task_collection": "c", "edits": edits, "dry_run": True}).json()
    assert r["counts"] == {"invalid": 1, "valid": 1}
    assert "cursor_column is required for load_type cursor" in r["results"][0]["errors"]


def test_add_table_validation_error_is_422(client):
    r = client.post("/api/tables", json={"task_collection": "c"})
    assert r.status_code == 422 and "src_host is required" in r.json()["detail"]["errors"]


def test_bad_status_filter_is_400_and_unknown_api_is_404(client):
    assert client.get("/api/tables?status=bogus").status_code == 400
    assert client.get("/api/nope").status_code == 404
