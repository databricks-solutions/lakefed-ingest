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
