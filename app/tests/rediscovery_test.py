"""Re-discovery: when a job is due, what the rules add per selection part (a whole server with its filter, a whole
database, specific tables), the discovery specs that read each server again, and a run through the app's own write path
with a fake database. No database is needed."""
import contextlib
import json
import time
from datetime import datetime, timezone

import pytest

import rediscovery as rd
import queries as q

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import main  # noqa: E402
from api_test import FakeCursor, FakeDB  # noqa: E402

NOW = datetime(2026, 10, 29, 4, 0, tzinfo=timezone.utc)
YESTERDAY = "2026-10-28T03:00:00+00:00"
EARLIER_TODAY = "2026-10-29T01:00:03+00:00"


# ---- when a job is due --------------------------------------------------------------------------------------------

def test_a_job_is_due_from_its_hour_and_once_a_day():
    assert not rd.is_due({}, NOW)
    assert not rd.is_due({"rediscover_hour": 5}, NOW)            # 04:00 UTC: not yet 05:00
    assert rd.is_due({"rediscover_hour": 4}, NOW)
    assert rd.is_due({"rediscover_hour": 1}, NOW)                # the hour passed and nothing ran today yet
    assert not rd.is_due({"rediscover_hour": 1, "rediscovery": {"status": "done", "started_at": EARLIER_TODAY}}, NOW)
    assert rd.is_due({"rediscover_hour": 1, "rediscovery": {"status": "done", "started_at": YESTERDAY}}, NOW)
    assert not rd.is_due({"rediscover_hour": 1}, NOW, paused=True)


def test_a_running_re_discovery_holds_the_job_while_its_heartbeat_is_fresh():
    beating = {"status": "running", "started_at": "2026-10-28T20:00:00+00:00", "heartbeat_at": "2026-10-29T03:59:30+00:00"}
    assert not rd.is_due({"rediscover_hour": 1, "rediscovery": beating}, NOW)
    # Its thread is gone (no beat for minutes): the job is due again, on the next day as the once-a-day rule has it.
    stopped = {"status": "running", "started_at": "2026-10-28T20:00:00+00:00", "heartbeat_at": "2026-10-28T20:00:00+00:00"}
    assert rd.is_due({"rediscover_hour": 1, "rediscovery": stopped}, NOW)
    stopped_today = {"status": "running", "started_at": "2026-10-29T03:00:00+00:00", "heartbeat_at": "2026-10-29T03:01:00+00:00"}
    assert not rd.is_due({"rediscover_hour": 1, "rediscovery": stopped_today}, NOW)


@pytest.mark.parametrize("value", [None, -1, 24, True, "4", 4.0])
def test_an_hour_must_be_a_whole_hour_of_the_day(value):
    assert rd.hour_of({"rediscover_hour": value}) is None
    assert rd.hour_of({"rediscover_hour": 0}) == 0 and rd.hour_of({"rediscover_hour": 23}) == 23


def test_the_job_page_shows_the_hour_and_the_last_record():
    options = {"rediscover_hour": 3, "rediscovery": {"status": "done", "added": 2}}
    assert rd.view(options, NOW) == {"hour": 3, "last": {"status": "done", "added": 2},
                                     "progress": {"state": "done", "found": 0, "added": 2, "note": None}}
    assert rd.view({}, NOW) == {"hour": None, "last": None, "progress": None}


# ---- the rules, per selection part ----------------------------------------------------------------------------------

def test_a_whole_server_adds_each_new_database_matching_its_filter_with_all_its_tables():
    picked = rd.wanted([server_part("eng")], FOUND, STATUSES, [job_row("eng_001", "t1")])
    assert [table_of(r) for r in picked["add"]] == [("eng_002", "t1")]
    assert picked["new_databases"] == ["h1/eng_002"] and picked["vanished"] == []


def test_a_whole_server_with_no_filter_adds_every_new_database():
    picked = rd.wanted([server_part("")], FOUND, STATUSES, [])
    assert [table_of(r) for r in picked["add"]] == [("eng_001", "t1"), ("eng_001", "t2"),
                                                     ("eng_002", "t1"), ("prod_001", "t1")]
    assert picked["new_databases"] == ["h1/eng_001", "h1/eng_002", "h1/prod_001"]


def test_a_whole_server_does_not_add_new_tables_of_a_database_the_job_already_has():
    picked = rd.wanted([server_part("")], FOUND, STATUSES, [job_row("eng_001", "t1")])
    assert ("eng_001", "t2") not in [table_of(r) for r in picked["add"]]


def test_a_whole_database_adds_its_new_tables_and_nothing_elsewhere():
    picked = rd.wanted([database_part("eng_001")], FOUND, STATUSES, [job_row("eng_001", "t1")])
    assert [table_of(r) for r in picked["add"]] == [("eng_001", "t2")]
    assert picked["new_databases"] == []


def test_specific_tables_add_nothing_even_when_the_discovery_finds_new_ones():
    picked = rd.wanted([tables_part("eng_001", ["t1"])], FOUND, STATUSES, [])
    assert picked["add"] == [] and picked["new_databases"] == []


def test_adding_again_adds_nothing():
    first = rd.wanted([server_part("")], FOUND, STATUSES, [])
    job_rows = [job_row(r["src_database"], r["src_table"]) for r in first["add"]]
    again = rd.wanted([server_part("")], FOUND, STATUSES, job_rows)
    assert again["add"] == [] and again["new_databases"] == []


def test_a_database_that_cannot_be_opened_fails_alone():
    statuses = [status("eng_001", "failed", "login failed"), status("eng_002", "ok"), status("prod_001", "ok")]
    found = [r for r in FOUND if r["src_database"] != "eng_001"]
    picked = rd.wanted([server_part("")], found, statuses, [job_row("eng_001", "t9")])
    assert [table_of(r) for r in picked["add"]] == [("eng_002", "t1"), ("prod_001", "t1")]
    assert picked["errors"] == [{"database": "h1/eng_001", "error": "login failed"}]
    assert picked["vanished"] == []


def test_a_table_the_source_no_longer_has_is_kept_and_reported_as_vanished():
    picked = rd.wanted([database_part("eng_001")], FOUND, STATUSES, [job_row("eng_001", "t1"), job_row("eng_001", "t9")])
    assert [table_of(r) for r in picked["add"]] == [("eng_001", "t2")]
    assert picked["vanished"] == ["h1/eng_001.dbo.t9"]


def test_a_database_the_discovery_did_not_read_is_not_judged_vanished():
    picked = rd.wanted([server_part("")], FOUND, STATUSES, [job_row("prod_002", "t1")])
    assert picked["vanished"] == []


def test_a_removed_table_is_not_added_back_by_a_whole_server_or_a_whole_database():
    excluded = {rd.table_key("h1", "eng_001", "dbo", "t2")}
    picked = rd.wanted([server_part("")], FOUND, STATUSES, [job_row("eng_001", "t1")], excluded)
    assert [table_of(r) for r in picked["add"]] == [("eng_002", "t1"), ("prod_001", "t1")]
    picked = rd.wanted([database_part("eng_001")], FOUND, STATUSES, [job_row("eng_001", "t1")], excluded)
    assert picked["add"] == [] and picked["new_databases"] == []


def test_a_database_whose_only_new_tables_were_removed_is_not_new():
    excluded = {rd.table_key("h1", "eng_002", "dbo", "t1"), rd.table_key("h1", "prod_001", "dbo", "t1")}
    picked = rd.wanted([server_part("")], FOUND, STATUSES, [job_row("eng_001", "t1"), job_row("eng_001", "t2")],
                       excluded)
    assert picked["add"] == [] and picked["new_databases"] == []


def test_a_new_database_takes_its_tables_that_were_not_removed():
    found = FOUND + [found_row("eng_002", "t2")]
    excluded = {rd.table_key("h1", "eng_002", "dbo", "t1")}
    picked = rd.wanted([server_part("")], found, STATUSES, [job_row("eng_001", "t1"), job_row("eng_001", "t2")],
                       excluded)
    assert [table_of(r) for r in picked["add"]] == [("eng_002", "t2"), ("prod_001", "t1")]
    assert picked["new_databases"] == ["h1/eng_002", "h1/prod_001"]


def test_specific_tables_still_add_nothing_when_a_removed_table_is_among_them():
    excluded = {rd.table_key("h1", "eng_001", "dbo", "t1")}
    picked = rd.wanted([tables_part("eng_001", ["t1"])], FOUND, STATUSES, [], excluded)
    assert picked["add"] == [] and picked["new_databases"] == []


# ---- the discovery specs that read each server again ---------------------------------------------------------------

def test_a_connection_server_reads_again_as_a_whole_server_with_its_pattern():
    readings, errors = rd.specs([discovery_part(server_part("", server="sql_a"))], [connection_source()])
    assert errors == []
    [(server, spec)] = readings
    assert server == "sql_a"
    assert spec["src_connection"] == "sql_a" and spec["databases"] == "*" and spec["pattern"] == "lfc_%"
    assert spec["kind"] == "tables" and "src_host" not in spec


def test_a_host_discovered_before_the_listing_was_kept_cannot_read_a_whole_server_again():
    source = host_source(listing=None)
    readings, errors = rd.specs([discovery_part(server_part(""))], [source])
    assert readings == [] and "discovered before re-discovery kept how it lists" in errors[0]


def test_a_host_whole_server_reads_again_with_its_recorded_secret_template():
    source = host_source(listing={"secret_key_template": "creds-{database}"})
    [(server, spec)] = rd.specs([discovery_part(server_part(""))], [source])[0]
    assert spec["secret_key_template"] == "creds-{database}" and "secret_key" not in spec and spec["databases"] == "*"


def test_a_host_whose_databases_take_one_secret_each_reads_each_secret_apart():
    sources = [host_source(database="eng_001", secret="s1"), host_source(database="eng_002", secret="s2")]
    parts = [discovery_part(database_part("eng_001")), discovery_part(database_part("eng_002"))]
    readings, errors = rd.specs(parts, sources)
    assert errors == []
    assert sorted((spec["databases"][0], spec["secret_key"]) for _, spec in readings) == [
        ("eng_001", "s1"), ("eng_002", "s2")]


def test_a_server_that_is_no_longer_kept_says_so():
    readings, errors = rd.specs([discovery_part(server_part(""))], [])
    assert readings == [] and "no longer kept" in errors[0]


def test_a_connection_server_with_only_specific_tables_is_not_read_again():
    readings, errors = rd.specs([discovery_part(tables_part("eng_001", ["t1"], server="sql_a"))], [connection_source()])
    assert readings == [] and errors == []


def test_a_table_only_server_that_is_no_longer_kept_is_not_reported():
    readings, errors = rd.specs([discovery_part(tables_part("eng_001", ["t1"]))], [])
    assert readings == [] and errors == []


# ---- a run, through the app's write path (fake database) ------------------------------------------------------------

def test_the_tick_starts_each_due_job_once_and_skips_one_it_cannot_claim(monkeypatch):
    claims = [True, False]
    started = []
    monkeypatch.setattr(main, "_start_background", lambda work: work())
    monkeypatch.setattr(main, "_run_rediscovery", lambda sid, when: started.append(sid))
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): _tick_rows(sql, claims))
    assert main._rediscovery_tick(NOW) == [7]
    assert main._rediscovery_tick(NOW) == []
    assert started == [7]


def test_a_run_adds_what_the_rules_allow_through_the_add_tables_path_as_re_discovery(monkeypatch):
    fake, records, syncs = FakeDB(), [], []
    _fake_run(monkeypatch, fake, records, syncs)
    main._run_rediscovery(7, NOW)
    record = json.loads(records[-1][1][0])
    assert record["status"] == "done" and record["found"] == 1 and record["added"] == 1
    assert record["new_databases"] == ["h1/eng_002"] and record["vanished"] == {"count": 0, "tables": []}
    inserted = fake.of("insert into lakefed_ingest.control (")
    assert len(inserted) == 1 and inserted[0][q.CONTROL_INSERT_COLUMNS.index("src_database")] == "eng_002"
    assert {p[0] for p in fake.of("insert into lakefed_ingest.control_change")} == {"re-discovery"}
    assert syncs == []


def test_a_run_that_meets_a_running_group_is_deferred_and_writes_nothing(monkeypatch):
    fake, records, syncs = FakeDB(), [], []
    _fake_run(monkeypatch, fake, records, syncs)

    def running(cur, collections):
        raise main._running_refusal("g1")

    monkeypatch.setattr(main, "_lock_collections", running)
    main._run_rediscovery(7, NOW)
    record = json.loads(records[-1][1][0])
    assert record["status"] == "deferred" and "is running" in record["error"]
    assert fake.of("insert into lakefed_ingest.control (") == []


def test_a_clash_refusal_naming_a_job_called_running_is_failed_not_deferred(monkeypatch):
    fake, records, syncs = FakeDB(), [], []
    _fake_run(monkeypatch, fake, records, syncs)

    def clash(cur, collections):
        raise main.HTTPException(409, {"message": "database sales is split with the job orders running total",
                                       "jobs": [{"id": 3, "name": "orders running total"}]})

    monkeypatch.setattr(main, "_lock_collections", clash)
    main._run_rediscovery(7, NOW)
    record = json.loads(records[-1][1][0])
    assert record["status"] == "failed" and "orders running total" in record["error"]


def test_only_the_running_reason_reads_as_running():
    assert main._refused_while_running(main._running_refusal("g1"))
    assert not main._refused_while_running(main.HTTPException(409, "a job named running is going"))
    assert not main._refused_while_running(main.HTTPException(422, {"reason": "running"}))


# ---- private helpers ---------------------------------------------------------------------------------------------------

def found_row(database: str, table: str, server: str = "h1") -> dict:
    return {"server": server, "src_database": database, "src_schema": "dbo", "src_table": table,
            "fields": {"src_host": server, "src_database": database, "src_schema": "dbo", "src_table": table,
                       "src_port": 1433, "secret_key": f"creds-{database}", "load_type": "ct",
                       "select_list": "id, v", "primary_key": "id"}}


FOUND = [found_row("eng_001", "t1"), found_row("eng_001", "t2"), found_row("eng_002", "t1"),
         found_row("prod_001", "t1")]
STATUSES = [{"server": "h1", "src_database": name, "status": "ok", "error": None}
            for name in ("eng_001", "eng_002", "prod_001")]


def status(database: str, value: str, error=None) -> dict:
    return {"server": "h1", "src_database": database, "status": value, "error": error}


def server_part(filter_: str, server: str = "h1") -> dict:
    return {"scope": "server", "server": server, "src_database": None, "tables": None, "filter": filter_}


def database_part(database: str, server: str = "h1") -> dict:
    return {"scope": "database", "server": server, "src_database": database, "tables": None, "filter": None}


def tables_part(database: str, tables: list, server: str = "h1") -> dict:
    return {"scope": "tables", "server": server, "src_database": database, "tables": tables, "filter": None}


def discovery_part(part: dict, server: str = "h1") -> dict:
    return {**part, "source_kind": "discovery", "source_id": "d1", "server": part.get("server", server)}


def job_row(database: str, table: str, server: str = "h1") -> dict:
    return {**{c: None for c in ("id",) + q.CONTROL_EDIT_COLUMNS}, "task_collection": "g1", "task_enabled": True,
            "src_type": "sqlserver", "src_host": server, "src_port": 1433, "secret_key": f"creds-{database}",
            "src_database": database, "src_schema": "dbo", "src_table": table, "select_list": "id",
            "primary_key": "id", "load_type": "ct", "sink_catalog": "main", "sink_schema": "bronze",
            "sink_table": f"{database}_{table}", "write_mode": "merge", "load_partitioned": False,
            "use_remote_query": False}


def table_of(row: dict) -> tuple:
    return row["src_database"], row["src_table"]


def connection_source() -> dict:
    return {"server": "sql_a", "src_connection": "sql_a", "src_host": None, "src_port": None, "src_type": "sqlserver",
            "secret_key": None, "src_database": "master", "listing": {"pattern": "lfc_%"}, "finished_at": YESTERDAY}


def host_source(database: str = "eng_001", secret: str = "s1", listing="none") -> dict:
    listing = {} if listing == "none" else listing
    return {"server": "h1", "src_connection": None, "src_host": "h1", "src_port": 1433, "src_type": "sqlserver",
            "secret_key": secret, "src_database": database, "listing": listing, "finished_at": YESTERDAY}


def _tick_rows(sql: str, claims: list):
    if sql == q.schedules_sql():
        return [{"id": 7, "paused": False, "options": {"rediscover_hour": 1}},
                {"id": 8, "paused": True, "options": {"rediscover_hour": 1}}]
    if sql == q.claim_rediscovery_sql():
        return [{"id": 7}] if claims.pop(0) else []
    raise AssertionError(sql)


def _fake_run(monkeypatch, fake: FakeDB, records: list, syncs: list) -> None:
    """The whole run with the database faked: the job has eng_001.t1 under a whole server (filter 'eng'), and the
    discovery finds eng_002 as new. The discover job's run is started and reads as succeeded."""
    job = {"id": 7, "name": "lakefed job", "cadence_minutes": 1440, "stagger": False, "options": {}, "paused": False,
           "job_variant": "default", "timezone": "UTC", "created_at": None, "deleted_at": None,
           "created_by": "ops@example.com"}
    # The job reads by host, so its maker must be an app manager (host_reads_test.py has the refusals).
    monkeypatch.setattr(main, "_manage_refusal", lambda user: None if user == "ops@example.com" else "not a manager")
    members = [{"id": 1, "schedule_id": 7, "task_collection": "g1", "offset_minutes": 0, "job_id": 99,
                "synced_at": None, "sync_error": None, "added_at": None, "removed_at": None, "delete_pending": False}]
    selection = [{"scope": "server", "server": "h1", "src_database": None, "tables": None, "filter": "eng",
                  "source_kind": "discovery", "source_id": "d1"}]
    sources = [host_source(listing={"pattern": "eng%", "secret_key_template": "creds-{database}"})]
    job_rows = [job_row("eng_001", "t1")]
    statuses = [{"server": "h1", "src_database": name, "status": "ok", "error": None} for name in ("eng_001", "eng_002")]
    found = [found_row("eng_001", "t1"), found_row("eng_002", "t1")]
    answers = {
        q.schedule_sql(): [job], q.schedule_members_sql(): members, q.collections_sql(): [{"task_collection": "g1"}],
        q.job_selection_sql(): selection, q.selection_discoveries_sql(): sources,
        q.discovery_statuses_sql(): statuses, q.source_rows_sql("discovery"): found,
    }
    for_update = q.groups_control_rows_sql(for_update=True)

    def query(sql, params=()):
        if sql not in answers:
            raise AssertionError(sql)
        return answers[sql]

    def fetch(cur, sql, params=()):
        return job_rows if sql == for_update else members if sql == q.schedule_members_sql() else [job]

    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main.db, "fetch", fetch)
    monkeypatch.setattr(main.db, "execute", lambda sql, params=(): records.append((sql, params)))
    monkeypatch.setattr(main, "_write", contextlib.contextmanager(lambda: (yield FakeCursor(fake))))
    monkeypatch.setattr(main, "_lock_job", lambda cur, job_id: ["g1"])
    monkeypatch.setattr(main, "_lock_collections", lambda cur, collections: None)
    monkeypatch.setattr(main, "_sync", lambda sid, user: syncs.append(sid))
    monkeypatch.setattr(main.jobs, "DISCOVER_JOB_ID", "42")
    monkeypatch.setattr(main.jobs, "run_now", lambda job_id, parameters: 99)
    monkeypatch.setattr(main.jobs, "run_ended", lambda run_id: {"state": "TERMINATED", "result": "SUCCESS",
                                                               "message": None, "ended": None})
    monkeypatch.setattr(main, "REDISCOVERY_POLL_S", 0)


# ---- what the job page shows while a re-discovery runs --------------------------------------------------------------

def test_a_re_discovery_that_has_not_read_a_server_yet_is_pending():
    assert rd.progress(rd.running_record(NOW), NOW) == {"state": "pending", "url": None, "server": None}


def test_while_a_discovery_run_goes_the_page_is_pending_with_its_run_and_server():
    last = {"status": "running", "started_at": "2026-10-29T03:50:00+00:00", "heartbeat_at": "2026-10-29T03:59:50+00:00",
            "phase": rd.DISCOVERING, "run_id": 5, "server": "h1"}
    assert rd.progress(last, NOW, {"state": "pending", "url": "https://h/run/5"}) == {
        "state": "pending", "url": "https://h/run/5", "server": "h1"}


def test_a_discovery_run_past_its_wait_is_a_timeout_and_not_done():
    last = {"status": "running", "started_at": "2026-10-29T03:50:00+00:00", "heartbeat_at": "2026-10-29T03:59:50+00:00",
            "phase": rd.DISCOVERING, "run_id": 5, "server": "h1"}
    outcome = {"state": "timeout", "url": "https://h/run/5",
               "reason": "Databricks has not reported it ended after 360 minutes: it is still listed as running"}
    progress = rd.progress(last, NOW, outcome)
    assert progress["state"] == "timeout" and progress["reason"] == outcome["reason"]


def test_while_the_tables_are_written_the_page_says_adding():
    last = {"status": "running", "started_at": "2026-10-29T03:50:00+00:00", "heartbeat_at": "2026-10-29T03:59:50+00:00",
            "phase": rd.ADDING}
    assert rd.progress(last, NOW) == {"state": rd.ADDING, "server": None}


def test_a_finished_re_discovery_says_what_it_found_and_added():
    last = {"status": "done", "found": 3, "added": 2, "note": None, "started_at": YESTERDAY}
    assert rd.progress(last, NOW) == {"state": "done", "found": 3, "added": 2, "note": None}


def test_a_re_discovery_that_failed_or_was_deferred_says_why():
    assert rd.progress({"status": "failed", "error": "discovery run ended FAILED: no route"}, NOW) == {
        "state": "failed", "reason": "discovery run ended FAILED: no route"}
    deferred = {"status": "deferred", "error": "ingestion group 'g1' is running; try again after the run finishes"}
    assert rd.progress(deferred, NOW)["state"] == "failed"
    assert "is running" in rd.progress(deferred, NOW)["reason"]


def test_a_running_re_discovery_whose_heartbeat_stopped_failed_because_the_app_restarted():
    stopped = {"status": "running", "started_at": "2026-10-29T03:00:00+00:00", "heartbeat_at": "2026-10-29T03:01:00+00:00",
               "phase": rd.DISCOVERING, "run_id": 5, "server": "h1"}
    assert rd.progress(stopped, NOW, {"state": "pending"}) == {"state": "failed", "reason": "the app restarted; run it again"}
    adding = {**stopped, "phase": rd.ADDING}
    assert rd.progress(adding, NOW) == {"state": "failed", "reason": rd.RESTARTED}


def test_a_fresh_heartbeat_keeps_a_run_pending_however_long_ago_it_started():
    beating = {"status": "running", "started_at": "2026-10-28T20:00:00+00:00", "heartbeat_at": "2026-10-29T03:59:30+00:00",
               "phase": rd.DISCOVERING, "run_id": 5, "server": "h1"}
    assert rd.progress(beating, NOW, {"state": "pending", "url": None}) == {"state": "pending", "url": None, "server": "h1"}


def test_the_claim_and_the_beat_read_the_heartbeat():
    claim = q.claim_rediscovery_sql()
    assert "'heartbeat_at'" in claim and "make_interval(secs => %s)" in claim
    beat = q.beat_rediscovery_sql()
    assert "jsonb_set(options, '{rediscovery,heartbeat_at}'" in beat and "options->'rediscovery'->>'status' = 'running'" in beat
    assert rd.STALE_S == 120 and rd.BEAT_S * 4 <= rd.STALE_S


def test_no_record_has_no_progress():
    assert rd.progress(None, NOW) is None


def test_a_run_records_each_phase_as_it_starts_and_its_outcome_last(monkeypatch):
    fake, records, syncs = FakeDB(), [], []
    _fake_run(monkeypatch, fake, records, syncs)
    main._run_rediscovery(7, NOW)
    written = [json.loads(params[0]) for sql, params in records if sql == q.set_rediscovery_sql()]
    assert [w.get("phase") for w in written] == [rd.DISCOVERING, rd.ADDING, None]
    assert written[0].items() >= {"status": "running", "started_at": NOW.isoformat(), "phase": rd.DISCOVERING,
                                  "run_id": 99, "server": "h1"}.items()
    assert "heartbeat_at" in written[0]
    assert written[-1]["status"] == "done" and "run_id" not in written[-1] and "server" not in written[-1]


def test_run_now_starts_a_re_discovery_unless_one_holds_the_job(monkeypatch):
    from fastapi.testclient import TestClient
    started = []
    monkeypatch.setattr(main, "_load_schedule", lambda sid, cur=None: ([{"id": 7, "options": {}}], []))
    monkeypatch.setattr(main, "_job_permission_refusals", lambda s, user: {"change": None})
    monkeypatch.setattr(main, "_start_background", lambda work: work())
    monkeypatch.setattr(main, "_run_rediscovery", lambda sid, when: started.append(sid))
    client = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})

    monkeypatch.setattr(main.db, "query", lambda sql, params=(): [{"id": 7}])
    assert client.post("/api/ingestion-jobs/7/rediscover").status_code == 200 and started == [7]

    monkeypatch.setattr(main.db, "query", lambda sql, params=(): [])
    refused = client.post("/api/ingestion-jobs/7/rediscover")
    assert refused.status_code == 409 and "already running" in refused.json()["detail"]
    assert started == [7]


def test_run_now_is_refused_when_the_job_cannot_change(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main, "_load_schedule", lambda sid, cur=None: ([{"id": 7, "options": {}}], []))
    monkeypatch.setattr(main, "_job_permission_refusals", lambda s, user: {"change": "the job is being deleted"})
    claims = []
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): claims.append(sql) or [{"id": 7}])
    response = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"}).post("/api/ingestion-jobs/7/rediscover")
    assert response.status_code >= 400 and claims == []


def test_a_running_re_discovery_beats_its_heartbeat_while_it_goes(monkeypatch):
    beats, records = [], []
    monkeypatch.setattr(rd, "BEAT_S", 0.01)
    monkeypatch.setattr(main, "_forget_shared_reads", lambda: None)

    def execute(sql, params=()):
        (beats if sql == q.beat_rediscovery_sql() else records).append((sql, params))

    monkeypatch.setattr(main.db, "execute", execute)

    def slow_run(sid, report):
        deadline = time.time() + 2
        while len(beats) < 2 and time.time() < deadline:
            time.sleep(0.01)
        return {"found": 0, "added": 0}

    monkeypatch.setattr(main, "_rediscover", slow_run)
    main._run_rediscovery(7, NOW)
    assert len(beats) >= 2 and all(params[1] == 7 for _, params in beats)
    assert all(params[0] for _, params in beats)
    written = [json.loads(params[0]) for sql, params in records if sql == q.set_rediscovery_sql()]
    assert written[-1]["status"] == "done" and "heartbeat_at" in written[-1]

