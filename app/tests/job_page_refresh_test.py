"""The job page's two fixes from the 16:51 run of a 1,002-table job (server side; the page's own refresh is in
tests/js/job_page_refresh.test.mjs):

- Step 1 of Running now said "Checking the databases" for 10.5 minutes after the check had finished, because it took its
  state from the check's Databricks task, which stays Running while it waits for the group runs it started.
- The tree's servers and the first page of Target tables are read once per job for everyone watching.

Plain function tests: a fake Lakebase and a fake Jobs API, no database."""
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import ingestion_jobs as ij  # noqa: E402
import main  # noqa: E402
import queries as q  # noqa: E402
from schedules_api_test import BODY, _run_now_setup, api  # noqa: E402,F401  (api is the fixture)

NOW = datetime(2026, 11, 11, 17, 3, tzinfo=timezone.utc)
CHECK_STARTED = "2026-11-11T16:52:12+00:00"
CHECK_FINISHED = datetime(2026, 11, 11, 16, 52, 19, tzinfo=timezone.utc)


def _run(run_id, state, started, result=None, **parameters):
    return {"run_id": run_id, "state": state, "result": result, "started": started, "ended": None,
            "url": f"https://h/runs/{run_id}", "parameters": parameters}


def _record(records=1, running=0, failed=0, finished_at=CHECK_FINISHED):
    return {"records": records, "running": running, "failed": failed, "finished_at": finished_at}


def _live(check_record, *, group_runs=1, check_state="RUNNING"):
    """The 16:51 run: the check's task Running (it waits for the group run), one group run going."""
    check = _run(5, check_state, CHECK_STARTED, schedule_id="7")
    ingest = [_run(11, "RUNNING", "2026-11-11T16:52:20+00:00", task_collection="c1", work_id="w1")][:group_runs]
    return ij.live_view([_run(4, "RUNNING", "2026-11-11T16:51:00+00:00")], [check], ingest, ["c1"],
                        [{"task_collection": "c1", "databases": 1}], 0, [], NOW, check_record)


def test_a_check_that_found_its_work_and_started_its_group_run_is_finished_while_its_task_waits():
    view = _live(_record())
    assert view["check"]["status"] == {"label": "Succeeded", "tone": "ok"}
    assert view["check"]["ended"] == CHECK_FINISHED.isoformat()          # its own finish time, not the task's
    assert view["found"] == {"groups_with_work": 1, "databases_with_work": 1, "unchanged_databases": 0, "groups": 1, "started": 1}
    assert [g["active"] for g in view["group_runs"]] == [True]            # the waiting belongs to step 2
    assert view["active"] is True                                         # the run as a whole stays going


@pytest.mark.parametrize("record, group_runs", [
    (None, 1),                          # no record read yet
    (_record(records=0), 1),            # the check has written no record of its own yet
    (_record(running=1), 1),            # a group's check is still going
    (_record(failed=1), 1),             # a failed check keeps the task's state
    (_record(finished_at=None), 1),
    (_record(), 0),                     # it has not started a group run yet: it is still starting them
])
def test_a_check_still_checking_keeps_its_tasks_state(record, group_runs):
    view = _live(record, group_runs=group_runs)
    assert view["check"]["status"]["label"] == "Running"
    assert view["check"]["ended"] is None


def test_a_group_run_left_from_an_earlier_cycle_does_not_finish_the_check():
    check = _run(5, "RUNNING", CHECK_STARTED, schedule_id="7")
    earlier = _run(3, "RUNNING", "2026-11-11T16:30:00+00:00", task_collection="c1", work_id="old")
    view = ij.live_view([_run(4, "RUNNING", "2026-11-11T16:51:00+00:00")], [check], [earlier], ["c1"], [], 0, [], NOW,
                        _record())
    assert view["check"]["status"]["label"] == "Running"


def test_a_check_whose_task_has_ended_shows_the_tasks_own_state_and_time():
    check = _run(5, "TERMINATED", CHECK_STARTED, "FAILED", schedule_id="7")
    check["ended"] = "2026-11-11T16:55:00+00:00"
    view = ij.live_view([_run(4, "RUNNING", "2026-11-11T16:51:00+00:00")], [check], [_run(11, "RUNNING", "2026-11-11T16:52:20+00:00",
                        task_collection="c1")], ["c1"], [], 0, [], NOW, _record())
    assert view["check"]["status"]["label"] == "Failed" and view["check"]["ended"] == "2026-11-11T16:55:00+00:00"


def test_the_live_endpoint_reads_the_checks_own_record_while_its_task_runs(api, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main.jobs, "CHECK_JOB_ID", "777")
    sid, _ = _run_now_setup(api, monkeypatch, dict(BODY, job_variant="check"))
    wrapper = int(next(m["job_id"] for m in api.store.members.values()))
    api.active_runs[wrapper] = [_run(4, "RUNNING", "2026-11-11T16:51:00+00:00")]
    api.active_runs[777] = [_run(5, "RUNNING", CHECK_STARTED, schedule_id=str(sid))]
    api.active_runs[555] = [_run(11, "RUNNING", "2026-11-11T16:52:20+00:00", task_collection="c1", work_id="w1")]
    real, asked = main.db.query, []

    def query(sql, params=()):
        if sql == q.check_work_found_sql():
            return [{"task_collection": "c1", "databases": 1}]
        if sql == q.database_checks_since_sql():
            return [{"databases": 0}]
        if sql == q.check_records_since_sql():
            asked.append(params)
            return [_record()]
        return real(sql, params)
    monkeypatch.setattr(main.db, "query", query)
    view = api.get(f"/api/ingestion-jobs/{sid}/live").json()
    assert asked == [(["c1", "c2"], CHECK_STARTED)]
    assert view["check"]["status"]["label"] == "Succeeded" and view["check"]["ended"] == CHECK_FINISHED.isoformat()


def test_the_checks_record_query_counts_only_the_check_records_of_the_groups_since_it_started():
    sql = q.check_records_since_sql()
    assert "ingest_run" in sql and "= any(%s)" in sql and "'check-'" in sql and "started_at >= %s" in sql
    assert "status not in ('running', 'completed')" in sql


class _Reads:
    """Counts the tree and target reads the endpoints make."""

    def __init__(self, monkeypatch):
        self.calls = []
        monkeypatch.setattr(main, "MONITOR_CACHE_S", 15)
        monkeypatch.setattr(main, "_job_groups", lambda job_id: ["c1"])
        monkeypatch.setattr(main, "_job_jdbc", lambda job_id: None)
        monkeypatch.setattr(main.db, "workspace", lambda: type("W", (), {"config": type("C", (), {"host": "https://h"})()})())
        rows = {q.job_servers_sql(): "servers", q.job_databases_sql(): "databases", q.job_targets_sql(): "targets"}

        def query(sql, params=()):
            self.calls.append(rows[sql])
            return []
        monkeypatch.setattr(main.db, "query", query)

    def count(self, kind):
        return self.calls.count(kind)


@pytest.fixture
def reads(monkeypatch):
    return _Reads(monkeypatch)


def test_two_viewers_within_15_seconds_cause_one_tree_read(reads):
    first = main.ingestion_job_tree(5, limit=200)
    second = main.ingestion_job_tree(5, limit=200)
    assert first == second and reads.count("servers") == 1


def test_the_first_page_of_target_tables_is_one_read_per_job(reads):
    main.ingestion_job_targets(5)
    main.ingestion_job_targets(5)
    main.ingestion_job_targets(6)
    assert reads.count("targets") == 2                                   # jobs 5 and 6, once each


def test_a_level_a_viewer_opens_or_searches_is_read_for_that_viewer(reads):
    main.ingestion_job_tree(5, server="sql_01", limit=25)
    main.ingestion_job_tree(5, server="sql_01", limit=25)
    main.ingestion_job_tree(5, search="sales", limit=200)
    main.ingestion_job_targets(5, search="orders")
    main.ingestion_job_targets(5, offset=25)
    assert (reads.count("databases"), reads.count("servers"), reads.count("targets")) == (2, 1, 2)


def test_a_committed_write_drops_the_shared_tree_read(reads):
    main.ingestion_job_tree(5, limit=200)
    main._forget_shared_reads()
    main.ingestion_job_tree(5, limit=200)
    assert reads.count("servers") == 2


def test_the_first_live_read_that_finds_the_run_over_drops_the_shared_reads_once(reads):
    main._run_was_going.clear()
    main.ingestion_job_tree(5, limit=200)
    main._forget_shared_reads_when_run_ends(5, True)
    main.ingestion_job_tree(5, limit=200)
    assert reads.count("servers") == 1                                    # a run going keeps them shared
    main._forget_shared_reads_when_run_ends(5, False)
    main.ingestion_job_tree(5, limit=200)
    main._forget_shared_reads_when_run_ends(5, False)
    main.ingestion_job_tree(5, limit=200)
    assert reads.count("servers") == 2                                    # dropped at the end, and only then
