"""A stored login goes only to the host of the connection it was typed for: each read through a JDBC connection is
checked when it is made, not only when the connection is. A discovery's keys, a job's rows, an edit of a row and a
connection's secret scope are each tried the way an attacker would, and refused; the normal path still works. A stubbed
database, no app run; the SQL's own round trip is jdbc_connection_reads_pg_test.py."""
import contextlib
import json
from datetime import datetime, timezone

import pytest

import jdbc_connections as jc
import jdbc_logins as jl
import queries as q

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import jobs  # noqa: E402
import main  # noqa: E402

CREATED = datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)
LOGIN_SCOPE = "lakefed_ingest_jdbc_logins"
# Mallory's connection: in the login scope, one login per database, her own host.
EVIL = {"name": "evil", "src_type": "sqlserver", "src_host": "attacker.example", "src_port": 1433,
        "secret_scope": LOGIN_SCOPE, "secret_key": "{database}", "trust_server_certificate": "false",
        "created_by": "mallory@example.com", "created_at": CREATED}
# Ann's connection, whose login for her database 'sales' is under the key 'creds-sales'.
ORDERS = {**EVIL, "name": "orders jdbc", "src_host": "orders.internal.example", "secret_key": "creds-{database}",
          "created_by": "ann@example.com"}


def _row(**fields) -> dict:
    """A control row read through the orders connection, with some fields changed."""
    return {"id": 1, "task_collection": "orders_g01", "src_type": "sqlserver", "src_host": "orders.internal.example",
            "src_port": 1433, "secret_key": "creds-sales", "use_remote_query": False, "src_connection": None,
            "src_database": "sales", "src_schema": "dbo", "src_table": "orders", **fields}


@pytest.fixture
def stub(monkeypatch):
    """The database as the reads see it: the stored connections, the newest login per key and which connection each
    job reads through; the manager check by user; the discover job's runs."""
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    state = {"connections": {"evil": EVIL, "orders jdbc": ORDERS},
             "logins": {"creds-sales": "orders jdbc", "x": "evil"},
             "managers": set(), "run_calls": [], "job_connection": {5: "orders jdbc"}}
    monkeypatch.setattr(main, "_manage_refusal",
                        lambda user: None if user in state["managers"] else "not an app manager")
    monkeypatch.setattr(main, "_refuse_run_of", lambda *args, **kwargs: None)

    def query(sql, params=()):
        if sql == jc.get_sql():
            return [state["connections"][params[0]]] if params[0] in state["connections"] else []
        if sql == q.jdbc_login_latest_per_key_sql():
            scope, keys = params
            return [{"secret_key": key, "connection": state["logins"][key]} for key in keys
                    if scope == LOGIN_SCOPE and key in state["logins"]]
        if sql == q.schedules_sql():
            return [{"id": job_id} for job_id in state["job_connection"]]
        if sql == q.schedule_members_sql():
            return [{"schedule_id": job_id, "task_collection": "orders_g01", "removed_at": None}
                    for job_id in params[0] if job_id in state["job_connection"]]
        return []

    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main, "_job_connection_row",
                        lambda job_id: state["connections"].get(state["job_connection"].get(int(job_id))))
    monkeypatch.setattr(jobs, "DISCOVER_JOB_ID", 7)
    monkeypatch.setattr(jobs, "run_now", lambda job_id, params: state["run_calls"].append((job_id, params)) or 99)
    return state


def _client(user="mallory@example.com"):
    return TestClient(main.app, headers={"X-Forwarded-Email": user})


def _errors(response) -> str:
    return " ".join(response.json()["detail"]["errors"])


# ---- N1 route A: a {database} connection naming another user's key ---------------------------------------------------

def test_a_discovery_through_a_database_key_connection_naming_another_users_key_is_refused(stub):
    r = _client().post("/api/discoveries", json={"jdbc_connection": "evil", "databases": "creds-sales"})
    assert r.status_code == 422 and "creds-sales" in _errors(r) and "was not typed for the connection evil" in _errors(r)
    assert stub["run_calls"] == []


def test_a_discovery_naming_a_key_with_no_login_at_all_is_refused(stub):
    r = _client().post("/api/discoveries", json={"jdbc_connection": "evil", "databases": "x, nothing-here"})
    assert r.status_code == 422 and "nothing-here" in _errors(r) and stub["run_calls"] == []


def test_a_discovery_whose_keys_hold_its_own_logins_starts(stub):
    r = _client("ann@example.com").post("/api/discoveries", json={"jdbc_connection": "orders jdbc", "databases": "sales"})
    assert r.status_code == 200, r.text
    spec = json.loads(stub["run_calls"][0][1]["spec"])
    assert spec["src_host"] == "orders.internal.example" and spec["secret_key_template"] == "creds-{database}"


def test_a_key_whose_newest_login_was_typed_for_another_connection_is_refused_even_after_an_older_one(stub):
    stub["logins"]["creds-sales"] = "evil"     # the newest row decides: an older row naming orders jdbc doesn't count
    with pytest.raises(HTTPException) as refused:
        main._jdbc_discovery("orders jdbc", ["sales"])
    assert refused.value.status_code == 422


def test_a_whole_server_through_a_connection_with_one_login_per_database_is_refused(stub):
    with pytest.raises(HTTPException) as refused:
        main._jdbc_discovery("orders jdbc", "*")
    assert "a whole server can't be read" in str(refused.value.detail)


def test_a_whole_server_through_a_connection_with_one_login_checks_that_login(stub):
    stub["connections"]["one login"] = {**ORDERS, "name": "one login", "secret_key": "orders-login"}
    with pytest.raises(HTTPException):
        main._jdbc_discovery("one login", "*")
    stub["logins"]["orders-login"] = "one login"
    spec, _ = main._jdbc_discovery("one login", "*")
    assert spec["secret_key_template"] == "orders-login"


def test_the_whole_server_marker_named_by_hand_is_a_database_name_and_its_key_is_checked():
    assert main._database_keys(ORDERS, ["*", "hr"]) == ["creds-*", "creds-hr"]
    assert main._database_keys(ORDERS, "*") == ["creds-master"]


def test_a_rediscovery_through_a_connection_takes_the_connection_as_stored_and_checks_its_keys(stub):
    spec = {"jdbc_connection": "orders jdbc", "src_host": "old.example", "databases": ["sales"], "kind": "tables"}
    read, parameters = main._rediscovery_reading(spec)
    assert read["src_host"] == "orders.internal.example" and parameters["secret_scope"] == LOGIN_SCOPE
    with pytest.raises(HTTPException):
        main._rediscovery_reading({**spec, "databases": ["creds-hr"]})
    assert main._rediscovery_reading({"src_connection": "uc", "databases": "*"}) == (
        {"src_connection": "uc", "databases": "*"}, {})


def test_rows_written_into_a_jdbc_job_need_their_keys_typed_for_its_connection(stub):
    main._refuse_connection_rows(ORDERS, [_row()])
    with pytest.raises(HTTPException) as refused:
        main._refuse_connection_rows(ORDERS, [_row(src_database="hr", secret_key="creds-hr")])
    assert "creds-hr" in str(refused.value.detail)


def test_rows_written_into_a_jdbc_job_must_read_its_host_port_type_and_key_template(stub):
    for changed in ({"src_host": "attacker.example"}, {"src_port": 1444}, {"src_type": "postgresql"},
                    {"secret_key": "other-sales"}, {"use_remote_query": True, "src_connection": "uc"}):
        with pytest.raises(HTTPException) as refused:
            main._refuse_connection_rows(ORDERS, [_row(**changed)])
        assert "take that connection's host" in str(refused.value.detail), changed
    main._refuse_connection_rows(ORDERS, [_row(src_port=None, src_host="Orders.Internal.Example")])


def test_a_table_added_by_hand_into_a_jdbc_jobs_group_is_held_to_its_connection(stub):
    with pytest.raises(HTTPException) as refused:
        main._refuse_rows_off_connections([_row(src_host="attacker.example")])
    assert refused.value.status_code == 422
    main._refuse_rows_off_connections([_row(task_collection="other_g01", src_host="attacker.example")])


# ---- N1 route B: an edit of a JDBC job's row --------------------------------------------------------------------

class _Cursor:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append(sql)


@pytest.mark.parametrize("field, value", [("secret_key", "creds-hr"), ("src_host", "attacker.example"),
                                          ("src_port", 1444), ("src_type", "postgresql"),
                                          ("use_remote_query", True), ("src_connection", "uc")])
def test_an_edit_of_a_jdbc_jobs_row_source_or_key_is_refused_before_anything_is_written(stub, field, value):
    row = _row()
    cur = _Cursor()
    plans = [{"id": 1, "changes": {field: [row[field], value]}, "reseed": None, "errors": []}]
    with pytest.raises(HTTPException) as refused:
        main._apply_plans(cur, [row], plans, "mallory@example.com", "b1")
    assert refused.value.status_code == 422 and field in str(refused.value.detail)
    assert cur.statements == []


def test_a_row_moved_into_a_jdbc_jobs_group_must_read_through_its_connection(stub):
    row = _row(task_collection="other_g01", src_host="attacker.example")
    plans = [{"id": 1, "changes": {"task_collection": ["other_g01", "orders_g01"]}, "reseed": None, "errors": []}]
    with pytest.raises(HTTPException) as refused:
        main._refuse_connection_edits([row], plans)
    assert "take that connection's host" in str(refused.value.detail)


def test_other_edits_of_a_jdbc_jobs_row_and_any_edit_of_another_jobs_row_pass(stub):
    main._refuse_connection_edits([_row()], [{"id": 1, "changes": {"priority": [0, 5]}}])
    main._refuse_connection_edits([_row(task_collection="other_g01")],
                                  [{"id": 1, "changes": {"src_host": ["a.example", "b.example"]}}])


def test_the_tables_edit_route_refuses_a_key_edit_on_a_jdbc_jobs_row(stub, monkeypatch):
    cur = _Cursor()
    monkeypatch.setattr(main, "_write", contextlib.contextmanager(lambda: (yield cur)))
    monkeypatch.setattr(main, "_refuse_rows_groups", lambda ids, request, also=(): {"orders_g01"})
    monkeypatch.setattr(main, "_refuse_groups_outside_jobs", lambda groups: None)
    full = _row(select_list="id, total", sink_catalog="main", sink_schema="sales", sink_table="orders", load_type="full")
    monkeypatch.setattr(main, "_locked_rows", lambda cur, ids: [full])
    monkeypatch.setattr(main, "_recheck_locked_rows", lambda rows, checked, request: None)
    monkeypatch.setattr(main, "_lock_collections", lambda cur, groups: None)
    r = _client().post("/api/tables/edit", json={"ids": [1], "changes": {"secret_key": "creds-hr"}, "dry_run": False})
    assert r.status_code == 422 and "secret_key" in _errors(r)
    assert cur.statements == []


# ---- #2: a connection in a scope other than the login scope ------------------------------------------------------

def test_a_connection_a_non_manager_made_in_another_scope_is_refused_when_it_is_read(stub):
    stub["connections"]["old"] = {**ORDERS, "name": "old", "secret_scope": "lakefed_ingest",
                                  "created_by": "mallory@example.com"}
    r = _client().post("/api/discoveries", json={"jdbc_connection": "old", "databases": "sales"})
    assert r.status_code == 422 and "only when an app manager makes it" in _errors(r)
    assert stub["run_calls"] == []
    with pytest.raises(HTTPException):
        main._refuse_connection_rows(stub["connections"]["old"], [_row()])


def test_a_connection_with_no_maker_on_record_in_another_scope_is_refused(stub):
    stub["connections"]["unknown"] = {**ORDERS, "name": "unknown", "secret_scope": "lakefed", "created_by": None}
    with pytest.raises(HTTPException):
        main._jdbc_discovery("unknown", ["sales"])


def test_a_connection_a_manager_made_in_another_scope_is_read_and_its_keys_are_not_login_checked(stub):
    stub["managers"].add("ann@example.com")
    stub["connections"]["admin"] = {**ORDERS, "name": "admin", "secret_scope": "lakefed"}
    spec, parameters = main._jdbc_discovery("admin", ["hr"])
    assert spec["src_host"] == "orders.internal.example" and parameters["secret_scope"] == "lakefed"
    main._refuse_connection_rows(stub["connections"]["admin"], [_row(secret_key="creds-hr", src_database="hr")])
