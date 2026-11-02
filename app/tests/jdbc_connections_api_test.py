"""JDBC connections through the API, with a stubbed database: making a connection (any signed-in user, names only,
a taken name refused), listing them (never a secret), a discovery through one (its host and key from the connection,
its scope and trust from the connection, never from the request), a job or a table set that mixes a JDBC source
with another, and the job page's JDBC block. No database is needed; the Postgres round trip is
jdbc_connections_pg_test.py."""
import contextlib
import json
from datetime import datetime, timezone

import pytest

import jdbc_connections as jc
import jdbc_logins as jl
import queries as q
import rediscovery as rd

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import jobs  # noqa: E402
import main  # noqa: E402

CREATED = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
LOGIN_SCOPE = "lakefed_ingest_jdbc_logins"
CONNECTION = {"name": "orders jdbc", "src_type": "sqlserver", "src_host": "orders.internal.example", "src_port": 1433,
              "secret_scope": "lakefed", "secret_key": "orders-login", "trust_server_certificate": "false",
              "created_by": "ann", "created_at": CREATED}
BODY = {"name": "orders jdbc", "src_type": "sqlserver", "src_host": "orders.internal.example", "src_port": 1433,
        "secret_scope": "lakefed", "secret_key": "orders-login", "trust_server_certificate": False}


class _Cursor:
    def __init__(self, store):
        self.store, self.last = store, None

    def execute(self, sql, params=()):
        self.store.append((sql, params))
        self.last = params

    def fetchone(self):
        return None if self.store.taken else (self.last[0],)


class _Store(list):
    taken = False


@pytest.fixture
def stub(monkeypatch):
    """Patches the database and the manage check. Returns a dict: the statements written, the reads, and the knobs."""
    state = {"writes": _Store(), "rows": {}, "run_calls": [], "manager": True}
    monkeypatch.setattr(main, "_manage_refusal", lambda user: None if state["manager"] else "not an app manager")
    monkeypatch.setattr(main, "_refuse_run_of", lambda *args, **kwargs: None)
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield _Cursor(state["writes"]))))

    def query(sql, params=()):
        if sql == jc.list_sql():
            return [CONNECTION]
        if sql == jc.get_sql():
            return [state["rows"][params[0]]] if params[0] in state["rows"] else []
        return state.get("reads", {}).get(sql, [])

    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(jobs, "DISCOVER_JOB_ID", 7)
    monkeypatch.setattr(jobs, "run_now", lambda job_id, params: state["run_calls"].append((job_id, params)) or 99)
    state["rows"]["orders jdbc"] = CONNECTION
    return state


def _client():
    return TestClient(main.app, headers={"X-Forwarded-Email": "ann@example.com"})


def test_a_connection_in_the_login_scope_is_made_only_with_a_login_its_maker_typed(stub, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    body = {**BODY, "secret_scope": LOGIN_SCOPE}
    r = _client().post("/api/jdbc-connections", json=body)
    assert r.status_code == 422 and "needs its login first" in r.json()["detail"]["errors"][0]
    assert stub["writes"] == []
    stub["reads"] = {q.jdbc_connection_login_sql(): [{"secret_key": "orders-login"}]}
    assert _client().post("/api/jdbc-connections", json=body).status_code == 200
    assert stub["writes"][0][1][4] == LOGIN_SCOPE


def test_a_login_under_a_key_the_connection_does_not_make_is_not_its_login(stub, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    stub["reads"] = {q.jdbc_connection_login_sql(): [{"secret_key": "someone-else"}]}
    r = _client().post("/api/jdbc-connections", json={**BODY, "secret_scope": LOGIN_SCOPE})
    assert r.status_code == 422 and "needs its login first" in r.json()["detail"]["errors"][0]


def test_a_connection_in_another_scope_needs_no_login(stub, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    assert _client().post("/api/jdbc-connections", json=BODY).status_code == 200


def test_the_login_a_connection_needs_is_matched_by_its_maker_and_its_key_template(monkeypatch):
    seen = []
    typed = [{"secret_key": "other-sales"}, {"secret_key": "creds-sales"}]
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): seen.append((sql, params)) or typed)
    assert main._login_typed_for({"name": "orders jdbc", "secret_key": "creds-{database}"}, "ann@example.com") is True
    assert seen == [(q.jdbc_connection_login_sql(), (LOGIN_SCOPE, "orders jdbc", "ann@example.com"))]
    assert main._login_typed_for({"name": "orders jdbc", "secret_key": "hr-{database}"}, "ann@example.com") is False


def test_a_job_that_reads_through_a_connection_may_name_the_login_scope(monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    main._refuse_login_scope({"secret_scope": LOGIN_SCOPE}, CONNECTION)
    main._refuse_login_scope({"secret_scope": "lakefed"}, None)
    with pytest.raises(HTTPException) as refused:
        main._refuse_login_scope({"secret_scope": LOGIN_SCOPE}, None)
    assert refused.value.status_code == 422


def test_a_manager_makes_a_connection_and_the_store_gets_names_and_no_secret(stub):
    r = _client().post("/api/jdbc-connections", json=BODY)
    assert r.status_code == 200
    sql, params = stub["writes"][0]
    assert sql == jc.insert_sql() and params == ("orders jdbc", "sqlserver", "orders.internal.example", 1433, "lakefed",
                                               "orders-login", "false", "ann@example.com")
    assert r.json()["connection"]["trust"] == "verify"


def test_a_signed_in_user_who_is_not_a_manager_makes_a_connection_in_the_login_scope(stub, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    stub["manager"] = False
    stub["reads"] = {q.jdbc_connection_login_sql(): [{"secret_key": "orders-login"}]}
    r = _client().post("/api/jdbc-connections", json={**BODY, "secret_scope": LOGIN_SCOPE})
    assert r.status_code == 200, r.text
    assert stub["writes"][0][1][-1] == "ann@example.com"


def test_a_user_who_is_not_a_manager_may_not_name_another_scope(stub, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    stub["manager"] = False
    r = _client().post("/api/jdbc-connections", json={**BODY, "secret_scope": "lakefed_ingest",
                                                      "secret_key": "creds-{database}", "src_host": "attacker.example"})
    assert r.status_code == 422 and "only when an app manager makes it" in r.json()["detail"]["errors"][0]
    assert stub["writes"] == []


def test_a_call_with_no_signed_in_user_makes_no_connection(stub):
    r = TestClient(main.app).post("/api/jdbc-connections", json=BODY)
    assert r.status_code == 403 and r.json()["detail"] == main.NO_IDENTITY
    assert stub["writes"] == []


def test_an_invalid_connection_is_refused_with_its_reasons_and_nothing_is_written(stub):
    r = _client().post("/api/jdbc-connections", json={**BODY, "secret_scope": "", "src_port": 70000})
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert any("scope" in e for e in errors) and any("port" in e for e in errors)
    assert stub["writes"] == []


def test_a_name_already_taken_is_refused_and_not_replaced(stub):
    stub["writes"].taken = True
    r = _client().post("/api/jdbc-connections", json=BODY)
    assert r.status_code == 409 and "already exists" in r.json()["detail"]


def test_the_list_names_each_connection_and_shows_no_secret_value(stub):
    body = _client().get("/api/jdbc-connections").json()
    shown = body["connections"][0]
    assert shown["secret_scope"] == "lakefed" and shown["secret_key"] == "orders-login" and shown["trust"] == "verify"
    assert "password" not in json.dumps(body).lower()


def test_a_discovery_through_a_connection_takes_its_host_key_scope_and_trust_from_the_connection(stub):
    stub["rows"]["orders jdbc"] = {**CONNECTION, "trust_server_certificate": "true"}
    r = _client().post("/api/discoveries", json={"databases": "sales, hr", "jdbc_connection": "orders jdbc",
                                                  "src_host": "attacker.example", "secret_key_template": "other"})
    assert r.status_code == 200
    _, params = stub["run_calls"][0]
    spec = json.loads(params["spec"])
    assert (spec["src_type"], spec["src_host"], spec["src_port"], spec["secret_key_template"]) == (
        "sqlserver", "orders.internal.example", 1433, "orders-login")
    assert spec["jdbc_connection"] == "orders jdbc" and spec["databases"] == ["sales", "hr"]
    assert params["secret_scope"] == "lakefed" and params["trust_server_certificate"] == "true"


def test_a_discovery_that_names_no_connection_keeps_the_job_defaults_and_needs_a_host(stub):
    r = _client().post("/api/discoveries", json={"databases": "sales", "src_host": "h1"})
    assert r.status_code == 200 and "secret_scope" not in stub["run_calls"][0][1]
    assert _client().post("/api/discoveries", json={"databases": "sales"}).status_code == 422


def test_a_discovery_through_a_connection_that_is_not_there_is_refused(stub):
    r = _client().post("/api/discoveries", json={"databases": "sales", "jdbc_connection": "nope"})
    assert r.status_code == 422 and stub["run_calls"] == []


def test_a_selection_mixing_a_jdbc_discovery_with_another_is_refused(stub):
    stub["reads"] = {q.selection_discoveries_sql(): [{"listing": {"jdbc_connection": "orders jdbc"}},
                                                     {"listing": {}}]}
    with pytest.raises(HTTPException) as refused:
        main._jdbc_source(main.Selection(discovery_ids=["d1", "d2"], whole_server=True))
    assert refused.value.status_code == 422 and "one source" in str(refused.value.detail)


def test_a_selection_of_one_connections_discoveries_is_that_connection(stub):
    stub["reads"] = {q.selection_discoveries_sql(): [{"listing": {"jdbc_connection": "orders jdbc"}},
                                                     {"listing": {"jdbc_connection": "orders jdbc"}}]}
    assert main._jdbc_source(main.Selection(discovery_ids=["d1", "d2"], whole_server=True))["name"] == "orders jdbc"


def test_a_selection_of_plain_discoveries_or_an_upload_is_no_connection(stub):
    stub["reads"] = {q.selection_discoveries_sql(): [{"listing": {"pattern": "a%"}}]}
    assert main._jdbc_source(main.Selection(discovery_ids=["d1"], whole_server=True)) is None
    assert main._jdbc_source(main.Selection(upload_id="u1")) is None


def test_adding_tables_from_a_jdbc_discovery_into_a_job_is_refused_plainly(stub):
    stub["reads"] = {q.selection_discoveries_sql(): [{"listing": {"jdbc_connection": "orders jdbc"}}]}
    r = _client().post("/api/ingestion-jobs/5/tables", json={"source": {"discovery_id": "d1", "whole_server": True}})
    assert r.status_code == 422 and "a job of their own" in r.json()["detail"]["errors"][0]


def test_the_job_page_names_its_jdbc_connection_with_no_secret(stub):
    stub["reads"] = {q.job_selection_sql(): [{"source_kind": "discovery", "source_id": "d1,d2", "scope": "server"}],
                     q.selection_discoveries_sql(): [{"listing": {"jdbc_connection": "orders jdbc"}}]}
    shown = main._job_jdbc(5)
    assert shown["src_host"] == "orders.internal.example" and shown["secret_scope"] == "lakefed"
    assert shown["secret_key"] == "orders-login" and shown["trust"] == "verify"


def test_the_tree_shows_the_jdbc_block_at_the_top_level_only(stub, monkeypatch):
    stub["reads"] = {q.job_selection_sql(): [{"source_kind": "discovery", "source_id": "d1", "scope": "server"}],
                     q.selection_discoveries_sql(): [{"listing": {"jdbc_connection": "orders jdbc"}}]}
    monkeypatch.setattr(main, "_job_groups", lambda job_id: ["sales"])
    monkeypatch.setattr(main.db, "query", _dispatch(stub, {q.job_servers_sql(): [], q.job_databases_sql(): []}))
    top = _client().get("/api/ingestion-jobs/5/tree").json()
    assert top["jdbc"]["secret_scope"] == "lakefed"
    below = _client().get("/api/ingestion-jobs/5/tree", params={"server": "orders.internal.example"}).json()
    assert below["jdbc"] is None


def _dispatch(stub, extra):
    def query(sql, params=()):
        if sql in extra:
            return extra[sql]
        if sql == jc.get_sql():
            return [stub["rows"][params[0]]]
        return stub.get("reads", {}).get(sql, [])
    return query


def test_a_job_that_reads_through_no_connection_shows_no_jdbc_block(stub):
    stub["reads"] = {q.job_selection_sql(): [{"source_kind": "upload", "source_id": "u1", "scope": "server"}]}
    assert main._job_jdbc(5) is None


def test_a_rediscovery_spec_keeps_the_connection_name_for_its_discovery_run():
    parts = [{"source_kind": "discovery", "server": "orders.internal.example", "scope": "server", "src_database": None}]
    sources = [{"server": "orders.internal.example", "src_host": "orders.internal.example", "src_port": 1433,
                "src_type": "sqlserver", "src_connection": None, "secret_key": "orders-login", "src_database": "sales",
                "finished_at": "2026-10-30 14:00", "listing": {"secret_key_template": "orders-login",
                                                               "jdbc_connection": "orders jdbc", "databases": "*"}}]
    readings, errors = rd.specs(parts, sources)
    assert errors == [] and readings[0][1]["jdbc_connection"] == "orders jdbc"
    assert readings[0][1]["src_host"] == "orders.internal.example" and readings[0][1]["secret_key_template"] == "orders-login"
