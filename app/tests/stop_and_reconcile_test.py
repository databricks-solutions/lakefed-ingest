"""Runs recorded as running whose Databricks run has ended are closed by the app (never one still going, and nothing
is ever cancelled on its own), and Cancel run on the job page stops the job's wrapper, check and group runs."""
import contextlib
import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import jobs  # noqa: E402
import main  # noqa: E402
import queries as q  # noqa: E402
from schedules_api_test import BODY, _run_now_setup, api  # noqa: E402,F401  (api is the fixture)

NOW = datetime.now(timezone.utc)
_real_running = main._running      # _run_now_setup replaces it with an empty one


def _open(run_id, collection, job_run_id, started=NOW):
    return {"run_id": run_id, "task_collection": collection, "job_run_id": job_run_id, "started_at": started.isoformat()}


def _reconcile(monkeypatch, open_runs, states):
    """_close_ended_runs over open_runs, with each Databricks run's state from states (an exception is raised);
    returns how many it closed and the closes it wrote, as (run_id, detail, status)."""
    writes, read = [], []

    class Cur:
        def execute(self, sql, params=()):
            if sql == q.close_cancelled_run_sql():
                writes.append((params[2], json.loads(params[1]), "cancelled"))
            else:
                assert sql == q.close_ended_run_sql()
                writes.append((params[1], json.loads(params[0]), "failed"))

    def run_ended(run_id):
        read.append(run_id)
        state = states[run_id]
        if isinstance(state, Exception):
            raise state
        return state
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): open_runs if sql == q.open_runs_sql() else [])
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield Cur())))
    monkeypatch.setattr(main.jobs, "run_ended", run_ended)
    monkeypatch.setattr(main.jobs, "cancel", lambda run_id: pytest.fail("the reconcile never cancels a run"))
    return main._close_ended_runs(), writes, read


def test_a_run_whose_databricks_run_ended_cancelled_is_closed_as_cancelled_with_how_it_ended(monkeypatch):
    ended = {"state": "TERMINATED", "result": "CANCELED", "message": "cancelled by a user", "ended": "2026-10-29T14:30:00+00:00"}
    closed, writes, _ = _reconcile(monkeypatch, [_open(7, "c1", "650")], {"650": ended})
    assert closed == 1 and [(w[0], w[2]) for w in writes] == [(7, "cancelled")]
    detail = writes[0][1]
    assert detail["closed_by_app"] == "reconcile" and detail["databricks_run"] == 650
    assert detail["databricks_state"] == "TERMINATED" and detail["databricks_result"] == "CANCELED"
    assert detail["reason"] == "cancelled, and its Databricks run ended CANCELED at 2026-10-29T14:30:00+00:00"


def test_a_run_whose_databricks_run_failed_is_still_closed_as_failed(monkeypatch):
    ended = {"state": "INTERNAL_ERROR", "result": None, "message": "lost", "ended": None}
    closed, writes, _ = _reconcile(monkeypatch, [_open(7, "c1", "650")], {"650": ended})
    assert closed == 1 and [(w[0], w[2]) for w in writes] == [(7, "failed")]
    assert writes[0][1]["reason"] == "its Databricks run ended INTERNAL_ERROR without finishing (lost)"


def test_a_run_still_going_unreadable_or_with_no_job_run_is_left_running(monkeypatch):
    runs = [_open(1, "c1", "101"), _open(2, "c1", "102"), _open(3, "c2", None), _open(4, "c2", "by hand"),
            _open(5, "c2", "105")]
    states = {"101": None, "102": RuntimeError("503 from the Jobs API"), "105": None}
    closed, writes, read = _reconcile(monkeypatch, runs, states)
    assert closed == 0 and writes == []
    assert sorted(read) == ["101", "102", "105"]                 # no job run id: not asked at all


def test_a_run_databricks_does_not_know_is_closed_only_once_it_is_a_day_old(monkeypatch):
    gone = {"state": "NOT_FOUND", "result": None, "message": None, "ended": None}
    runs = [_open(1, "c1", "101", NOW - timedelta(hours=2)), _open(2, "c1", "102", NOW - timedelta(hours=30)),
            dict(_open(3, "c1", "103"), started_at=(NOW - timedelta(hours=30)).replace(tzinfo=None).isoformat())]
    closed, writes, _ = _reconcile(monkeypatch, runs, {"101": gone, "102": gone, "103": gone})
    assert closed == 2 and [w[0] for w in writes] == [2, 3]
    assert writes[0][1]["reason"] == "its Databricks run no longer exists, so it ended without finishing"


def test_a_failing_reconcile_still_lets_the_pages_read_what_is_running(monkeypatch):
    main._forget_shared_reads()
    monkeypatch.setattr(main, "_close_ended_runs", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): [{"run_id": 1}] if sql == q.running_progress_sql() else [])
    assert main._running() == [{"run_id": 1}]
    main._forget_shared_reads()


def test_run_ended_reads_the_jobs_api_once_in_a_while_and_never_guesses(monkeypatch):
    from databricks.sdk.errors import NotFound, PermissionDenied
    from databricks.sdk.service import jobs as sdk_jobs
    calls = []
    answers = {
        1: sdk_jobs.Run(state=sdk_jobs.RunState(life_cycle_state=sdk_jobs.RunLifeCycleState.RUNNING)),
        2: sdk_jobs.Run(state=sdk_jobs.RunState(life_cycle_state=sdk_jobs.RunLifeCycleState.TERMINATING)),
        3: sdk_jobs.Run(state=sdk_jobs.RunState(life_cycle_state=sdk_jobs.RunLifeCycleState.TERMINATED,
                                                result_state=sdk_jobs.RunResultState.FAILED, state_message="boom"),
                        end_time=1761747000000),
        4: NotFound("Run 4 does not exist"),
        5: PermissionDenied("no access"),
        6: sdk_jobs.Run(state=None),
    }

    def get_run(run_id):
        calls.append(run_id)
        answer = answers[run_id]
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(jobs, "workspace", lambda: type("W", (), {"jobs": type("J", (), {"get_run": staticmethod(get_run)})()})())
    jobs.forget_run_states()
    assert jobs.run_ended(1) is None and jobs.run_ended(2) is None and jobs.run_ended(6) is None
    assert jobs.run_ended(3)["state"] == "TERMINATED" and jobs.run_ended(3)["result"] == "FAILED"
    assert jobs.run_ended(4)["state"] == "NOT_FOUND"
    with pytest.raises(PermissionDenied):
        jobs.run_ended(5)
    assert calls.count(3) == 1                                    # read once, then kept for RUN_STATE_CACHE_S
    jobs.forget_run_states()
    jobs.run_ended(3)
    assert calls.count(3) == 2


def test_a_run_the_app_closed_reads_as_ended_without_finishing_with_its_reason():
    run = {"status": "failed", "detail": json.dumps({"closed_by_app": "op@example.com", "reason": "stopped from the app by op@example.com"})}
    assert q.run_outcome(run) == {"label": "Ended without finishing", "tone": "bad", "note": "stopped from the app by op@example.com"}


def _going(run_id, state="RUNNING", **parameters):
    return {"run_id": run_id, "state": state, "result": None, "started": "2026-10-29T15:03:00+00:00", "ended": None,
            "url": f"https://h/runs/{run_id}", "parameters": parameters}


def _stop_setup(api, monkeypatch, recorded=(), states=None):  # noqa: F811
    sid, _ = _run_now_setup(api, monkeypatch, BODY)
    cancelled, closes = [], []
    monkeypatch.setattr(main.jobs, "cancel", lambda run_id: cancelled.append(int(run_id)))

    def run_ended(run_id):
        state = (states or {})[str(run_id)]
        if isinstance(state, Exception):
            raise state
        return state
    monkeypatch.setattr(main.jobs, "run_ended", run_ended)
    real_query, real_run = api.store.query, api.store.run

    def query(sql, params=()):
        return [dict(r) for r in recorded] if sql == q.open_runs_sql() else real_query(sql, params)

    def run(sql, params):
        if sql in (q.close_ended_run_sql(), q.close_cancelled_run_sql()):
            closes.append((params[1] if sql == q.close_ended_run_sql() else params[2], sql))
            return [(1, 0)]
        return real_run(sql, params)
    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(api.store, "run", run)
    return sid, cancelled, closes


def test_cancel_run_cancels_the_jobs_wrappers_and_its_groups_runs_and_leaves_their_records_for_the_poller(api, monkeypatch):  # noqa: F811
    sid, cancelled, closes = _stop_setup(api, monkeypatch, recorded=[_open(41, "c1", "11"), _open(42, "other", "13")])
    wrapper = int(next(m["job_id"] for m in api.store.members.values() if m["task_collection"] == "c1"))
    api.active_runs[wrapper] = [_going(4)]
    api.active_runs[555] = [_going(11, task_collection="c1"), _going(12, "PENDING", task_collection="c2"),
                            _going(13, task_collection="other"), _going(9, "TERMINATED", task_collection="c1")]
    r = api.post(f"/api/ingestion-jobs/{sid}/stop")
    assert r.status_code == 200, r.text
    assert sorted(cancelled) == [4, 11, 12]                       # not another job's group, nor a run already over
    body = r.json()
    assert sorted((c["kind"], c["run_id"]) for c in body["cancelled"]) == [("group", 11), ("group", 12), ("wrapper", 4)]
    assert body["errors"] == [] and "closed" not in body
    assert closes == []                                           # the poller closes each record once Databricks ends it
    stops = [p for p in api.store.log if p[1] == "job_stop"]
    assert sorted(p[3] for p in stops) == ["c1", "c2"] and len({p[-1] for p in stops}) == 1     # one batch


def test_cancel_run_while_every_group_loads_cancels_each_group_run_and_its_close_statements_postgres_can_type(api, monkeypatch):  # noqa: F811
    # The 29 Oct sequence: every group of the job loading, Cancel run, Databricks then reporting each run cancelled.
    # The store refuses a statement as Postgres does when a parameter can't be typed, so an untyped one fails here.
    from sql_parameter_types_test import untyped_parameters
    sid, cancelled, closes = _stop_setup(api, monkeypatch, recorded=[_open(65, "c1", "21"), _open(66, "c2", "22")])
    real_run = api.store.run

    def typed_run(sql, params):
        if untyped_parameters(sql):
            raise RuntimeError("could not determine data type of parameter $3")
        return real_run(sql, params)
    monkeypatch.setattr(api.store, "run", typed_run)
    api.active_runs[555] = [_going(21, task_collection="c1"), _going(22, task_collection="c2")]
    r = api.post(f"/api/ingestion-jobs/{sid}/stop")
    assert r.status_code == 200, r.text
    assert sorted(cancelled) == [21, 22] and closes == []
    # The poller closes them once Databricks reports them over, with these statements: each must be one Postgres can type.
    assert untyped_parameters(q.close_cancelled_run_sql()) == [] and untyped_parameters(q.close_ended_run_sql()) == []
    assert untyped_parameters(q.mark_cancel_requested_sql()) == []


def test_cancel_run_on_a_check_job_cancels_its_check_run_too_but_not_another_jobs(api, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main.jobs, "CHECK_JOB_ID", "777")
    sid, _ = _run_now_setup(api, monkeypatch, dict(BODY, job_variant="check"))
    cancelled = []
    monkeypatch.setattr(main.jobs, "cancel", lambda run_id: cancelled.append(int(run_id)))
    api.active_runs[777] = [_going(5, schedule_id=str(sid)), _going(6, schedule_id=str(sid + 1)),
                            _going(3, "TERMINATED", schedule_id=str(sid))]
    api.active_runs[555] = [_going(11, task_collection="c1", work_id="5")]
    body = api.post(f"/api/ingestion-jobs/{sid}/stop").json()
    assert sorted(cancelled) == [5, 11] and sorted(c["kind"] for c in body["cancelled"]) == ["check", "group"]


def test_cancel_run_also_stops_a_recorded_run_the_list_missed_and_skips_one_already_over(api, monkeypatch):  # noqa: F811
    ended = {"state": "TERMINATED", "result": "CANCELED", "message": None, "ended": None}
    recorded = [_open(51, "c1", "77"), _open(52, "c2", "78"), _open(53, "c2", "79")]
    sid, cancelled, closes = _stop_setup(api, monkeypatch, recorded, {"77": None, "78": ended, "79": RuntimeError("503")})
    body = api.post(f"/api/ingestion-jobs/{sid}/stop").json()
    assert cancelled == [77] and [(c["kind"], c["task_collection"]) for c in body["cancelled"]] == [("group", "c1")]
    assert closes == [] and body["errors"] == ["run 79: 503"]


def test_cancel_run_reports_a_run_it_could_not_cancel_and_still_stops_the_others(api, monkeypatch):  # noqa: F811
    sid, cancelled, closes = _stop_setup(api, monkeypatch, recorded=[_open(41, "c1", "11"), _open(42, "c2", "12")])
    api.active_runs[555] = [_going(11, task_collection="c1"), _going(12, task_collection="c2")]

    def cancel(run_id):
        if int(run_id) == 11:
            raise RuntimeError("Run 11 is already in a terminal state. More text")
        cancelled.append(int(run_id))
    monkeypatch.setattr(main.jobs, "cancel", cancel)
    body = api.post(f"/api/ingestion-jobs/{sid}/stop").json()
    assert cancelled == [12] and [c["run_id"] for c in body["cancelled"]] == [12]
    assert body["errors"] == ["run 11: Run 11 is already in a terminal state"]
    assert closes == []                                           # 41's run wasn't cancelled: its record stays running


def test_cancel_run_with_nothing_going_is_refused(api, monkeypatch):  # noqa: F811
    sid, cancelled, _ = _stop_setup(api, monkeypatch)
    r = api.post(f"/api/ingestion-jobs/{sid}/stop")
    assert r.status_code == 409 and "no run of this job is going" in r.text and cancelled == []


def test_cancel_run_needs_what_run_now_needs(api, monkeypatch):  # noqa: F811
    sid, cancelled, _ = _stop_setup(api, monkeypatch)
    api.active_runs[555] = [_going(11, task_collection="c1")]
    monkeypatch.setattr(main.jobs, "user_job_permission", lambda job_id, user: "CAN_VIEW")
    r = api.post(f"/api/ingestion-jobs/{sid}/stop")
    assert r.status_code == 403 and cancelled == []


def test_speed_counts_loaded_tables_only():
    start = datetime(2026, 10, 29, 14, 0, tzinfo=timezone.utc)
    run = {"ok": 60, "failed": 40, "skipped": 0, "reseeded": 0, "work_started_at": start.isoformat(),
           "work_finished_at": (start + timedelta(minutes=10)).isoformat()}
    assert q.work_speed(run) == 6.0
    assert q.work_speed(dict(run, ok=0)) is None                   # a run whose every table failed has no speed
    progress = {"ok_last_5m": 50, "tables_failed": 30, "rows_last_5m": 0, "work_started_at": (start - timedelta(minutes=10)).isoformat()}
    assert q.live_speed(progress, start)["tables_per_min"] == 10.0


def test_run_now_opens_again_once_the_app_has_closed_a_stale_run_and_stays_refused_while_one_is_going(api, monkeypatch):  # noqa: F811
    sid, started = _run_now_setup(api, monkeypatch, BODY)
    monkeypatch.setattr(main, "_running", _real_running)
    records = {7: dict(_open(7, "c1", "650"), status="running")}
    states = {"650": None}
    real_query, real_run = api.store.query, api.store.run

    def query(sql, params=()):
        if sql == q.open_runs_sql():
            return [dict(r) for r in records.values() if r["status"] == "running"]
        if sql == q.running_progress_sql():
            return [dict(r) for r in records.values() if r["status"] == "running"]
        return real_query(sql, params)

    def run(sql, params):
        if sql == q.close_cancelled_run_sql():
            records[params[2]]["status"] = "cancelled"
            return [(1, 0)]
        return real_run(sql, params)
    monkeypatch.setattr(main.db, "query", query)
    monkeypatch.setattr(api.store, "run", run)
    monkeypatch.setattr(main.jobs, "run_ended", lambda run_id: states[str(run_id)])
    main._forget_shared_reads()
    r = api.post(f"/api/ingestion-jobs/{sid}/run")
    assert r.status_code == 409 and "already running" in r.text and started == []
    states["650"] = {"state": "TERMINATED", "result": "CANCELED", "message": None, "ended": None}
    main._forget_shared_reads()
    r = api.post(f"/api/ingestion-jobs/{sid}/run")
    assert r.status_code == 200, r.text
    assert records[7]["status"] == "cancelled" and len(started) == 2
    main._forget_shared_reads()

