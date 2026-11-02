"""The Overview and the ingestion job endpoints, with a fake database and a fixed clock."""
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import queries as q  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 26, 14, 10, tzinfo=UTC)
ADDED = datetime(2026, 10, 1, tzinfo=UTC)


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


def state(group, tables, current=0, late=0, failing=0):
    """A database's rollup row: its loaded tables, and whether it is late."""
    return dict(server="srv1", src_connection=None, src_host="h", task_collection=group, src_database=f"{group}_db",
                tables=tables, state_loaded=current + late, late_database=late > 0, state_first_load=0,
                state_failing=failing, no_sla_tables=0)


def group_run(run_id, group, started, status="completed", failed=0):
    return dict(run_id=run_id, task_collection=group, status=status, started_at=started.isoformat(),
                finished_at=None if status == "running" else (started + timedelta(minutes=5)).isoformat(),
                duration_s=300, total=10, ok=10 - failed, failed=failed, skipped=8, reseeded=0, job_run_id=None,
                cluster_id=f"cl-{run_id}", parallelism=32, detail=None)


SCHEDULES = [dict(id=1, name="East hourly", cadence_minutes=60, timezone="UTC", paused=False, updated_at=ADDED,
                  parameters={}, job_variant="default", stagger=True),
             dict(id=2, name="Nightly", cadence_minutes=1440, timezone="UTC", paused=True, updated_at=ADDED,
                  parameters={}, job_variant="default", stagger=False)]
MEMBERS = [dict(id=1, schedule_id=1, task_collection="c1", offset_minutes=0, job_id=None, added_at=ADDED, removed_at=None),
           dict(id=2, schedule_id=1, task_collection="c2", offset_minutes=30, job_id=None, added_at=ADDED, removed_at=None),
           dict(id=3, schedule_id=2, task_collection="c3", offset_minutes=0, job_id=None, added_at=ADDED, removed_at=None)]
# The paused nightly job's group c3 last ran ten days ago, before its recent triggers and the 7 days a job page places.
OLDER = [group_run(5, "c3", datetime(2026, 10, 16, 0, 0, 30, tzinfo=UTC))]
RUNS = [group_run(9, "c1", datetime(2026, 10, 26, 13, 0, 20, tzinfo=UTC), status="failed", failed=2),
        group_run(10, "c1", datetime(2026, 10, 26, 13, 40, tzinfo=UTC)),
        group_run(8, "c2", datetime(2026, 10, 26, 13, 30, 10, tzinfo=UTC))]


@pytest.fixture
def client(monkeypatch):
    answers = {
        q.schedules_sql(): SCHEDULES,
        q.rollup_states_sql(): [state("c1", 10, current=8, failing=2), state("c2", 5, current=5),
                                state("c3", 4, late=4), state("c4", 3, current=3)],
        q.rollup_group_names_sql(): [dict(task_collection=g) for g in ("c1", "c2", "c3", "c4")],
        q.control_group_names_sql(): [dict(task_collection=g) for g in ("c1", "c2", "c3", "c4")],
        q.running_progress_sql(): [], q.last_runs_sql(): [],
        q.collection_run_stats_sql(): [dict(task_collection="c1", p50_s=300, p95_s=400),
                                       dict(task_collection="c2", p50_s=300, p95_s=400)],
        q.run_failures_sql(): [dict(task_collection="c1", run_id=9, control_id=1, server="srv1", src_database="c1_db",
                                    src_schema="dbo", src_table="t1", error="connection reset", finished_at=NOW.isoformat()),
                               dict(task_collection="c1", run_id=9, control_id=2, server="srv1", src_database="c1_db",
                                    src_schema="dbo", src_table="t2", error="connection reset", finished_at=NOW.isoformat())],
        q.job_run_databases_sql(): [dict(task_collection="c1", server="srv1", src_database="c1_db", loaded=8, failed=2)],
    }

    def query(sql, params=()):
        if sql == q.schedule_members_sql():
            return [m for m in MEMBERS if m["schedule_id"] in params[0]]
        if sql == q.last_ran_group_runs_sql():
            found = []
            for m in MEMBERS:
                if m["schedule_id"] not in params[0]:
                    continue
                ran = [dict(r, schedule_id=m["schedule_id"]) for r in RUNS + OLDER if r["task_collection"] == m["task_collection"]
                       and r["status"] != "skipped" and datetime.fromisoformat(r["started_at"]) >= m["added_at"]
                       and (m["removed_at"] is None or datetime.fromisoformat(r["started_at"]) < m["removed_at"])]
                found += ran
            newest = {}
            for r in sorted(found, key=lambda r: r["started_at"], reverse=True):
                if r["status"] != "running":
                    newest.setdefault((r["schedule_id"], r["task_collection"]), r)
            return list(newest.values())
        if sql == q.group_runs_in_windows_sql():
            calls.append(len(params[0]))
            return [dict(r) for r in RUNS + OLDER for g, start, end in zip(*params)
                    if r["task_collection"] == g and start <= datetime.fromisoformat(r["started_at"]) < end]
        if sql == q.group_runs_sql():
            groups, start, end = params
            return [dict(r) for r in RUNS + OLDER if r["task_collection"] in groups
                    and start <= datetime.fromisoformat(r["started_at"]) < end]
        return [dict(r) for r in answers.get(sql, [])]
    calls = []
    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main, "datetime", FixedClock)
    monkeypatch.setattr(main, "MONITOR_CACHE_S", 0)
    monkeypatch.setattr(main, "STATES_CACHE_S", 0)
    client = TestClient(main.app)
    client.window_reads = calls
    return client


def test_the_overview_lists_jobs_not_in_any_job_and_attention_without_a_paused_late_job(client):
    body = client.get("/api/overview").json()
    assert [j["name"] for j in body["jobs"]] == ["East hourly", "Nightly"] and body["jobs_total"] == 2
    east = body["jobs"][0]
    assert east["last_job_run"]["outcome"]["label"] == "Succeeded with failures"
    assert east["status"]["label"] == "Running" and "2 tables failing" in east["status"]["detail"]     # c2 starts at 14:30
    assert east["status"]["cause"] == "Could not connect to the source database"
    assert body["running_total"] == 1 and body["running_job_runs"][0]["waiting"] == 2     # c1 starting, c2 at 14:30
    assert [j["name"] for j in body["attention"]] == ["East hourly"] and body["attention_total"] == 1
    assert body["not_in_job"]["groups"] == ["c4"]
    assert body["totals"]["enabled_tables"] == 22 and "collections" not in body and "servers" not in body


def test_a_jobs_runs_page_by_slot_and_mark_extra_runs(client):
    body = client.get("/api/ingestion-jobs/1/runs?limit=3").json()
    slots = [(r["slot"][11:16], r["outcome"]["label"], r["extra"]) for r in body["job_runs"]]
    assert slots[:2] == [("14:00", "Running", 0), ("13:00", "Succeeded with failures", 1)]
    assert body["job_runs"][1]["clusters"] == 3 and body["job_runs"][1]["parallelism"] == [32]


def test_a_job_run_lists_its_waiting_members_with_their_start(client):
    body = client.get("/api/ingestion-jobs/1/runs/2026-10-26T14:00:00+00:00").json()
    waiting = [g for g in body["group_runs"] if g["state"] == "waiting"]
    assert [(g["task_collection"], g["fire_at"]) for g in waiting] == [("c2", "2026-10-26T14:30:00+00:00")]
    assert client.get("/api/ingestion-jobs/1/runs/2026-10-26T14:30:00+00:00").status_code == 404
    done = client.get("/api/ingestion-jobs/1/runs/2026-10-26T13:00:00+00:00").json()
    assert done["databases"][0]["failed"] == 2 and done["failures"][0]["tables"] == 2


def test_the_ingestion_jobs_list_filters_with_counts_and_ends_with_not_in_any_job(client):
    body = client.get("/api/ingestion-jobs").json()
    assert [j["name"] for j in body["jobs"]] == ["East hourly", "Nightly"] and body["total"] == 2
    assert body["not_in_job"]["groups"] == ["c4"]
    assert body["counts"] == {"all": 2, "running": 1, "failing": 1, "late": 1, "paused": 1, "attention": 1}
    assert [j["name"] for j in client.get("/api/ingestion-jobs?filter=attention").json()["jobs"]] == ["East hourly"]
    assert [j["name"] for j in client.get("/api/ingestion-jobs?filter=late").json()["jobs"]] == ["Nightly"]
    assert [j["name"] for j in client.get("/api/ingestion-jobs?search=c3").json()["jobs"]] == ["Nightly"]


def test_a_job_run_finds_a_group_run_that_started_near_the_end_of_its_slot(client):
    RUNS.append(group_run(11, "c2", datetime(2026, 10, 26, 12, 10, tzinfo=UTC)))     # late, after c2's 11:30 trigger
    try:
        body = client.get("/api/ingestion-jobs/1/runs/2026-10-26T11:00:00+00:00").json()
    finally:
        RUNS.pop()
    assert [g["run_id"] for g in body["group_runs"] if g["task_collection"] == "c2"] == [11]


def test_runs_across_jobs_pin_the_running_job_runs_and_count_outcomes(client):
    body = client.get("/api/job-runs").json()
    labels = [(r["job_name"], r["slot"][11:16], r["outcome"]["label"]) for r in body["job_runs"]][:3]
    assert labels[0] == ("East hourly", "14:00", "Running") and ("East hourly", "13:00", "Succeeded with failures") in labels
    assert body["counts"]["Succeeded with failures"] == 1
    none = client.get("/api/job-runs?job=none").json()
    assert none["job_runs"] == [] and "group_runs" in none


def test_sources_list_a_uc_connection_with_nothing_registered(client, monkeypatch):
    class Connection:
        def __init__(self, name, host):
            self.name, self.connection_type, self.options, self.owner, self.comment = name, "SQLSERVER", {"host": host}, "o", None

    class Workspace:
        class connections:
            @staticmethod
            def list():
                return [Connection("unused_conn", "nobody.example.com")]
    monkeypatch.setattr(main.db, "workspace", lambda: Workspace())
    body = client.get("/api/sources").json()
    empty = [i for i in body["instances"] if i["nothing_registered"]]
    assert [i["server"] for i in empty] == ["unused_conn"] and body["nothing_registered"] == 1
    assert {i["server"] for i in body["instances"] if not i["nothing_registered"]} == {"srv1"}
    on_srv1 = next(i for i in body["instances"] if i["server"] == "srv1")
    assert {j["name"]: j.get("description") for j in on_srv1["jobs"] if j["id"]}["East hourly"] == "Every hour, on the hour (UTC)"


def test_one_database_lists_its_tables_with_the_group_and_its_job(client, monkeypatch):
    calls = []
    real = main.db.query

    def query(sql, params=()):
        if sql == q.database_tables_sql():
            calls.append(params)
            return [dict(id=1, src_schema="dbo", src_table="t1", state="failing")]
        if sql == q.database_tables_count_sql():
            return [dict(tables=1)]
        if sql == q.database_counts_sql():
            found = params[1] == "srv1"
            return [dict(control_rows=int(found), tables=10 if found else 0, no_sla_tables=0, state_current=8,
                         state_late=0, state_first_load=0, state_failing=2)]
        return real(sql, params)
    monkeypatch.setattr(main.db, "query", query)
    body = client.get("/api/collections/c1/databases/srv1/c1_db?search=t1").json()
    assert (body["tables"][0]["state"], body["total"], body["jobs"]) == ("failing", 1, [{"id": 1, "name": "East hourly"}])
    assert (body["enabled_tables"], body["state_current"], body["state_failing"]) == (10, 8, 2)     # the database's, not the page's
    assert calls[0][:5] == ("c1", "srv1", "c1_db", None, "%t1%")
    assert client.get("/api/collections/c1/databases/srv9/c1_db").status_code == 404


def test_an_outcome_filter_reads_older_windows_until_it_finds_matches(client):
    RUNS.append(group_run(13, "c1", datetime(2026, 10, 26, 9, 1, tzinfo=UTC), status="failed", failed=4))
    try:
        body = client.get("/api/job-runs", params={"outcome": "Succeeded with failures", "limit": 1,
                                                    "before": "2026-10-26T12:00:00+00:00"}).json()
    finally:
        RUNS.pop()
    assert [r["slot"][11:16] for r in body["job_runs"]] == ["09:00"]


def test_runs_of_groups_in_no_job_are_read_from_the_database_before_a_time(client, monkeypatch):
    calls, real = [], main.db.query

    def query(sql, params=()):
        if sql == q.group_runs_before_sql():
            calls.append(params)
            return [group_run(30, "c4", datetime(2026, 10, 26, 8, 0, tzinfo=UTC))]
        return real(sql, params)
    monkeypatch.setattr(main.db, "query", query)
    body = client.get("/api/job-runs", params={"job": "none", "before": "2026-10-26T12:00:00+00:00"}).json()
    assert [r["run_id"] for r in body["group_runs"]] == [30] and calls[0][0] == ["c4"]


def test_a_jobs_last_job_run_is_its_last_real_one_however_long_ago_and_never_a_missed_trigger(client):
    jobs = {j["name"]: j for j in client.get("/api/overview").json()["jobs"]}
    nightly = jobs["Nightly"]["last_job_run"]
    assert (nightly["slot"], nightly["outcome"]["label"]) == ("2026-10-16T00:00:00+00:00", "Succeeded")
    assert client.window_reads == [1]       # one read, of the windows around every such job's slot
    assert client.get("/api/ingestion-jobs/2").json()["last_job_run"]["slot"] == "2026-10-16T00:00:00+00:00"
    listed = {j["name"]: j for j in client.get("/api/ingestion-jobs").json()["jobs"]}
    assert all(j["last_job_run"] is None or j["last_job_run"]["outcome"]["label"] != "Not run" for j in listed.values())


def test_a_jobs_page_gives_its_next_trigger_or_none_while_paused(client):
    assert client.get("/api/ingestion-jobs/1").json()["next_start"] == "2026-10-26T14:30:00+00:00"
    assert client.get("/api/ingestion-jobs/2").json()["next_start"] is None
    overview = client.get("/api/overview").json()
    assert [(t["id"], t["next_start"]) for t in overview["next_triggers"]] == [(1, "2026-10-26T14:30:00+00:00")]
    assert overview["paused_jobs"] == 1


def test_the_groups_in_no_job_have_a_page_of_their_own(client):
    # A read-only app with no jobs still lets you click through: the "Not in any job" row lists its groups and tables.
    body = client.get("/api/ingestion-jobs/not-in-job").json()
    assert [g["task_collection"] for g in body["groups_detail"]] == ["c4"]
    assert (body["groups_detail"][0]["enabled_tables"], body["groups_detail"][0]["state_loaded"]) == (3, 3)
    assert (body["enabled_tables"], body["name"]) == (3, "Not in any job")


def test_the_job_page_follows_a_run_from_starting_to_running_to_cancelling_and_idle(client, monkeypatch):
    monkeypatch.setattr(main.jobs, "ingest_variants", lambda: {"default": "555"})
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE_RUN")
    signed_in = {"X-Forwarded-Email": "op@example.com"}
    now = NOW       # the page's clock (this file reads the app at a fixed time)
    going = []      # the poller's active group runs of job 555, as its table shows them
    monkeypatch.setattr(main.run_poller, "runs_of", lambda job_id, active_only=False, limit=25:
                        [dict(r) for r in going] if str(job_id) == "555" else [])

    def run_now():
        return client.get("/api/ingestion-jobs/1", headers=signed_in).json()["run_now"]

    assert run_now()["phase"] == "idle"
    going.append(dict(run_id=7, job_id=555, task_collection="c1", state="PENDING", started=now.isoformat(), seen=None))
    starting = run_now()
    assert (starting["phase"], starting["going"], starting["timed_out"]) == ("starting", True, False)
    going[0].update(state="RUNNING", url="https://h/run/7")
    assert (run_now()["phase"], run_now()["run_url"]) == ("running", "https://h/run/7")
    going[0].update(state="TERMINATING", seen=(now - timedelta(minutes=11)).isoformat())
    cancelling = run_now()
    assert (cancelling["phase"], cancelling["timed_out"]) == ("cancelling", True), cancelling
    going.clear()
    assert (run_now()["phase"], run_now()["going"]) == ("idle", False)


def test_a_job_run_whose_cancel_is_not_closed_yet_reads_cancelling_on_the_list_and_its_page(client, monkeypatch):
    real_query = main.db.query
    cancelling_run = dict(group_run(701, "c1", datetime(2026, 10, 26, 14, 0, 30, tzinfo=UTC), status="running"),
                          job_run_id="700")

    def query(sql, params=()):
        if sql == q.cancel_requested_runs_sql():
            return [{"run_id": 700}]
        rows = real_query(sql, params)
        if sql in (q.group_runs_sql(), q.group_runs_in_windows_sql()):
            rows = rows + [dict(cancelling_run)]
        return rows

    monkeypatch.setattr(main.run_poller, "table_ready", lambda: True)
    monkeypatch.setattr(main.db, "query", query)
    labels = [(r["job_name"], r["slot"][11:16], r["outcome"]["label"])
              for r in client.get("/api/job-runs?job=1").json()["job_runs"]]
    assert ("East hourly", "14:00", "Cancelling") in labels
    page = client.get("/api/ingestion-jobs/1/runs/2026-10-26T14:00:00+00:00").json()
    assert page["outcome"]["label"] == "Cancelling"
    assert "Cancelling" in [r["status"]["label"] for r in page["group_runs"]]


def test_the_job_page_says_why_run_now_or_delete_cannot_start(client, monkeypatch):
    monkeypatch.setattr(main.jobs, "ingest_variants", lambda: {"default": "555"})
    monkeypatch.setattr(main.run_poller, "runs_of", lambda job_id, active_only=False, limit=25: [])
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_MANAGE_RUN")
    signed_in = {"X-Forwarded-Email": "op@example.com"}
    body = client.get("/api/ingestion-jobs/1", headers=signed_in).json()
    assert body["run_now"] == {"blocked": "the job's Databricks jobs are not synced yet", "refused": None, "going": False,
                              "groups": 2, "check": False, "phase": "idle", "since": None, "timed_out": False,
                              "run_id": None, "run_url": None}
    assert body["delete_blocked"] == "deleting needs Can Manage on the Databricks job 555; you have Can Manage Run"
    # With no user from the Apps proxy, nothing is allowed, whatever the job's access list says.
    anonymous = client.get("/api/ingestion-jobs/1").json()
    assert anonymous["run_now"]["blocked"] == anonymous["delete_blocked"] == main.NO_IDENTITY

    def unreadable(job_id, user):
        raise RuntimeError("403")
    monkeypatch.setattr(main.jobs, "user_job_permission", unreadable)
    assert client.get("/api/ingestion-jobs/1", headers=signed_in).json()["run_now"]["blocked"].startswith(
        "can't read your permission")


def test_a_jobs_target_tables_come_with_their_sources_and_a_catalog_explorer_link(monkeypatch):
    target = dict(target="bronze.sales.orders", catalog="bronze", schema="sales", table="orders", sources=100,
                  databases=100, with_client_id=100, outside_sources=0, rows_written_7d=4253, total=1, fan_in_total=1)
    seen = {}

    def query(sql, params=()):
        if sql == q.schedule_sql():
            return [dict(SCHEDULES[0])]
        if sql == q.schedule_members_sql():
            return [m for m in MEMBERS if m["schedule_id"] in params[0]]
        seen[sql] = params
        return [dict(target)] if sql == q.job_targets_sql() else [dict(id=7, database="db_1", total=1)]
    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(main.db, "workspace", lambda: type("W", (), {"config": type("C", (), {"host": "https://h/"})()})())
    client = TestClient(main.app)
    body = client.get("/api/ingestion-jobs/1/targets", params={"search": "ord"}).json()
    assert body["total"] == 1 and body["fan_in"] == 1 and {"total", "fan_in_total"}.isdisjoint(body["items"][0])
    assert body["items"][0]["url"] == "https://h/explore/data/bronze/sales/orders"
    assert seen[q.job_targets_sql()] == (["c1", "c2"], ["c1", "c2"], ["c1", "c2"], "%ord%", 25, 0)
    sources = client.get("/api/ingestion-jobs/1/targets/sources", params={"target": "bronze.sales.orders"}).json()
    assert sources["items"] == [{"id": 7, "database": "db_1"}]
    assert seen[q.job_target_sources_sql()] == (["c1", "c2"], ["c1", "c2"], "bronze", "sales", "orders", 25, 0)
    assert client.get("/api/ingestion-jobs/1/targets/sources", params={"target": "orders"}).status_code == 400


def test_the_timeline_draws_each_jobs_runs_in_its_own_lane_with_their_groups(client, monkeypatch):
    monkeypatch.setattr(main.jobs, "schedule_client", lambda: None)
    monkeypatch.setattr(main, "_skipped_starts", lambda job_id, since, now, client=None: [])
    body = client.get("/api/timeline?hours=168").json()
    lanes = {lane["name"]: lane for lane in body["jobs"]}
    ran = [r for lane in lanes.values() for r in lane["runs"] if r["started_at"]]
    assert ran and all(r["ended_at"] and r["groups"] and {"group", "status", "run_id"} <= set(r["groups"][0]) for r in ran)
    # A trigger that did not run has no span; a running job run with no group run started yet ends now.
    assert all(r["ended_at"] is None for lane in lanes.values() for r in lane["runs"] if r["outcome"]["label"] == "Not run")
    assert not any(r["outcome"].get("paused") for r in lanes["Nightly"]["runs"])      # Nightly is paused
