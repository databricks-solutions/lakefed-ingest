"""The configuration import's endpoints (main.py), with a fake database: preview, apply, a group's settings and its
export."""
import contextlib
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import queries as q  # noqa: E402
from api_test import FakeCursor, FakeDB  # noqa: E402


class GroupCursor(FakeCursor):
    """The fake cursor, also answering the group's rows inside the transaction."""
    def execute(self, sql, params=()):
        super().execute(sql, params)
        if sql == q.collection_control_rows_sql():
            rows = [r for r in GROUP_ROWS if r["task_collection"] == params[0]]
            cols = list(GROUP_ROWS[0].keys())
            self.description = [type("D", (), {"name": c})() for c in cols]
            self._rows = [tuple(r[c] for c in cols) for r in rows]

SETTINGS = dict(src_type="sqlserver", use_remote_query=True, load_type="ct", sink_catalog="main",
                sink_schema="bronze_{database}", sink_table="{table}", freshness_sla_minutes=60)
CSV = ("src_connection,src_database,src_schema,src_table,select_list,primary_key\n"
       "sql_a,Sales,dbo,orders,\"id,amount\",id\n"
       "sql_a,Sales,dbo,lines,\"id,qty\",id\n")
GROUP_ROWS = [dict(id=1, task_collection="g1", src_type="sqlserver", src_host=None, src_port=None, secret_key=None,
                   src_connection="sql_a", use_remote_query=True, src_database="Hr", src_schema="dbo",
                   src_table="people", select_list="id", primary_key="id", cursor_column=None, load_type="ct",
                   write_mode="merge", sink_catalog="main", sink_schema="bronze_hr", sink_table="people",
                   client_id=None, client_attrs=None, freshness_sla_minutes=60, jdbc_options=None)]


ORDER_CSV = ("src_connection,src_database,src_schema,src_table,select_list,primary_key\n"
             "sql_a,Sales,dbo,Order-Lines,\"id,amount\",id\n")


@pytest.fixture
def client(monkeypatch):
    fake = FakeDB()
    # These tests are about the writes; that every group belongs to a job is tested in group_rule_test.py.
    monkeypatch.setattr(main, "_refuse_groups_outside_jobs", lambda groups: None)

    def query(sql, params=()):
        if sql == q.collection_control_rows_sql():
            return [dict(r) for r in GROUP_ROWS if r["task_collection"] == params[0]]
        return []
    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield GroupCursor(fake))))
    c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
    c.db = fake
    return c


def _import(client, csv_text=CSV, group="g9", settings=SETTINGS, dry_run=True):
    return client.post("/api/import", json={"task_collection": group, "settings": settings, "csv": csv_text,
                                            "dry_run": dry_run})


def _held_by(monkeypatch, options):
    """Makes job 1 hold group g9, with the stored options given (None: an older job with none), and lets an apply
    past the Databricks permission check."""
    query = main.db.query

    def held(sql, params=()):
        if sql == q.schedules_sql():
            return [{"id": 1, "name": "Hr", "options": options}]
        if sql == q.schedule_members_sql():
            return [{"schedule_id": 1, "task_collection": "g9", "removed_at": None}]
        if sql == q.group_job_options_sql():
            return [{"id": 1, "options": options}] if params[0] == "g9" else []
        return query(sql, params)
    monkeypatch.setattr(main.db, "query", held)
    monkeypatch.setattr(main, "_refuse_groups", lambda groups, request: None)


def _inserted_rows(client):
    return [dict(zip(q.CONTROL_INSERT_COLUMNS, p)) for p in client.db.of("insert into lakefed_ingest.control (")]


def test_a_preview_writes_nothing_and_counts_each_lines_status(client):
    body = _import(client).json()
    assert body["counts"] == {"valid": 2} and [line["sink"] for line in body["lines"]] == [
        "main.bronze_sales.orders", "main.bronze_sales.lines"]
    assert client.db.of("insert into lakefed_ingest.control (") == []


def test_apply_inserts_under_the_groups_lock_and_logs_each_row_as_an_import(client):
    body = _import(client, dry_run=False).json()
    assert body["counts"] == {"imported": 1, "skipped": 1}      # the fake's sink for "lines" is taken
    inserted = [dict(zip(q.CONTROL_INSERT_COLUMNS, p)) for p in client.db.of("insert into lakefed_ingest.control (")]
    assert [(r["task_collection"], r["src_table"], r["sink_schema"]) for r in inserted] == [
        ("g9", "orders", "bronze_sales"), ("g9", "lines", "bronze_sales")]
    assert client.db.of("select pg_try_advisory_xact_lock")
    assert [p[0:2] for p in client.db.of("insert into lakefed_ingest.control_change")] == [("op@example.com", "import")]


def test_apply_is_refused_while_any_line_is_invalid_or_the_file_has_errors(client):
    bad = CSV + "sql_a,Sales,dbo,people,id,\n"
    r = _import(client, bad, dry_run=False)
    assert r.status_code == 422 and r.json()["detail"]["invalid"] == 1
    assert client.db.of("insert into lakefed_ingest.control (") == []
    r = _import(client, "load_type\nct\n")
    assert r.status_code == 422 and r.json()["detail"]["errors"] == ["unknown column load_type: a group setting, set at import"]


def test_an_existing_group_lends_its_settings_and_its_tables_show_as_registered(client):
    text = CSV + "sql_a,Hr,dbo,people,id,id\n"
    body = _import(client, text, group="g1", settings={}).json()
    assert body["counts"] == {"valid": 2, "registered": 1}
    assert body["settings"]["sink_schema"] == "bronze_{database}" and body["settings"]["freshness_sla_minutes"] == 60


def test_a_group_no_job_holds_names_its_tables_by_unity_catalog_rules(client):
    assert [line["sink"] for line in _import(client, ORDER_CSV).json()["lines"]] == ["main.bronze_sales.order-lines"]


def test_a_group_an_older_job_holds_keeps_the_older_names(client, monkeypatch):
    _held_by(monkeypatch, None)
    assert [line["sink"] for line in _import(client, ORDER_CSV).json()["lines"]] == ["main.bronze_sales.order_lines"]


def test_a_group_a_unity_catalog_job_holds_names_its_tables_by_unity_catalog_rules(client, monkeypatch):
    _held_by(monkeypatch, {"target_names": "unity_catalog"})
    assert [line["sink"] for line in _import(client, ORDER_CSV).json()["lines"]] == ["main.bronze_sales.order-lines"]
    assert _import(client, ORDER_CSV, dry_run=False).status_code == 200
    assert [row["sink_table"] for row in _inserted_rows(client)] == ["order-lines"]


def test_a_typed_sink_name_is_kept_as_typed_and_checked_under_its_groups_rule(client, monkeypatch):
    _held_by(monkeypatch, {"target_names": "unity_catalog"})
    typed = dict(SETTINGS, sink_table="Sales-Orders")
    assert _import(client, ORDER_CSV, settings=typed, dry_run=False).status_code == 200
    assert [row["sink_table"] for row in _inserted_rows(client)] == ["Sales-Orders"]
    r = _import(client, ORDER_CSV, settings=dict(SETTINGS, sink_table="Sales Orders"))
    assert r.status_code == 422 and "Unity Catalog refuses" in r.text


def test_a_groups_settings_and_its_export(client):
    settings = client.get("/api/collections/g1/settings").json()
    assert {s["setting"]: s.get("value") for s in settings["settings"]}["sink_table"] == "{table}"
    assert settings["tables"] == 1
    r = client.get("/api/collections/g1/export.csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="g1.csv"' in r.headers["content-disposition"]
    assert r.text.splitlines() == ["src_connection,src_database,src_schema,src_table,select_list,primary_key",
                                   "sql_a,Hr,dbo,people,id,id"]
    assert client.get("/api/collections/none/settings").json() == {
        "task_collection": "none", "tables": 0, "settings": json.loads(json.dumps(
            [{"setting": name, "value": None} for name in __import__("config_import").SETTINGS]))}


def test_apply_reads_the_groups_rows_only_after_taking_its_lock(client):
    _import(client, group="g1", settings={}, dry_run=False)
    order = [sql for sql, _ in client.db.statements]
    lock = next(i for i, sql in enumerate(order) if "pg_try_advisory_xact_lock" in sql)
    read = next(i for i, sql in enumerate(order) if sql == q.collection_control_rows_sql())
    assert lock < read


def test_a_blank_group_name_is_refused(client):
    r = _import(client, group="   ", dry_run=False)
    assert r.status_code == 422 and "name the ingestion group" in r.text
    assert client.db.of("insert into lakefed_ingest.control (") == []


def test_an_import_that_would_split_a_database_across_two_groups_is_refused(client):
    client.db.split = {q.database_key(dict(src_connection="sql_a", use_remote_query=True, src_database="Sales",
                                           task_collection="g9"))}
    r = _import(client, dry_run=False)
    assert r.status_code == 409 and "Sales" in r.json()["detail"]["message"]
    assert "Move all of its tables into one group. Nothing was written." in r.text


def test_a_mistyped_setting_is_refused_and_false_reads_as_false(client):
    r = _import(client, settings=dict(SETTINGS, sink_shema="x"))
    assert r.status_code == 422 and "unknown setting sink_shema" in r.text
    r = _import(client, settings=dict(SETTINGS, use_remote_query="maybe"))
    assert r.status_code == 422 and "use_remote_query: 'maybe' is not true or false" in r.text
    host_csv = ("src_host,secret_key,src_database,src_schema,src_table,select_list,primary_key\n"
                "sql.example.net,k,Sales,dbo,orders,id,id\n")
    lines = _import(client, csv_text=host_csv, settings=dict(SETTINGS, use_remote_query="false")).json()["lines"]
    assert lines[0]["status"] == "valid" and lines[0]["errors"] == []


def test_an_upload_over_the_limit_is_refused_before_it_is_read(client):
    limited = TestClient(main.UploadLimit(main.app, limit=1000), headers={"X-Forwarded-Email": "op@example.com"})
    big = CSV + "sql_a,Sales,dbo,t,\"id\",id\n" * 100
    for path in ("/api/import", "/api/ingestion-jobs", "/api/ingestion-jobs/3/tables"):
        r = limited.post(path, json={"task_collection": "g9", "settings": SETTINGS, "csv": big, "name": "j"})
        assert r.status_code == 413 and "split the file" in r.text, path
    # A body sent without a length is counted as it arrives.
    r = limited.post("/api/import", content=iter([b'{"csv": "' + b"x" * 600, b"x" * 600 + b'"}']),
                     headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert limited.post("/api/import", json={"task_collection": "g9", "settings": SETTINGS, "csv": CSV}).status_code == 200
