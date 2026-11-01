"""A job that isn't Ready yet, through the API (main.py) with the schedule endpoints' fake Lakebase and Jobs API
(schedules_api_test): what every route refuses or limits while it prepares, the stored settings no edit reaches, the
set-aside databases and their actions with today's job permissions, the write-time checks stage B calls with the job's
maker and its connection as stored then, and Ready.

The routes that write a job's options, each checked here: POST /api/schedules (create), POST /api/schedules/{id}
(Edit's save), POST /api/ingestion-jobs (Create, in new_job's and the big path's fixtures), pause and resume, the
groups (regroup), the settings, the set-aside actions, a table removal and Add tables' exclusions (refused while it
prepares), and re-discovery (refused, and skipped by its scheduler). Delete, Delete now and Restore write only their
own options and work while it prepares."""
import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import jdbc_logins as jl  # noqa: E402
import main  # noqa: E402
import new_job as nj  # noqa: E402
import preparing as p  # noqa: E402
import queries as q  # noqa: E402
import schedules as sch  # noqa: E402
import jdbc_connections as jc  # noqa: E402

from schedules_api_test import BODY, api  # noqa: E402,F401

SETTINGS = nj.settings_of({"fan_in": True, "sink_catalog": "main", "sink_schema": "bronze", "sink_table": "{table}"},
                          new_job=True)
LOGIN_SCOPE = "lakefed_ingest_jdbc_logins"
CONNECTION = {"name": "orders jdbc", "src_type": "sqlserver", "src_host": "orders.internal.example", "src_port": 1433,
              "secret_scope": LOGIN_SCOPE, "secret_key": "orders-{database}", "trust_server_certificate": "true",
              "created_by": "ann@example.com", "created_at": None}


@pytest.fixture
def prep(api, monkeypatch):  # noqa: F811
    """The schedule endpoints' fixture, with the statements a job's preparation adds answered from the store: its
    record (set_prepare_sql), its selection, the reads of its claimed drafts, its groups' tables and logins."""
    store = api.store
    store.selection, store.reads, store.newest_logins, store.controls = {}, {}, {}, {}
    store.connection_rows, store.claimed, store.failed, store.started = {}, [], {}, []
    base_run, base_query = store.run, store.query

    def run(sql, params):
        if sql == q.set_prepare_sql():
            record, sid = params
            row = store.schedules[sid]
            row["options"] = json.dumps({**sch.options_of(row), p.OPTION: json.loads(record)})
            return []
        if sql == q.claim_rediscovery_sql():
            store.claimed.append(params[1])
            return [{"id": params[1]}]
        return base_run(sql, params)

    def query(sql, params=()):
        if sql == q.job_selection_sql():
            return [dict(part) for part in store.selection.get(params[0], [])]
        if sql == q.job_reads_sql():
            return [dict(store.reads.get(params[2]) or {"databases": 0})]
        if sql == q.groups_row_count_sql():
            return [{"n": 0}]
        if sql == q.control_groups_sql():
            return [{"task_collection": store.controls[i]} for i in params[0] if i in store.controls]
        if sql == q.selection_discoveries_sql():
            return [{"listing": {"jdbc_connection": name}} for name in store.connection_rows]
        if sql == jc.get_sql():
            return [dict(store.connection_rows[params[0]])] if params[0] in store.connection_rows else []
        if sql == q.jdbc_login_latest_per_key_sql():
            return [{"secret_key": key, "connection": store.newest_logins[key]} for key in params[1]
                    if key in store.newest_logins]
        if sql == q.claim_rediscovery_sql():
            return run(sql, params)
        if sql == q.job_failed_reads_sql():
            rows = store.failed.get(int(params[1]), [])
            return [dict(r, total=len(rows)) for r in rows]
        if sql in (q.prepare_read_again_sql(), q.prepare_leave_out_reads_sql()):
            _, job, every, servers, names = params
            rows = store.failed.get(int(job), [])
            hit = [r for r in rows if every or (r["server"], r["src_database"].lower()) in set(zip(servers, names))]
            store.failed[int(job)] = [r for r in rows if r not in hit]
            return [dict(r) for r in hit]
        return base_query(sql, params)

    store.run, store.query = run, query
    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main.db, "execute", lambda sql, params=(): run(sql, params))
    monkeypatch.setattr(main.db, "fetch", lambda cur, sql, params=(): query(sql, params))
    monkeypatch.setattr(main, "_start_prepare", lambda sid, backoff_s=0.0: store.started.append(sid))
    return api


def _job(client, state=p.PREPARING, stage=p.READING, set_aside=(), maker="ann@example.com", scope=None,
         turn_on=True, **options):
    """A job that isn't Ready (or is, with state READY), made as a schedule of groups c1 and c2, paused, with its record
    and the given options, chosen from the discovery d1."""
    sid = client.post("/api/schedules", json=dict(BODY, paused=True)).json()["id"]
    record = p.new_record(maker, SETTINGS, {"tables_per_group": None, "groups_per_server": None}, scope or {},
                          {"kind": "discovery", "source_id": "d1"}, turn_on=turn_on, state=state, stage=stage)
    if stage in (p.WRITING, p.SYNCING):
        record = p.moved(record, stage=stage)
    if set_aside:
        record = p.with_set_aside(record, set_aside)
    row = client.store.schedules[sid]
    row["options"] = json.dumps({**sch.options_of(row), **options, p.OPTION: record})
    client.store.selection[sid] = [{"scope": "server", "server": "sql_a", "src_database": None, "tables": None,
                                    "filter": "eng", "source_kind": "discovery", "source_id": "d1"}]
    return sid


def _record(client, sid):
    return p.record_of(sch.options_of(client.store.schedules[sid]))


def _not_ready(response, stage=p.READING):
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason"] == p.NOT_READY_REASON and detail["stage"] == stage
    return detail["message"]


# ---- the run gate ---------------------------------------------------------------------------------------------------

def test_run_now_is_refused_while_the_job_prepares_saying_which_stage(prep):
    sid = _job(prep)
    message = _not_ready(prep.post(f"/api/ingestion-jobs/{sid}/run"))
    assert message == ("the job isn't ready yet (preparing: reading tables): Run now is available when the job is "
                       "ready")
    job, members = main._load_schedule(sid)
    assert main._run_now_blocker(job, members) == message


def test_a_writing_job_says_so_and_a_job_that_needs_you_says_how_many(prep):
    sid = _job(prep, stage=p.WRITING)
    assert "(preparing: writing the job's tables)" in _not_ready(prep.post(f"/api/ingestion-jobs/{sid}/run"),
                                                                p.WRITING)
    other = _job(prep, stage=p.WRITING, set_aside=[p.entry("sql_a", "eng_1", p.UNREADABLE, "Login failed")])
    response = prep.post(f"/api/ingestion-jobs/{other}/run")
    assert response.status_code == 409 and response.json()["detail"]["state"] == p.NEEDS_YOU
    assert "(needs you: 1 database set aside)" in response.json()["detail"]["message"]


def test_a_ready_job_with_its_record_runs_as_before(prep, monkeypatch):
    sid = _job(prep, state=p.READY)
    job, members = main._load_schedule(sid)
    assert main._run_now_blocker(job, [m for m in members if m["removed_at"] is None]) is None


def test_nothing_else_starts_a_run_of_a_job_that_isnt_ready(prep, monkeypatch):
    sid = _job(prep)
    monkeypatch.setattr(main.jobs, "INGEST_JOB_ID", "555")
    started = []
    monkeypatch.setattr(main.jobs, "run_now", lambda job_id, parameters: started.append(parameters) or 1)
    _not_ready(prep.post(f"/api/ingestion-jobs/{sid}/failing-tables/retry", json={"task_collection": "c1"}))
    _not_ready(prep.post("/api/jobs/ingest/run", json={"task_collection": "c1"}))
    assert started == []
    assert prep.post("/api/jobs/ingest/run", json={"task_collection": "c9"}).status_code == 200
    assert [run["task_collection"] for run in started] == ["c9"]


def test_the_schedule_does_not_turn_on_until_ready_and_pause_keeps_it_off_then(prep):
    sid = _job(prep)
    assert "its schedule is available when the job is ready" in _not_ready(prep.post(f"/api/schedules/{sid}/resume"))
    assert prep.store.schedules[sid]["paused"] is True and _record(prep, sid)["turn_on"] is True
    paused = prep.post(f"/api/schedules/{sid}/pause")
    assert paused.status_code == 200 and paused.json()["turn_on"] is False
    assert _record(prep, sid)["turn_on"] is False and prep.store.schedules[sid]["paused"] is True
    assert any(line[1] == "schedule_pause" for line in prep.store.log)


def test_re_discovery_waits_for_ready(prep, monkeypatch):
    sid = _job(prep, rediscover_hour=0)
    ready = _job(prep, state=p.READY, rediscover_hour=0)
    prep.store.schedules[ready]["paused"] = False
    prep.store.schedules[sid]["paused"] = False
    _not_ready(prep.post(f"/api/ingestion-jobs/{sid}/rediscover"))
    monkeypatch.setattr(main, "_start_background", lambda work: None)
    assert main._rediscovery_tick(datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)) == [ready]
    assert prep.store.claimed == [ready]


# ---- what it ingests waits for Ready --------------------------------------------------------------------------------

def test_the_job_routes_that_change_what_it_ingests_are_refused(prep, monkeypatch):
    sid = _job(prep, stage=p.WRITING)
    monkeypatch.setattr(main.jobs, "DELETE_JOB_ID", "900")
    refused = [
        prep.post(f"/api/ingestion-jobs/{sid}/enable", json={"enabled": False}),
        prep.post(f"/api/ingestion-jobs/{sid}/tables/edit", json={"schema": "dbo", "table": "t1",
                                                                  "changes": {"load_type": "full"}, "dry_run": False}),
        prep.post(f"/api/ingestion-jobs/{sid}/databases", json={"databases": [{"server": "sql_a", "database": "x"}],
                                                                "dry_run": False}),
        prep.post(f"/api/ingestion-jobs/{sid}/tables/remove", json={"control_ids": [1], "confirmed": True}),
        prep.post(f"/api/ingestion-jobs/{sid}/tables", json={"csv": "src_connection,src_database,src_schema,src_table\n"
                                                                    "sql_a,eng_9,dbo,t1\n", "dry_run": False}),
    ]
    for response in refused:
        assert "what it ingests can be changed when the job is ready" in _not_ready(response, p.WRITING)


def test_the_table_routes_that_name_groups_are_refused_for_a_preparing_jobs_group(prep):
    sid = _job(prep, stage=p.WRITING)
    prep.store.controls = {1: "c1", 2: "c9"}
    _not_ready(prep.post("/api/tables/enable", json={"ids": [1], "enabled": False}), p.WRITING)
    _not_ready(prep.post("/api/tables/reseed", json={"ids": [1]}), p.WRITING)
    assert sid


# ---- the stored settings: editable while reading, then waiting for Ready ---------------------------------------------

def test_a_preparing_jobs_settings_are_the_ones_its_record_keeps(prep):
    sid = _job(prep)
    shown = prep.get(f"/api/ingestion-jobs/{sid}/settings").json()
    assert shown["stored"] is True and shown["settings_locked"] is None
    assert shown["settings"]["sink_schema"] == {"value": "bronze"} and shown["settings"]["fan_in"] == {"value": True}
    assert shown["settings"]["client_id"] == {"value": "{server}_{database}"}
    assert shown["settings"]["identity"] == {"value": {"source_server": "{server}", "source_database": "{database}"}}


def test_while_reading_a_settings_edit_changes_the_record_and_is_checked_as_create_checks(prep):
    sid = _job(prep)
    preview = prep.post(f"/api/ingestion-jobs/{sid}/settings", json={"settings": {"sink_schema": "silver"}})
    assert preview.status_code == 200 and preview.json()["summary"]["changed"] == 1
    assert _record(prep, sid)["settings"]["sink_schema"] == "bronze"
    applied = prep.post(f"/api/ingestion-jobs/{sid}/settings", json={"settings": {"sink_schema": "silver"},
                                                                     "dry_run": False})
    assert applied.json()["summary"]["applied"] is True and _record(prep, sid)["settings"]["sink_schema"] == "silver"
    bad = prep.post(f"/api/ingestion-jobs/{sid}/settings", json={"settings": {"sink_table": "{nope}"},
                                                                 "dry_run": False})
    assert bad.status_code == 422 and _record(prep, sid)["settings"]["sink_table"] == "{table}"
    unknown = prep.post(f"/api/ingestion-jobs/{sid}/settings", json={"settings": {"lowercase_all": True}})
    assert unknown.status_code == 422 and "unknown setting lowercase_all" in unknown.text


def test_once_the_write_has_begun_the_settings_and_groups_wait_for_ready(prep):
    sid = _job(prep, stage=p.WRITING)
    assert prep.get(f"/api/ingestion-jobs/{sid}/settings").json()["settings_locked"]
    message = _not_ready(prep.post(f"/api/ingestion-jobs/{sid}/settings",
                                   json={"settings": {"sink_schema": "silver"}, "dry_run": False}), p.WRITING)
    assert "editable when the job is ready" in message
    _not_ready(prep.post(f"/api/ingestion-jobs/{sid}/groups", json={"tables_per_group": 2000, "dry_run": False}),
               p.WRITING)
    assert _record(prep, sid)["settings"]["sink_schema"] == "bronze"


def test_while_reading_the_groups_size_is_kept_on_the_record_for_the_write(prep):
    sid = _job(prep)
    preview = prep.post(f"/api/ingestion-jobs/{sid}/groups", json={"tables_per_group": 2000})
    assert preview.json()["summary"]["stored"] is True and _record(prep, sid)["sizing"]["tables_per_group"] is None
    applied = prep.post(f"/api/ingestion-jobs/{sid}/groups", json={"tables_per_group": 2000, "dry_run": False})
    assert applied.json()["summary"]["applied"] is True
    assert _record(prep, sid)["sizing"] == {"tables_per_group": 2000, "groups_per_server": None}
    assert sch.options_of(prep.store.schedules[sid])[main.TABLES_PER_GROUP_OPTION] == 2000


def test_a_settings_or_groups_change_needs_can_manage_as_edit_does(prep, monkeypatch):
    sid = _job(prep)
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_VIEW")
    assert prep.post(f"/api/ingestion-jobs/{sid}/settings", json={"settings": {"sink_schema": "silver"},
                                                                  "dry_run": False}).status_code == 403
    assert prep.post(f"/api/ingestion-jobs/{sid}/groups", json={"tables_per_group": 2000,
                                                                "dry_run": False}).status_code == 403
    assert _record(prep, sid)["settings"]["sink_schema"] == "bronze"


# ---- Edit's save: name, schedule, e-mails and advanced settings change; the rest is kept ----------------------------

def test_edit_saves_the_name_and_schedule_of_a_preparing_job_and_keeps_its_groups_and_record(prep):
    sid = _job(prep)
    forged = p.ready(_record(prep, sid))
    body = dict(BODY, name="Renamed", cadence_minutes=30, paused=True, members=[], notify_emails="a@example.com",
                parameters={"parallelism": "8"}, options={p.OPTION: forged, "rediscover_hour": 3})
    saved = prep.post(f"/api/schedules/{sid}", json=body)
    assert saved.status_code == 200, saved.text
    row = prep.store.schedules[sid]
    assert (row["name"], row["cadence_minutes"], row["notify_emails"]) == ("Renamed", 30, "a@example.com")
    assert sorted(m["task_collection"] for m in prep.store.members.values()
                  if m["schedule_id"] == sid and m["removed_at"] is None) == ["c1", "c2"]
    assert _record(prep, sid)["state"] == p.PREPARING              # the forged record was ignored
    assert sch.options_of(row)["rediscover_hour"] == 3


def test_edit_may_not_turn_the_schedule_on_or_change_the_scope_while_the_job_prepares(prep, monkeypatch):
    sid = _job(prep, scope={"secret_scope": "lakefed"})
    prep.store.schedules[sid]["parameters"] = {"secret_scope": "lakefed"}
    on = prep.post(f"/api/schedules/{sid}", json=dict(BODY, paused=False, members=[]))
    assert "its schedule is available when the job is ready" in _not_ready(on)
    monkeypatch.setattr(main, "_manage_refusal", lambda user: None)
    moved = prep.post(f"/api/schedules/{sid}", json=dict(BODY, paused=True, members=[],
                                                         parameters={"secret_scope": "other"}))
    assert "secret_scope is kept as Create checked" in _not_ready(moved)
    kept = prep.post(f"/api/schedules/{sid}", json=dict(BODY, paused=True, members=[],
                                                        parameters={"secret_scope": "lakefed", "parallelism": "4"}))
    assert kept.status_code == 200 and prep.store.schedules[sid]["parameters"]["parallelism"] == "4"


def test_an_edit_keeps_the_record_as_it_is_under_the_lock_not_as_the_request_read_it(prep, monkeypatch):
    sid = _job(prep)
    validate = main._validate_schedule

    def moved_meanwhile(body, members_kept=False):
        row = prep.store.schedules[sid]
        row["options"] = json.dumps({**sch.options_of(row),
                                     p.OPTION: p.moved(p.record_of(sch.options_of(row)), stage=p.WRITING)})
        return validate(body, members_kept)
    monkeypatch.setattr(main, "_validate_schedule", moved_meanwhile)
    body = dict(BODY, paused=True, members=[], options={"rediscover_hour": 5})
    assert prep.post(f"/api/schedules/{sid}", json=body).status_code == 200
    assert _record(prep, sid)["stage"] == p.WRITING and sch.options_of(prep.store.schedules[sid])["rediscover_hour"] == 5


def test_a_new_schedule_may_not_send_a_preparation_record(prep):
    forged = p.new_record("mallory", {}, {}, {}, {}, turn_on=True, state=p.PREPARING, stage=p.READING)
    sid = prep.post("/api/schedules", json=dict(BODY, options={p.OPTION: forged})).json()["id"]
    assert p.OPTION not in sch.options_of(prep.store.schedules[sid])


def test_delete_works_while_the_job_prepares(prep):
    sid = _job(prep)
    assert prep.post(f"/api/schedules/{sid}/delete", json={}).status_code == 200
    assert prep.store.schedules[sid]["deleted_at"] is not None


# ---- the set-aside databases and their actions ----------------------------------------------------------------------

def _needs_you(prep):
    return _job(prep, stage=p.WRITING, set_aside=[
        p.entry("sql_a", "eng_1", p.UNREADABLE, "Login failed for user 'ingest'"),
        p.entry("sql_a", "eng_2", p.CLASH, "another table already writes main.bronze.orders",
                target="main.bronze.orders", job="Sales nightly")])


def test_the_page_reads_the_state_the_reads_and_the_set_aside_databases(prep):
    sid = _needs_you(prep)
    prep.store.reads[str(sid)] = {"databases": 3000, "pending": 1760, "done": 1238, "failed": 2, "tables": 58280,
                                  "recent": 240, "elapsed_s": 600.0}
    shown = prep.get(f"/api/ingestion-jobs/{sid}/prepare").json()
    assert shown["state"] == p.NEEDS_YOU and shown["words"] == "needs you: 2 databases set aside"
    assert shown["reading"]["read"] == 1240 and shown["reading"]["minutes_left"] == 15
    assert [(e["database"], e["kind"]) for e in shown["set_aside"]] == [("eng_1", "unreadable"), ("eng_2", "clash")]
    assert shown["set_aside"][1]["target"] == "main.bronze.orders" and shown["set_aside"][1]["job"] == "Sales nightly"
    assert shown["actions"] == {"read_again": None, "leave_out": None, "targets": None, "delete": None, "stop": None,
                                "again": None}


def test_a_job_made_before_the_record_reads_as_ready_with_nothing_to_act_on(prep):
    sid = prep.post("/api/schedules", json=BODY).json()["id"]
    shown = prep.get(f"/api/ingestion-jobs/{sid}/prepare").json()
    assert shown["state"] == p.READY and shown["recorded"] is False and shown["reading"] is None
    assert shown["actions"]["read_again"] == main.NOTHING_SET_ASIDE
    assert prep.post(f"/api/ingestion-jobs/{sid}/prepare/leave-out", json={}).status_code == 409


def _failed(prep, sid, *databases):
    """The databases of the job's claimed drafts that couldn't be read, as their rows have them."""
    prep.store.failed[sid] = [{"server": "sql_a", "src_database": name, "error": "Login failed for user 'ingest'",
                               "whole_server": False} for name in databases]


def test_read_them_again_resets_the_unreadable_rows_for_the_reader(prep):
    sid = _needs_you(prep)
    _failed(prep, sid, "eng_1")
    marked = prep.post(f"/api/ingestion-jobs/{sid}/prepare/read-again", json={})
    assert marked.status_code == 200 and marked.json()["marked"] == 1 and marked.json()["state"] == p.NEEDS_YOU
    record = _record(prep, sid)
    assert p.database_key("sql_a", "eng_1") not in record["set_aside"] and p.counts_of(record)[p.UNREADABLE] == 0
    assert record["read_again"]["by"] == "op@example.com" and prep.store.failed[sid] == []
    assert any(line[1] == "prepare_read_again" for line in prep.store.log)
    assert prep.store.started == []          # the clash still waits for a choice: nothing for the task yet


def test_read_them_again_with_nothing_else_waiting_asks_the_prepare_task_to_go_on(prep):
    sid = _job(prep, stage=p.WRITING, set_aside=[p.entry("sql_a", "eng_1", p.UNREADABLE, "Login failed")])
    _failed(prep, sid, "eng_1", "eng_3")
    marked = prep.post(f"/api/ingestion-jobs/{sid}/prepare/read-again",
                       json={"databases": [{"server": "SQL_A", "database": "ENG_1"}]})
    assert marked.json()["marked"] == 1 and [r["src_database"] for r in prep.store.failed[sid]] == ["eng_3"]
    assert marked.json()["state"] == p.PREPARING and prep.store.started == [sid]
    assert _record(prep, sid)["stage"] == p.READING


def test_leave_them_out_takes_the_named_ones_out_of_the_choice_and_the_job_goes_on(prep):
    sid = _needs_you(prep)
    _failed(prep, sid, "eng_1")
    left = prep.post(f"/api/ingestion-jobs/{sid}/prepare/leave-out",
                     json={"databases": [{"server": "sql_a", "database": "eng_2"}]})
    assert left.json()["left_out"] == 1 and left.json()["state"] == p.NEEDS_YOU and prep.store.started == []
    left = prep.post(f"/api/ingestion-jobs/{sid}/prepare/leave-out", json={})
    assert left.json()["left_out"] == 1 and left.json()["state"] == p.PREPARING
    assert p.left_out_keys(sch.options_of(prep.store.schedules[sid])) == {
        p.database_key("sql_a", "eng_1"), p.database_key("sql_a", "eng_2")}
    assert prep.store.failed[sid] == [] and prep.store.started == [sid]      # its row is marked left out too


def test_change_the_target_names_previews_then_stores_only_the_templates(prep):
    sid = _needs_you(prep)
    preview = prep.post(f"/api/ingestion-jobs/{sid}/prepare/targets", json={"sink_table": "{table}_v2"})
    assert preview.json() == {"id": sid, "targets": {"sink_table": "{table}_v2"}, "replan": 1,
                              "applied": False}
    assert _record(prep, sid)["settings"]["sink_table"] == "{table}"
    bad = prep.post(f"/api/ingestion-jobs/{sid}/prepare/targets", json={"sink_table": "{nope}", "dry_run": False})
    assert bad.status_code == 422
    applied = prep.post(f"/api/ingestion-jobs/{sid}/prepare/targets",
                        json={"sink_table": "{table}_v2", "dry_run": False})
    assert applied.json()["applied"] is True and applied.json()["replan"] == 1
    record = _record(prep, sid)
    assert record["settings"] == {**SETTINGS, "sink_table": "{table}_v2"}
    assert p.database_key("sql_a", "eng_2") not in record["set_aside"] and p.counts_of(record)[p.CLASH] == 0
    assert record["targets_changed"]["by"] == "op@example.com"


def test_the_set_aside_actions_need_can_manage_on_the_job_as_its_edits_do(prep, monkeypatch):
    sid = _needs_you(prep)
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE_RUN")
    shown = prep.get(f"/api/ingestion-jobs/{sid}/prepare").json()
    assert shown["actions"]["leave_out"] and "Can Manage" in shown["actions"]["leave_out"]
    for action, body in (("read-again", {}), ("leave-out", {}), ("targets", {"sink_table": "{table}_v2",
                                                                            "dry_run": False})):
        assert prep.post(f"/api/ingestion-jobs/{sid}/prepare/{action}", json=body).status_code == 403, action
    record = _record(prep, sid)
    assert record["left_out"] == {} and record["settings"]["sink_table"] == "{table}"
    assert not any(e.get("again") for e in record["set_aside"].values())


def test_a_deleted_job_refuses_the_set_aside_actions(prep):
    sid = _needs_you(prep)
    prep.store.schedules[sid]["deleted_at"] = datetime.now(timezone.utc).isoformat()
    response = prep.post(f"/api/ingestion-jobs/{sid}/prepare/leave-out", json={})
    assert response.status_code == 403 and "this job is deleted" in response.json()["detail"]


# ---- the write-time checks stage B calls --------------------------------------------------------------------------

def _host_row(database="eng_1"):
    return {"src_type": "sqlserver", "src_host": "sql-a.internal.example", "src_port": 1433, "secret_key": database,
            "use_remote_query": False, "src_connection": None, "src_database": database, "src_schema": "dbo",
            "src_table": "t1", "task_collection": "c1"}


def test_the_checks_read_the_record_back_from_where_only_the_app_writes(prep, monkeypatch):
    sid = _job(prep, scope={"secret_scope": "lakefed"})
    prep.store.schedules[sid]["parameters"] = {"secret_scope": "lakefed", "parallelism": "8"}
    monkeypatch.setattr(main, "_manage_refusal", lambda user: None)
    checked = main._prepare_checks(sid)
    assert checked.maker == "ann@example.com" and checked.connection is None
    assert checked.parameters == {"secret_scope": "lakefed", "parallelism": "8"}
    assert checked.settings == SETTINGS and checked.sizing is None
    selection = checked.selection
    assert (selection.discovery_ids, selection.whole_server, selection.filter) == (["d1"], True, "eng")


def test_a_maker_who_has_lost_the_manager_right_since_create_is_refused(prep, monkeypatch):
    sid = _job(prep, scope={"secret_scope": "lakefed"})
    asked = []
    monkeypatch.setattr(main, "_manage_refusal",
                        lambda user: asked.append(user) or (None if user == "ann@example.com" else "not a manager"))
    main._prepare_checks(sid, rows=[_host_row()])                 # still a manager: the scope and the host read pass
    assert set(asked) == {"ann@example.com"}
    monkeypatch.setattr(main, "_manage_refusal", lambda user: "not an app manager")
    with pytest.raises(main.HTTPException) as refused:
        main._prepare_checks(sid)
    assert refused.value.status_code == 403 and "only an app manager" in refused.value.detail
    unscoped = _job(prep)
    with pytest.raises(main.HTTPException) as refused:
        main._prepare_checks(unscoped, rows=[_host_row()])
    assert refused.value.status_code == 403 and "a read by host and password" in refused.value.detail


def test_a_connection_whose_login_was_typed_again_since_create_is_read_again(prep, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    sid = _job(prep)
    prep.store.connection_rows = {"orders jdbc": CONNECTION}
    rows = [{**_host_row(), "src_host": "orders.internal.example", "secret_key": "orders-eng_1"}]
    prep.store.newest_logins = {"orders-eng_1": "orders jdbc"}
    checked = main._prepare_checks(sid, rows=rows, cur=object())
    assert checked.connection["name"] == "orders jdbc"
    assert checked.parameters["secret_scope"] == LOGIN_SCOPE and checked.parameters["trust_server_certificate"] == "true"
    prep.store.newest_logins = {"orders-eng_1": "attacker jdbc"}        # typed again, for another connection
    with pytest.raises(main.HTTPException) as refused:
        main._prepare_checks(sid, rows=rows, cur=object())
    assert refused.value.status_code == 422 and "was not typed for the connection orders jdbc" in json.dumps(
        refused.value.detail)
    elsewhere = [{**rows[0], "src_host": "attacker.example"}]
    prep.store.newest_logins = {"orders-eng_1": "orders jdbc"}
    with pytest.raises(main.HTTPException) as refused:
        main._prepare_checks(sid, rows=elsewhere)
    assert "take that connection's host" in json.dumps(refused.value.detail)


def test_a_job_with_no_record_has_nothing_to_check(prep):
    sid = prep.post("/api/schedules", json=BODY).json()["id"]
    with pytest.raises(main.HTTPException) as refused:
        main._prepare_checks(sid)
    assert refused.value.status_code == 409 and refused.value.detail == main.NO_RECORD


# ---- what stage B's prepare task calls to move the job on ---------------------------------------------------------

def test_the_prepare_task_moves_the_record_and_sets_databases_aside(prep):
    sid = _job(prep)
    main._move_prepare(sid, stage=p.WRITING, progress={"tables_written": 10, "tables_total": 100})
    record = _record(prep, sid)
    assert record["stage"] == p.WRITING and record["writing_started"] and record["progress"]["tables_total"] == 100
    main._set_aside(sid, [p.entry("sql_a", "eng_7", p.CLASH, "two tables of the job write main.bronze.t1")])
    assert _record(prep, sid)["state"] == p.NEEDS_YOU
    main._clear_set_aside(sid, [p.database_key("sql_a", "eng_7")])
    assert _record(prep, sid)["state"] == p.PREPARING


def test_ready_turns_the_schedule_on_when_its_maker_may_still_run_it(prep):
    sid = _job(prep, stage=p.SYNCING)
    record = main._make_ready(sid)
    assert record["state"] == p.READY and record["kept_paused"] is None
    assert prep.store.schedules[sid]["paused"] is False
    assert any(line[0] == main.PREPARE_USER and line[1] == "schedule_resume" for line in prep.store.log)
    assert main._make_ready(sid)["ready_at"] == record["ready_at"]       # once Ready, nothing moves


def test_ready_keeps_the_schedule_off_when_its_maker_may_no_longer_run_it(prep, monkeypatch):
    sid = _job(prep, stage=p.SYNCING)
    asked = []
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: asked.append(user) or "CAN_VIEW")
    record = main._make_ready(sid)
    assert asked == ["ann@example.com"] and record["state"] == p.READY and record["kept_paused"]
    assert prep.store.schedules[sid]["paused"] is True


def test_ready_leaves_a_schedule_off_that_was_not_to_turn_on(prep):
    sid = _job(prep, stage=p.SYNCING, turn_on=False)
    assert main._make_ready(sid)["state"] == p.READY and prep.store.schedules[sid]["paused"] is True
