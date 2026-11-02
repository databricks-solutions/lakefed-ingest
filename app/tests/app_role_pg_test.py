"""The app's writes as the app's own Lakebase role can make them: select, insert and update only, as
grant_app_access.ipynb grants, no DELETE. The other Postgres tests run as the tables' owner, who may do anything, so a
statement the deployed app is refused (a DELETE before an insert, say) passes there; here it fails.

Each test drives the app through its API (TestClient), with Lakebase reached as a role holding exactly those grants
(SET ROLE on every connection the app opens) and the workspace stubbed. The asserts read as the owner.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py) where the test may create a
role; skipped, saying so, when it can't."""
import itertools
import os
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import table_removal as tr  # noqa: E402
from delete_job_pg_test import _row  # noqa: E402

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]
SETTINGS = {"fan_in": True, "sink_catalog": "main", "sink_schema": "eng", "sink_table": "{table}"}
# The grants of grant_app_access.ipynb, on the schema's tables and sequences.
GRANTS = ("grant usage on schema lakefed_ingest to {role}",
          "grant select, insert, update on all tables in schema lakefed_ingest to {role}",
          "revoke update on lakefed_ingest.jdbc_login_write, lakefed_ingest.jdbc_connection from {role}",
          "grant usage, select on all sequences in schema lakefed_ingest to {role}")
SUMMARY = {"sch": "dbo", "row_count": "12", "ct_enabled": "1", "select_cols": "id, amount", "excluded_cols": None,
           "pk_cols": "id", "pk_unreadable": "0", "pk_first_type": "int", "rowversion_col": None, "identity_col": None,
           "identity_type": None}


SOURCE_ROW = {"src_type": "sqlserver", "src_host": "h", "src_port": 1433, "src_connection": None, "secret_key": "k1",
              "src_database": "db"}      # the source database _row writes its control rows on


class _Warehouse:
    """The SQL warehouse, answering at once: the server's databases, every database open with 2 tables, and each
    database's two tables."""

    def __init__(self, databases):
        self.databases, self.answers, self.ids = databases, {}, itertools.count(1)

    def start(self, statement, parameters, wait_s=None):
        query = " ".join(str(p.get("value") if isinstance(p, dict) else p) for p in parameters) + statement
        if "sys.databases" in query:
            rows = [{"name": name} for name in self.databases]
        elif "COUNT(*) AS tables" in query:
            rows = [{"tables": "2"}]
        else:
            rows = [{**SUMMARY, "tbl": table} for table in ("orders", "lines")]
        statement_id = f"s-{next(self.ids)}"
        self.answers[statement_id] = {"statement_id": statement_id, "state": "SUCCEEDED", "rows": rows, "error": None}
        return self.answers[statement_id]

    def read(self, statement_id):
        return self.answers[statement_id]


@pytest.fixture
def client(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_role_test_{uuid.uuid4().hex[:8]}"
    role = f"lakefed_app_role_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    target = psycopg.conninfo.make_conninfo(URI, dbname=name)
    owner = psycopg.connect(target, autocommit=True)
    made_role = False
    try:
        schema = (REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()
        for statement in state_store.split_sql_statements(schema):
            owner.execute(statement)
        try:
            owner.execute(f"create role {role} nologin")
            made_role = True
            owner.execute(f"grant {role} to current_user")
        except psycopg.Error as error:
            pytest.skip(f"can't make a role to test the app's grants with here: {error}".splitlines()[0])
        for grant in GRANTS:
            owner.execute(grant.format(role=role))

        def as_app():
            conn = psycopg.connect(target, autocommit=True)
            conn.execute(f"set role {role}")
            return conn
        monkeypatch.setattr(main.db, "connect", as_app)
        monkeypatch.setattr(main.jobs, "schedule_variants", lambda: {"default": "1", "check": "2"})
        monkeypatch.setattr(main.jobs, "ingest_variants", lambda: {"default": "555"})
        monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE")
        monkeypatch.setattr(main, "_sync", lambda sid, user: [])
        monkeypatch.setattr(main, "_refuse_job_tables", lambda job, groups, request: None)
        monkeypatch.setattr(main, "_require_connection", lambda name: None)
        monkeypatch.setattr(main, "_job_active_runs", lambda job, members: [])
        monkeypatch.setattr(main, "_start_background", lambda work: work())
        monkeypatch.setattr(main, "TASK_BEAT_S", 3600)
        fake = _Warehouse([f"eng_{n:03d}" for n in range(1, 4)])
        monkeypatch.setattr(main.warehouse, "configured", lambda: True)
        monkeypatch.setattr(main.warehouse, "start", fake.start)
        monkeypatch.setattr(main.warehouse, "read", fake.read)
        monkeypatch.setattr(main.warehouse, "warehouse_state", lambda: "RUNNING")
        c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
        c.role, c.owner = role, owner
        c.sql = lambda statement, params=(): owner.execute(statement, params).fetchall()
        yield c
    finally:
        if made_role:
            owner.execute(f"drop owned by {role}")
        owner.close()
        admin.execute(f"drop database {name} with (force)")
        if made_role:
            admin.execute(f"drop role if exists {role}")
        admin.close()


def _discover(client, databases):
    """Discover on a source as the page does it through the warehouse: the listing (the connection test), the access
    check of the page shown, then the read of the chosen databases until it ends. Returns the read's discovery id."""
    listing = client.post("/api/discoveries/source", json={"src_connection": "sql_a", "kind": "list"})
    assert listing.status_code == 200 and listing.json()["state"] == "SUCCEEDED", listing.text
    listing_id = listing.json()["discovery_id"]
    for _ in range(3):
        access = client.post(f"/api/discoveries/{listing_id}/access?connection=sql_a", json={"first": databases})
        assert access.status_code == 200, access.text
    read = client.post("/api/discoveries/source", json={"src_connection": "sql_a", "kind": "tables",
                                                        "databases": databases})
    assert read.status_code == 200, read.text
    did = read.json()["discovery_id"]
    for _ in range(5):
        step = client.get(f"/api/discoveries/{did}/reads?connection=sql_a")
        assert step.status_code == 200, step.text
        if step.json()["state"] == "SUCCEEDED":
            break
    assert step.json()["failed"] == 0 and step.json()["done"] == len(databases), step.json()
    return did


def test_the_role_really_has_no_delete(client):
    """The harness itself: the app's role is refused a DELETE, so the other tests would see one."""
    conn = main.db.connect()
    try:
        with pytest.raises(Exception, match="permission denied"):
            conn.execute("delete from lakefed_ingest.discovered_table")
    finally:
        conn.close()


def test_the_role_cannot_update_the_two_jdbc_record_tables(client):
    """The app only reads and inserts into the JDBC record tables, so grant_app_access revokes UPDATE on them: an update
    the app's role makes is refused, as it would rewrite who typed a login or where a connection points."""
    conn = main.db.connect()
    try:
        with pytest.raises(Exception, match="permission denied"):
            conn.execute("update lakefed_ingest.jdbc_login_write set entered_by = entered_by")
        with pytest.raises(Exception, match="permission denied"):
            conn.execute("update lakefed_ingest.jdbc_connection set src_host = src_host")
    finally:
        conn.close()


def test_discovery_through_the_warehouse_runs_as_the_apps_role(client):
    did = _discover(client, ["eng_001", "eng_002"])
    tables = client.sql("select src_database, src_table from lakefed_ingest.discovered_table where discovery_id = %s "
                        "order by 1, 2", (did,))
    assert tables == [("eng_001", "lines"), ("eng_001", "orders"), ("eng_002", "lines"), ("eng_002", "orders")]


def test_a_job_by_database_then_add_tables_and_a_new_number_of_groups_run_as_the_apps_role(client):
    did = _discover(client, ["eng_001", "eng_002", "eng_003"])
    selection = {"discovery_id": did, "databases": [{"server": "sql_a", "database": d} for d in ("eng_001", "eng_002")]}
    made = client.post("/api/ingestion-jobs", json={"name": "Role", "settings": SETTINGS, "groups_per_server": 2,
                                                    "dry_run": False, "request_id": "role-1", "source": selection})
    assert made.status_code in (200, 202), made.text
    job_id = made.json().get("id") or client.get(f"/api/tasks/{made.json()['task']['id']}").json()["job_id"]
    added = client.post(f"/api/ingestion-jobs/{job_id}/tables", json={
        "source": {"discovery_id": did, "databases": [{"server": "sql_a", "database": "eng_003"}]},
        "dry_run": False, "request_id": "role-2"})
    assert added.status_code in (200, 202), added.text
    regrouped = client.post(f"/api/ingestion-jobs/{job_id}/groups", json={"groups_per_server": 3, "dry_run": False,
                                                                           "request_id": "role-3"})
    assert regrouped.status_code == 200, regrouped.text
    assert client.sql("select count(*), count(distinct src_database) from lakefed_ingest.control") == [(6, 3)]


def _draft(client, databases):
    """The read started as the databases are chosen (a draft), one step done: its discovery id, its reads not ended."""
    listing = client.post("/api/discoveries/source", json={"src_connection": "sql_a", "kind": "list"}).json()
    access = client.post(f"/api/discoveries/{listing['discovery_id']}/access?connection=sql_a",
                         json={"first": databases})
    assert access.status_code == 200, access.text
    read = client.post("/api/discoveries/source", json={"src_connection": "sql_a", "kind": "tables", "draft": True,
                                                        "databases": databases[:1]})
    assert read.status_code == 200, read.text
    did = read.json()["discovery_id"]
    grown = client.post("/api/discoveries/source", json={"src_connection": "sql_a", "kind": "tables", "draft_id": did,
                                                         "databases": databases[1:]})
    assert grown.status_code == 200 and grown.json()["discovery_id"] == did, grown.text
    return did


@pytest.mark.parametrize("by_database", [False, True])
def test_create_from_a_draft_still_reading_waits_for_it_then_takes_it_as_the_apps_role(client, monkeypatch, by_database):
    monkeypatch.setattr(main, "DRAFT_POLL_S", 0)
    if by_database:
        monkeypatch.setattr(main, "BACKGROUND_ROWS", 1)
    did = _draft(client, ["eng_001", "eng_002"])
    assert client.sql("select count(*) from lakefed_ingest.discovery where discovery_id = %s and status = 'running'",
                      (did,))[0][0] > 0, "the draft's read should still be going when Create is pressed"
    selection = {"discovery_id": did, "databases": [{"server": "sql_a", "database": d} for d in ("eng_001", "eng_002")]}
    preview = client.post("/api/ingestion-jobs", json={"name": "Draft", "settings": SETTINGS, "source": selection})
    assert preview.status_code == 200, preview.text
    made = client.post("/api/ingestion-jobs", json={"name": "Draft", "settings": SETTINGS, "dry_run": False,
                                                    "request_id": f"draft-{by_database}", "source": selection})
    assert made.status_code in (200, 202), made.text
    assert client.sql("select count(*), count(distinct src_database) from lakefed_ingest.control") == [(4, 2)]
    marks = client.sql("select distinct listing ? 'draft', (listing ->> 'job_id') is not null "
                       "from lakefed_ingest.discovery where discovery_id = %s", (did,))
    assert marks == [(False, True)], "Create takes the draft: no draft mark left, the job named"


def test_a_discarded_draft_reads_no_more_and_cant_be_created_from_as_the_apps_role(client, monkeypatch):
    cancelled = []
    monkeypatch.setattr(main.warehouse, "cancel", cancelled.append)
    did = _draft(client, ["eng_001", "eng_002"])
    gone = client.post(f"/api/discoveries/{did}/discard")
    assert gone.status_code == 200 and gone.json()["cancelled"] == len(cancelled), gone.text
    step = client.get(f"/api/discoveries/{did}/reads?connection=sql_a").json()
    assert step["state"] in ("SUCCEEDED", "RUNNING")
    assert client.sql("select count(*) from lakefed_ingest.discovery where discovery_id = %s "
                      "and not listing ? 'discarded'", (did,)) == [(0,)]
    made = client.post("/api/ingestion-jobs", json={"name": "Gone", "settings": SETTINGS, "dry_run": False,
                                                    "request_id": "draft-gone", "source": {"discovery_id": did,
                                                    "databases": [{"server": "sql_a", "database": "eng_001"}]}})
    assert made.status_code == 422 and "discarded" in made.text, made.text
    assert client.sql("select count(*) from lakefed_ingest.control") == [(0,)]
    import queries as q
    assert main.db.query(q.drafts_to_purge_sql(), (7200,)) == [{"discovery_id": did}]    # the poller's look, as the app


def test_manages_export_reads_as_the_apps_role(client, monkeypatch):
    did = _discover(client, ["eng_001", "eng_002"])
    selection = {"discovery_id": did, "databases": [{"server": "sql_a", "database": d} for d in ("eng_001", "eng_002")]}
    made = client.post("/api/ingestion-jobs", json={"name": "Role", "settings": SETTINGS, "groups_per_server": 1,
                                                    "dry_run": False, "request_id": "role-export", "source": selection})
    assert made.status_code in (200, 202), made.text
    monkeypatch.setattr(main.jobs, "APP_NAME", "lakefed-ops")
    monkeypatch.setattr(main.jobs, "user_app_permission", lambda user: "CAN_MANAGE")
    monkeypatch.setattr(main.q, "EXPORT_PAGE", 3)
    exported = client.get("/api/manage/export.csv")
    assert exported.status_code == 200, exported.text
    lines = exported.text.splitlines()
    assert lines[0].startswith("ingestion_job,src_connection,src_database,") and len(lines) == 5
    assert {line.split(",")[0] for line in lines[1:]} == {"Role"}


def test_the_schema_upgrade_reads_the_version_and_takes_its_lock_as_the_apps_role(client, monkeypatch):
    import schema_upgrade
    started = []

    def run_now(job_id):
        started.append(job_id)
        return 41
    # The schema is applied here without recording a version, as a database with no schema reads: left alone.
    nothing = schema_upgrade.check(main.db.connect, "16", lambda job_id: [], run_now)
    assert (nothing["state"], nothing["database"], started) == ("missing", 0, [])
    behind = schema_upgrade.SCHEMA_VERSION - 1
    client.sql("insert into lakefed_ingest.schema_version (version) values (%s) returning version", (behind,))
    with main.db.connect() as conn:
        assert schema_upgrade.database_version(conn) == behind
    other = main.db.connect()
    try:
        assert other.execute("select pg_try_advisory_lock(%s, %s)", schema_upgrade.UPGRADE_LOCK).fetchone()[0]
        held = schema_upgrade.check(main.db.connect, "16", lambda job_id: [], run_now)
        assert (held["state"], started) == ("busy", []), held
        other.execute("select pg_advisory_unlock(%s, %s)", schema_upgrade.UPGRADE_LOCK)
        first = schema_upgrade.check(main.db.connect, "16", lambda job_id: [], run_now)
        assert (first["state"], first["database"], first["run_id"], first["error"]) == ("started", behind, 41, None)
        again = schema_upgrade.check(main.db.connect, "16", lambda job_id: [{"run_id": 41, "url": "https://h/run/41"}],
                                     run_now)
        assert (again["state"], started) == ("running", ["16"]), again
        assert other.execute("select pg_try_advisory_lock(%s, %s)", schema_upgrade.UPGRADE_LOCK).fetchone()[0], (
            "the check lets its lock go")
    finally:
        other.close()
    assert client.sql("select action, field, old_value, new_value from lakefed_ingest.control_change "
                      "where action = 'schema_upgrade'") == [("schema_upgrade", "schema_version", str(behind),
                                                              str(schema_upgrade.SCHEMA_VERSION))]
    monkeypatch.setattr(main.jobs, "APP_NAME", "lakefed-ops")
    monkeypatch.setattr(main.jobs, "user_app_permission", lambda user: "CAN_MANAGE")
    monkeypatch.setattr(main.jobs, "app_deployment", lambda: {})
    monkeypatch.setattr(main.jobs, "SETUP_JOB_ID", "16")
    monkeypatch.setattr(main.run_poller, "runs_of", lambda job_id, active_only, limit: [{"run_id": 41, "url": "u"}])
    schema = client.get("/api/manage/version").json()["schema"]
    assert (schema["database"], schema["app"], schema["run"], schema["error"]) == (
        behind, schema_upgrade.SCHEMA_VERSION, {"run_id": 41, "url": "u"}, None)


def test_removing_a_table_records_the_request_and_starts_the_delete_job_with_no_delete_as_the_apps_role(client,
                                                                                                        monkeypatch):
    started = []

    def start(schedule_id, control_ids):
        started.append((schedule_id, sorted(control_ids)))
        return 91

    monkeypatch.setattr(main.jobs, "start_table_remove", start)
    monkeypatch.setattr(main.jobs, "workspace_url", lambda: "https://ws.example")
    monkeypatch.setattr(main.jobs, "DELETE_JOB_ID", "99")
    did = _discover(client, ["eng_001", "eng_002"])
    selection = {"discovery_id": did, "databases": [{"server": "sql_a", "database": d} for d in ("eng_001", "eng_002")]}
    made = client.post("/api/ingestion-jobs", json={"name": "Remove", "settings": SETTINGS, "dry_run": False,
                                                    "request_id": "remove-1", "source": selection})
    assert made.status_code in (200, 202), made.text
    job_id = made.json().get("id") or client.get(f"/api/tasks/{made.json()['task']['id']}").json()["job_id"]
    [(control_id,)] = client.sql("select id from lakefed_ingest.control where src_database = 'eng_001' "
                                 "and src_table = 'orders'")

    removed = client.post(f"/api/ingestion-jobs/{job_id}/tables/remove",
                          json={"control_ids": [control_id], "confirmed": True})
    assert removed.status_code == 200, removed.text
    assert removed.json()["run"]["run_id"] == 91 and removed.json()["error"] is None
    assert started == [(job_id, [control_id])]

    assert client.sql("select count(*) from lakefed_ingest.control where id = %s", (control_id,)) == [(1,)], \
        "the app records and starts the removal; the delete job deletes the row, not the app"
    [(removal,)] = client.sql("select options -> 'table_remove' from lakefed_ingest.schedule where id = %s", (job_id,))
    assert removal[str(control_id)]["run_id"] == 91 and removal[str(control_id)]["requested_by"] == "op@example.com"
    assert client.sql("select count(*) from lakefed_ingest.control_change where action = 'remove' "
                      "and control_id = %s", (control_id,)) == [(1,)]
    assert client.sql("select deleted_at is null from lakefed_ingest.schedule where id = %s", (job_id,)) == [(True,)]


def test_a_removed_table_stays_out_of_a_database_add_until_it_is_named_again_as_the_apps_role(client, monkeypatch):
    monkeypatch.setattr(main.jobs, "start_table_remove", lambda schedule_id, control_ids: 91)
    monkeypatch.setattr(main.jobs, "workspace_url", lambda: "https://ws.example")
    monkeypatch.setattr(main.jobs, "DELETE_JOB_ID", "99")
    did = _discover(client, ["eng_001", "eng_002"])
    selection = {"discovery_id": did, "databases": [{"server": "sql_a", "database": d} for d in ("eng_001", "eng_002")]}
    made = client.post("/api/ingestion-jobs", json={"name": "Excluded", "settings": SETTINGS, "dry_run": False,
                                                    "request_id": "excluded-1", "source": selection})
    assert made.status_code in (200, 202), made.text
    job_id = made.json().get("id") or client.get(f"/api/tasks/{made.json()['task']['id']}").json()["job_id"]
    [(control_id,)] = client.sql("select id from lakefed_ingest.control where src_database = 'eng_001' "
                                 "and src_table = 'orders'")
    removed = client.post(f"/api/ingestion-jobs/{job_id}/tables/remove",
                          json={"control_ids": [control_id], "confirmed": True})
    assert removed.status_code == 200, removed.text
    excluded = tr.exclusion_key("sql_a", "eng_001", "dbo", "orders")
    assert client.sql("select options -> 'table_exclude' ? %s from lakefed_ingest.schedule where id = %s",
                      (excluded, job_id)) == [(True,)]

    client.owner.execute("delete from lakefed_ingest.control where id = %s", (control_id,))
    again = client.post(f"/api/ingestion-jobs/{job_id}/tables", json={
        "source": {"discovery_id": did, "databases": [{"server": "sql_a", "database": "eng_001"}]},
        "dry_run": False, "request_id": "excluded-2"})
    assert again.status_code in (200, 202), again.text
    assert client.sql("select count(*) from lakefed_ingest.control where src_database = 'eng_001' "
                      "and src_table = 'orders'") == [(0,)], "a whole-database add must not take the removed table back"

    named = client.post(f"/api/ingestion-jobs/{job_id}/tables", json={
        "source": {"discovery_id": did,
                   "tables": [{"server": "sql_a", "database": "eng_001", "tables": ["dbo.orders"]}]},
        "dry_run": False, "request_id": "excluded-3"})
    assert named.status_code in (200, 202), named.text
    assert client.sql("select count(*) from lakefed_ingest.control where src_database = 'eng_001' "
                      "and src_table = 'orders'") == [(1,)]
    assert client.sql("select options -> 'table_exclude' from lakefed_ingest.schedule where id = %s",
                      (job_id,)) == [({},)]


def test_a_delete_a_refused_new_table_and_a_restore_run_as_the_apps_role(client, monkeypatch):
    """Delete, the Deleted jobs list, a new table refused by the database it is in, and Restore: none of them needs
    DELETE, and the refusal reads the owner as the role does."""
    monkeypatch.setattr(main.jobs, "DELETE_JOB_ID", "99")
    sid = _seed_job(client.owner, "Role kept", "role_g1")
    _row(client.owner, "role_g1", "a")
    deleted = client.post(f"/api/schedules/{sid}/delete", json={"drop_targets": True})
    assert deleted.status_code == 200, deleted.text
    assert client.sql("select deleted_at is not null from lakefed_ingest.schedule where id = %s", (sid,)) == [(True,)]
    kept = client.get("/api/ingestion-jobs/deletions")
    assert kept.status_code == 200 and [row["id"] for row in kept.json()["deleted"]] == [sid], kept.text
    conn = main.db.connect()
    try:
        with pytest.raises(main.HTTPException) as refused:
            main._refuse_deleted_databases(conn.cursor(), [SOURCE_ROW])
    finally:
        conn.close()
    assert refused.value.status_code == 409 and "in the deleted ingestion job Role kept" in refused.value.detail
    restored = client.post(f"/api/schedules/{sid}/restore")
    assert restored.status_code == 200, restored.text
    assert client.sql("select deleted_at from lakefed_ingest.schedule where id = %s", (sid,)) == [(None,)]
    members = client.sql("select count(*) from lakefed_ingest.schedule_member where schedule_id = %s "
                         "and removed_at is null", (sid,))
    assert members == [(1,)]


def test_a_groups_naming_rule_is_read_in_one_query_as_the_apps_role(client):
    """_group_unity_catalog reads the job holding a group with group_job_options_sql: a removed member doesn't hold
    it, a purged job doesn't either, and a group no job holds takes Unity Catalog's rule."""
    owner = client.owner
    older = _seed_job(owner, "A older", "eng_c02")
    newer = _seed_job(owner, "B newer", "eng_c01")
    purged = _seed_job(owner, "C purged", "eng_c03")
    owner.execute("update lakefed_ingest.schedule set options = '{\"target_names\": \"unity_catalog\"}'::jsonb "
                  "where id = %s", (newer,))
    owner.execute("insert into lakefed_ingest.schedule_member (schedule_id, task_collection, removed_at) "
                  "values (%s, 'eng_c01', now())", (older,))
    owner.execute("update lakefed_ingest.schedule set deleted_at = now(), options = '{\"delete_finished\": true}'::jsonb "
                  "where id = %s", (purged,))
    assert main._group_unity_catalog("eng_c01") is True
    assert main._group_unity_catalog("eng_c02") is False
    assert main._group_unity_catalog("eng_c03") is True
    assert main._group_unity_catalog("nobody") is True


def _seed_job(conn, name, group):
    """A live job with one group, inserted as the owner for the test's setup."""
    with conn.transaction():
        sid = conn.execute("insert into lakefed_ingest.schedule (name, cadence_minutes) values (%s, 60) returning id",
                           (name,)).fetchone()[0]
        conn.execute("insert into lakefed_ingest.schedule_member (schedule_id, task_collection) values (%s, %s)",
                     (sid, group))
    return sid
