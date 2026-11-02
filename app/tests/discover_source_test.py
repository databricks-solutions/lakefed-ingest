"""Discover on a source: starting a discovery through a UC connection (POST /api/discoveries/source), and the tables
it found (GET /api/discoveries/{id}/tables), with the discovery job and the database faked."""
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import queries as q  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    started = []
    monkeypatch.setattr(main, "connections", lambda: {"connections": [{"name": "sql_a"}], "error": None})
    monkeypatch.setattr(main.jobs, "DISCOVER_JOB_ID", "555")
    monkeypatch.setattr(main, "_refuse_run_of", lambda job_id, request: None)
    monkeypatch.setattr(main.jobs, "run_now", lambda job_id, params: started.append((job_id, params)) or 42)
    found = [dict(src_database="eng_001", src_schema="gen", src_table="t0001", select_list="id, a", primary_key="id",
                  load_type="ct", cursor_column=None, row_count=10, size_mb=1, ct_enabled=True, excluded_columns=None,
                  notes=None, src_connection="sql_a", secret_key=None, src_host=None, registered_id=None)]
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): found if sql == q.discovered_tables_sql() else [])
    c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
    c.started = started
    return c


def _spec(client):
    job_id, params = client.started[-1]
    return job_id, json.loads(params["spec"])


def test_test_connection_lists_the_servers_databases_with_no_secret(client):
    body = client.post("/api/discoveries/source", json={"src_connection": "sql_a", "kind": "list"}).json()
    assert body["run_id"] == 42 and body["discovery_id"]
    job_id, spec = _spec(client)
    assert job_id == "555" and spec == {"src_type": "sqlserver", "src_connection": "sql_a", "kind": "list",
                                        "requested_by": "op@example.com"}


def test_the_whole_server_or_chosen_databases_are_read(client):
    client.post("/api/discoveries/source", json={"src_connection": "sql_a", "whole_server": True})
    assert _spec(client)[1]["databases"] == "*"
    client.post("/api/discoveries/source", json={"src_connection": "sql_a", "databases": ["eng_001", " eng_001", "eng_002"]})
    assert _spec(client)[1]["databases"] == ["eng_001", "eng_002"]


def test_nothing_chosen_or_an_unknown_connection_is_refused(client):
    assert client.post("/api/discoveries/source", json={"src_connection": "sql_a"}).status_code == 422
    assert client.post("/api/discoveries/source", json={"src_connection": "nope", "kind": "list"}).status_code == 404
    assert client.post("/api/discoveries/source", json={"src_connection": "sql_a", "kind": "drop"}).status_code == 422
    assert client.started == []


def test_a_discoverys_tables_keep_only_what_add_tables_needs(client):
    tables = client.get("/api/discoveries/d1/tables").json()
    assert tables[0]["select_list"] == "id, a" and "secret_key" not in tables[0] and "registered_id" not in tables[0]


def test_a_connection_the_app_cant_see_names_the_grant_it_needs(client, monkeypatch):
    monkeypatch.setattr(main.jobs, "APP_PRINCIPAL", "6f1c-app")
    r = client.post("/api/discoveries/source", json={"src_connection": "sql_hidden", "kind": "list"})
    assert r.status_code == 404
    assert "can't see the connection 'sql_hidden'" in r.text and "(6f1c-app) needs USE CONNECTION on 'sql_hidden'" in r.text
    monkeypatch.setattr(main, "connections", lambda: {"connections": [], "error": "PERMISSION_DENIED: no access. More"})
    r = client.post("/api/discoveries/source", json={"src_connection": "sql_a", "kind": "list"})
    assert r.status_code == 404 and "can't list connections (PERMISSION_DENIED: no access)" in r.text
    assert client.started == []


def test_a_name_filter_goes_with_the_listing_and_the_whole_server(client):
    client.post("/api/discoveries/source", json={"src_connection": "sql_a", "kind": "list", "pattern": "lfc_eng_%"})
    assert _spec(client)[1]["pattern"] == "lfc_eng_%"
    client.post("/api/discoveries/source", json={"src_connection": "sql_a", "whole_server": True, "pattern": "lfc_eng_%"})
    assert _spec(client)[1]["databases"] == "*" and _spec(client)[1]["pattern"] == "lfc_eng_%"
    r = client.post("/api/discoveries/source", json={"src_connection": "sql_a", "kind": "list", "pattern": "x' or 1=1"})
    assert r.status_code == 422 and len(client.started) == 2


def test_an_azure_sql_servers_databases_can_be_listed_through_azure_by_the_discovery_job(client):
    server = "/subscriptions/0000-1111/resourceGroups/rg-lab/providers/Microsoft.Sql/servers/lab-sql"
    through_azure = {"azure_credential": "lab_reader", "azure_resource_id": server}
    client.post("/api/discoveries/source", json={"src_connection": "sql_a", "kind": "list", **through_azure})
    assert {k: _spec(client)[1].get(k) for k in through_azure} == through_azure
    client.post("/api/discoveries/source", json={"src_connection": "sql_a", "whole_server": True, "pattern": "eng_%",
                                                 **through_azure})
    assert _spec(client)[1]["databases"] == "*" and _spec(client)[1]["azure_resource_id"] == server
    client.post("/api/discoveries/source", json={"src_connection": "sql_a", "kind": "list"})
    assert "azure_credential" not in _spec(client)[1]
    for wrong in [{"azure_credential": "lab_reader"}, {"azure_resource_id": server},
                  {"azure_credential": "lab reader", "azure_resource_id": server},
                  {"azure_credential": "lab_reader", "azure_resource_id": server + "/../../x"},
                  {"azure_credential": "lab_reader", "azure_resource_id": "https://elsewhere.example.com" + server}]:
        r = client.post("/api/discoveries/source", json={"src_connection": "sql_a", "kind": "list", **wrong})
        assert r.status_code == 422, wrong
    assert len(client.started) == 3
