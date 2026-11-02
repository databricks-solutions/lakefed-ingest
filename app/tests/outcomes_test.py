"""A group run's outcome, speed and time left (queries.run_outcome, work_speed, live_speed, time_left)."""
import json
from datetime import datetime, timedelta, timezone

import queries as q

NOW = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)


def test_outcomes_name_what_the_engine_recorded():
    assert q.run_outcome(dict(status="completed", failed=0))["label"] == "Succeeded"
    with_failures = q.run_outcome(dict(status="failed", failed=3, detail=json.dumps({"batch_rows": 0})))
    assert (with_failures["label"], with_failures["tone"], with_failures["note"]) == ("Succeeded with failures", "warn",
                                                                                       "3 tables failed")
    ended = q.run_outcome(dict(status="failed", failed=0, detail=json.dumps({"closed_by_run": 41, "reason": "x"})))
    assert (ended["label"], ended["tone"], ended["note"]) == ("Ended without finishing", "bad", "closed by run 41")
    assert q.run_outcome(dict(status="failed", failed=0, detail="not json"))["label"] == "Failed"
    assert q.run_outcome(dict(status="skipped"))["label"] == "Skipped: already running"
    assert q.run_outcome(dict(status="running"), dict(tables_ok=0, tables_failed=0, tables_running=0,
                                                      tables_checked=0))["label"] == "Detecting changes"
    assert q.run_outcome(dict(status="running"), dict(tables_ok=1, tables_checked=1))["label"] == "Loading changed tables"


def test_a_run_another_run_closed_has_no_duration():
    run = dict(status="failed", failed=0, duration_s=86000, detail=json.dumps({"closed_by_run": 41}))
    assert q.with_outcome(run)["duration_s"] is None


def test_speed_is_over_the_work_span_only():
    start = NOW - timedelta(minutes=10)
    assert q.work_speed(dict(loaded=50, work_started_at=start, work_finished_at=start + timedelta(minutes=5))) == 10.0
    assert q.work_speed(dict(loaded=0, work_started_at=None, work_finished_at=None)) is None


def test_live_speed_waits_for_three_minutes_of_loading_and_counts_only_time_spent_loading():
    early = dict(work_started_at=NOW - timedelta(minutes=2), ok_last_5m=40, rows_last_5m=4000)
    assert q.live_speed(early, NOW) is None
    later = dict(work_started_at=NOW - timedelta(minutes=4), ok_last_5m=40, rows_last_5m=4000)
    assert q.live_speed(later, NOW) == {"tables_per_min": 10.0, "rows_per_min": 1000.0}
    settled = dict(later, work_started_at=NOW - timedelta(minutes=30))
    assert q.live_speed(settled, NOW) == {"tables_per_min": 8.0, "rows_per_min": 800.0}


def test_time_left_from_the_rate_while_loading_else_from_the_typical_run():
    loading = dict(enabled_tables=1000, tables_checked=500, tables_running=50, tables_failed=50, ok_last_5m=100,
                   work_started_at=NOW - timedelta(minutes=10), elapsed_s=900, p50_s=1200)
    assert q.time_left(loading, NOW) == {"seconds": 1200, "basis": "rate"}
    detecting = dict(enabled_tables=1000, tables_checked=0, tables_running=0, tables_failed=0, ok_last_5m=0,
                     work_started_at=None, elapsed_s=300, p50_s=1200)
    assert q.time_left(detecting, NOW) == {"seconds": 900, "basis": "typical"}
    assert q.time_left(dict(detecting, p50_s=None), NOW) is None
    assert q.time_left(dict(detecting, elapsed_s=5000), NOW) == {"seconds": 0, "basis": "typical"}


def test_speed_and_time_left_read_timestamps_as_the_database_layer_sends_them():
    start = (NOW - timedelta(minutes=10)).isoformat()
    finish = (NOW - timedelta(minutes=5)).isoformat()
    assert q.work_speed(dict(loaded=50, work_started_at=start, work_finished_at=finish)) == 10.0
    progress = dict(work_started_at=start, ok_last_5m=40, rows_last_5m=4000, enabled_tables=100, tables_checked=20)
    assert q.live_speed(progress, NOW) == {"tables_per_min": 8.0, "rows_per_min": 800.0}
    assert q.time_left(progress, NOW) == {"seconds": 600, "basis": "rate"}


def test_a_completed_record_with_failed_tables_never_reads_as_a_clean_completion():
    assert q.run_outcome(dict(status="completed", failed=2))["label"] == "Succeeded with failures"


def test_finished_speed_counts_only_tables_actually_loaded():
    start = NOW - timedelta(minutes=10)
    run = dict(ok=100, skipped=60, reseeded=10, loaded=90, work_started_at=start, work_finished_at=start + timedelta(minutes=3))
    assert q.work_speed(run) == 10.0


def test_reseeds_already_queued_are_not_left_to_go():
    progress = dict(enabled_tables=100, tables_checked=90, tables_skipped=10, tables_running=0, tables_failed=0,
                    ok_last_5m=5, work_started_at=NOW - timedelta(minutes=10), elapsed_s=600, p50_s=900)
    assert q.time_left(progress, NOW) == {"seconds": 0, "basis": "rate"}


def _member(state, run=None, fire=NOW, group="g"):
    return dict(task_collection=group, state=state, run=run, fire_at=fire, extra=[])


def test_a_job_run_is_the_worst_of_its_group_runs_and_running_while_a_member_waits():
    done = _member("run", dict(status="completed", failed=0))
    with_failures = _member("run", dict(status="failed", failed=2))
    closed = _member("run", dict(status="failed", failed=0, detail=json.dumps({"closed_by_run": 5})))
    assert q.job_run_outcome([done, with_failures])["label"] == "Succeeded with failures"
    assert q.job_run_outcome([done, with_failures, closed])["label"] == "Ended without finishing"
    assert q.job_run_outcome([done, _member("waiting")])["label"] == "Running"
    assert q.job_run_outcome([done, _member("run", dict(status="running"))])["label"] == "Running"
    counted = q.job_run_outcome([done, _member("skipped"), _member("run", dict(status="skipped")), _member("no run")])
    assert (counted["label"], counted["note"]) == ("Succeeded", "1 of 4 group runs, 2 skipped, 1 not run")
    assert q.job_run_outcome([_member("no run"), _member("no run")])["label"] == "Not run"


def test_a_job_runs_expected_end_is_its_latest_members():
    running = _member("run", dict(status="running", run_id=7), group="a")
    waiting = _member("waiting", fire=NOW + timedelta(minutes=30), group="b")
    progress = {7: dict(enabled_tables=100, tables_checked=50, ok_last_5m=50, work_started_at=NOW - timedelta(minutes=10),
                        elapsed_s=700, p50_s=900)}
    assert q.job_run_expected_end([running, waiting], progress, {"b": 600}, NOW) == NOW + timedelta(minutes=40)
    assert q.job_run_expected_end([running], progress, {}, NOW) == NOW + timedelta(minutes=5)
    assert q.job_run_expected_end([_member("run", dict(status="completed"))], {}, {}, NOW) is None


def test_a_running_group_run_past_its_typical_time_gives_no_expected_end():
    running = _member("run", dict(status="running", run_id=7), group="a")
    overdue = {7: dict(enabled_tables=100, tables_checked=50, ok_last_5m=0, work_started_at=None, elapsed_s=5000, p50_s=900)}
    assert q.job_run_expected_end([running], overdue, {}, NOW) is None


def test_a_member_starting_keeps_the_job_run_running_and_ends_its_typical_run_from_now():
    done = _member("run", dict(status="completed", failed=0))
    starting = _member("starting", fire=NOW - timedelta(minutes=5), group="b")
    outcome = q.job_run_outcome([done, starting])
    assert (outcome["label"], outcome["note"]) == ("Running", "1 of 2 group runs, 1 starting")
    assert q.job_run_expected_end([done, starting], {}, {"b": 600}, NOW) == NOW + timedelta(minutes=10)


def test_a_member_its_check_found_unchanged_completes_and_a_check_going_keeps_the_job_run_running():
    done = _member("run", dict(status="completed", failed=0))
    unchanged = _member("unchanged", group="b")
    outcome = q.job_run_outcome([done, unchanged])
    assert (outcome["label"], outcome["note"]) == ("Succeeded", "1 of 2 group runs, 1 unchanged")
    assert q.job_run_outcome([unchanged, _member("unchanged", group="c")])["label"] == "Succeeded"
    assert q.job_run_outcome([done, _member("checking", group="b")])["label"] == "Running"


def test_a_job_run_with_a_cancel_not_closed_yet_reads_cancelling_whatever_its_other_members_do():
    asked = _member("run", dict(status="running", cancelling=True))
    assert q.job_run_outcome([asked])["label"] == "Cancelling"
    assert q.job_run_outcome([_member("run", dict(status="running")), asked])["label"] == "Cancelling"
    assert q.job_run_outcome([_member("waiting"), asked])["label"] == "Cancelling"


def test_a_job_run_whose_cancelled_group_run_is_closed_reads_cancelled():
    closed = _member("run", dict(status="cancelled", job_run_id="8"))
    assert q.job_run_outcome([_member("run", dict(status="running")), closed])["label"] == "Running"
    assert q.job_run_outcome([closed])["label"] == "Cancelled"
