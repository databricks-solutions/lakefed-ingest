"""The connection-first CSV: an upload read through the connection chosen with it (new_job.read_upload), the tables of
its database-only lines found by name (main._resolve_databases), the table rows that win over a discovered table
(main._with_databases), and the Create and Add routes around them. Fakes only: no database, workspace or Databricks
call, and the discovery run is a stub that the test ends."""
import json

import pytest

import main
import new_job as nj
import queries as q
from background_task_test import client  # noqa: F401  (the fixture the route tests use)
from new_job_test import FAN_IN

HEAD = "src_database,src_schema,src_table,select_list,primary_key\n"
DETAILS = {"select_list": "id, a", "primary_key": "id"}


@pytest.fixture(autouse=True)
def _no_database_reads_kept():
    main._database_reads.clear()
    yield
    main._database_reads.clear()


# ---- the file, read through its connection --------------------------------------------------------------------------

def test_a_file_is_read_through_the_connection_it_was_chosen_with():
    read = nj.read_upload(HEAD + 'eng_001,gen,t1,"id, a",id\neng_001,,,,\neng_002,gen,t9,id,id\n', "SQL_A")
    assert read["errors"] == [] and read["connection"] == "SQL_A"
    assert [(line["line"], line["status"]) for line in read["lines"]] == [(2, "ok"), (3, "database"), (4, "ok")]
    assert {line["server"] for line in read["lines"]} == {"sql_a"}
    assert read["lines"][0]["fields"]["src_connection"] == "SQL_A"


def test_a_file_naming_another_connection_is_refused_with_each_line_named():
    text = "src_connection,src_database,src_schema,src_table\nsql_a,eng_001,gen,t1\nsql_b,eng_001,gen,t2\n"
    read = nj.read_upload(text, "sql_a")
    assert read["lines"] == []
    assert read["errors"] == ["1 line names another connection: line 3 names sql_b, not sql_a"]


def test_a_file_with_a_refused_column_or_no_database_column_is_refused():
    assert nj.read_upload("src_database,src_host\neng,h\n", "sql_a")["errors"] == [
        "column src_host is not accepted: the file reads through the connection chosen before it, and the job sets "
        "its groups"]
    assert nj.read_upload("src_schema,src_table\ndbo,t\n", "sql_a")["errors"] == ["the file has no src_database column"]


def test_a_table_line_and_a_database_line_in_one_file_are_both_kept():
    read = nj.read_upload(HEAD + "eng_001,gen,t1,id,id\neng_002,,,,\n", "sql_a")
    assert [line["status"] for line in read["lines"]] == ["ok", "database"]


def test_a_database_named_twice_or_a_table_without_its_database_is_invalid_with_why():
    read = nj.read_upload(HEAD + "eng_001,,,,\neng_001,,,,\n,gen,t1,,\n", "sql_a")
    assert [(line["line"], line["status"]) for line in read["lines"]] == [(2, "database"), (3, "invalid"), (4, "invalid")]
    assert read["lines"][1]["errors"] == ["database eng_001 is already named on line 2: it is taken once"]
    assert read["lines"][2]["errors"] == ["src_database is required"]


# ---- the database-only lines, discovered by name --------------------------------------------------------------------

def test_database_lines_are_discovered_by_name_through_the_connection(monkeypatch):
    discovery = _patch_discovery(monkeypatch, statuses=OK_BOTH, rows=ROWS_BOTH, ended={"result": "SUCCESS"})
    resolved = main._resolve_databases(_database_lines(("eng_001", 3), ("eng_002", 4)), wait=False)
    assert resolved["discovering"] is False and resolved["unreadable"] == []
    assert [(r["line"], r["src_database"], r["src_table"]) for r in resolved["rows"]] == [
        (3, "eng_001", "t1"), (3, "eng_001", "t2"), (4, "eng_002", "t9")]
    spec = json.loads(discovery.runs[0]["spec"])
    assert (spec["src_connection"], spec["kind"], spec["databases"]) == ("sql_a", "tables", ["eng_001", "eng_002"])


def test_a_preview_asks_again_while_the_discovery_runs_and_shares_its_run(monkeypatch):
    discovery = _patch_discovery(monkeypatch, statuses=OK_BOTH, rows=ROWS_BOTH)
    selected = _database_lines(("eng_001", 3))
    assert main._resolve_databases(selected, wait=False)["discovering"] is True
    assert main._resolve_databases(selected, wait=False)["discovering"] is True
    assert len(discovery.runs) == 1


def test_a_database_line_takes_its_databases_table_rows_whatever_is_picked(monkeypatch):
    def query(sql, params=()):
        if sql == q.upload_sql():
            return [{"upload_id": "u1", "columns": list(nj.UPLOAD_COLUMNS)}]
        if sql == q.upload_database_lines_sql():
            return [{"line": 3, "src_database": "eng_001", "fields": {"src_connection": "sql_a", "src_database": "eng_001"}}]
        if sql == q.selected_rows_page_sql("upload"):
            taken = q.selection_key("sql_a", "eng_001") in params[3]
            return [_table_row(2, "eng_001", "t1", "id")] if taken else []
        return []

    monkeypatch.setattr(main.db, "query", query)
    selected = main._selected(main.Selection(upload_id="u1"))
    assert [r["src_table"] for r in selected["rows"]] == ["t1"]
    found = {"rows": [_found(3, "eng_001", "t1", "id, a"), _found(3, "eng_001", "t2", "id")], "unreadable": []}
    out = main._with_databases(selected, found)
    assert (out["covered"], out["discovered"]) == (1, 1) and "id, a" not in out["text"]


def test_a_database_that_cannot_be_opened_fails_alone_and_is_named(monkeypatch):
    statuses = [{"src_database": "eng_001", "status": "ok", "error": None},
                {"src_database": "eng_002", "status": "failed", "error": "login failed for the service principal"}]
    _patch_discovery(monkeypatch, statuses=statuses, rows=ROWS_BOTH, ended={"result": "SUCCESS"})
    resolved = main._resolve_databases(_database_lines(("eng_001", 3), ("eng_002", 4)), wait=False)
    assert resolved["unreadable"] == [{"line": 4, "database": "eng_002", "error": "login failed for the service principal"}]
    assert [r["src_table"] for r in resolved["rows"]] == ["t1", "t2"]


def test_a_discovery_run_that_failed_names_every_database_and_is_not_kept(monkeypatch):
    discovery = _patch_discovery(monkeypatch, ended={"result": "FAILED", "message": "cluster lost"})
    resolved = main._resolve_databases(_database_lines(("eng_001", 3)), wait=False)
    assert resolved["rows"] == [] and resolved["unreadable"] == [
        {"line": 3, "database": "eng_001", "error": "its discovery run ended FAILED: cluster lost"}]
    assert main._database_reads == {}
    assert len(discovery.runs) == 1


def test_a_table_row_wins_over_the_discovered_row_it_covers(monkeypatch):
    explicit = _table_row(2, "eng_001", "t1", select_list="id")
    found = {"rows": [_found(3, "eng_001", "t1", "id, a"), _found(3, "eng_001", "t2", "id")], "unreadable": []}
    out = main._with_databases({"rows": [explicit], "columns": list(nj.UPLOAD_COLUMNS), "lines": [2]}, found)
    assert (out["covered"], out["discovered"], out["lines"]) == (1, 1, [2, 3])
    assert "eng_001,gen,t1,id,id" in out["text"] and "id, a" not in out["text"]


# ---- the routes: a preview says discovering, Create and Add are background tasks ------------------------------------

def test_an_upload_answers_its_tables_databases_and_the_line_that_cant_be_taken(client):
    body = {"csv": HEAD + "eng_001,gen,t1,,\neng_002,,,,\neng_002,,,,\n", "connection": "sql_a", "file_name": "f.csv"}
    r = client.post("/api/uploads", json=body)
    assert r.status_code == 200
    assert (r.json()["tables"], r.json()["databases"], r.json()["invalid"]) == (1, 1, 1)
    assert r.json()["invalid_lines"] == [{"line": 4, "table": "eng_002", "errors": [
        "database eng_002 is already named on line 3: it is taken once"]}]


def test_an_upload_naming_another_connection_is_refused_before_anything_is_kept(client):
    r = client.post("/api/uploads", json={"csv": "src_connection,src_database\nsql_b,eng\n", "connection": "sql_a"})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"] == ["1 line names another connection: line 2 names sql_b, not sql_a"]
    assert client.db.statements == []


def test_a_preview_of_database_lines_says_discovering_and_then_counts_their_tables(client, monkeypatch):
    discovery = _patch_discovery(monkeypatch, statuses=OK_BOTH, rows=ROWS_BOTH, answers=_UPLOAD,
                                 fallback=client.tasks.query)
    body = {"name": "Ref", "settings": FAN_IN, "source": {"upload_id": "u1"}}
    r = client.post("/api/ingestion-jobs", json=body)
    assert r.json()["discovering"] is True and r.json()["summary"] is None and r.json()["databases"] == 1
    discovery.ended = {"result": "SUCCESS"}
    summary = client.post("/api/ingestion-jobs", json=body).json()["summary"]
    assert (summary["tables"], summary["discovered"], summary["covered"]) == (3, 2, 0)
    assert summary["invalid"] == []


def test_a_create_with_database_lines_is_a_background_task_that_discovers_then_writes(client, monkeypatch):
    discovery = _patch_discovery(monkeypatch, statuses=OK_BOTH, rows=ROWS_BOTH, answers=_UPLOAD,
                                 fallback=client.tasks.query, ended={"result": "SUCCESS"})
    body = {"name": "Ref", "settings": FAN_IN, "source": {"upload_id": "u1"}, "dry_run": False, "request_id": "r1"}
    r = client.post("/api/ingestion-jobs", json=body)
    assert r.status_code == 202 and r.json()["created"] is False
    client.tasks.run()
    stages = [json.loads(params[0])["stage"] for sql, params in client.tasks.executed if sql == q.task_progress_json_sql()]
    assert stages == ["discovering", "writing"]
    task = client.tasks.rows[1]
    assert task["status"] == "succeeded" and task["result"]["summary"]["tables"] == 3
    assert len(discovery.runs) == 1


def test_a_database_that_cannot_be_opened_is_named_as_an_invalid_line_and_blocks_the_create(client, monkeypatch):
    statuses = [{"src_database": "eng_001", "status": "ok", "error": None},
                {"src_database": "eng_002", "status": "failed", "error": "login failed"}]
    two_databases = {**_UPLOAD, q.upload_database_lines_sql(): [
        {"line": 3, "src_database": "eng_001", "fields": {"src_connection": "sql_a", "src_database": "eng_001"}},
        {"line": 4, "src_database": "eng_002", "fields": {"src_connection": "sql_a", "src_database": "eng_002"}}]}
    _patch_discovery(monkeypatch, statuses=statuses, rows=ROWS_BOTH, answers=two_databases,
                     fallback=client.tasks.query, ended={"result": "SUCCESS"})
    body = {"name": "Ref", "settings": FAN_IN, "source": {"upload_id": "u1"}, "dry_run": False, "request_id": "r2"}
    client.post("/api/ingestion-jobs", json=body)
    client.tasks.run()
    task = client.tasks.rows[1]
    assert task["status"] == "failed" and task["error"] == "line 4 (eng_002): login failed"
    assert not any(sql == q.insert_schedule_sql() for sql, _ in client.tasks.executed)


def test_a_background_create_whose_lines_are_all_invalid_ends_failed_and_names_them(client, monkeypatch):
    # A kept deleted job of the same name still holds the groups this job's tables would go to, so every line is
    # refused. The task must end failed with those lines, not succeed with no job.
    taken = {q.collections_sql(): [{"task_collection": name} for name in nj.group_names("Ref", "sql_a", 99)]}
    _patch_discovery(monkeypatch, statuses=OK_BOTH, rows=ROWS_BOTH, answers={**_UPLOAD, **taken},
                     fallback=client.tasks.query, ended={"result": "SUCCESS"})
    body = {"name": "Ref", "settings": FAN_IN, "source": {"upload_id": "u1"}, "dry_run": False, "request_id": "r3"}
    client.post("/api/ingestion-jobs", json=body)
    client.tasks.run()
    task = client.tasks.rows[1]
    assert task["status"] == "failed" and task["job_id"] is None and task["result"] is None
    named = task["error"].split("; ")
    assert len(named) == 3 and all("is already in use: give the job another name" in line for line in named)
    assert [line.split(" ")[1] for line in named] == ["2", "3", "3"]
    assert not any(sql == q.insert_schedule_sql() for sql, _ in client.tasks.executed)


# ---- helpers ---------------------------------------------------------------------------------------------------------

OK_BOTH = [{"src_database": "eng_001", "status": "ok", "error": None},
           {"src_database": "eng_002", "status": "ok", "error": None}]


def _found(line: int, database: str, table: str, select_list: str) -> dict:
    """A table a discovery found, as source_rows_sql('discovery') reads it, numbered by its database line."""
    fields = {"src_connection": "sql_a", "src_database": database, "src_schema": "gen", "src_table": table,
              "select_list": select_list, "primary_key": "id"}
    return {"line": line, "server": "sql_a", "src_database": database, "src_schema": "gen", "src_table": table,
            "fields": fields}


ROWS_BOTH = [{"server": "sql_a", "src_database": "eng_001", "src_schema": "gen", "src_table": "t1",
              "fields": {"src_connection": "sql_a", "src_database": "eng_001", "src_schema": "gen", "src_table": "t1",
                         **DETAILS}},
             {"server": "sql_a", "src_database": "eng_001", "src_schema": "gen", "src_table": "t2",
              "fields": {"src_connection": "sql_a", "src_database": "eng_001", "src_schema": "gen", "src_table": "t2",
                         **DETAILS}},
             {"server": "sql_a", "src_database": "eng_002", "src_schema": "gen", "src_table": "t9",
              "fields": {"src_connection": "sql_a", "src_database": "eng_002", "src_schema": "gen", "src_table": "t9",
                         **DETAILS}}]


def _table_row(line: int, database: str, table: str, select_list: str) -> dict:
    fields = {"src_connection": "sql_a", "src_database": database, "src_schema": "gen", "src_table": table,
              "select_list": select_list, "primary_key": "id"}
    return {"line": line, "server": "sql_a", "src_database": database, "src_schema": "gen", "src_table": table,
            "fields": fields}


def _database_lines(*named: tuple) -> dict:
    """A selection's database-only lines, given as (database, line) pairs, read through sql_a."""
    return {"database_lines": [{"line": line, "database": database, "connection": "sql_a"} for database, line in named],
            "rows": []}


_UPLOAD = {
    q.upload_sql(): [{"upload_id": "u1", "file_name": "f.csv", "columns": ["src_database", "src_schema", "src_table",
                                                                             "select_list", "primary_key"]}],
    q.upload_databases_count_sql(): [{"n": 1}],
    q.upload_database_lines_sql(): [{"line": 3, "src_database": "eng_001", "fields": {"src_connection": "sql_a",
                                                                                      "src_database": "eng_001"}}],
    q.selected_rows_page_sql("upload"): [{"server": "sql_a", "src_database": "eng_002", "src_schema": "gen", "src_table": "t9",
                                          "line": 2, "fields": {"src_connection": "sql_a", "src_database": "eng_002",
                                                                "src_schema": "gen", "src_table": "t9", **DETAILS}}],
}


class _Discovery:
    def __init__(self):
        self.runs, self.ended = [], None


def _patch_discovery(monkeypatch, statuses=(), rows=(), ended=None, answers=None, fallback=None) -> _Discovery:
    """Stubs the discovery run: connections, starting and ending a run, the statuses and rows it reads, and the upload
    queries (answers, by SQL) before the fallback. Returns what was started, and the run's end (set it to finish it)."""
    discovery = _Discovery()
    discovery.ended = ended
    answers = answers or {}

    def query(sql, params=()):
        if sql in answers:
            return list(answers[sql])
        if sql == q.discovery_statuses_sql():
            return list(statuses)
        if sql == q.source_rows_sql("discovery"):
            return list(rows)
        return fallback(sql, params) if fallback else []

    monkeypatch.setattr(main, "connections", lambda fresh=False: {"connections": [{"name": "sql_a"}], "error": None})
    monkeypatch.setattr(main.jobs, "DISCOVER_JOB_ID", 9)
    monkeypatch.setattr(main, "_refuse_run_of", lambda job_id, request: None)
    monkeypatch.setattr(main.jobs, "run_now", lambda job_id, params: discovery.runs.append(params) or 100 + len(discovery.runs))
    monkeypatch.setattr(main.jobs, "run_ended", lambda run_id: discovery.ended)
    monkeypatch.setattr(main.db, "query", query)
    return discovery
