"""The SQL warehouse client (warehouse.py), against a stand-in for the Statement Execution and Warehouses APIs."""
import pytest
from databricks.sdk.service.sql import (ColumnInfo, ResultData, ResultManifest, ResultSchema, ServiceError,
                                        StatementResponse, StatementState, StatementStatus, State)

import warehouse


def _answer(state, rows=None, columns=("name",), chunks=1, error=None, statement_id="s1"):
    return StatementResponse(
        statement_id=statement_id,
        status=StatementStatus(state=state, error=ServiceError(message=error) if error else None),
        manifest=ResultManifest(schema=ResultSchema(columns=[ColumnInfo(name=c) for c in columns]),
                                total_chunk_count=chunks) if state == StatementState.SUCCEEDED else None,
        result=ResultData(data_array=rows, next_chunk_index=1 if chunks > 1 else None)
        if state == StatementState.SUCCEEDED else None)


class _Statements:
    def __init__(self, answers, chunks=None):
        self.answers, self.chunks, self.sent, self.asked = list(answers), chunks or {}, [], []

    def execute_statement(self, **request):
        self.sent.append(request)
        return self.answers.pop(0)

    def get_statement(self, statement_id):
        self.asked.append(statement_id)
        return self.answers.pop(0)

    def get_statement_result_chunk_n(self, statement_id, chunk_index):
        rows, following = self.chunks[chunk_index]
        return ResultData(data_array=rows, next_chunk_index=following)


class _Warehouses:
    def __init__(self, state=State.RUNNING, error=None):
        self.state, self.error = state, error

    def get(self, id):
        if self.error:
            raise self.error
        return type("Warehouse", (), {"state": self.state})()


class _Workspace:
    def __init__(self, statements, warehouses=None):
        self.statement_execution, self.warehouses = statements, warehouses or _Warehouses()


@pytest.fixture
def workspace(monkeypatch):
    def use(statements, warehouses=None):
        client = _Workspace(statements, warehouses)
        monkeypatch.setattr(warehouse, "workspace", lambda: client)
        monkeypatch.setattr(warehouse, "WAREHOUSE_ID", "wh1")
        return client
    return use


def test_remote_query_sends_the_connection_database_and_query_as_parameters_never_in_the_text():
    text, parameters = warehouse.remote_query_statement("lfc_sql", "master", "SELECT name FROM sys.databases")
    assert text == "SELECT * FROM remote_query(:conn, database => :db, query => :q)"
    assert parameters == [{"name": "conn", "value": "lfc_sql"}, {"name": "db", "value": "master"},
                          {"name": "q", "value": "SELECT name FROM sys.databases"}]


def test_a_statement_is_sent_to_the_configured_warehouse_and_waits_briefly_then_carries_on_in_the_background(workspace):
    client = workspace(_Statements([_answer(StatementState.PENDING)]))
    answer = warehouse.start("SELECT 1", [{"name": "a", "value": "x"}])
    request = client.statement_execution.sent[0]
    assert request["warehouse_id"] == "wh1" and request["statement"] == "SELECT 1"
    assert request["wait_timeout"] == f"{warehouse.START_WAIT_S}s" and request["on_wait_timeout"].value == "CONTINUE"
    assert request["disposition"].value == "INLINE" and request["format"].value == "JSON_ARRAY"
    assert [(p.name, p.value) for p in request["parameters"]] == [("a", "x")]
    assert answer == {"statement_id": "s1", "state": "PENDING", "rows": None, "error": None}


def test_a_statement_sent_with_no_wait_answers_at_once(workspace):
    client = workspace(_Statements([_answer(StatementState.PENDING)]))
    warehouse.start("SELECT 1", [], wait_s=0)
    assert client.statement_execution.sent[0]["wait_timeout"] == "0s"


def test_a_finished_statement_gives_its_rows_by_column_name_across_every_chunk(workspace):
    client = workspace(_Statements([_answer(StatementState.SUCCEEDED, rows=[["db_a"], ["db_b"]], chunks=3)],
                                   chunks={1: ([["db_c"]], 2), 2: ([["db_d"]], None)}))
    answer = warehouse.start("SELECT name", [])
    assert answer["state"] == "SUCCEEDED"
    assert answer["rows"] == [{"name": "db_a"}, {"name": "db_b"}, {"name": "db_c"}, {"name": "db_d"}]
    assert client.statement_execution.asked == []


def test_a_statement_with_no_rows_finishes_with_an_empty_list(workspace):
    workspace(_Statements([_answer(StatementState.SUCCEEDED, rows=None)]))
    assert warehouse.start("SELECT name", [])["rows"] == []


def test_reading_a_statement_again_gives_its_state_then_its_rows(workspace):
    client = workspace(_Statements([_answer(StatementState.RUNNING),
                                    _answer(StatementState.SUCCEEDED, rows=[["x", "1"]], columns=("name", "n"))]))
    assert warehouse.read("s1")["state"] == "RUNNING"
    assert warehouse.read("s1")["rows"] == [{"name": "x", "n": "1"}]
    assert client.statement_execution.asked == ["s1", "s1"]


def test_a_failed_statement_says_why_in_its_first_line(workspace):
    workspace(_Statements([_answer(StatementState.FAILED, error="[REMOTE_QUERY] Login failed for user 'x'.\nmore")]))
    answer = warehouse.start("SELECT 1", [])
    assert answer["state"] == "FAILED" and answer["error"] == "[REMOTE_QUERY] Login failed for user 'x'."
    assert answer["rows"] is None


def test_the_warehouse_state_is_read_for_the_page_and_an_unreadable_one_is_blank(workspace):
    workspace(_Statements([]), _Warehouses(State.STARTING))
    assert warehouse.warehouse_state() == "STARTING"
    workspace(_Statements([]), _Warehouses(error=RuntimeError("no access")))
    assert warehouse.warehouse_state() == ""


def test_no_warehouse_configured_says_so_plainly(monkeypatch):
    monkeypatch.setattr(warehouse, "WAREHOUSE_ID", "")
    assert warehouse.configured() is False
    with pytest.raises(warehouse.NotConfigured, match="WAREHOUSE_ID"):
        warehouse.start("SELECT 1", [])


def test_unit_tests_never_reach_a_workspace_through_the_warehouse_client(monkeypatch):
    monkeypatch.setattr(warehouse, "WAREHOUSE_ID", "wh1")
    with pytest.raises(RuntimeError, match="never reach a Databricks workspace"):
        warehouse.start("SELECT 1", [])


# ---- which warehouse the app's queries run on, and what Manage says of it ------------------------------------------

def test_app_warehouse_id_wins_over_the_bundles_own_warehouse_when_it_is_set():
    assert warehouse.chosen_warehouse({"APP_WAREHOUSE_ID": " mine ", "WAREHOUSE_ID": "own"}) == (
        "mine", "app_warehouse_id")


def test_the_bundles_own_warehouse_is_used_when_app_warehouse_id_is_unset_or_blank():
    assert warehouse.chosen_warehouse({"WAREHOUSE_ID": "own"}) == ("own", "the bundle's own")
    assert warehouse.chosen_warehouse({"APP_WAREHOUSE_ID": "", "WAREHOUSE_ID": "own"}) == ("own", "the bundle's own")
    assert warehouse.chosen_warehouse({"APP_WAREHOUSE_ID": "   ", "WAREHOUSE_ID": "own"}) == ("own", "the bundle's own")
    assert warehouse.chosen_warehouse({}) == ("", "the bundle's own")


def test_the_bundles_id_prefix_is_read_as_the_setting_and_a_bare_prefix_as_unset():
    # The bundle always sends "id:<app_warehouse_id>", since Databricks Apps refuses a blank env value (1 Nov deploy).
    assert warehouse.chosen_warehouse({"APP_WAREHOUSE_ID": "id:", "WAREHOUSE_ID": "own"}) == ("own", "the bundle's own")
    assert warehouse.chosen_warehouse({"APP_WAREHOUSE_ID": "id:  ", "WAREHOUSE_ID": "own"}) == (
        "own", "the bundle's own")
    assert warehouse.chosen_warehouse({"APP_WAREHOUSE_ID": "id:4a07038bb7c29b51", "WAREHOUSE_ID": "own"}) == (
        "4a07038bb7c29b51", "app_warehouse_id")


def test_every_statement_goes_to_the_chosen_warehouse(workspace, monkeypatch):
    client = workspace(_Statements([_answer(StatementState.PENDING)]))
    monkeypatch.setattr(warehouse, "WAREHOUSE_ID", warehouse.chosen_warehouse(
        {"APP_WAREHOUSE_ID": "mine", "WAREHOUSE_ID": "own"})[0])
    warehouse.start("SELECT 1", [])
    assert client.statement_execution.sent[0]["warehouse_id"] == "mine"


class _Described:
    """The Warehouses API's get, answering one warehouse as GetWarehouseResponse, or refusing."""

    def __init__(self, found=None, error=None):
        self.found, self.error, self.asked = found, error, []

    def get(self, id):
        self.asked.append(id)
        if self.error:
            raise self.error
        return self.found


def _describe(monkeypatch, warehouses, chosen_by="app_warehouse_id"):
    from types import SimpleNamespace
    monkeypatch.setattr(warehouse, "workspace", lambda: SimpleNamespace(warehouses=warehouses))
    monkeypatch.setattr(warehouse, "WAREHOUSE_ID", "wh1")
    monkeypatch.setattr(warehouse, "CHOSEN_BY", chosen_by)
    return warehouse.description()


def _found(warehouse_type, serverless, state="RUNNING", name="ops warehouse"):
    from databricks.sdk.service.sql import GetWarehouseResponse
    return GetWarehouseResponse.from_dict({"id": "wh1", "name": name, "warehouse_type": warehouse_type,
                                           "enable_serverless_compute": serverless, "state": state})


def test_a_pro_serverless_warehouse_is_described_with_no_problem(monkeypatch):
    warehouses = _Described(_found("PRO", True, name="lakefed_ingest discovery"))
    assert _describe(monkeypatch, warehouses, "the bundle's own") == {
        "id": "wh1", "chosen_by": "the bundle's own", "name": "lakefed_ingest discovery", "type": "PRO",
        "serverless": True, "state": "RUNNING", "problem": None}
    assert warehouses.asked == ["wh1"]


def test_a_pro_warehouse_on_the_customers_network_is_described_with_no_problem(monkeypatch):
    assert _describe(monkeypatch, _Described(_found("PRO", False, state="STOPPED"))) == {
        "id": "wh1", "chosen_by": "app_warehouse_id", "name": "ops warehouse", "type": "PRO", "serverless": False,
        "state": "STOPPED", "problem": None}


def test_a_classic_warehouse_is_described_with_a_plain_problem_line_and_nothing_refuses_it(monkeypatch):
    described = _describe(monkeypatch, _Described(_found("CLASSIC", False)))
    assert (described["type"], described["serverless"], described["state"]) == ("CLASSIC", False, "RUNNING")
    assert described["problem"] == ("This is a Classic warehouse, and remote_query doesn't run on one: set "
                                    "app_warehouse_id to a Pro or serverless warehouse on version 2025.35 or above.")
    assert warehouse.configured() is True


def test_an_unreadable_warehouse_says_why_and_names_the_grants_job_for_one_of_your_own(monkeypatch):
    refused = _Described(error=PermissionError("PERMISSION_DENIED: no access to warehouse wh1.\ntrace"))
    described = _describe(monkeypatch, refused)
    assert {k: described[k] for k in ("id", "chosen_by", "name", "type", "serverless", "state")} == {
        "id": "wh1", "chosen_by": "app_warehouse_id", "name": None, "type": None, "serverless": None, "state": None}
    assert described["problem"] == ("The warehouse can't be read: PERMISSION_DENIED: no access to warehouse wh1. The "
                                    "grants job (lakefed_ingest_app_grants) gives the app CAN_USE on it.")
    assert _describe(monkeypatch, refused, "the bundle's own")["problem"] == (
        "The warehouse can't be read: PERMISSION_DENIED: no access to warehouse wh1.")


def test_no_warehouse_at_all_is_described_without_asking_databricks(monkeypatch):
    monkeypatch.setattr(warehouse, "WAREHOUSE_ID", "")
    monkeypatch.setattr(warehouse, "CHOSEN_BY", "the bundle's own")
    described = warehouse.description()
    assert described["id"] is None and described["name"] is None
    assert described["problem"].startswith("No SQL warehouse is set")
