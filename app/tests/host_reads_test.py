"""Reads by host and password outside a JDBC connection are an app manager's: such a read takes its login from a secret
scope an admin wrote (the job's secret_scope, by default the admin's scope) and sends it to the host the request names.
Each path that sets one up is tried as a user who isn't a manager, and refused; a manager, a read through a Unity
Catalog connection and a read through a JDBC connection still work. Stubbed database, no app run."""
import json

import pytest

import queries as q

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import jdbc_connections as jc  # noqa: E402
import jdbc_logins as jl  # noqa: E402
import jobs  # noqa: E402
import main  # noqa: E402
from api_test import ROW  # noqa: E402
from api_test import client as api_client  # noqa: E402,F401
from import_api_test import SETTINGS  # noqa: E402
from import_api_test import client as import_client  # noqa: E402,F401
from new_job_test import CSV, FAN_IN  # noqa: E402
from new_job_test import client  # noqa: E402,F401

LOGIN_SCOPE = "lakefed_ingest_jdbc_logins"
HOST_CSV = ("src_host,secret_key,src_database,src_schema,src_table,select_list,primary_key\n"
            "attacker.example,creds-sales,sales,dbo,orders,\"id, total\",id\n")
NOT_A_MANAGER = "not an app manager"


@pytest.fixture
def managers(monkeypatch):
    """Who manages the app: a set the test fills; anyone else is refused."""
    names = set()
    monkeypatch.setattr(main, "_manage_refusal", lambda user: None if user in names else NOT_A_MANAGER)
    return names


@pytest.fixture
def discover(monkeypatch, managers):
    runs = []
    monkeypatch.setattr(main, "_refuse_run_of", lambda *args, **kwargs: None)
    monkeypatch.setattr(jobs, "DISCOVER_JOB_ID", 7)
    monkeypatch.setattr(jobs, "run_now", lambda job_id, params: runs.append(params) or 99)
    return runs


def _client(user="mallory@example.com"):
    return TestClient(main.app, headers={"X-Forwarded-Email": user})


# ---- discovery -------------------------------------------------------------------------------------------------

def test_a_discovery_by_host_without_a_connection_is_refused_for_a_user_who_is_not_a_manager(discover):
    r = _client().post("/api/discoveries", json={"databases": "sales", "src_host": "attacker.example",
                                                 "secret_key_template": "creds-{database}"})
    assert r.status_code == 403 and "only an app manager" in r.json()["detail"]
    assert discover == []


def test_a_manager_still_discovers_by_host(discover, managers):
    managers.add("ann@example.com")
    r = _client("ann@example.com").post("/api/discoveries", json={"databases": "sales", "src_host": "h1"})
    assert r.status_code == 200 and json.loads(discover[0]["spec"])["src_host"] == "h1"


def test_a_user_who_is_not_a_manager_still_discovers_through_a_jdbc_connection(discover, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    connection = {"name": "mine", "src_type": "sqlserver", "src_host": "mine.example", "src_port": 1433,
                  "secret_scope": LOGIN_SCOPE, "secret_key": "creds-{database}", "trust_server_certificate": "false",
                  "created_by": "mallory@example.com", "created_at": None}

    def query(sql, params=()):
        if sql == jc.get_sql():
            return [connection]
        if sql == q.jdbc_login_latest_per_key_sql():
            return [{"secret_key": "creds-sales", "connection": "mine"}]
        return []

    monkeypatch.setattr(main.db, "query", query)
    r = _client().post("/api/discoveries", json={"databases": "sales", "jdbc_connection": "mine"})
    assert r.status_code == 200, r.text


def test_a_rediscovery_by_host_runs_only_for_a_job_a_manager_made(managers):
    spec = {"src_host": "h1", "secret_key_template": "creds-{database}", "databases": ["sales"]}
    with pytest.raises(HTTPException) as refused:
        main._rediscovery_reading(spec, "mallory@example.com")
    assert refused.value.status_code == 403
    with pytest.raises(HTTPException):
        main._rediscovery_reading(spec, None)
    managers.add("ann@example.com")
    assert main._rediscovery_reading(spec, "ann@example.com") == (spec, {})
    assert main._rediscovery_reading({"src_connection": "uc", "databases": "*"}, None)[1] == {}


# ---- New job ---------------------------------------------------------------------------------------------------

def test_new_job_with_tables_read_by_host_is_refused_for_a_user_who_is_not_a_manager(client, managers):
    r = client.post("/api/ingestion-jobs", json={"name": "Job", "csv": HOST_CSV,
                                                 "settings": {**FAN_IN, "fan_in": False}, "dry_run": False})
    assert r.status_code == 403 and "only an app manager" in r.json()["detail"]
    assert client.db.of("insert into lakefed_ingest.control (") == []


def test_new_job_naming_a_secret_scope_is_refused_for_a_user_who_is_not_a_manager(client, managers):
    r = client.post("/api/ingestion-jobs", json={"name": "Job", "csv": CSV, "settings": FAN_IN, "dry_run": False,
                                                 "run": {"cadence_minutes": 60, "paused": True,
                                                         "parameters": {"secret_scope": "lakefed_ingest"}}})
    assert r.status_code == 403 and "only an app manager" in r.json()["detail"]


def test_new_job_through_unity_catalog_connections_stays_open_to_any_user(client, managers):
    r = client.post("/api/ingestion-jobs", json={"name": "Job", "csv": CSV, "settings": FAN_IN, "dry_run": False,
                                                 "run": {"cadence_minutes": 60, "paused": True}})
    assert r.status_code == 200, r.text
    assert client.db.of("insert into lakefed_ingest.control (")


def test_a_manager_still_makes_a_job_read_by_host(client, managers):
    managers.add("op@example.com")
    r = client.post("/api/ingestion-jobs", json={"name": "Job", "csv": HOST_CSV,
                                                 "settings": {**FAN_IN, "fan_in": False}, "dry_run": False,
                                                 "run": {"cadence_minutes": 60, "paused": True}})
    assert r.status_code == 200, r.text


# ---- tables written into a job -------------------------------------------------------------------------------

def test_a_table_added_by_hand_read_by_host_is_refused_for_a_user_who_is_not_a_manager(api_client, managers):
    r = api_client.post("/api/tables", json=ROW)
    assert r.status_code == 403 and "only an app manager" in r.json()["detail"]
    assert api_client.db.of("insert into lakefed_ingest.control (") == []
    managers.add("op@example.com")
    assert api_client.post("/api/tables", json=ROW).status_code == 200


def test_an_import_of_tables_read_by_host_is_refused_for_a_user_who_is_not_a_manager(import_client, managers):
    csv = ("src_host,secret_key,src_database,src_schema,src_table,select_list,primary_key\n"
           "attacker.example,creds-sales,Sales,dbo,orders,\"id,amount\",id\n")
    body = {"task_collection": "g9", "settings": {**SETTINGS, "use_remote_query": False}, "csv": csv, "dry_run": False}
    r = import_client.post("/api/import", json=body)
    assert r.status_code == 403 and "only an app manager" in r.json()["detail"], r.text
    assert import_client.db.of("insert into lakefed_ingest.control (") == []


def test_rows_read_by_host_into_a_job_with_no_connection_need_a_manager(managers):
    rows = [{"task_collection": "g1", "src_host": "attacker.example", "secret_key": "creds-sales"}]
    with pytest.raises(HTTPException) as refused:
        main._refuse_host_reads(rows, "mallory@example.com", {})
    assert refused.value.status_code == 403
    main._refuse_host_reads([{"task_collection": "g1", "src_connection": "uc", "use_remote_query": True}],
                            "mallory@example.com", {})
    main._refuse_host_reads(rows, "mallory@example.com", {"g1": {"name": "a connection"}})
    managers.add("ann@example.com")
    main._refuse_host_reads(rows, "ann@example.com", {})


def test_an_edit_that_points_a_table_at_a_host_needs_a_manager_and_other_edits_do_not(managers, monkeypatch):
    monkeypatch.setattr(main, "_group_connections", lambda groups: {})
    row = {"id": 1, "task_collection": "g1", "src_connection": "uc", "use_remote_query": True, "src_host": None,
           "secret_key": None, "src_type": "sqlserver", "src_port": None}
    to_host = [{"id": 1, "changes": {"use_remote_query": [True, False], "src_host": [None, "attacker.example"],
                                     "secret_key": [None, "creds-sales"]}}]
    with pytest.raises(HTTPException) as refused:
        main._refuse_connection_edits([row], to_host, user="mallory@example.com")
    assert refused.value.status_code == 403
    host_row = {**row, "use_remote_query": False, "src_host": "h1", "secret_key": "k"}
    main._refuse_connection_edits([host_row], [{"id": 1, "changes": {"priority": [0, 5]}}], user="mallory@example.com")
    managers.add("ann@example.com")
    main._refuse_connection_edits([row], to_host, user="ann@example.com")


# ---- a job's secret_scope ------------------------------------------------------------------------------------------

def test_a_scope_parameter_set_or_changed_needs_a_manager_and_one_kept_does_not(managers):
    with pytest.raises(HTTPException):
        main._refuse_scope_parameter({"secret_scope": "lakefed_ingest"}, "mallory@example.com")
    with pytest.raises(HTTPException):
        main._refuse_scope_parameter({"secret_scope": "other"}, "mallory@example.com", {"secret_scope": "lakefed"})
    main._refuse_scope_parameter({"secret_scope": "lakefed"}, "mallory@example.com", {"secret_scope": "lakefed"})
    main._refuse_scope_parameter({}, "mallory@example.com", {"secret_scope": "lakefed"})
    main._refuse_scope_parameter({"parallelism": "8"}, "mallory@example.com")
    managers.add("ann@example.com")
    main._refuse_scope_parameter({"secret_scope": "other"}, "ann@example.com")
