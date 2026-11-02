"""Running now on the job page: the job run as its Databricks runs are, from the Jobs API (which sees a run from its
start, while Lakebase records a group run only once its cluster is up), joined with what the check found."""
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import ingestion_jobs as ij  # noqa: E402
import main  # noqa: E402
import queries as q  # noqa: E402
from schedules_api_test import BODY, _run_now_setup, api  # noqa: E402,F401  (api is the fixture)

NOW = datetime(2026, 10, 29, 14, 12, tzinfo=timezone.utc)


def _run(run_id, state, started, result=None, **parameters):
    return {"run_id": run_id, "state": state, "result": result, "started": started, "ended": None,
            "url": f"https://h/runs/{run_id}", "parameters": parameters}


def test_nothing_going_is_no_live_run():
    view = ij.live_view([], [_run(1, "TERMINATED", "2026-10-29T13:00:00+00:00", "SUCCESS", schedule_id="7")], [], ["c1"],
                        [], 0, [], NOW)
    assert view["active"] is False and view["group_runs"] == []


def test_a_check_run_is_the_jobs_by_its_schedule_id_or_its_group_list():
    assert ij.check_run_of(_run(1, "RUNNING", "t", schedule_id="7"), 7, ["c1"])
    assert not ij.check_run_of(_run(1, "RUNNING", "t", schedule_id="8"), 7, ["c1"])
    assert ij.check_run_of(_run(1, "RUNNING", "t", task_collections="c1,c2"), 7, ["c1", "c2", "c3"])
    assert not ij.check_run_of(_run(1, "RUNNING", "t", task_collections=""), 7, ["c1"])   # every collection: no one job's


def test_a_group_run_whose_cluster_is_starting_shows_as_starting_and_one_running_shows_its_progress():
    ingest = [_run(11, "PENDING", "2026-10-29T14:10:31+00:00", task_collection="c1", work_id="w1"),
              _run(12, "RUNNING", "2026-10-29T14:10:32+00:00", task_collection="c2", work_id="w1"),
              _run(13, "RUNNING", "2026-10-29T14:10:33+00:00", task_collection="other_jobs_group"),
              _run(9, "TERMINATED", "2026-10-29T12:00:00+00:00", "SUCCESS", task_collection="c1")]   # an earlier run
    running = [{"job_run_id": "12", "run_id": 501, "tables_checked": 4000, "enabled_tables": 12500, "tables_failed": 2}]
    check = _run(5, "TERMINATED", "2026-10-29T14:09:50+00:00", "SUCCESS", schedule_id="7")
    view = ij.live_view([_run(4, "RUNNING", "2026-10-29T14:09:42+00:00")], [check], ingest, ["c1", "c2"],
                        [{"task_collection": "c1", "databases": 12}, {"task_collection": "c2", "databases": 13}], 0, running, NOW)
    assert view["active"] and [w["run_id"] for w in view["wrappers"]] == [4]
    assert view["check"]["status"]["label"] == "Succeeded"
    assert view["found"] == {"groups_with_work": 2, "databases_with_work": 25, "unchanged_databases": 0, "groups": 2, "started": 2}
    by_group = {g["task_collection"]: g for g in view["group_runs"]}
    assert set(by_group) == {"c1", "c2"}                       # the other job's group and the earlier run are not shown
    assert by_group["c1"]["status"]["label"] == "Starting" and by_group["c1"]["progress"] is None
    assert by_group["c2"]["progress"] == {"done": 4000, "planned": 12500, "failed": 2, "group_run_id": 501}


def test_the_live_endpoint_reads_the_wrapper_the_check_and_the_ingestion_job(api, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main.jobs, "CHECK_JOB_ID", "777")
    sid, _ = _run_now_setup(api, monkeypatch, dict(BODY, job_variant="check"))
    wrapper = int(next(m["job_id"] for m in api.store.members.values()))
    api.active_runs[wrapper] = [_run(4, "RUNNING", "2026-10-29T14:09:42+00:00")]
    api.active_runs[777] = [_run(5, "RUNNING", "2026-10-29T14:09:50+00:00", schedule_id=str(sid))]
    api.active_runs[555] = [_run(11, "PENDING", "2026-10-29T14:10:31+00:00", task_collection="c1", work_id="5")]
    real = main.db.query
    asked = []

    def query(sql, params=()):
        if sql == q.check_work_found_sql():
            asked.append(params)
            return [{"task_collection": "c1", "databases": 12}]
        if sql == q.database_checks_since_sql():
            return [{"databases": 30}]
        return real(sql, params)
    monkeypatch.setattr(main.db, "query", query)
    view = api.get(f"/api/ingestion-jobs/{sid}/live").json()
    assert view["active"] and view["check"]["run_id"] == 5 and asked == [(["5"],)]
    assert view["found"]["databases_with_work"] == 12 and view["found"]["unchanged_databases"] == 30
    assert [g["task_collection"] for g in view["group_runs"]] == ["c1"]
