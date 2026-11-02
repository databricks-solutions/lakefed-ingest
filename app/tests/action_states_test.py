"""What an action shows while Databricks has not confirmed its work (action_states): a run it started is pending until the
run has ended, then succeeded or failed with Databricks' message, and timeout once it has gone on past its limit; a
write's sync failures are named. Plain functions, no database."""
from datetime import datetime, timedelta, timezone

import action_states as ast

UTC = timezone.utc
NOW = datetime(2026, 10, 29, 16, 0, tzinfo=UTC)


def _run(state, result=None, started=None, message=None):
    return {"run_id": 42, "state": state, "result": result, "message": message, "url": "https://h/run/42",
            "started": started}


def test_a_run_not_recorded_yet_is_pending():
    assert ast.run_outcome(None, NOW) == {"state": ast.PENDING, "reason": None, "url": None, "run_id": None}


def test_a_run_still_going_is_pending_with_its_page():
    outcome = ast.run_outcome(_run("RUNNING", started=NOW - timedelta(minutes=3)), NOW, limit_s=3600)
    assert (outcome["state"], outcome["reason"], outcome["url"]) == (ast.PENDING, None, "https://h/run/42")


def test_a_run_that_ended_with_success_succeeds():
    assert ast.run_outcome(_run("TERMINATED", "SUCCESS"), NOW)["state"] == ast.SUCCEEDED


def test_a_run_that_failed_fails_with_databricks_message():
    outcome = ast.run_outcome(_run("TERMINATED", "FAILED", message="table x would not drop"), NOW)
    assert (outcome["state"], outcome["reason"]) == (ast.FAILED, "table x would not drop")


def test_a_run_that_ended_without_a_message_names_how_it_ended():
    assert ast.run_outcome(_run("INTERNAL_ERROR", None), NOW)["reason"] == "the run ended INTERNAL_ERROR"
    assert ast.run_outcome(_run("TERMINATED", "CANCELED"), NOW)["reason"] == "the run ended CANCELED"


def test_a_run_databricks_no_longer_has_fails_with_the_reason():
    outcome = ast.run_outcome(_run("NOT_FOUND"), NOW)
    assert (outcome["state"], outcome["reason"]) == (ast.FAILED, "Databricks has no record of the run")


def test_a_run_going_past_its_limit_is_a_timeout_that_is_not_done():
    outcome = ast.run_outcome(_run("TERMINATING", started=NOW - timedelta(minutes=31)), NOW, limit_s=30 * 60)
    assert outcome["state"] == ast.TIMEOUT
    assert outcome["reason"] == "Databricks has not reported it ended after 30 minutes: it is still listed as terminating"


def test_a_run_within_its_limit_is_still_pending_not_a_timeout():
    outcome = ast.run_outcome(_run("RUNNING", started=NOW - timedelta(minutes=29)), NOW, limit_s=30 * 60)
    assert outcome["state"] == ast.PENDING


def test_a_run_with_no_start_time_is_never_a_timeout():
    assert ast.run_outcome(_run("RUNNING"), NOW, limit_s=60)["state"] == ast.PENDING


def test_a_start_time_as_text_counts_from_its_moment():
    started = (NOW - timedelta(hours=2)).isoformat()
    assert ast.run_outcome(_run("PENDING", started=started), NOW, limit_s=3600)["state"] == ast.TIMEOUT


def test_no_sync_failures_when_every_job_synced():
    results = [{"task_collection": "a", "action": "updated", "detail": ""},
               {"task_collection": "b", "action": "unchanged", "detail": ""}]
    assert ast.sync_failures(results) == []
    assert ast.sync_failures(None) == []


def test_a_failed_member_sync_is_named_with_its_collection_and_why():
    results = [{"task_collection": "sales", "action": "error", "detail": "job is locked"},
               {"task_collection": "hr", "action": "created", "detail": ""}]
    assert ast.sync_failures(results) == ["sales: job is locked"]


def test_a_sync_failure_with_no_collection_says_what_it_was():
    results = [{"task_collection": "", "action": "error", "detail": "listing the schedule's jobs: denied"}]
    assert ast.sync_failures(results) == ["listing the schedule's jobs: denied"]


def test_a_sync_failure_with_no_detail_still_says_it_failed():
    assert ast.sync_failures([{"task_collection": "sales", "action": "error", "detail": ""}]) == [
        "sales: its Databricks job was not synced"]
