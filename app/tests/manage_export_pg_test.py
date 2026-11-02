"""Manage's export against a real Postgres: a throwaway database with the Lakebase schema, two ingestion jobs imported
through the app's own endpoint, then exported (GET /api/manage/export.csv) as the server runs the export's queries:
the jobs' groups, the identity column names (jsonb_object_keys over client_attrs), and the keyset pages.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
import csv
import io
import os
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import new_job as nj  # noqa: E402

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]
UNITS = ("src_connection,src_database,src_schema,src_table,select_list,primary_key,client_id,attr:business_unit\n"
         "sql_a,Unit1,dbo,orders,\"id,amount\",id,u1,North\n"
         "sql_a,Unit2,dbo,orders,\"id,amount\",id,u2,South\n"
         "sql_a,Unit3,dbo,orders,\"id,amount\",id,u3,East\n")
STAFF = ("src_connection,src_database,src_schema,src_table,select_list,primary_key\n"
         "sql_a,People,dbo,staff,\"id,name\",id\n")
SETTINGS = dict(src_type="sqlserver", use_remote_query=True, load_type="ct", fan_in=True, sink_catalog="main",
                sink_schema="bronze", sink_table="{table}", freshness_sla_minutes=60)


@pytest.fixture
def client(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_export_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    target = psycopg.conninfo.make_conninfo(URI, dbname=name)
    try:
        with psycopg.connect(target, autocommit=True) as conn:
            for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
                conn.execute(statement)
            ids = {}
            for job, group in (("Units", "units"), ("Staff", "staff")):
                ids[job] = conn.execute("insert into lakefed_ingest.schedule (name, cadence_minutes, timezone, "
                                        "parameters) values (%s, 60, 'UTC', '{}'::jsonb) returning id",
                                        (job,)).fetchone()[0]
                conn.execute("insert into lakefed_ingest.schedule_member (schedule_id, task_collection, "
                             "offset_minutes) values (%s, %s, 0)", (ids[job], group))
        monkeypatch.setattr(main.db, "connect", lambda: psycopg.connect(target, autocommit=True))
        monkeypatch.setattr(main.jobs, "ingest_variants", lambda: {"default": "555"})
        monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE")
        monkeypatch.setattr(main.jobs, "APP_NAME", "lakefed-ops")
        monkeypatch.setattr(main.jobs, "user_app_permission", lambda user: "CAN_MANAGE")
        c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
        staff = {**SETTINGS, "fan_in": False, "sink_table": "{database}_{table}"}
        for group, text, settings in (("units", UNITS, SETTINGS), ("staff", STAFF, staff)):
            done = c.post("/api/import", json={"task_collection": group, "settings": settings, "csv": text,
                                               "dry_run": False})
            assert done.status_code == 200, done.text
        c.target, c.ids = target, ids
        yield c
    finally:
        admin.execute(f"drop database {name} with (force)")
        admin.close()


def _lines(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_the_export_of_every_job_holds_each_enabled_table_once_with_its_job(client, monkeypatch):
    import psycopg
    with psycopg.connect(client.target, autocommit=True) as conn:
        conn.execute("update lakefed_ingest.control set task_enabled = false where src_database = 'Unit3'")
    monkeypatch.setattr(main.q, "EXPORT_PAGE", 1)
    r = client.get("/api/manage/export.csv")
    assert r.status_code == 200, r.text
    assert r.text.splitlines()[0] == ("ingestion_job,src_connection,src_database,src_schema,src_table,select_list,"
                                      "primary_key,cursor_column,client_id,load_type,attr:business_unit")
    assert [(line["ingestion_job"], line["src_database"], line["client_id"], line["attr:business_unit"])
            for line in _lines(r.text)] == [("Units", "Unit1", "u1", "North"), ("Units", "Unit2", "u2", "South"),
                                            ("Staff", "People", "", "")]


def test_one_jobs_export_reads_back_through_create_from_one_csv(client):
    r = client.get(f"/api/manage/export.csv?jobs={client.ids['Units']}")
    assert r.headers["content-disposition"] == 'attachment; filename="units.csv"'
    upload = nj.read_upload(r.text, "sql_a")
    assert upload["errors"] == []
    assert [(u["src_database"], u["src_table"], u["load_type"], u["fields"]["select_list"],
             u["fields"]["attr:business_unit"]) for u in upload["lines"]] == [
        ("Unit1", "orders", "ct", "id,amount", "North"), ("Unit2", "orders", "ct", "id,amount", "South"),
        ("Unit3", "orders", "ct", "id,amount", "East")]


def test_a_deleted_job_is_not_exported(client):
    import psycopg
    with psycopg.connect(client.target, autocommit=True) as conn:
        conn.execute("update lakefed_ingest.schedule set deleted_at = now() where name = 'Staff'")
    assert {line["ingestion_job"] for line in _lines(client.get("/api/manage/export.csv").text)} == {"Units"}
    assert client.get(f"/api/manage/export.csv?jobs={client.ids['Staff']}").text.count("\n") == 1
