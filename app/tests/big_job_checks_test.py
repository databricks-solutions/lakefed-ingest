"""A job of more than BACKGROUND_ROWS tables is made and added to by database (_create_by_database, _add_by_database),
and gets the same checks as a smaller one: a JDBC connection's scope and trust replace the request's, the login scope
and other scopes are held as for any new job, and each transaction's rows are held to the job's connection or, without
one, to the manager rule for reads by host. The big path's fixture (by_database_test, BACKGROUND_ROWS at 50) with a
stubbed database; no app run."""
import json

import pytest

import jdbc_logins as jl
import main
from by_database_test import _add, _create, _job_for_a_big_add  # noqa: F401
from by_database_test import big  # noqa: F401

LOGIN_SCOPE = "lakefed_ingest_jdbc_logins"
EVIL = {"name": "evil", "src_type": "sqlserver", "src_host": "attacker.example", "src_port": 1433,
        "secret_scope": LOGIN_SCOPE, "secret_key": "{database}", "trust_server_certificate": "true",
        "created_by": "op@example.com", "created_at": None}


def _schedule_parameters(client) -> dict:
    [schedule] = client.db.of("insert into lakefed_ingest.schedule (")
    return json.loads(schedule[3])


def _create_with(client, parameters):
    return client.post("/api/ingestion-jobs", json={"name": "Big job", "settings": {
        "fan_in": True, "sink_catalog": "main", "sink_schema": "eng", "sink_table": "{table}"}, "dry_run": False,
        "request_id": "big-checks", "run": {"cadence_minutes": 60, "paused": True, "parameters": parameters},
        "source": {"upload_id": "u1", "whole_server": True}})


def test_a_big_job_through_a_jdbc_connection_takes_its_scope_and_trust_and_holds_each_chunk_to_it(big, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    monkeypatch.setattr(main, "_jdbc_source", lambda source: EVIL)
    held = []
    monkeypatch.setattr(main, "_refuse_connection_rows",
                        lambda connection, rows, cur=None: held.append((connection["name"], len(rows), cur is not None)))
    r = _create_with(big, {"secret_scope": "lakefed_ingest", "trust_server_certificate": "false", "parallelism": "8"})
    assert r.status_code == 202, r.text
    big.tasks.run()
    assert big.get(f"/api/tasks/{r.json()['task']['id']}").json()["status"] == "succeeded"
    assert _schedule_parameters(big) == {"parallelism": "8", "secret_scope": LOGIN_SCOPE,
                                         "trust_server_certificate": "true"}
    assert ("evil", 150, False) in held                     # the check, before the job is made
    assert ("evil", 150, True) in held                      # the chunk's own transaction


def test_a_big_job_whose_rows_do_not_read_through_its_connection_is_refused_before_anything_is_written(big, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    monkeypatch.setattr(main, "_jdbc_source", lambda source: EVIL)
    r = _create_with(big, {})
    assert r.status_code == 202
    big.tasks.run()
    failed = big.get(f"/api/tasks/{r.json()['task']['id']}").json()
    assert failed["status"] == "failed" and "take that connection's host" in failed["error"]
    assert big.store.controls == [] and big.store.schedules == []


def test_a_big_job_naming_a_secret_scope_is_refused_for_a_user_who_is_not_a_manager(big, monkeypatch):
    monkeypatch.setattr(main, "_manage_refusal", lambda user: "not an app manager")
    r = _create_with(big, {"secret_scope": "lakefed_ingest"})
    assert r.status_code == 403 and "only an app manager" in r.json()["detail"]
    assert big.tasks.rows == {} and big.store.schedules == []


def test_a_big_job_not_read_through_a_connection_may_not_name_the_login_scope(big, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    monkeypatch.setattr(main, "_manage_refusal", lambda user: None)
    r = _create_with(big, {"secret_scope": LOGIN_SCOPE})
    assert r.status_code == 422 and "a job reads it only through a JDBC connection" in r.text


def test_a_big_job_read_by_host_is_refused_for_a_user_who_is_not_a_manager(big, monkeypatch):
    monkeypatch.setattr(main, "_manage_refusal", lambda user: "not an app manager")
    monkeypatch.setattr(main, "_reads_a_secret", lambda row: True)      # as rows of a manager's discovery by host
    r = _create(big)
    assert r.status_code == 202
    big.tasks.run()
    failed = big.get(f"/api/tasks/{r.json()['task']['id']}").json()
    assert failed["status"] == "failed" and "only an app manager" in failed["error"]
    assert big.store.controls == [] and big.store.schedules == []


def test_a_big_job_through_unity_catalog_connections_is_still_made(big):
    r = _create(big)
    big.tasks.run()
    assert big.get(f"/api/tasks/{r.json()['task']['id']}").json()["status"] == "succeeded"
    assert len(big.store.controls) == 150


def test_a_big_add_into_a_jdbc_job_holds_its_rows_to_the_connection(big, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    _job_for_a_big_add(big, monkeypatch)
    monkeypatch.setattr(main, "_job_connection_row", lambda job_id: EVIL)
    r = _add(big, "add-checks-1")
    assert r.status_code == 202
    big.tasks.run()
    failed = big.get(f"/api/tasks/{r.json()['task']['id']}").json()
    assert failed["status"] == "failed" and "take that connection's host" in failed["error"]
    assert big.store.controls == []


def test_a_big_add_read_by_host_is_refused_for_a_user_who_is_not_a_manager(big, monkeypatch):
    _job_for_a_big_add(big, monkeypatch)
    monkeypatch.setattr(main, "_manage_refusal", lambda user: "not an app manager")
    monkeypatch.setattr(main, "_reads_a_secret", lambda row: True)
    r = _add(big, "add-checks-2")
    big.tasks.run()
    failed = big.get(f"/api/tasks/{r.json()['task']['id']}").json()
    assert failed["status"] == "failed" and "only an app manager" in failed["error"]
    assert big.store.controls == []


def test_each_chunk_of_a_big_write_is_checked_in_its_own_transaction(big, monkeypatch):
    seen = []
    monkeypatch.setattr(main, "_refuse_job_rows",
                        lambda rows, connection, user, cur=None: seen.append((len(rows), cur is not None)))
    r = _create(big)
    big.tasks.run()
    assert big.get(f"/api/tasks/{r.json()['task']['id']}").json()["status"] == "succeeded"
    assert (150, True) in seen and (150, False) in seen
