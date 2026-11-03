"""A connection test that still carries the old Azure settings, against a real Postgres with the Lakebase schema: the
app lists the databases through the SQL warehouse as it does without them, and saves the listing as a warehouse
listing, with none of the Azure keys. Before the Azure listing was removed such a request went to the discovery job.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
import os
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from discover_warehouse_test import _Warehouse, _answer  # noqa: E402

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]
OLD_AZURE_SETTINGS = {
    "azure_credential": "lab_reader",
    "azure_resource_id": "/subscriptions/0000-1111/resourceGroups/rg-lab/providers/Microsoft.Sql/servers/lab-sql",
}


@pytest.fixture
def client(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_azure_drop_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    target = psycopg.conninfo.make_conninfo(URI, dbname=name)
    try:
        with psycopg.connect(target, autocommit=True) as conn:
            for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
                conn.execute(statement)
        monkeypatch.setattr(main.db, "connect", lambda: psycopg.connect(target, autocommit=True))
        monkeypatch.setattr(main, "connections", lambda: {"connections": [{"name": "sql_a"}], "error": None})
        monkeypatch.setattr(main.jobs, "run_now", lambda job_id, params: pytest.fail("the discovery job was started"))
        monkeypatch.setattr(main, "warehouse", _Warehouse([_answer("SUCCEEDED", rows=[{"name": "eng_002"}, {"name": "eng_001"}])]))
        c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
        c.target = target
        yield c
    finally:
        admin.execute(f"drop database {name} with (force)")
        admin.close()


def test_a_listing_request_with_the_old_azure_settings_is_saved_as_a_warehouse_listing(client):
    import psycopg
    response = client.post("/api/discoveries/source",
                           json={"src_connection": "sql_a", "kind": "list", **OLD_AZURE_SETTINGS})
    body = response.json()
    assert response.status_code == 200 and body.get("via") == "warehouse", body
    assert body["databases"] == 2
    with psycopg.connect(client.target) as conn:
        rows = conn.execute("select src_database, status, listing from lakefed_ingest.discovery "
                            "where discovery_id = %s order by src_database", (body["discovery_id"],)).fetchall()
    assert [(database, status) for database, status, _ in rows] == [("eng_001", "listed"), ("eng_002", "listed")]
    for _, _, listing in rows:
        assert listing == {"via": "warehouse"}
