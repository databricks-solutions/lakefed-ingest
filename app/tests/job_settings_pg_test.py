"""An ingestion job's settings, read and edited through the API against a real Postgres: one edit writes every row of
every group in the job, in one transaction under the groups' run locks, logged in the change log.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import run_poller  # noqa: E402

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]
COLUMNS = ("task_collection", "src_type", "src_connection", "use_remote_query", "src_database", "src_schema",
           "src_table", "select_list", "primary_key", "load_type", "write_mode", "sink_catalog", "sink_schema",
           "sink_table", "client_id", "client_attrs", "freshness_sla_minutes")


def _row(group, database, table, load_type="ct"):
    return (group, "sqlserver", "sql_01", True, database, "dbo", table, "id,qty", "id", load_type, "append",
            "bronze", "extraction", table.lower(), database,
            json.dumps({"source_database": database}), 60)


@pytest.fixture
def client(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_jobset_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    target = psycopg.conninfo.make_conninfo(URI, dbname=name)
    try:
        with psycopg.connect(target, autocommit=True) as conn:
            for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
                conn.execute(statement)
            sid = conn.execute("insert into lakefed_ingest.schedule (name, cadence_minutes, timezone, parameters) "
                               "values ('Customer', 30, 'UTC', '{}'::jsonb) returning id").fetchone()[0]
            for group in ("cust_c01", "cust_c02"):
                conn.execute("insert into lakefed_ingest.schedule_member (schedule_id, task_collection, offset_minutes) "
                             "values (%s, %s, 0)", (sid, group))
            rows = [_row("cust_c01", "db_1", "OrderLine"), _row("cust_c01", "db_1", "Invoice"),
                    _row("cust_c02", "db_2", "OrderLine"), _row("cust_c02", "db_2", "Invoice"),
                    _row("other_group", "X", "OrderLine")]
            conn.cursor().executemany(f"insert into lakefed_ingest.control ({', '.join(COLUMNS)}) values "
                                      f"({', '.join(['%s'] * len(COLUMNS))})", rows)
        monkeypatch.setattr(main.db, "connect", lambda: psycopg.connect(target, autocommit=True))
        monkeypatch.setattr(main.jobs, "ingest_variants", lambda: {"default": "555"})
        monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE")
        # This database has the run poller's table, so a job page reads its runs from it, not from the Jobs API.
        monkeypatch.setattr(run_poller, "table_ready", lambda: True)
        c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
        c.target, c.sid = target, sid
        yield c
    finally:
        admin.execute(f"drop database {name} with (force)")
        admin.close()


def test_a_jobs_settings_are_read_from_every_group_in_it(client):
    body = client.get(f"/api/ingestion-jobs/{client.sid}/settings").json()
    assert body["tables"] == 4
    s = body["settings"]
    assert s["fan_in"] == {"value": True} and s["sink_table"] == {"value": "{table}"}
    assert s["client_id"] == {"value": "{database}"} and s["identity"] == {"value": {"source_database": "{database}"}}


def test_a_dry_run_shows_what_an_edit_does_and_writes_nothing(client):
    import psycopg
    body = client.post(f"/api/ingestion-jobs/{client.sid}/settings",
                       json={"settings": {"identity": {"source_server": "{server}", "source_database": "{database}"}}}).json()
    assert body["summary"] == {"rows": 4, "changed": 4, "reseed": 0, "errors": 0, "applied": False}
    assert body["errors"] == []
    with psycopg.connect(client.target) as conn:
        assert conn.execute("select count(*) from lakefed_ingest.control where client_attrs like '%source_server%'").fetchone()[0] == 0


def test_applying_writes_every_row_of_the_job_and_logs_each_change(client):
    import psycopg
    body = client.post(f"/api/ingestion-jobs/{client.sid}/settings",
                       json={"settings": {"freshness_sla_minutes": 90,
                                          "identity": {"source_server": "{server}", "source_database": "{database}"}},
                             "dry_run": False}).json()
    assert body["summary"]["applied"] is True and body["summary"]["changed"] == 4
    with psycopg.connect(client.target) as conn:
        rows = conn.execute("select task_collection, freshness_sla_minutes, client_attrs from lakefed_ingest.control "
                            "order by id").fetchall()
        logged = conn.execute("select count(*), count(distinct batch_id) from lakefed_ingest.control_change").fetchone()
    assert rows[0] == ("cust_c01", 90, json.dumps({"source_server": "sql_01", "source_database": "db_1"}))
    assert rows[4][1] == 60     # a row of another group is not part of the job
    assert logged == (2, 1)     # one line per database of the job, one batch


def test_a_new_target_reseeds_the_tables_and_a_write_mode_change_needs_one(client):
    alone = client.post(f"/api/ingestion-jobs/{client.sid}/settings", json={"settings": {"write_mode_changes": "merge"}})
    assert alone.status_code == 422 and "change the target names too" in alone.json()["detail"]["errors"][0]
    both = client.post(f"/api/ingestion-jobs/{client.sid}/settings",
                       json={"settings": {"write_mode_changes": "merge", "sink_schema": "current"}}).json()
    assert both["summary"]["errors"] == 0 and both["summary"]["reseed"] == 4


def test_a_bad_template_is_refused_with_its_reason(client):
    r = client.post(f"/api/ingestion-jobs/{client.sid}/settings", json={"settings": {"sink_table": "{database}_{table}"}})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"] == [
        "target table: {database} gives every database its own table, so fan-in can't use it"]


def test_an_edit_is_refused_while_a_group_of_the_job_is_running(client):
    import psycopg
    with psycopg.connect(client.target, autocommit=True) as holder:
        holder.execute("select pg_advisory_lock(hashtextextended(%s, 0))", ("lakefed_ingest:cust_c02",))
        r = client.post(f"/api/ingestion-jobs/{client.sid}/settings",
                        json={"settings": {"freshness_sla_minutes": 90}, "dry_run": False})
    assert r.status_code == 409 and r.json()["detail"]["group"] == "cust_c02"
    assert r.json()["detail"]["reason"] == "running" and "cust_c02" in r.json()["detail"]["message"]


def test_the_tree_opens_at_servers_then_a_servers_databases_then_a_databases_tables(client):
    servers = client.get(f"/api/ingestion-jobs/{client.sid}/tree").json()
    assert servers["level"] == "servers" and servers["total"] == 1
    assert {k: servers["items"][0][k] for k in ("server", "databases", "tables", "enabled_tables")} == {
        "server": "sql_01", "databases": 2, "tables": 4, "enabled_tables": 4}
    dbs = client.get(f"/api/ingestion-jobs/{client.sid}/tree", params={"server": "sql_01"}).json()
    assert [(d["database"], d["client_id"], d["tables"], d["missing"]) for d in dbs["items"]] == [
        ("db_1", "db_1", 2, 0), ("db_2", "db_2", 2, 0)]
    tables = client.get(f"/api/ingestion-jobs/{client.sid}/tree",
                        params={"server": "sql_01", "database": "db_2"}).json()
    assert [(t["schema"], t["table"], t["load_type"], t["columns"], t["target"]) for t in tables["items"]] == [
        ("dbo", "Invoice", "ct", 2, "bronze.extraction.invoice"),
        ("dbo", "OrderLine", "ct", 2, "bronze.extraction.orderline")]


def test_the_tree_searches_and_pages_at_each_level(client):
    dbs = client.get(f"/api/ingestion-jobs/{client.sid}/tree",
                     params={"server": "sql_01", "search": "db_2"}).json()
    assert [d["database"] for d in dbs["items"]] == ["db_2"] and dbs["total"] == 1
    page = client.get(f"/api/ingestion-jobs/{client.sid}/tree", params={"server": "sql_01", "limit": 1}).json()
    assert len(page["items"]) == 1 and page["total"] == 2


def test_disabling_a_database_disables_its_tables_in_the_job_only(client):
    import psycopg
    r = client.post(f"/api/ingestion-jobs/{client.sid}/enable",
                    json={"enabled": False, "server": "sql_01", "database": "db_2"}).json()
    assert r == {"updated": 2}
    with psycopg.connect(client.target) as conn:
        off = conn.execute("select src_database, src_table from lakefed_ingest.control where not task_enabled "
                           "order by src_table").fetchall()
    assert off == [("db_2", "Invoice"), ("db_2", "OrderLine")]


def test_a_tables_load_type_changes_in_every_database_of_the_job(client):
    import psycopg
    r = client.post(f"/api/ingestion-jobs/{client.sid}/tables/edit",
                    json={"schema": "dbo", "table": "OrderLine", "changes": {"load_type": "full"}, "dry_run": False})
    assert r.json()["summary"]["changed"] == 2
    with psycopg.connect(client.target) as conn:
        full = conn.execute("select task_collection from lakefed_ingest.control where load_type = 'full' "
                            "order by id").fetchall()
    assert full == [("cust_c01",), ("cust_c02",)]


DATABASES_CSV = ("listID,serverName,databaseName,isActive\n"
                 "3,sql_01,db_3,True\n"
                 "4,sql_02,db_4,False\n"
                 "1,sql_01,db_1,True\n")
MATCH = {"server": "serverName", "database": "databaseName", "enabled": "isActive"}


def test_a_databases_file_adds_each_database_with_the_jobs_tables_and_a_group_on_a_new_server(client, monkeypatch):
    import psycopg
    synced = []
    monkeypatch.setattr(main, "_sync", lambda sid, user: synced.append(sid) or [])
    preview = client.post(f"/api/ingestion-jobs/{client.sid}/databases",
                          json={"csv": DATABASES_CSV, "columns": MATCH, "full_list": True, "groups_per_server": 2}).json()
    summary = preview["summary"]
    assert {k: summary[k] for k in ("databases", "new_tables", "enable", "disable", "applied")} == {
        "databases": 3, "new_tables": 4, "enable": 0, "disable": 2, "applied": False}
    assert len(summary["new_groups"]) == 1 and summary["new_groups"][0] in ("customer_sql_02_c01", "customer_sql_02_c02")
    applied = client.post(f"/api/ingestion-jobs/{client.sid}/databases",
                          json={"csv": DATABASES_CSV, "columns": MATCH, "full_list": True, "groups_per_server": 2,
                                "dry_run": False}).json()
    assert applied["summary"]["applied"] is True and synced == [client.sid]
    with psycopg.connect(client.target) as conn:
        added = conn.execute("select src_database, task_enabled, client_id, sink_table, task_collection from "
                             "lakefed_ingest.control where src_database in ('db_3', 'db_4') order by src_database, "
                             "src_table").fetchall()
        off = conn.execute("select src_database from lakefed_ingest.control where not task_enabled and "
                           "src_database = 'db_2'").fetchall()
        members = conn.execute("select task_collection from lakefed_ingest.schedule_member where removed_at is null "
                               "order by 1").fetchall()
    assert [(d, e, c, s) for d, e, c, s, _ in added] == [
        ("db_3", True, "db_3", "invoice"), ("db_3", True, "db_3", "orderline"),
        ("db_4", False, "db_4", "invoice"), ("db_4", False, "db_4", "orderline")]
    assert {g for *_, g in added} - {"cust_c01", "cust_c02"} == set(applied["summary"]["new_groups"])
    assert len(off) == 2 and len(members) == 3


def test_a_daily_start_time_is_stored_and_moves_the_members_offsets(client, monkeypatch):
    import psycopg
    monkeypatch.setattr(main, "_sync", lambda sid, user: [])
    body = {"name": "Customer", "cadence_minutes": 1440, "timezone": "Europe/London", "members": ["cust_c01", "cust_c02"],
            "options": {"start_minute": 150}, "parameters": {}}
    r = client.post(f"/api/schedules/{client.sid}", json=body)
    assert r.status_code == 200, r.text
    with psycopg.connect(client.target) as conn:
        options = conn.execute("select options from lakefed_ingest.schedule where id = %s", (client.sid,)).fetchone()[0]
        offsets = conn.execute("select task_collection, offset_minutes from lakefed_ingest.schedule_member "
                               "where removed_at is null order by 1").fetchall()
    assert options == {"start_minute": 150, "updated_by": "op@example.com"}
    assert offsets == [("cust_c01", 150), ("cust_c02", 180)]
    assert client.get(f"/api/ingestion-jobs/{client.sid}").json()["description"] == "Every day at 02:30 (Europe/London)"
    bad = client.post(f"/api/schedules/{client.sid}", json=dict(body, cadence_minutes=30))
    assert bad.status_code == 422 and "set time" in bad.text


def test_a_schedule_save_without_options_keeps_the_start_time(client, monkeypatch):
    import psycopg
    monkeypatch.setattr(main, "_sync", lambda sid, user: [])
    body = {"name": "Customer", "cadence_minutes": 1440, "timezone": "Europe/London", "members": ["cust_c01", "cust_c02"],
            "parameters": {}}
    assert client.post(f"/api/schedules/{client.sid}", json=dict(body, options={"start_minute": 150})).status_code == 200
    assert client.post(f"/api/schedules/{client.sid}", json=body).status_code == 200
    with psycopg.connect(client.target) as conn:
        assert conn.execute("select options from lakefed_ingest.schedule where id = %s", (client.sid,)).fetchone()[0] == {
            "start_minute": 150, "updated_by": "op@example.com"}
    # An explicit {} clears the options a user sets; who last changed the job stays.
    assert client.post(f"/api/schedules/{client.sid}", json=dict(body, options={})).status_code == 200
    with psycopg.connect(client.target) as conn:
        assert conn.execute("select options from lakefed_ingest.schedule where id = %s", (client.sid,)).fetchone()[0] == {
            "updated_by": "op@example.com"}


def test_a_jobs_target_tables_count_their_sources_inside_and_outside_it_and_seven_days_of_rows(client, monkeypatch):
    import psycopg
    monkeypatch.setattr(main.db, "workspace", lambda: type("W", (), {"config": type("C", (), {"host": "https://h"})()})())
    with psycopg.connect(client.target, autocommit=True) as conn:
        order_line = conn.execute("select id from lakefed_ingest.control where task_collection = 'cust_c01' "
                                  "and src_table = 'OrderLine'").fetchone()[0]
        for rows, status, age in ((10, "ok", "1 hour"), (99, "failed", "1 hour"), (5, "ok", "8 days")):
            conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, status, "
                         "rows_merged, finished_at) values (1, 'cust_c01', %s, %s, %s, now() - %s::interval)",
                         (order_line, status, rows, age))
    body = client.get(f"/api/ingestion-jobs/{client.sid}/targets").json()
    by_target = {t["target"]: t for t in body["items"]}
    assert body["total"] == 2 and list(by_target) == ["bronze.extraction.invoice", "bronze.extraction.orderline"]
    assert body["fan_in"] == 2      # each is written from more than one source table
    order = by_target["bronze.extraction.orderline"]
    assert (order["sources"], order["databases"], order["with_client_id"], order["outside_sources"]) == (2, 2, 2, 1)
    assert order["rows_written_7d"] == 10 and by_target["bronze.extraction.invoice"]["rows_written_7d"] is None
    assert order["url"] == "https://h/explore/data/bronze/extraction/orderline"
    found = client.get(f"/api/ingestion-jobs/{client.sid}/targets", params={"search": "INVO"}).json()
    assert [t["target"] for t in found["items"]] == ["bronze.extraction.invoice"]
    sources = client.get(f"/api/ingestion-jobs/{client.sid}/targets/sources",
                         params={"target": "bronze.extraction.orderline"}).json()
    assert [(s["database"], s["rows_written_7d"]) for s in sources["items"]] == [("db_1", 10), ("db_2", None)]
