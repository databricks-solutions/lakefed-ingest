"""The configuration import applied against a real Postgres: a throwaway database with the Lakebase schema, written
through the app's own transaction (lakebase.transaction), so the control row insert, the change log and the
mixed-sink check run as the server runs them.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
import os
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]
CSV = ("src_connection,src_database,src_schema,src_table,select_list,primary_key,client_id,attr:business_unit\n"
       "sql_a,Unit1,dbo,orders,\"id,amount\",id,u1,North\n"
       "sql_a,Unit2,dbo,orders,\"id,amount\",id,u2,South\n")
SETTINGS = dict(src_type="sqlserver", use_remote_query=True, load_type="ct", fan_in=True, sink_catalog="main",
                sink_schema="bronze", sink_table="{table}", freshness_sla_minutes=60)


@pytest.fixture
def client(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_import_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    target = psycopg.conninfo.make_conninfo(URI, dbname=name)
    try:
        with psycopg.connect(target, autocommit=True) as conn:
            for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
                conn.execute(statement)
            # Every group belongs to an ingestion job, so the group imported into is one job's.
            sid = conn.execute("insert into lakefed_ingest.schedule (name, cadence_minutes, timezone, parameters) "
                               "values ('Units', 60, 'UTC', '{}'::jsonb) returning id").fetchone()[0]
            conn.execute("insert into lakefed_ingest.schedule_member (schedule_id, task_collection, offset_minutes) "
                         "values (%s, 'units', 0)", (sid,))
        monkeypatch.setattr(main.db, "connect", lambda: psycopg.connect(target, autocommit=True))
        monkeypatch.setattr(main.jobs, "ingest_variants", lambda: {"default": "555"})
        monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE")
        c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
        c.target = target
        yield c
    finally:
        admin.execute(f"drop database {name} with (force)")
        admin.close()


def _apply(client):
    return client.post("/api/import", json={"task_collection": "units", "settings": SETTINGS, "csv": CSV,
                                            "dry_run": False})


def test_an_import_writes_its_rows_and_their_log_once(client):
    import psycopg
    assert _apply(client).json()["counts"] == {"imported": 2}
    with psycopg.connect(client.target) as conn:
        rows = conn.execute("select task_collection, src_database, sink_schema, sink_table, client_id, client_attrs, "
                            "use_remote_query, write_mode from lakefed_ingest.control order by id").fetchall()
        log = conn.execute("select action, task_collection from lakefed_ingest.control_change").fetchall()
    assert rows == [("units", "Unit1", "bronze", "orders", "u1", '{"business_unit": "North"}', True, "merge"),
                    ("units", "Unit2", "bronze", "orders", "u2", '{"business_unit": "South"}', True, "merge")]
    assert log == [("import", "units"), ("import", "units")]
    again = _apply(client).json()
    assert again["counts"] == {"skipped": 2}
    settings = {s["setting"]: s for s in client.get("/api/collections/units/settings").json()["settings"]}
    assert settings["fan_in"] == {"setting": "fan_in", "value": True} and settings["sink_table"]["value"] == "{table}"
    assert client.get("/api/collections/units/export.csv").text == CSV
