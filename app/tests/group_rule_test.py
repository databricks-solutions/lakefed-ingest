"""Every ingestion group belongs to an ingestion job: a write that would put tables into a group no job holds is
refused before anything is written (main._refuse_groups_outside_jobs), and a group with tables can't leave its job
(schedules_api_test.py). New ingestion job makes its groups with the job, and Add tables uses the job's own groups."""
import contextlib

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import queries as q  # noqa: E402
from api_test import DISCOVERED, FakeCursor, FakeDB  # noqa: E402

JOBS = [{"id": 1, "name": "Engagements"}]
MEMBERS = [{"schedule_id": 1, "task_collection": "eng_c01", "removed_at": None},
           {"schedule_id": 1, "task_collection": "eng_c02", "removed_at": "2026-10-29T10:00:00+00:00"}]

ORDER_LINES = [dict(DISCOVERED[0], src_table="Order-Lines")]


def _jobs(sql, params=()):
    if sql == q.schedules_sql():
        return [dict(j) for j in JOBS]
    if sql == q.schedule_members_sql():
        return [dict(m) for m in MEMBERS]
    if sql == q.group_job_options_sql():
        return _holding(JOBS, params[0])
    return DISCOVERED if "discovered_table" in sql else []


def _holding(jobs, group):
    """group_job_options_sql's answer: the job of these that holds the group (its member not removed), or none."""
    held = {m["schedule_id"] for m in MEMBERS if m["task_collection"] == group and m["removed_at"] is None}
    return [{"id": j["id"], "options": j.get("options")} for j in jobs if j["id"] in held][:1]


def _holder(monkeypatch, options, discovered=DISCOVERED):
    """Makes job 1 hold eng_c01 with the stored options (None: an older job with none), with the discovery's rows given,
    and lets an apply past the Databricks permission check."""
    def query(sql, params=()):
        if sql == q.schedules_sql():
            return [dict(j, options=options) for j in JOBS]
        if sql == q.group_job_options_sql():
            return _holding([dict(j, options=options) for j in JOBS], params[0])
        if "discovered_table" in sql:
            return [dict(r) for r in discovered]
        return _jobs(sql, params)
    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main, "_refuse_groups", lambda groups, request: None)


def _register(client, **body):
    return client.post("/api/discoveries/d1/register", json=dict(task_collection="eng_c01", **body))


def _review_sinks(client, **body):
    return [r["sink_table"] for r in client.post("/api/discoveries/d1/review", json=body).json()]


def _sinks(client):
    inserts = client.db.of("insert into lakefed_ingest.control (")
    return [dict(zip(q.CONTROL_INSERT_COLUMNS, p))["sink_table"] for p in inserts]


@pytest.fixture
def client(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(main.db, "query", _jobs)
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield FakeCursor(fake))))
    monkeypatch.setattr(main, "_manage_refusal", lambda user: None)     # by host as a manager: host_reads_test.py
    c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
    c.db = fake
    return c


def test_a_group_a_job_holds_takes_tables(monkeypatch):
    monkeypatch.setattr(main.db, "query", _jobs)
    main._refuse_groups_outside_jobs(["eng_c01", None, ""])


def test_a_group_in_no_job_or_only_removed_from_one_is_refused(monkeypatch):
    monkeypatch.setattr(main.db, "query", _jobs)
    with pytest.raises(HTTPException) as refused:
        main._refuse_groups_outside_jobs(["eng_c01", "eng_c02", "loose"])
    assert refused.value.status_code == 422
    assert "eng_c02, loose is in none" in refused.value.detail and "New ingestion job" in refused.value.detail


def test_add_table_into_a_group_in_no_job_writes_nothing(client):
    row = {"task_collection": "loose", "src_type": "sqlserver", "src_host": "h", "secret_key": "k", "src_database": "db1",
           "src_schema": "dbo", "src_table": "orders", "select_list": "id", "primary_key": "id", "load_type": "ct",
           "sink_catalog": "main", "sink_schema": "bronze", "sink_table": "orders"}
    r = client.post("/api/tables", json=row)
    assert r.status_code == 422 and "is in none" in r.text
    assert client.db.of("insert into lakefed_ingest.control (") == []


def test_register_and_import_into_a_group_in_no_job_write_nothing(client):
    r = client.post("/api/discoveries/d1/register", json={"task_collection": "loose"})
    assert r.status_code == 422 and "is in none" in r.text
    csv = "src_host,secret_key,src_database,src_schema,src_table,select_list,primary_key\nh,k,db1,dbo,orders,id,id\n"
    r = client.post("/api/import", json={"task_collection": "loose", "csv": csv, "dry_run": False})
    assert r.status_code == 422 and "is in none" in r.text
    assert client.db.of("insert into lakefed_ingest.control (") == []


def test_bulk_import_into_a_group_an_older_job_holds_keeps_the_older_names(client, monkeypatch):
    _holder(monkeypatch, None, ORDER_LINES)
    assert _register(client).json()["counts"] == {"registered": 1}
    assert _sinks(client) == ["order_lines"]


def test_bulk_import_into_a_unity_catalog_job_group_names_its_tables_by_unity_catalog_rules(client, monkeypatch):
    _holder(monkeypatch, {"target_names": "unity_catalog"}, ORDER_LINES)
    assert _register(client).json()["counts"] == {"registered": 1}
    assert _sinks(client) == ["order-lines"]


def test_bulk_import_keeps_a_typed_target_name_as_typed(client, monkeypatch):
    _holder(monkeypatch, {"target_names": "unity_catalog"}, ORDER_LINES)
    assert _register(client, shared={"sink_table": "Sales-Orders"}).json()["counts"] == {"registered": 1}
    assert _sinks(client) == ["Sales-Orders"]


def test_bulk_import_refuses_a_typed_target_name_unity_catalog_refuses_in_a_unity_catalog_group(client, monkeypatch):
    _holder(monkeypatch, {"target_names": "unity_catalog"}, ORDER_LINES)
    r = _register(client, shared={"sink_table": "Sales Orders"}).json()
    assert r["counts"] == {"invalid": 1} and "Unity Catalog refuses" in r["results"][0]["errors"][0]
    assert _sinks(client) == []


def test_bulk_import_refuses_a_typed_name_a_reviewer_edits_in_a_unity_catalog_group(client, monkeypatch):
    _holder(monkeypatch, {"target_names": "unity_catalog"}, ORDER_LINES)
    edit = dict(src_database="db1", src_schema="dbo", src_table="Order-Lines", sink_table="Sales Orders")
    assert _register(client, edits=[edit]).json()["counts"] == {"invalid": 1}
    assert _sinks(client) == []


def test_review_names_rows_by_the_rule_of_the_group_they_go_into(client, monkeypatch):
    _holder(monkeypatch, None, ORDER_LINES)
    assert _review_sinks(client, task_collection="eng_c01") == ["order_lines"]
    _holder(monkeypatch, {"target_names": "unity_catalog"}, ORDER_LINES)
    assert _review_sinks(client, task_collection="eng_c01") == ["order-lines"]
    assert _review_sinks(client, task_collection="loose") == ["order-lines"]
    assert _review_sinks(client) == ["order-lines"]
