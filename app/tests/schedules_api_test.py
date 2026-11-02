"""Schedule endpoints (main.py) with a fake Lakebase and a fake Jobs API: create, edit, pause, delete."""
import contextlib
import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import jdbc_connections as jc  # noqa: E402
import jdbc_logins as jl  # noqa: E402
import main  # noqa: E402
import queries as q  # noqa: E402
import schedules as sch  # noqa: E402

from schedules_test import FakeJobs  # noqa: E402


RESTORE_CLEARS = ("delete_drop_targets", "delete_run_id", "delete_start_error", "delete_finished")
LOGIN_SCOPE = "lakefed_ingest_jdbc_logins"
CONNECTION_ROW = {"name": "orders jdbc", "src_type": "sqlserver", "src_host": "orders.internal.example",
                  "src_port": 1433, "secret_scope": "lakefed", "secret_key": "orders-login",
                  "trust_server_certificate": "false", "created_by": "ann", "created_at": None}


class Store:
    """A tiny in-memory stand-in for the schedule tables, driven by the SQL main.py runs."""

    def __init__(self):
        self.schedules, self.members, self.log, self.unique, self.locks = {}, {}, [], False, []
        self.with_tables = {"c1", "c2", "c3"}           # groups with control rows
        self.targets = {}                               # group: its rows' target tables (default: one of its own)
        # What the restore and the database checks answer; a test sets them.
        self.owners, self.conflicts = [], []
        self.rowcount, self.clock = -1, self.stamp()

    def stamp(self):
        return datetime.now(timezone.utc).isoformat()

    def begin(self):
        """A transaction's timestamps are one instant, as now() is in Postgres: a delete's deleted_at and the members it
        removes match, which a Restore relies on."""
        self.clock = self.stamp()

    def now(self):
        return self.clock

    def kept(self, row):
        """A deleted job the Deleted jobs list shows: not yet purged (its delete job hasn't marked it finished)."""
        return row["deleted_at"] is not None and "delete_finished" not in sch.options_of(row)

    def tables_left(self, schedule_id):
        """Whether a deleted job's groups still have tables a Delete would remove (groups no live job holds)."""
        live = {i for i, x in self.schedules.items() if x["deleted_at"] is None}
        groups = {m["task_collection"] for m in self.members.values() if m["schedule_id"] == schedule_id}
        held = {m["task_collection"] for m in self.members.values()
                if m["schedule_id"] != schedule_id and m["removed_at"] is None and m["schedule_id"] in live}
        return bool((groups - held) & self.with_tables)

    def run(self, sql, params):
        self.rowcount = -1
        if sql == q.restore_schedule_sql():
            row = self.schedules.get(params[0])
            self.rowcount = 1 if row and self.kept(row) else 0
            if self.rowcount:
                import json
                options = {k: v for k, v in sch.options_of(row).items() if k not in RESTORE_CLEARS}
                row.update(deleted_at=None, paused=True, options=json.dumps(options))
            return []
        if sql == q.restore_members_sql():
            deleted_at = self.schedules[params[1]]["deleted_at"]
            for m in self.members.values():
                if m["schedule_id"] == params[0] and m["removed_at"] is not None and m["removed_at"] == deleted_at:
                    m["removed_at"] = None
            return []
        if sql in (q.touch_jobs_sql(), q.touch_group_jobs_sql()):
            import json
            user, keys = params
            ids = keys if sql == q.touch_jobs_sql() else {m["schedule_id"] for m in self.members.values()
                                                          if m["task_collection"] in keys and m["removed_at"] is None}
            for sid in ids:
                row = self.schedules[sid]
                row.update(updated_at=self.now(), options=json.dumps({**sch.options_of(row), "updated_by": user}))
            return []
        if sql in (q.set_schedule_option_sql(), q.set_schedule_flag_sql(), q.set_schedule_text_option_sql(),
                   q.clear_schedule_option_sql()):
            import json
            row = self.schedules[params[-1]]
            options = sch.options_of(row)
            if sql != q.clear_schedule_option_sql():
                options[params[0][0]] = params[1]
            else:
                options.pop(params[0], None)
            row["options"] = json.dumps(options)
            return []
        if sql.startswith(f"insert into {q.SCHEMA}.schedule ("):
            if self.unique:
                import psycopg
                raise psycopg.errors.UniqueViolation("dup")
            sid = len(self.schedules) + 1
            cols = q.SCHEDULE_FIELDS + ("created_by",)
            row = dict(zip(cols, params), id=sid, deleted_at=None, created_at=self.now(), updated_at=self.now())
            import json
            row["parameters"] = json.loads(row["parameters"])
            self.schedules[sid] = row
            return [(sid,)]
        if sql.startswith(f"insert into {q.SCHEMA}.schedule_member"):
            mid = len(self.members) + 1
            self.members[mid] = dict(id=mid, schedule_id=params[0], task_collection=params[1], offset_minutes=params[2],
                                     job_id=None, synced_at=None, sync_error=None, added_at=self.now(), removed_at=None)
            return [(mid,)]
        if sql.startswith(f"update {q.SCHEMA}.schedule set deleted_at"):
            self.schedules[params[0]]["deleted_at"] = self.now()
        elif sql.startswith(f"update {q.SCHEMA}.schedule set"):
            import json
            fields = [p.split(" = ")[0] for p in sql.split(" set ")[1].split(", updated_at")[0].split(", ")]
            for f, v in zip(fields, params[:-1]):
                self.schedules[params[-1]][f] = json.loads(v) if f == "parameters" else v
        elif sql.startswith(f"update {q.SCHEMA}.schedule_member set removed_at"):
            for mid in params[0]:
                self.members[mid]["removed_at"] = self.now()
        elif sql.startswith(f"update {q.SCHEMA}.schedule_member set offset_minutes"):
            self.members[params[1]]["offset_minutes"] = params[0]
        elif sql == q.member_synced_sql():
            self.members[params[1]].update(job_id=params[0], sync_error=None, synced_at=self.now())
        elif sql == q.member_sync_failed_sql():
            self.members[params[1]]["sync_error"] = params[0]
        elif sql == q.schedule_lock_sql():
            self.locks.append(params)
        elif "control_change" in sql:
            self.log.append(params)
        return self.query(sql, params)

    def query(self, sql, params=()):
        if "from lakefed_ingest.control" in sql and "distinct task_collection" in sql:
            return [{"task_collection": c} for c in sorted(self.with_tables)]
        if sql == q.schedules_sql():
            return [dict(s) for s in self.schedules.values() if s["deleted_at"] is None or self.kept(s)]
        if sql == q.live_name_taken_sql():
            name, sid = params
            return [(i,) for i, x in self.schedules.items()
                    if x["deleted_at"] is None and x["name"].lower() == name.lower() and i != sid]
        if sql == q.groups_held_elsewhere_sql():
            groups, sid = params
            live = {i for i, x in self.schedules.items() if x["deleted_at"] is None}
            return [(m["task_collection"], self.schedules[m["schedule_id"]]["name"]) for m in self.members.values()
                    if m["task_collection"] in groups and m["removed_at"] is None and m["schedule_id"] in live
                    and m["schedule_id"] != sid]
        if sql == q.restore_conflicts_sql():
            return list(self.conflicts)
        if sql == q.deleted_database_owners_sql():
            return list(self.owners)
        if sql == q.deleted_tables_left_sql():
            return [{"schedule_id": sid} for sid in params[0] if self.tables_left(sid)]
        if sql == q.schedule_sql():
            s = self.schedules.get(params[0])
            return [dict(s)] if s else []
        if sql == q.delete_groups_sql():
            live = {i for i, x in self.schedules.items() if x["deleted_at"] is None}
            held = {m["task_collection"] for m in self.members.values()
                    if m["schedule_id"] != params[0] and m["removed_at"] is None and m["schedule_id"] in live}
            return [{"task_collection": g} for g in sorted({m["task_collection"] for m in self.members.values()
                                                            if m["schedule_id"] == params[0]} - held)]
        if sql == q.delete_targets_sql():
            groups = set(params[0])
            sinks = lambda g: self.targets.get(g, [f"main.sales.{g}"]) if g in self.with_tables else []
            mine = {t for g in groups for t in sinks(g)}
            theirs = {t.lower() for g in self.with_tables - groups for t in sinks(g)}
            kept = sorted(t for t in mine if t.lower() in theirs)
            return [{"tables": sum(len(sinks(g)) for g in groups), "targets": len(mine) - len(kept), "kept": len(kept),
                     "kept_names": kept[:q.DELETE_KEPT_LISTED]}]
        if sql == q.deleted_jobs_count_sql():
            return [{"total": sum(1 for x in self.schedules.values() if self.kept(x))}]
        if sql == q.deleted_jobs_sql():
            rows = sorted((dict(x) for x in self.schedules.values() if self.kept(x)), key=lambda x: x["deleted_at"],
                          reverse=True)
            return rows[params[1]:params[1] + params[0]]
        if sql == q.schedule_members_sql():
            pending = lambda m: bool(m["removed_at"] and m["job_id"] and (m["synced_at"] is None or m["synced_at"] < m["removed_at"]))
            return [dict(m, delete_pending=pending(m)) for m in self.members.values() if m["schedule_id"] in params[0]]
        return []


class Cur:
    def __init__(self, store):
        self.store, self._rows = store, []
        self.store.begin()
        self.rowcount = -1

    def execute(self, sql, params=()):
        self._rows = self.store.run(sql, params)
        self.rowcount = self.store.rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


@pytest.fixture
def api(monkeypatch):
    store, jobs_api = Store(), FakeJobs()
    monkeypatch.setattr(main.db, "query", store.query)
    monkeypatch.setattr(main.db, "fetch", lambda cur, sql, params=(): store.query(sql, params))
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield Cur(store))))
    monkeypatch.setattr(main.db, "workspace", lambda: type("W", (), {"config": type("C", (), {"host": "https://h"})()})())
    monkeypatch.setattr(main.jobs, "schedule_client", lambda: jobs_api)
    monkeypatch.setattr(main.jobs, "ingest_variants", lambda: {"default": "555"})
    # Run now and Delete ask Databricks for the user's permission: the signed-in operator manages the job here.
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE")
    monkeypatch.setattr(main.jobs, "SCHEDULE_JOB_VIEW_GROUP", None)
    # Delete starts the delete job; Databricks has no wrapper or delete job run going unless a test adds one.
    active_runs, delete_runs = {}, {}
    monkeypatch.setattr(main.jobs, "runs", lambda job_id, active_only=False, limit=25: list(active_runs.get(int(job_id), [])))
    monkeypatch.setattr(main.jobs, "DELETE_JOB_ID", "900")
    monkeypatch.setattr(main.jobs, "workspace_url", lambda: "https://h")

    def start_delete_job(sid, drop_targets):
        run_id = 9000 + len(delete_runs) + 1
        delete_runs[run_id] = {"run_id": run_id, "schedule_id": sid, "drop_targets": drop_targets, "state": "RUNNING",
                               "result": None, "message": "", "url": f"https://h/jobs/900/runs/{run_id}"}
        return run_id
    monkeypatch.setattr(main.jobs, "start_delete_job", start_delete_job)
    monkeypatch.setattr(main.jobs, "run_state", lambda run_id: dict(delete_runs[int(run_id)]))
    monkeypatch.setattr(q, "collections_sql", lambda: "select distinct task_collection from lakefed_ingest.control")
    c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
    c.store, c.jobs, c.active_runs, c.delete_runs = store, jobs_api, active_runs, delete_runs
    return c


BODY = dict(name="Prod 15", cadence_minutes=15, timezone="UTC", parameters={"parallelism": "16"},
            stagger=True, paused=False, members=["c1", "c2"])


def test_a_job_not_read_through_a_connection_may_not_name_the_login_scope(api, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    r = api.post("/api/schedules", json=dict(BODY, parameters={"secret_scope": LOGIN_SCOPE}))
    assert r.status_code == 422 and LOGIN_SCOPE in r.json()["detail"]["errors"][0]
    assert api.store.schedules == {}


def test_an_edit_may_not_move_a_job_onto_the_login_scope(api, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    r = api.post(f"/api/schedules/{sid}", json=dict(BODY, parameters={"secret_scope": LOGIN_SCOPE}))
    assert r.status_code == 422 and LOGIN_SCOPE in r.json()["detail"]["errors"][0]
    assert LOGIN_SCOPE not in json.dumps(api.store.schedules[sid])


def test_an_edit_of_a_job_that_reads_through_a_connection_keeps_the_connections_scope_and_trust(api, monkeypatch):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    _reads_through(monkeypatch, api, {**CONNECTION_ROW, "trust_server_certificate": "true"})
    body = dict(BODY, parameters={"parallelism": "16", "secret_scope": "elsewhere",
                                  "trust_server_certificate": "false"})
    assert api.post(f"/api/schedules/{sid}", json=body).status_code == 200
    members = [m for m in api.store.members.values() if m["schedule_id"] == sid]
    assert len(members) == 2
    for member in members:
        params = api.jobs.jobs[member["job_id"]]["tasks"][0]["run_job_task"]["job_parameters"]
        assert (params["secret_scope"], params["trust_server_certificate"], params["parallelism"]) == (
            "lakefed", "true", "16")


def test_an_edit_may_not_move_a_job_whose_connection_is_gone_onto_the_login_scope(api, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    _reads_through(monkeypatch, api, None)
    r = api.post(f"/api/schedules/{sid}", json=dict(BODY, parameters={"secret_scope": LOGIN_SCOPE}))
    assert r.status_code == 422 and LOGIN_SCOPE in r.json()["detail"]["errors"][0]
    assert LOGIN_SCOPE not in json.dumps(api.store.schedules[sid])


def test_an_app_manager_may_name_any_other_scope_and_no_one_else_may(api, monkeypatch):
    monkeypatch.setenv(jl.SCOPE_ENV, LOGIN_SCOPE)
    monkeypatch.setattr(main, "_manage_refusal", lambda user: "not an app manager")
    r = api.post("/api/schedules", json=dict(BODY, parameters={"secret_scope": "lakefed"}))
    assert r.status_code == 403 and "only an app manager" in r.json()["detail"]
    assert api.post("/api/schedules", json=BODY).status_code == 200
    monkeypatch.setattr(main, "_manage_refusal", lambda user: None)
    assert api.post("/api/schedules", json=dict(BODY, name="Other", parameters={"secret_scope": "lakefed"})).status_code == 200


def test_an_edit_that_keeps_the_stored_scope_passes_and_one_that_changes_it_is_a_managers(api, monkeypatch):
    monkeypatch.setattr(main, "_manage_refusal", lambda user: None)
    sid = api.post("/api/schedules", json=dict(BODY, parameters={"secret_scope": "lakefed"})).json()["id"]
    monkeypatch.setattr(main, "_manage_refusal", lambda user: "not an app manager")
    kept = dict(BODY, cadence_minutes=30, parameters={"secret_scope": "lakefed"})
    assert api.post(f"/api/schedules/{sid}", json=kept).status_code == 200
    moved = dict(BODY, parameters={"secret_scope": "other_admin_scope"})
    r = api.post(f"/api/schedules/{sid}", json=moved)
    assert r.status_code == 403 and "only an app manager" in r.json()["detail"]


def test_create_writes_rows_creates_one_job_per_member_and_logs(api):
    r = api.post("/api/schedules", json=BODY)
    assert r.status_code == 200, r.text
    assert sorted(x["action"] for x in r.json()["sync"]) == ["created", "created"]
    members = {m["task_collection"]: m for m in api.store.members.values()}
    assert {c: m["offset_minutes"] for c, m in members.items()} == {"c1": 0, "c2": 7}
    assert all(m["job_id"] in api.jobs.jobs for m in members.values())
    job = api.jobs.jobs[members["c2"]["job_id"]]
    assert job["schedule"]["quartz_cron_expression"] == "0 7/15 * * * ?"
    assert job["tasks"][0]["run_job_task"] == {"job_id": 555, "job_parameters": {"task_collection": "c2", "parallelism": "16"}}
    actions = [p[1] for p in api.store.log]
    assert actions.count("schedule_create") == 2 and actions.count("schedule_sync") == 2

    listed = api.get("/api/schedules").json()
    assert listed[0]["name"] == "Prod 15" and len(listed[0]["members"]) == 2
    m = {x["task_collection"]: x for x in listed[0]["members"]}
    assert m["c2"]["when"] == "Every 15 minutes, starting at 7 minutes past the hour (UTC)" and m["c2"]["next_run"] and m["c2"]["job_url"].startswith("https://h/jobs/")


def test_a_job_made_here_stores_unity_catalog_names_and_keeps_them_on_edit(api):
    r = api.post("/api/schedules", json=dict(BODY, options={"target_names": "legacy"}))
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert sch.options_of(api.store.schedules[sid])["target_names"] == "unity_catalog"
    assert main._unity_catalog(api.store.schedules[sid]) is True
    assert api.post(f"/api/schedules/{sid}", json=BODY).status_code == 200
    assert main._unity_catalog(api.store.schedules[sid]) is True


def test_an_edit_after_a_table_removal_is_saved_and_keeps_the_removals(api):
    import table_removal
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    removals = {table_removal.EXCLUDED: [["h1:1433", "sales", "dbo", "orders"]],
                table_removal.OPTION: {"run_id": 12, "tables": 1}}
    stored = api.store.schedules[sid]
    stored["options"] = json.dumps({**sch.options_of(stored), **removals})
    sent = sch.options_of(stored)                                   # Edit sends every stored option it doesn't own
    r = api.post(f"/api/schedules/{sid}", json=dict(BODY, cadence_minutes=30, options=sent))
    assert r.status_code == 200, r.text
    options = sch.options_of(api.store.schedules[sid])
    assert options[table_removal.EXCLUDED] == removals[table_removal.EXCLUDED]
    assert options[table_removal.OPTION] == removals[table_removal.OPTION]
    r = api.post(f"/api/schedules/{sid}", json=dict(BODY, options={}))
    assert r.status_code == 200, r.text
    assert sch.options_of(api.store.schedules[sid])[table_removal.EXCLUDED] == removals[table_removal.EXCLUDED]
    tampered = {table_removal.EXCLUDED: []}
    assert api.post(f"/api/schedules/{sid}", json=dict(BODY, options=tampered)).status_code == 200
    assert sch.options_of(api.store.schedules[sid])[table_removal.EXCLUDED] == removals[table_removal.EXCLUDED]


def test_validation_and_duplicate_name(api):
    r = api.post("/api/schedules", json=dict(BODY, cadence_minutes=7, members=["c1", "nope"]))
    assert r.status_code == 422 and "cadence_minutes" in r.text and "unknown ingestion groups nope" in r.text
    api.store.unique = True
    assert api.post("/api/schedules", json=BODY).status_code == 409


def test_edit_adds_and_removes_members_restaggers_and_resets_jobs(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    api.store.with_tables.discard("c1")      # its tables have moved on, so it may leave the job
    before = {m["task_collection"]: m["job_id"] for m in api.store.members.values()}
    r = api.post(f"/api/schedules/{sid}", json=dict(BODY, cadence_minutes=30, members=["c2", "c3"])).json()
    assert r["changed"] == ["cadence_minutes"] and r["added"] == ["c3"] and r["removed"] == ["c1"]
    actions = {x["task_collection"]: x["action"] for x in r["sync"]}
    assert actions == {"c2": "updated", "c3": "created", "c1": "deleted"}
    assert before["c1"] not in api.jobs.jobs                                      # removed member's job is gone
    active = {m["task_collection"]: m for m in api.store.members.values() if m["removed_at"] is None}
    assert {c: m["offset_minutes"] for c, m in active.items()} == {"c2": 0, "c3": 15}
    assert api.jobs.jobs[active["c3"]["job_id"]]["schedule"]["quartz_cron_expression"] == "0 15/30 * * * ?"
    again = api.post(f"/api/schedules/{sid}/sync").json()["sync"]                 # nothing left to do
    assert {x["action"] for x in again} == {"unchanged"}



def test_pause_resume_and_delete(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    api.post(f"/api/schedules/{sid}/pause")
    assert all(j["schedule"]["pause_status"] == "PAUSED" for j in api.jobs.jobs.values())
    assert api.get("/api/schedules").json()[0]["members"][0]["next_run"] is None
    api.post(f"/api/schedules/{sid}/resume")
    assert all(j["schedule"]["pause_status"] == "UNPAUSED" for j in api.jobs.jobs.values())
    r = api.post(f"/api/schedules/{sid}/delete").json()
    assert sorted(x["action"] for x in r["sync"]) == ["deleted", "deleted"] and api.jobs.jobs == {}
    listed = api.get("/api/schedules").json()     # kept until its purge: listed as deleted, with its state
    assert [(x["id"], x["delete"]["state"]) for x in listed] == [(sid, "kept")]
    assert api.post(f"/api/schedules/{sid}/pause").status_code == 409


def test_check_reports_drift_without_changing_anything(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    jid = next(m["job_id"] for m in api.store.members.values() if m["task_collection"] == "c1")
    api.jobs.jobs[jid] = dict(api.jobs.jobs[jid], max_concurrent_runs=4)
    status = api.get(f"/api/schedules/{sid}/check").json()
    assert status["c1"] == {"status": "drifted", "fields": ["max_concurrent_runs"]} and status["c2"]["status"] == "in_sync"
    assert api.jobs.jobs[jid]["max_concurrent_runs"] == 4


def test_the_timeline_has_one_lane_per_job_with_its_planned_triggers(api, monkeypatch):
    api.post("/api/schedules", json=BODY)
    lanes = api.get("/api/timeline?hours=2").json()["jobs"]
    assert [(lane["name"], lane["paused"]) for lane in lanes] == [("Prod 15", False)]
    assert len(lanes[0]["planned"]) <= 2      # only since the schedule began
    assert not any(r["started_at"] for r in lanes[0]["runs"])       # no group run has started (the current slot may be open)
    api.store.members[1]["added_at"] = api.store.members[2]["added_at"] = "2020-01-01T00:00:00+00:00"
    api.store.schedules[1]["updated_at"] = "2020-01-01T00:00:00+00:00"
    main._shared_reads.clear()      # the schedules are a shared read
    lane, = api.get("/api/timeline?hours=2").json()["jobs"]
    assert 14 <= len(lane["planned"]) <= 18     # both groups' triggers, staggered 7 minutes apart, in one lane
    assert lane["description"].startswith("Every 15 minutes")


def test_a_group_with_tables_cant_leave_its_job(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    r = api.post(f"/api/schedules/{sid}", json=dict(BODY, members=["c2"]))
    assert r.status_code == 422 and "c1 still has tables" in r.text
    assert {m["task_collection"] for m in api.store.members.values() if m["removed_at"] is None} == {"c1", "c2"}


def test_sync_takes_the_schedule_lock_and_a_failed_delete_is_retried(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    assert api.store.locks and api.store.locks[-1] == (q.SCHEDULE_LOCK_CLASS, sid)
    c1 = next(m for m in api.store.members.values() if m["task_collection"] == "c1")
    api.store.with_tables.discard("c1")
    real = api.jobs.delete
    api.jobs.delete = lambda job_id: (_ for _ in ()).throw(RuntimeError("503 temporarily unavailable"))
    r = api.post(f"/api/schedules/{sid}", json=dict(BODY, members=["c2"])).json()
    assert {x["task_collection"]: x["action"] for x in r["sync"]}["c1"] == "error"
    assert c1["job_id"] in api.jobs.jobs and "503" in c1["sync_error"]
    api.jobs.delete = real
    again = api.post(f"/api/schedules/{sid}/sync").json()["sync"]          # the delete is still pending
    assert ("c1", "deleted") in {(x["task_collection"], x["action"]) for x in again}
    assert c1["job_id"] not in api.jobs.jobs


def test_delete_without_the_variant_still_deletes_the_jobs(api, monkeypatch):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    monkeypatch.setattr(main.jobs, "ingest_variants", lambda: {})
    monkeypatch.setattr(main.jobs, "user_groups", lambda user: ["users"])
    assert api.post(f"/api/schedules/{sid}/delete").status_code == 403      # only an admin cleans up an orphan
    monkeypatch.setattr(main.jobs, "user_groups", lambda user: ["admins"])
    r = api.post(f"/api/schedules/{sid}/delete")
    assert r.status_code == 200 and api.jobs.jobs == {}
    assert api.post(f"/api/schedules/{sid}/sync").status_code == 200


def test_a_blank_time_zone_is_stored_as_utc(api):
    api.post("/api/schedules", json=dict(BODY, timezone="  "))
    assert api.store.schedules[1]["timezone"] == "UTC"
    assert api.get("/api/schedules").status_code == 200 and api.get("/api/timeline").status_code == 200


def test_ingest_runs_started_by_a_wrapper_show_their_schedule(api, monkeypatch):
    api.post("/api/schedules", json=BODY)
    monkeypatch.setattr(main.jobs, "INGEST_JOB_ID", "555")
    monkeypatch.setattr(main.jobs, "job_summary", lambda jid: {"job_id": 555, "parameters": {"parallelism": "32", "db_name": "x"}})
    monkeypatch.setattr(main.jobs, "runs", lambda jid, active_only=False, limit=25: [
        {"run_id": 1, "task_collection": "c1", "trigger": "RUN_JOB_TASK", "started": "2999-01-01T00:00:00+00:00"},
        {"run_id": 2, "task_collection": "c1", "trigger": "ONE_TIME", "started": "2999-01-01T00:00:00+00:00"},
        {"run_id": 3, "task_collection": "c1", "trigger": "RUN_JOB_TASK", "started": "2020-01-01T00:00:00+00:00"}])
    r = api.get("/api/jobs/ingest").json()
    assert [x["schedule"] for x in r["recent"]] == ["Prod 15", None, None]     # 3 ran before c1 joined
    opts = api.get("/api/schedules/options").json()
    assert opts["defaults"] == {"parallelism": "32"} and "table_retries" in opts["parameters"]
    assert opts["scheduled"] == {"c1": ["Prod 15"], "c2": ["Prod 15"]}
    assert opts["shared_client_id"] == "{server}_{database}"
    assert opts["shared_identity"] == {"source_server": "{server}", "source_database": "{database}"}


def test_a_schedule_body_keeps_minute_schedules_in_utc():
    import main
    body = main.ScheduleBody(name="n", cadence_minutes=30, timezone="Europe/London", members=["c1"])
    assert body.timezone == "UTC"
    daily = main.ScheduleBody(name="n", cadence_minutes=1440, timezone="Europe/London", members=["c1"])
    assert daily.timezone == "Europe/London"


def test_a_check_schedule_is_offered_only_with_the_check_job_and_syncs_one_job(api, monkeypatch):
    assert "check" not in api.get("/api/schedules/options").json()["variants"]
    monkeypatch.setattr(main.jobs, "CHECK_JOB_ID", "777")
    assert api.get("/api/schedules/options").json()["variants"] == ["check", "default"]
    r = api.post("/api/schedules", json=dict(BODY, job_variant="check"))
    assert r.status_code == 200, r.text
    created = [s for s in api.jobs.jobs.values() if s["tags"].get(main.sch.TAG_COLLECTION) == main.sch.GROUP_TAG]
    assert len(created) == 1 and len(api.jobs.jobs) == 1
    assert created[0]["tasks"][0]["run_job_task"] == {"job_id": 777, "job_parameters": {
        "schedule_id": str(r.json()["id"]), "task_collections": "c1,c2", "parallelism": "16"}}
    # The one wrapper fires every member at once: a stagger asked for (the form keeps the last choice) is not kept.
    assert api.store.schedules[r.json()["id"]]["stagger"] is False
    assert {m["offset_minutes"] for m in api.store.members.values()} == {0}


def test_a_jobs_compute_lists_what_its_schedule_starts(api, monkeypatch):
    ingest = {"name": "lakefed_ingest", "tasks": [{"task_key": "ingest", "job_cluster_key": "c"}],
              "job_clusters": [{"job_cluster_key": "c", "new_cluster": {"node_type_id": "Standard_D8ds_v5",
                                                                        "num_workers": 1}}]}
    check = {"name": "lakefed_ingest_check", "tasks": [{"task_key": "check", "environment_key": "e"}]}
    monkeypatch.setattr(main.jobs, "job_settings", lambda job_id: {"555": ingest, "777": check}[str(job_id)])
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    found = api.get(f"/api/ingestion-jobs/{sid}/compute").json()
    assert [(j["role"], j["job_id"], [t["kind"] for t in j["compute"]]) for j in found["jobs"]] == [
        ("ingestion", "555", ["job cluster"])]
    monkeypatch.setattr(main.jobs, "CHECK_JOB_ID", "777")
    monkeypatch.setattr(main.jobs, "INGEST_JOB_ID", "555")
    sid = api.post("/api/schedules", json=dict(BODY, name="Group", job_variant="check")).json()["id"]
    found = api.get(f"/api/ingestion-jobs/{sid}/compute").json()
    assert [(j["role"], [t["kind"] for t in j["compute"]]) for j in found["jobs"]] == [
        ("check", ["serverless"]), ("ingestion", ["job cluster"])]


def test_a_jobs_compute_says_so_when_a_job_cannot_be_read(api, monkeypatch):
    def unreadable(job_id):
        raise RuntimeError("no access")
    monkeypatch.setattr(main.jobs, "job_settings", unreadable)
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    [job] = api.get(f"/api/ingestion-jobs/{sid}/compute").json()["jobs"]
    assert (job["compute"], job["error"]) == ([], "can't read job 555: no access")


def _reads_through(monkeypatch, api, connection_row):
    """Makes the schedule's job read through the JDBC connection its discovery names, whose stored row is connection_row
    (None: the connection is gone)."""
    answers = {q.job_selection_sql(): [{"source_kind": "discovery", "source_id": "d1"}],
               q.selection_discoveries_sql(): [{"listing": {"jdbc_connection": "orders jdbc"}}],
               jc.get_sql(): [connection_row] if connection_row else []}
    served = main.db.query
    monkeypatch.setattr(main.db, "query",
                        lambda sql, params=(): answers[sql] if sql in answers else served(sql, params))


def _run_now_setup(api, monkeypatch, body):
    started = []
    monkeypatch.setattr(main.jobs, "run_now", lambda job_id, params: started.append(int(job_id)) or 900 + len(started))
    monkeypatch.setattr(main, "_running", lambda: [])
    api.active_runs = {}                                     # wrapper job id: its runs going now, as the Jobs API lists
    monkeypatch.setattr(main.jobs, "runs", lambda job_id, active_only=False, limit=25: list(api.active_runs.get(int(job_id), [])))
    monkeypatch.setattr(main.jobs, "workspace_url", lambda: "https://h")
    sid = api.post("/api/schedules", json=body).json()["id"]
    return sid, started


def test_run_now_starts_each_members_wrapper_as_the_schedule_would(api, monkeypatch):
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE")
    sid, started = _run_now_setup(api, monkeypatch, BODY)
    r = api.post(f"/api/ingestion-jobs/{sid}/run")
    assert r.status_code == 200, r.text
    wrappers = {m["job_id"] for m in api.store.members.values()}
    assert set(started) == wrappers and len(started) == 2
    assert [(x["run_id"], x["url"]) for x in r.json()["started"]] == [
        (901 + i, f"https://h/jobs/{w}/runs/{901 + i}") for i, w in enumerate(sorted(wrappers))]


def test_run_now_is_refused_while_a_wrapper_run_is_still_starting_and_says_where_it_is(api, monkeypatch):
    # Its clusters are still coming up, so Lakebase has no run yet; the Jobs API has, and Databricks would skip a
    # second run of the wrapper without a word.
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE")
    monkeypatch.setattr(main.jobs, "CHECK_JOB_ID", "777")
    sid, started = _run_now_setup(api, monkeypatch, dict(BODY, job_variant="check"))
    wrapper = next(m["job_id"] for m in api.store.members.values())
    api.active_runs[int(wrapper)] = [{"run_id": 650503851272052, "state": "RUNNING", "started": "2026-10-29T14:09:42+00:00",
                                      "url": f"https://h/jobs/{wrapper}/runs/650503851272052", "parameters": {}}]
    r = api.post(f"/api/ingestion-jobs/{sid}/run")
    assert r.status_code == 409 and started == []
    detail = r.json()["detail"]
    assert "already going" in detail["message"] and detail["run_url"].endswith("/runs/650503851272052")


def test_run_now_on_a_check_schedule_starts_its_one_wrapper(api, monkeypatch):
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE")
    monkeypatch.setattr(main.jobs, "CHECK_JOB_ID", "777")
    sid, started = _run_now_setup(api, monkeypatch, dict(BODY, job_variant="check"))
    assert api.post(f"/api/ingestion-jobs/{sid}/run").status_code == 200 and len(started) == 1


def test_run_now_is_refused_while_a_run_of_the_job_is_going_or_when_turned_off(api, monkeypatch):
    sid, started = _run_now_setup(api, monkeypatch, BODY)
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_VIEW")
    r = api.post(f"/api/ingestion-jobs/{sid}/run")
    assert r.status_code == 403 and r.json()["detail"] == (
        "starting a run needs Can Manage Run on the Databricks job 555; you have Can View")
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE_RUN")
    monkeypatch.setattr(main, "_running", lambda: [{"task_collection": "c1", "run_id": 5}])
    r = api.post(f"/api/ingestion-jobs/{sid}/run")
    assert r.status_code == 409 and "already running" in r.json()["detail"] and started == []


def test_deleting_a_job_needs_can_manage_on_its_databricks_job(api, monkeypatch):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE_RUN")
    r = api.post(f"/api/schedules/{sid}/delete")
    assert r.status_code == 403 and api.store.schedules[sid].get("deleted_at") is None
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "IS_OWNER")
    assert api.post(f"/api/schedules/{sid}/delete").status_code == 200


CHANGE_REFUSAL = "changing the job or its tables needs Can Manage on the Databricks job 555; you have Can Manage Run"


def test_changing_pausing_resuming_or_syncing_a_job_needs_can_manage_but_running_it_does_not(api, monkeypatch):
    sid, started = _run_now_setup(api, monkeypatch, BODY)
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE_RUN")
    for call in (lambda: api.post(f"/api/schedules/{sid}", json=dict(BODY, cadence_minutes=60)),
                 lambda: api.post(f"/api/schedules/{sid}/pause"), lambda: api.post(f"/api/schedules/{sid}/resume"),
                 lambda: api.post(f"/api/schedules/{sid}/sync")):
        r = call()
        assert (r.status_code, r.json()["detail"]) == (403, CHANGE_REFUSAL)
    assert api.store.schedules[sid]["cadence_minutes"] == 15 and not api.store.schedules[sid]["paused"]
    assert api.post(f"/api/ingestion-jobs/{sid}/run").status_code == 200 and len(started) == 2
    detail = api.get(f"/api/ingestion-jobs/{sid}").json()
    assert (detail["change_blocked"], detail["run_now"]["blocked"]) == (CHANGE_REFUSAL, None)


def test_anyone_may_set_up_a_paused_job_but_one_that_runs_needs_the_right_to_run_what_it_starts(api, monkeypatch):
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_VIEW")
    r = api.post("/api/schedules", json=BODY)
    assert r.status_code == 403 and r.json()["detail"].startswith("starting a run needs Can Manage Run")
    assert not api.store.schedules
    assert api.post("/api/schedules", json=dict(BODY, paused=True)).status_code == 200


def test_tables_of_a_group_in_a_job_need_can_manage_on_that_job_and_groups_in_no_job_stay_open(api, monkeypatch):
    api.post("/api/schedules", json=BODY)
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE_RUN")
    assert main._groups_refusal(["c1", "c3"], "op@example.com") == (
        f"these tables are in the ingestion job Prod 15: {CHANGE_REFUSAL}")
    assert main._groups_refusal(["c3"], "op@example.com") is None
    r = api.post("/api/tables", json={"task_collection": "c2", "src_type": "sqlserver", "src_connection": "conn",
                                      "use_remote_query": True, "src_database": "d", "src_schema": "dbo",
                                      "src_table": "t", "sink_catalog": "c", "sink_schema": "s", "sink_table": "t",
                                      "load_type": "full"})
    assert r.status_code == 403 and "Prod 15" in r.json()["detail"]


def test_the_permissions_answer_lists_what_the_user_may_not_do_on_each_job_group_and_bundle_job(api, monkeypatch):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    monkeypatch.setattr(main.jobs, "SETUP_JOB_ID", "556")
    monkeypatch.setattr(main.jobs, "DISCOVER_JOB_ID", None)
    asked = []
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: asked.append(str(job_id)) or (
        "CAN_MANAGE_RUN" if str(job_id) == "555" else "CAN_VIEW"))
    answer = api.get("/api/permissions").json()
    assert answer["jobs"][str(sid)] == {"run": None, "change": CHANGE_REFUSAL,
                                        "delete": CHANGE_REFUSAL.replace("changing the job or its tables", "deleting")}
    assert set(answer["groups"]) == {"c1", "c2"}
    assert answer["run"] == {"ingestion": "no ingestion job is configured",
                             "discovery": "no discovery job is configured"}
    assert answer["new_running_job"]["default"] is None
    # The app starts the setup job itself (schema_upgrade), so it never reads the setup job's access list.
    assert "556" not in asked and api.post("/api/jobs/setup/run").status_code in (404, 405)


def test_a_job_records_who_created_it_and_who_last_changed_it_and_a_sent_name_is_ignored(api, monkeypatch):
    sid = api.post("/api/schedules", json=dict(BODY, paused=True)).json()["id"]
    row = api.store.schedules[sid]
    assert (row["created_by"], sch.updated_by(row)) == ("op@example.com", "op@example.com")
    other = TestClient(main.app, headers={"X-Forwarded-Email": "second@example.com"})
    r = other.post(f"/api/schedules/{sid}", json=dict(BODY, paused=True, cadence_minutes=60,
                                                     options={"updated_by": "someone@else.com"}))
    assert r.status_code == 200 and sch.updated_by(api.store.schedules[sid]) == "second@example.com"
    other.post(f"/api/schedules/{sid}/resume")
    assert sch.updated_by(api.store.schedules[sid]) == "second@example.com"


BIG = {"driver_node_type_id": "Standard_D32ds_v5", "node_type_id": "Standard_D16ds_v5", "num_workers": 4}


def _cluster_setup(monkeypatch):
    started = []
    monkeypatch.setattr(main.jobs, "CLUSTER_COPIES_JOB_ID", "700")
    details = [{"id": n, "memory_gb": gb, "cores": cores} for n, gb, cores in
               (("Standard_D16ds_v5", 64, 16), ("Standard_D32ds_v5", 128, 32), ("Standard_D8ds_v5", 32, 8))]
    monkeypatch.setattr(main.jobs, "node_type_details", lambda: details)
    monkeypatch.setattr(main.jobs, "node_types", lambda: [d["id"] for d in details])
    monkeypatch.setattr(main.jobs, "start_cluster_copies", lambda sid: started.append(sid) or 8000 + len(started))
    return started


def test_a_cluster_of_its_own_starts_the_copies_job_and_a_forged_copy_id_is_ignored(api, monkeypatch):
    started = _cluster_setup(monkeypatch)
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    assert started == []
    body = dict(BODY, options={"cluster": BIG, "ingest_copy_job_id": 1})
    r = api.post(f"/api/schedules/{sid}", json=body)
    assert r.status_code == 200 and r.json()["cluster_error"] is None and started == [sid]
    assert r.json()["cluster_started"] is True
    options = sch.options_of(api.store.schedules[sid])
    assert options == {"cluster": BIG, "cluster_copy_run_id": 8001, "target_names": "unity_catalog",
                       "updated_by": "op@example.com"}
    # Saving again with the same cluster starts nothing; the wrappers keep the bundle's job until the copy exists.
    assert api.post(f"/api/schedules/{sid}", json=body).status_code == 200 and started == [sid]
    assert main._wrapper_target(api.store.schedules[sid]) == 555


def test_new_offers_the_job_cluster_fields_before_the_job_exists(api, monkeypatch):
    _cluster_setup(monkeypatch)
    monkeypatch.setattr(main.jobs, "job_settings", lambda job_id: {"job_clusters": [{"new_cluster": {
        "driver_node_type_id": "Standard_D16ds_v5", "node_type_id": "Standard_D8ds_v5", "num_workers": 1}}]})
    r = api.get("/api/cluster-options")
    assert r.status_code == 200
    got = r.json()
    assert got["defaults"] == {"driver_node_type_id": "Standard_D16ds_v5", "node_type_id": "Standard_D8ds_v5", "num_workers": 1}
    assert got["node_types"] and got["cluster"] is None and got["applying"] is None and got["error"] is None


def test_a_cluster_must_name_this_workspaces_node_types_and_at_most_eight_workers(api, monkeypatch):
    _cluster_setup(monkeypatch)
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    r = api.post(f"/api/schedules/{sid}", json=dict(BODY, options={"cluster": dict(BIG, node_type_id="Standard_X9")}))
    assert r.status_code == 422 and "Standard_X9 is not a node type" in r.text
    r = api.post(f"/api/schedules/{sid}", json=dict(BODY, options={"cluster": dict(BIG, num_workers=9)}))
    assert r.status_code == 422 and "0 to 8 workers" in r.text


def test_once_its_copy_exists_a_job_runs_it_and_its_permissions_are_the_copys(api, monkeypatch):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    api.store.schedules[sid]["options"] = '{"cluster": {"num_workers": 4}, "ingest_copy_job_id": 4242}'
    assert main._wrapper_target(api.store.schedules[sid]) == 4242
    asked = []
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: asked.append(str(job_id)) or "CAN_MANAGE")
    main._permission_refusals(api.store.schedules[sid], "op@example.com")
    assert asked == ["4242"]
    # Back at the defaults, the recorded copy is no longer used, before the copies job deletes it.
    api.store.schedules[sid]["options"] = '{"ingest_copy_job_id": 4242}'
    assert main._wrapper_target(api.store.schedules[sid]) == 555


def test_a_check_schedule_with_its_own_cluster_passes_its_copy_to_the_check_job():
    schedule = dict(id=3, name="Check", cadence_minutes=60, timezone="UTC", parameters={}, job_variant="check",
                    paused=False, options={"cluster": {"num_workers": 2}, "ingest_copy_job_id": 4242})
    settings = sch.group_job_settings(schedule, ["c1", "c2"], 777)
    task = settings["tasks"][0]["run_job_task"]
    assert task["job_id"] == 777 and task["job_parameters"]["ingest_job_id"] == "4242"
    assert "ingest_job_id" not in sch.group_job_settings(dict(schedule, options={}), ["c1"], 777)["tasks"][0][
        "run_job_task"]["job_parameters"]


def test_other_variants_cant_have_a_cluster_of_their_own():
    assert sch.cluster_problems({"num_workers": 2}, "large") == [
        "options.cluster: the large variant runs its own Databricks job, whose cluster is set there"]
    assert sch.cluster_problems({"num_workers": 2, "spark_version": "x"}, "default") == [
        "options.cluster: unknown key spark_version"]


def test_the_edit_view_follows_the_copies_run_and_syncs_the_wrappers_once_it_has_ended(api, monkeypatch):
    _cluster_setup(monkeypatch)
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    api.store.schedules[sid]["options"] = '{"cluster": {"num_workers": 4}, "cluster_copy_run_id": 8001}'
    state = {"state": "RUNNING", "result": None, "message": "", "url": "https://h/run/8001"}
    monkeypatch.setattr(main.jobs, "run_state", lambda run_id: dict(state, run_id=run_id))
    monkeypatch.setattr(main.jobs, "job_settings", lambda job_id: {"job_clusters": [{"new_cluster": {
        "driver_node_type_id": "Standard_D16ds_v5", "node_type_id": "Standard_D8ds_v5", "num_workers": 1}}]})
    synced = []
    monkeypatch.setattr(main, "_sync", lambda job_id, user: synced.append(job_id) or [])
    body = api.get(f"/api/ingestion-jobs/{sid}/cluster").json()
    assert body["applying"]["state"] == "RUNNING" and synced == []
    assert body["defaults"] == {"driver_node_type_id": "Standard_D16ds_v5", "node_type_id": "Standard_D8ds_v5",
                                "num_workers": 1} and body["max_workers"] == 8
    assert body["node_types"][0] == {"id": "Standard_D16ds_v5", "memory_gb": 64, "cores": 16}
    # The copies job records the copy, then its run ends: the next read syncs the wrappers to it.
    api.store.schedules[sid]["options"] = ('{"cluster": {"num_workers": 4}, "cluster_copy_run_id": 8001, '
                                           '"ingest_copy_job_id": 4242}')
    state.update(state="TERMINATED", result="SUCCESS")
    body = api.get(f"/api/ingestion-jobs/{sid}/cluster").json()
    assert body["applying"] is None and body["copy_job_id"] == 4242 and synced == [sid]
    assert "cluster_copy_run_id" not in sch.options_of(api.store.schedules[sid])


def test_a_save_without_options_keeps_the_jobs_start_time(api):
    daily = dict(BODY, cadence_minutes=1440, paused=True)
    sid = api.post("/api/schedules", json=dict(daily, options={"start_minute": 150})).json()["id"]
    assert api.post(f"/api/schedules/{sid}", json=daily).status_code == 200
    assert sch.options_of(api.store.schedules[sid])["start_minute"] == 150


def test_a_malformed_or_zoneless_date_in_a_request_is_a_bad_request_not_a_server_error(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    for path in (f"/api/ingestion-jobs/{sid}/runs?before=yesterday", "/api/job-runs?before=2026-13-40",
                 f"/api/ingestion-jobs/{sid}/runs/not-a-time", "/api/job-runs?before=2026-10-29T13:00:00"):
        r = api.get(path)
        assert r.status_code == 400, (path, r.status_code)
        assert "UTC offset" in r.text or "date and time" in r.text



def test_delete_is_soft_it_keeps_the_job_and_starts_no_delete_job(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    r = api.post(f"/api/schedules/{sid}/delete", json={"drop_targets": True})
    assert r.status_code == 200, r.text
    assert r.json()["delete_run"] is None and r.json()["retention_days"] == 7
    assert api.delete_runs == {}
    row = api.store.schedules[sid]
    assert row["deleted_at"] and sch.options_of(row)["delete_drop_targets"] is True
    assert "delete_run_id" not in sch.options_of(row)
    assert all(m["removed_at"] for m in api.store.members.values()) and api.jobs.jobs == {}
    other = api.post("/api/schedules", json=dict(BODY, name="Other")).json()["id"]
    api.post(f"/api/schedules/{other}/delete")                                 # no body: the target tables stay
    assert sch.options_of(api.store.schedules[other])["delete_drop_targets"] is False


def test_delete_is_refused_while_a_run_of_the_job_is_going(api, monkeypatch):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    monkeypatch.setattr(main, "_running", lambda: [{"task_collection": "c2", "run_id": 5}])
    r = api.post(f"/api/schedules/{sid}/delete", json={"drop_targets": True})
    assert r.status_code == 409 and "a run of this job is going" in r.json()["detail"]
    monkeypatch.setattr(main, "_running", lambda: [])
    wrapper = next(m["job_id"] for m in api.store.members.values() if m["task_collection"] == "c1")
    api.active_runs[int(wrapper)] = [{"run_id": 77, "state": "PENDING", "started": "2026-10-29T10:00:00Z", "url": "https://h/r/77"}]
    r = api.post(f"/api/schedules/{sid}/delete")
    assert r.status_code == 409 and r.json()["detail"]["run_url"] == "https://h/r/77"
    assert api.get(f"/api/ingestion-jobs/{sid}/delete-preview").json()["blocked"].startswith("a run of this job is going")
    assert api.store.schedules[sid]["deleted_at"] is None and api.delete_runs == {} and len(api.jobs.jobs) == 2


def test_delete_is_refused_while_a_background_write_to_the_job_is_running(api, monkeypatch):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    answer = main.db.query
    monkeypatch.setattr(main.db, "query",
                        lambda sql, params=(): [{"id": 9}] if sql == q.running_task_sql() else answer(sql, params))
    r = api.post(f"/api/schedules/{sid}/delete")
    assert r.status_code == 409 and "a write to this job is still running" in r.json()["detail"]
    assert api.store.schedules[sid]["deleted_at"] is None


def test_delete_needs_the_delete_job_when_the_groups_have_tables(api, monkeypatch):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    monkeypatch.setattr(main.jobs, "DELETE_JOB_ID", None)
    r = api.post(f"/api/schedules/{sid}/delete")
    assert r.status_code == 409 and "no delete job is configured" in r.json()["detail"]
    assert api.store.schedules[sid]["deleted_at"] is None
    api.store.with_tables -= {"c1", "c2"}                  # nothing would stay behind: no delete job is needed
    r = api.post(f"/api/schedules/{sid}/delete").json()
    assert r["deleted"] is True and r["delete_run"] is None
    assert sch.options_of(api.store.schedules[sid])["delete_drop_targets"] is False


def test_the_preview_counts_the_tables_and_names_the_targets_another_job_also_writes(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    api.store.targets = {"c1": ["main.sales.c1", "main.sales.fan_in"], "c3": ["main.sales.FAN_IN"]}
    preview = api.get(f"/api/ingestion-jobs/{sid}/delete-preview").json()
    assert (preview["groups"], preview["groups_kept"]) == (["c1", "c2"], [])
    assert (preview["tables"], preview["targets"], preview["kept"]) == (3, 2, 1)
    assert preview["kept_names"] == ["main.sales.fan_in"] and preview["blocked"] is None and preview["configured"]


def test_a_group_another_live_job_also_holds_stays_with_its_tables(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    other = api.post("/api/schedules", json=dict(BODY, name="Other", members=["c2"])).json()["id"]
    preview = api.get(f"/api/ingestion-jobs/{sid}/delete-preview").json()
    assert (preview["groups"], preview["groups_kept"], preview["tables"]) == (["c1"], ["c2"], 1)
    api.post(f"/api/schedules/{other}/delete")
    assert api.get(f"/api/ingestion-jobs/{sid}/delete-preview").json()["groups"] == ["c1", "c2"]



def test_a_purge_that_did_not_start_is_listed_as_failed_and_can_be_purged_again(api, monkeypatch):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    api.post(f"/api/schedules/{sid}/delete", json={"drop_targets": True})
    real = main.jobs.start_delete_job
    monkeypatch.setattr(main.jobs, "start_delete_job", lambda sid, drop: (_ for _ in ()).throw(RuntimeError("403 forbidden. more")))
    assert _listed(api, sid) is None                                             # a deleted job is in Deleted jobs, not the list
    r = api.post(f"/api/schedules/{sid}/purge").json()
    assert r["delete_run"]["error"] == "the delete job didn't start: 403 forbidden"
    job = _deleted(api, sid)
    assert (job["delete"]["state"], job["delete"]["reason"]) == ("failed", "the delete job didn't start: 403 forbidden")
    monkeypatch.setattr(main.jobs, "start_delete_job", real)
    again = api.post(f"/api/schedules/{sid}/purge")                            # Purge again: the stored tick box
    assert again.status_code == 200 and api.delete_runs[9001]["drop_targets"] is True
    assert api.post(f"/api/schedules/{sid}/purge").status_code == 409          # its purge is still running
    assert _deleted(api, sid)["delete"]["state"] == "deleting"
    api.delete_runs[9001].update(state="TERMINATED", result="SUCCESS")
    row = api.store.schedules[sid]
    row["options"] = json.dumps({**sch.options_of(row), "delete_finished": True})   # the purge marked it finished
    assert _deleted(api, sid) is None                                            # purged: it leaves the list
    assert api.post(f"/api/schedules/{sid}/purge").status_code == 409


def _delete_job(api, sid, drop_targets=False):
    return api.post(f"/api/schedules/{sid}/delete", json={"drop_targets": drop_targets})


def test_restore_brings_the_job_back_paused_with_only_the_members_its_delete_removed(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    # A group with no tables may leave the job.
    api.store.with_tables.discard("c2")
    assert api.post(f"/api/schedules/{sid}", json=dict(BODY, members=["c1"])).status_code == 200
    _delete_job(api, sid)
    r = api.post(f"/api/schedules/{sid}/restore")
    assert r.status_code == 200, r.text
    assert r.json() == {"id": sid, "restored": True, "sync": r.json()["sync"], "cluster_error": None}
    row = api.store.schedules[sid]
    assert row["deleted_at"] is None and row["paused"] is True
    assert sch.options_of(row).get("delete_drop_targets") is None and "delete_run_id" not in sch.options_of(row)
    live = {m["task_collection"] for m in api.store.members.values() if m["removed_at"] is None}
    assert live == {"c1"}
    assert [m["task_collection"] for m in api.store.members.values() if m["removed_at"] is not None] == ["c2"]
    assert len(api.jobs.jobs) == 1


def test_restore_is_refused_when_a_live_job_has_the_name(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    _delete_job(api, sid)
    api.post("/api/schedules", json=dict(BODY, members=["c3"]))                  # the same name, now live
    r = api.post(f"/api/schedules/{sid}/restore")
    assert r.status_code == 409 and "already named Prod 15" in r.json()["detail"]
    assert api.store.schedules[sid]["deleted_at"] is not None


def test_restore_is_refused_when_one_of_its_groups_is_now_in_another_live_job(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    _delete_job(api, sid)
    api.post("/api/schedules", json=dict(BODY, name="Other", members=["c1"]))
    r = api.post(f"/api/schedules/{sid}/restore")
    assert r.status_code == 409 and "ingestion group c1 is now in the ingestion job Other" in r.json()["detail"]
    assert api.store.schedules[sid]["deleted_at"] is not None


def test_restore_is_refused_when_a_live_job_shares_a_database_or_a_target_table(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    _delete_job(api, sid)
    api.store.conflicts = [("database", "sales_db", "Other")]
    r = api.post(f"/api/schedules/{sid}/restore")
    assert r.status_code == 409
    assert "database sales_db is also read or written by the live ingestion job Other" in r.json()["detail"]
    api.store.conflicts = [("target", "main.sales.fan_in", "Other")]
    r = api.post(f"/api/schedules/{sid}/restore")
    assert r.status_code == 409 and "target table main.sales.fan_in" in r.json()["detail"]
    assert api.store.schedules[sid]["deleted_at"] is not None


def test_new_tables_in_a_database_a_kept_deleted_job_reads_are_refused_before_anything_is_written():
    row = _control_row("East")
    cur = _OwnersCursor([("East", "Hourly", 7)])
    with pytest.raises(main.HTTPException) as refused:
        main._refuse_deleted_databases(cur, [row])
    assert refused.value.status_code == 409
    assert refused.value.detail == ("database East is in the deleted ingestion job Hourly, which is kept until it is "
                                    "purged: Restore Hourly, or Delete now to purge it, then add these tables")
    assert cur.executed == [(q.deleted_database_owners_sql(), ([q.database_key(row)],))]


def test_a_database_no_kept_deleted_job_reads_is_not_refused_and_no_rows_asks_nothing():
    cur = _OwnersCursor([])
    main._refuse_deleted_databases(cur, [_control_row("East")])
    assert len(cur.executed) == 1
    main._refuse_deleted_databases(cur, [])
    assert len(cur.executed) == 1


def _control_row(database):
    return {"src_type": "sqlserver", "src_host": "sql.example", "src_port": 1433, "src_connection": None,
            "secret_key": "scope/key", "src_database": database}


class _OwnersCursor:
    """Answers the deleted-database owners query with the rows given, and records each statement."""

    def __init__(self, owners):
        self.owners, self.executed, self._result = owners, [], []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        self._result = list(self.owners) if sql == q.deleted_database_owners_sql() else []

    def fetchall(self):
        return self._result


def test_restore_is_refused_once_the_purge_has_finished_and_a_live_job_cannot_be_restored(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    assert api.post(f"/api/schedules/{sid}/restore").status_code == 409         # not deleted
    _delete_job(api, sid)
    row = api.store.schedules[sid]
    row["options"] = json.dumps({**sch.options_of(row), "delete_finished": True})
    r = api.post(f"/api/schedules/{sid}/restore")
    assert r.status_code == 409 and "was purged" in r.json()["detail"]


def test_restore_needs_the_delete_permission(api, monkeypatch):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    _delete_job(api, sid)
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE_RUN")
    assert api.post(f"/api/schedules/{sid}/restore").status_code == 403
    assert api.store.schedules[sid]["deleted_at"] is not None


def test_the_deleted_jobs_list_is_paged_and_says_the_days_left_and_what_refuses(api, monkeypatch):
    sids = [api.post("/api/schedules", json=dict(BODY, name=f"Job {i}", members=[c])).json()["id"]
            for i, c in enumerate(["c1", "c2", "c3"])]
    for sid in sids:
        _delete_job(api, sid)
    page = api.get("/api/ingestion-jobs/deletions?limit=2").json()
    assert (page["total"], page["offset"], page["limit"], page["retention_days"]) == (3, 0, 2, 7)
    assert len(page["deleted"]) == 2 and page["deleted"][0]["delete"]["state"] == "kept"
    assert page["deleted"][0]["delete"]["days_left"] == 7 and page["deleted"][0]["delete_refusal"] is None
    last = api.get("/api/ingestion-jobs/deletions?offset=2&limit=2").json()
    assert len(last["deleted"]) == 1 and last["total"] == 3
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE_RUN")
    assert api.get("/api/ingestion-jobs/deletions?limit=1").json()["deleted"][0]["delete_refusal"]


def test_a_purge_running_is_the_refusal_on_its_row_and_the_job_page(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    _delete_job(api, sid)
    api.post(f"/api/schedules/{sid}/purge")
    row = api.get("/api/ingestion-jobs/deletions").json()["deleted"][0]
    assert row["delete"]["state"] == "deleting" and row["delete_refusal"] == "this job's delete is still running"
    assert api.get(f"/api/ingestion-jobs/{sid}").json()["delete_refusal"] == "this job's delete is still running"


def test_purge_is_refused_for_a_job_that_is_live_or_already_purged(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    assert api.post(f"/api/schedules/{sid}/purge").status_code == 409
    _delete_job(api, sid)
    row = api.store.schedules[sid]
    row["options"] = json.dumps({**sch.options_of(row), "delete_finished": True})
    assert api.post(f"/api/schedules/{sid}/purge").status_code == 409
    assert api.delete_runs == {}



def _listed(api, sid):
    """The Ingestion jobs list's row for a job, or None when it is not listed."""
    main._shared_reads.clear()      # the schedules are a shared read, kept for a few seconds
    jobs_listed = api.get("/api/ingestion-jobs?filter=all&limit=100").json()["jobs"]
    return next((j for j in jobs_listed if j["id"] == sid), None)


def _deleted(api, sid):
    """The Deleted jobs list's row for a job (its delete state), or None when it is not kept (not deleted, or purged)."""
    main._shared_reads.clear()
    kept = api.get("/api/ingestion-jobs/deletions?limit=100").json()["deleted"]
    return next((d for d in kept if d["id"] == sid), None)


def test_delete_refused_by_permission_starts_nothing(api, monkeypatch):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE_RUN")
    assert api.post(f"/api/schedules/{sid}/delete", json={"drop_targets": True}).status_code == 403
    assert api.delete_runs == {} and api.store.schedules[sid]["deleted_at"] is None


def _copy_run(monkeypatch, state):
    monkeypatch.setattr(main.run_poller, "run_state_of", lambda run_id: dict(state, run_id=run_id))
    monkeypatch.setattr(main.jobs, "job_settings", lambda job_id: {"job_clusters": [{"new_cluster": {
        "driver_node_type_id": "Standard_D16ds_v5", "node_type_id": "Standard_D8ds_v5", "num_workers": 1}}]})


def test_a_copies_run_that_succeeds_with_a_wrapper_that_did_not_sync_says_so(api, monkeypatch):
    _cluster_setup(monkeypatch)
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    api.store.schedules[sid]["options"] = '{"cluster": {"num_workers": 4}, "cluster_copy_run_id": 8001}'
    _copy_run(monkeypatch, {"state": "TERMINATED", "result": "SUCCESS", "message": "", "url": "https://h/run/8001",
                            "started": None})
    monkeypatch.setattr(main, "_sync", lambda job_id, user: [
        {"task_collection": "sales", "action": "error", "detail": "job is locked"}])
    body = api.get(f"/api/ingestion-jobs/{sid}/cluster").json()
    assert body["applying"] is None
    assert body["error"] == "the job's Databricks jobs did not sync: sales: job is locked"
    assert "cluster_copy_run_id" not in sch.options_of(api.store.schedules[sid])


def test_a_copies_run_that_failed_is_named_and_the_wrappers_are_not_synced_to_it(api, monkeypatch):
    _cluster_setup(monkeypatch)
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    api.store.schedules[sid]["options"] = '{"cluster": {"num_workers": 4}, "cluster_copy_run_id": 8001}'
    _copy_run(monkeypatch, {"state": "TERMINATED", "result": "FAILED", "message": "node type unavailable",
                            "url": "https://h/run/8001", "started": None})
    synced = []
    monkeypatch.setattr(main, "_sync", lambda job_id, user: synced.append(job_id) or [])
    body = api.get(f"/api/ingestion-jobs/{sid}/cluster").json()
    assert body["applying"] is None
    assert body["error"] == "the cluster copies run did not apply the cluster: node type unavailable"
    assert synced == []
    assert "cluster_copy_run_id" not in sch.options_of(api.store.schedules[sid])


def test_a_copies_run_still_listed_past_its_limit_is_a_timeout_and_keeps_the_save_not_done(api, monkeypatch):
    _cluster_setup(monkeypatch)
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    api.store.schedules[sid]["options"] = '{"cluster": {"num_workers": 4}, "cluster_copy_run_id": 8001}'
    _copy_run(monkeypatch, {"state": "RUNNING", "result": None, "message": "", "url": "https://h/run/8001",
                            "started": datetime.now(timezone.utc) - timedelta(hours=1)})
    synced = []
    monkeypatch.setattr(main, "_sync", lambda job_id, user: synced.append(job_id) or [])
    body = api.get(f"/api/ingestion-jobs/{sid}/cluster").json()
    assert body["applying"]["outcome"]["state"] == "timeout"
    assert body["error"] is None and synced == []
    assert sch.options_of(api.store.schedules[sid])["cluster_copy_run_id"] == 8001
