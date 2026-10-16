"""Schedule endpoints (main.py) with a fake Lakebase and a fake Jobs API: create, edit, pause, delete."""
import contextlib
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import queries as q  # noqa: E402
import schedules as sch  # noqa: E402

from schedules_test import FakeJobs  # noqa: E402


class Store:
    """A tiny in-memory stand-in for the schedule tables, driven by the SQL main.py runs."""

    def __init__(self):
        self.schedules, self.members, self.log, self.unique = {}, {}, [], False

    def now(self):
        return datetime.now(timezone.utc).isoformat()

    def run(self, sql, params):
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
        elif sql.startswith(f"update {q.SCHEMA}.schedule_member set job_id"):
            m = self.members[params[2]]
            m.update(job_id=params[0], sync_error=params[1], synced_at=self.now())
        elif "control_change" in sql:
            self.log.append(params)
        return []

    def query(self, sql, params=()):
        if "from lakefed_ingest.control" in sql and "distinct task_collection" in sql:
            return [{"task_collection": c} for c in ("c1", "c2", "c3")]
        if sql == q.schedules_sql():
            return [dict(s) for s in self.schedules.values() if s["deleted_at"] is None]
        if sql == q.schedule_sql():
            s = self.schedules.get(params[0])
            return [dict(s)] if s else []
        if sql == q.schedule_members_sql():
            return [dict(m) for m in self.members.values() if m["schedule_id"] in params[0]]
        return []


class Cur:
    def __init__(self, store):
        self.store, self._rows = store, []

    def execute(self, sql, params=()):
        self._rows = self.store.run(sql, params)

    def fetchone(self):
        return self._rows[0] if self._rows else None


@pytest.fixture
def api(monkeypatch):
    store, jobs_api = Store(), FakeJobs()
    monkeypatch.setattr(main.db, "query", store.query)
    monkeypatch.setattr(main.db, "execute", lambda sql, params=(): store.run(sql, params) and 0)
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield Cur(store))))
    monkeypatch.setattr(main.db, "workspace", lambda: type("W", (), {"config": type("C", (), {"host": "https://h"})()})())
    monkeypatch.setattr(main.jobs, "schedule_client", lambda: jobs_api)
    monkeypatch.setattr(main.jobs, "ingest_variants", lambda: {"default": "555"})
    monkeypatch.setattr(main.jobs, "SCHEDULE_JOB_VIEW_GROUP", None)
    monkeypatch.setattr(q, "collections_sql", lambda: "select distinct task_collection from lakefed_ingest.control")
    c = TestClient(main.app, headers={"X-Forwarded-Email": "op@example.com"})
    c.store, c.jobs = store, jobs_api
    return c


BODY = dict(name="Prod 15", cadence_minutes=15, timezone="UTC", parameters={"parallelism": "16"},
            stagger=True, paused=False, members=["c1", "c2"])


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
    assert m["c2"]["when"] == "every 15 minutes at :07 past" and m["c2"]["next_run"] and m["c2"]["job_url"].startswith("https://h/jobs/")


def test_validation_and_duplicate_name(api):
    r = api.post("/api/schedules", json=dict(BODY, cadence_minutes=7, members=["c1", "nope"]))
    assert r.status_code == 422 and "cadence_minutes" in r.text and "unknown collections nope" in r.text
    api.store.unique = True
    assert api.post("/api/schedules", json=BODY).status_code == 409


def test_edit_adds_and_removes_members_restaggers_and_resets_jobs(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
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
    assert api.get("/api/schedules").json() == []
    assert api.post(f"/api/schedules/{sid}/pause").status_code == 409


def test_check_reports_drift_without_changing_anything(api):
    sid = api.post("/api/schedules", json=BODY).json()["id"]
    jid = next(m["job_id"] for m in api.store.members.values() if m["task_collection"] == "c1")
    api.jobs.jobs[jid] = dict(api.jobs.jobs[jid], max_concurrent_runs=4)
    status = api.get(f"/api/schedules/{sid}/check").json()
    assert status["c1"] == {"status": "drifted", "fields": ["max_concurrent_runs"]} and status["c2"]["status"] == "in_sync"
    assert api.jobs.jobs[jid]["max_concurrent_runs"] == 4


def test_timeline_has_planned_triggers_for_scheduled_collections(api, monkeypatch):
    api.post("/api/schedules", json=BODY)
    t = api.get("/api/timeline?hours=2").json()
    rows = {r["task_collection"]: r for r in t["collections"]}
    assert set(rows) == {"c1", "c2"} and len(rows["c1"]["planned"]) <= 1      # only since the schedule began
    api.store.members[1]["added_at"] = "2020-01-01T00:00:00+00:00"
    api.store.schedules[1]["updated_at"] = "2020-01-01T00:00:00+00:00"
    rows = {r["task_collection"]: r for r in api.get("/api/timeline?hours=2").json()["collections"]}
    assert 7 <= len(rows["c1"]["planned"]) <= 9
    assert rows["c2"]["schedules"] == ["Prod 15 (every 15 minutes at :07 past)"]
