"""Large writes against a real Postgres: the batched control row inserts, the background task (app_task) with its
request id and stale rule, and tables taken by reference from an upload or a discovery, through the app's own
endpoints and transactions. A throwaway database with the Lakebase schema.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
import os
import sys
import time
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
HEAD = "src_connection,src_database,src_schema,src_table,select_list,primary_key\n"
SETTINGS = {"fan_in": True, "sink_catalog": "main", "sink_schema": "eng", "sink_table": "{table}"}


def _csv(databases: int, tables: int, server: str = "sql_a", head: bool = True) -> str:
    return (HEAD if head else "") + "".join(f"{server},eng_{d:03d},gen,t{t:04d},\"id, a\",id\n" for d in range(databases) for t in range(tables))


@pytest.fixture
def client(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_large_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    target = psycopg.conninfo.make_conninfo(URI, dbname=name)
    try:
        with psycopg.connect(target, autocommit=True) as conn:
            for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
                conn.execute(statement)
        monkeypatch.setattr(main.db, "connect", lambda: psycopg.connect(target, autocommit=True))
        monkeypatch.setattr(main.jobs, "schedule_variants", lambda: {"default": "1", "check": "2"})
        monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE")
        monkeypatch.setattr(main, "_sync", lambda sid, user: [])
        monkeypatch.setattr(main, "_refuse_job_tables", lambda job, groups, request: None)   # permissions: their own tests
        monkeypatch.setattr(main, "_start_background", lambda work: work())     # the thread, run inline
        monkeypatch.setattr(main, "BACKGROUND_ROWS", 500)
        monkeypatch.setattr(main, "TASK_BEAT_S", 3600)
        c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
        c.target = target

        def sql(statement, params=()):
            with psycopg.connect(target, autocommit=True) as conn:
                return conn.execute(statement, params).fetchall()
        c.sql = sql
        yield c
    finally:
        admin.execute(f"drop database {name} with (force)")
        admin.close()


def _create(client, request_id, name="Big", **tables):
    return client.post("/api/ingestion-jobs", json={"name": name, "settings": SETTINGS, "groups_per_server": 4,
                                                    "dry_run": False, "request_id": request_id, **tables})


# ---- batched writes and the task ---------------------------------------------------------------------------------

def test_a_large_create_is_written_by_its_task_in_batches_once(client):
    start = time.perf_counter()
    r = _create(client, "req-1", csv=_csv(10, 250))         # 2,500 tables: 3 statements of control rows
    took = time.perf_counter() - start
    assert r.status_code == 202
    task = client.get(f"/api/tasks/{r.json()['task']['id']}").json()
    assert task["status"] == "succeeded" and task["done"] == 2500 and task["total"] == 2500
    job_id = task["job_id"]
    assert task["result"]["id"] == job_id and task["result"]["created"] is True
    assert client.sql("select count(*), count(distinct task_collection) from lakefed_ingest.control") == [(2500, 4)]
    assert client.sql("select count(*) from lakefed_ingest.control_change where action = 'import'") == [(2500,)]
    assert client.sql("select count(*) from lakefed_ingest.schedule_member where schedule_id = %s", (job_id,)) == [(4,)]
    # The same request again answers with the same task and writes nothing more.
    again = _create(client, "req-1", csv=_csv(10, 250))
    assert again.status_code == 202 and again.json()["task"]["id"] == task["id"]
    assert client.sql("select count(*) from lakefed_ingest.schedule") == [(1,)]
    assert client.sql("select count(*) from lakefed_ingest.control") == [(2500,)]
    print(f"2,500-table create through the task on a local Postgres: {took:.2f} s")


def test_a_taken_target_fails_the_task_and_writes_nothing(client):
    assert _create(client, "small", name="First", csv=_csv(1, 3)).status_code == 200
    r = _create(client, "big", name="Second", csv=_csv(3, 300))   # eng_000.t0000..t0002 are First's targets
    task = client.get(f"/api/tasks/{r.json()['task']['id']}").json()
    assert task["status"] == "failed" and (
        "another table already writes main.eng.t0000 for database eng_000 on sql_a" in task["error"])
    assert task["result"] is None
    assert client.sql("select name from lakefed_ingest.schedule") == [("First",)]
    assert client.sql("select count(*) from lakefed_ingest.control") == [(3,)]


def test_a_task_whose_heartbeat_stopped_reads_failed_or_written(client):
    insert = main.q.insert_task_sql()
    gone = client.sql(insert, ("create", "gone", None, 10, None, "op"))[0][0]
    committed = client.sql(insert, ("create", "committed", None, 10, None, "op"))[0][0]
    alive = client.sql(insert, ("create", "alive", None, 10, None, "op"))[0][0]
    assert client.sql(insert, ("create", "gone", None, 10, None, "op")) == [(gone, False)]
    client.sql(main.q.task_written_sql() + " returning id", ('{"id": 5}', 5, 10, committed))
    client.sql("update lakefed_ingest.app_task set heartbeat_at = now() - interval '10 minutes' "
               "where id in (%s, %s) returning id", (gone, committed))
    read = {t: client.get(f"/api/tasks/{t}").json() for t in (gone, committed, alive)}
    assert (read[gone]["status"], read[gone]["error"]) == ("failed", main.TASK_ROLLED_BACK)
    assert (read[committed]["status"], read[committed]["error"]) == ("failed", main.TASK_UNSYNCED)
    assert read[alive]["status"] == "running"
    assert [t["id"] for t in client.get("/api/ingestion-jobs/5/tasks").json()] == [committed]
    # A late finish leaves an ended task as it is.
    main.db.execute(main.q.finish_task_sql(), ("succeeded", None, None, None, gone))
    assert client.get(f"/api/tasks/{gone}").json()["status"] == "failed"


# ---- uploads and selections ----------------------------------------------------------------------------------------

def test_an_upload_is_kept_once_and_its_top_level_and_pages_come_from_lakebase(client):
    text = _csv(30, 40) + "sql_a,,gen,orphan,id,id\n"
    start = time.perf_counter()
    up = client.post("/api/uploads", json={"csv": text, "connection": "sql_a", "file_name": "tables.csv"}).json()
    took = time.perf_counter() - start
    assert (up["lines"], up["tables"], up["invalid"]) == (1201, 1200, 1)
    assert up["invalid_lines"][0]["line"] == 1202 and "src_database is required" in up["invalid_lines"][0]["errors"]
    top = client.get(f"/api/sources/upload/{up['upload_id']}", params={"limit": 10}).json()
    assert top["totals"] == {"servers": 1, "databases": 30, "tables": 1200}
    assert [(s["server"], s["databases"], s["tables"], s["unset"]) for s in top["servers"]] == [("sql_a", 30, 1200, 1200)]
    assert len(top["databases"]) == 10 and top["databases"][0]["src_database"] == "eng_000"
    assert top["invalid_lines"][0]["line"] == 1202
    filtered = client.get(f"/api/sources/upload/{up['upload_id']}", params={"filter": "ENG_01"}).json()
    assert filtered["matched"] == {"databases": 10, "tables": 400} and filtered["totals"]["tables"] == 1200
    # A file reads through the one connection chosen with it: a line naming another is refused, and nothing is kept.
    other = client.post("/api/uploads", json={"csv": _csv(1, 2) + _csv(1, 1, server="sql_b", head=False),
                                              "connection": "sql_a"})
    assert other.status_code == 422 and "names sql_b, not sql_a" in other.text
    assert client.sql("select count(*) from lakefed_ingest.table_upload") == [(1,)]
    page = client.get(f"/api/sources/upload/{up['upload_id']}/tables",
                      params={"server": "sql_a", "database": "eng_003", "search": "t001", "limit": 4}).json()
    assert page["total"] == 10 and [t["src_table"] for t in page["tables"]] == ["t0010", "t0011", "t0012", "t0013"]
    assert client.get("/api/sources/upload/nope").status_code == 404
    print(f"1,211-line upload kept in {took:.2f} s")


def test_create_and_add_tables_by_reference_to_an_upload_keep_what_was_chosen(client):
    up = client.post("/api/uploads", json={"csv": _csv(20, 30), "connection": "sql_a"}).json()["upload_id"]
    source = {"upload_id": up, "whole_server": True, "filter": "eng_00"}
    preview = client.post("/api/ingestion-jobs", json={"name": "Ref", "settings": SETTINGS, "source": source}).json()
    assert preview["created"] is False and preview["summary"]["tables"] == 10 * 30
    assert client.sql("select count(*) from lakefed_ingest.control") == [(0,)]
    r = _create(client, "ref-1", name="Ref", source=source)
    assert r.status_code == 200 and r.json()["created"] is True
    job_id = r.json()["id"]
    assert client.sql("select count(*) from lakefed_ingest.control") == [(300,)]
    # Some tables of one more database, by name, then a whole database, as Add tables sends them.
    add = client.post(f"/api/ingestion-jobs/{job_id}/tables", json={"dry_run": False, "source": {
        "upload_id": up, "tables": [{"server": "sql_a", "database": "eng_015", "tables": ["gen.t0001", "GEN.T0002"]}],
        "databases": [{"server": "sql_a", "database": "eng_016"}]}})
    assert add.status_code == 200 and add.json()["applied"] is True and add.json()["summary"]["tables"] == 32
    assert client.sql("select count(*) from lakefed_ingest.control") == [(332,)]
    kept = client.get(f"/api/ingestion-jobs/{job_id}/selection").json()
    assert [(k["scope"], k["server"], k["src_database"], k["tables"], k["filter"], k["source_kind"]) for k in kept] == [
        ("server", "sql_a", None, None, "eng_00", "upload"), ("database", "sql_a", "eng_016", None, None, "upload"),
        ("tables", "sql_a", "eng_015", ["gen.t0001", "GEN.T0002"], None, "upload")]


def test_a_large_selection_runs_as_a_task(client):
    up = client.post("/api/uploads", json={"csv": _csv(10, 80), "connection": "sql_a"}).json()["upload_id"]
    r = _create(client, "ref-big", source={"upload_id": up, "whole_server": True})
    assert r.status_code == 202
    task = client.get(f"/api/tasks/{r.json()['task']['id']}").json()
    assert task["status"] == "succeeded" and task["done"] == 800
    assert client.sql("select count(*) from lakefed_ingest.job_selection where job_id = %s", (task["job_id"],)) == [(1,)]


def test_a_discovery_is_a_source_too(client):
    client.sql("insert into lakefed_ingest.discovery (discovery_id, src_type, src_database, status, src_connection) "
               "values ('d1', 'sqlserver', 'eng_001', 'ok', 'SQL_A'), ('d1', 'sqlserver', 'eng_002', 'ok', 'SQL_A'), "
               "('d2', 'sqlserver', 'eng_002', 'ok', 'SQL_A') returning 1")
    client.sql("insert into lakefed_ingest.discovered_table (discovery_id, src_database, src_schema, src_table, "
               "select_list, primary_key, load_type, row_count, discovered_at) values "
               "('d1', 'eng_001', 'gen', 't1', 'id, a', 'id', 'ct', 10, now() - interval '1 hour'), "
               "('d1', 'eng_002', 'gen', 't1', 'id, a', 'id', 'ct', 10, now() - interval '1 hour'), "
               "('d2', 'eng_002', 'gen', 't1', 'id, a, b', 'id', 'full', 12, now()), "
               "('d2', 'eng_002', 'gen', 't2', 'id', 'id', 'ct', 1, now()) returning 1")
    top = client.get("/api/sources/discovery/d1,d2").json()
    assert top["totals"] == {"servers": 1, "databases": 2, "tables": 3}
    assert [(d["src_database"], d["tables"], d["ct"], d["full"]) for d in top["databases"]] == [
        ("eng_001", 1, 1, 0), ("eng_002", 2, 1, 1)]     # eng_002.gen.t1 as d2, the latest, found it
    page = client.get("/api/sources/discovery/d1,d2/tables", params={"server": "sql_a", "database": "eng_002"}).json()
    assert [(t["src_table"], t["load_type"], t["row_count"]) for t in page["tables"]] == [("t1", "full", 12), ("t2", "ct", 1)]
    r = _create(client, "disc-1", name="Disc", source={"discovery_ids": ["d1", "d2"], "whole_server": True})
    assert r.status_code == 200 and r.json()["summary"]["tables"] == 3
    rows = client.sql("select src_connection, use_remote_query, src_database, src_table, load_type, select_list "
                      "from lakefed_ingest.control order by src_database, src_table")
    assert rows == [("SQL_A", True, "eng_001", "t1", "ct", "id, a"), ("SQL_A", True, "eng_002", "t1", "full", "id, a, b"),
                    ("SQL_A", True, "eng_002", "t2", "ct", "id")]


def test_a_create_by_reference_spreads_its_databases_evenly_by_tables(client):
    """queries.selected_databases_sql counts each chosen database's tables in Lakebase, and Create by reference spreads
    the databases over the groups by those counts: the biggest first, each into the group with the fewest tables."""
    import queries as q
    sizes = [60, 50, 40, 40, 30, 30, 20, 20, 10, 10, 10, 10]
    text = HEAD + "".join(f"sql_a,eng_{d:03d},gen,t{t:04d},\"id, a\",id\n" for d, n in enumerate(sizes) for t in range(n))
    up = client.post("/api/uploads", json={"csv": text, "connection": "sql_a"}).json()["upload_id"]
    counted = client.sql(q.selected_databases_sql("upload"), (up, True, "", [], []))
    assert [(server, database, tables) for server, database, tables in counted] == [
        ("sql_a", f"eng_{d:03d}", n) for d, n in enumerate(sizes)]
    r = _create(client, "even-1", name="Even", source={"upload_id": up, "whole_server": True}, groups_per_server=3)
    assert r.status_code in (200, 202)
    groups = client.sql("select task_collection, count(*), count(distinct src_database) from lakefed_ingest.control "
                        "group by task_collection order by task_collection")
    assert [g for g, _, _ in groups] == ["even_sql_a_c01", "even_sql_a_c02", "even_sql_a_c03"]
    assert sorted(tables for _, tables, _ in groups) == [110, 110, 110]
    split = client.sql("select src_database from lakefed_ingest.control group by src_database "
                       "having count(distinct task_collection) > 1")
    assert split == []                                         # a database is never split


def test_a_new_number_of_groups_moves_whole_databases_and_their_stamps_and_members(client, monkeypatch):
    """POST /groups against a real schema: the moved control rows, their databases' check stamps, both groups'
    rollups and the job's members, in one transaction; nothing moves on a preview."""
    import queries as q
    monkeypatch.setattr(main, "_job_active_runs", lambda job, members: [])     # asks Databricks: no run is going
    up = client.post("/api/uploads", json={"csv": _csv(12, 10), "connection": "sql_a"}).json()["upload_id"]
    r = _create(client, "regroup-1", name="Re", source={"upload_id": up, "whole_server": True}, groups_per_server=2)
    job_id = r.json().get("id") or r.json()["task"]["job_id"]
    group_of = dict(client.sql("select distinct lower(src_database), task_collection from lakefed_ingest.control"))
    stamped = next(iter(sorted(group_of)))
    client.sql("insert into lakefed_ingest.database_check (task_collection, source, src_database, checked_version, "
               "tables) select task_collection, 'sql_a', src_database, 5, 10 from lakefed_ingest.control "
               "where lower(src_database) = %s limit 1 returning 1", (stamped,))
    preview = client.post(f"/api/ingestion-jobs/{job_id}/groups", json={"groups_per_server": 3}).json()
    assert preview["blocked"] is None and preview["summary"]["new_groups"] == ["re_sql_a_c03"]
    assert dict(client.sql("select distinct lower(src_database), task_collection from lakefed_ingest.control")) == group_of
    done = client.post(f"/api/ingestion-jobs/{job_id}/groups", json={"groups_per_server": 3, "dry_run": False,
                                                                     "request_id": "regroup-2"})
    assert done.status_code == 200
    per_group = client.sql("select task_collection, count(distinct src_database), count(*) from lakefed_ingest.control "
                           "group by task_collection order by task_collection")
    assert [(g, d, t) for g, d, t in per_group] == [("re_sql_a_c01", 4, 40), ("re_sql_a_c02", 4, 40), ("re_sql_a_c03", 4, 40)]
    after = dict(client.sql("select distinct lower(src_database), task_collection from lakefed_ingest.control"))
    assert client.sql("select task_collection from lakefed_ingest.database_check where lower(src_database) = %s",
                      (stamped,)) == [(after[stamped],)]
    members = client.sql("select task_collection from lakefed_ingest.schedule_member where schedule_id = %s "
                         "and removed_at is null order by task_collection", (job_id,))
    assert [m for (m,) in members] == ["re_sql_a_c01", "re_sql_a_c02", "re_sql_a_c03"]
    rollups = dict(client.sql("select task_collection, tables from lakefed_ingest.rollup_group"))
    assert rollups == {"re_sql_a_c01": 40, "re_sql_a_c02": 40, "re_sql_a_c03": 40}
    moved = client.sql("select count(*) from lakefed_ingest.control_change where action = 'regroup'")
    assert moved == [(40,)]
