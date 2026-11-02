"""A deleted ingestion job's Delete as its state: kept until the purge starts, deleting while its delete job runs,
failed
with the reason, or gone (ingestion_jobs.delete_state), the days left (days_left, expires_at, kept_text), and the Status
cell it shows (ingestion_jobs.delete_status). Plain functions, no database."""
from datetime import datetime, timedelta, timezone

import ingestion_jobs as ij

UTC = timezone.utc
DELETED_AT = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
RETENTION_DAYS = 7
JUST_NOW = DELETED_AT + timedelta(seconds=30)
LATER = DELETED_AT + timedelta(hours=1)


def test_a_deleted_job_with_no_delete_run_is_kept_until_its_purge():
    state = ij.delete_state(_job(), tables_left=True, run=None, now=LATER, retention_days=RETENTION_DAYS)
    assert (state["state"], state["reason"], state["run"], state["drop_targets"]) == (ij.KEPT, None, None, True)
    assert state["expires_at"] == "2026-11-05T12:00:00+00:00"


def test_the_days_left_count_down_to_the_purge_and_round_up_a_part_day():
    assert _days_left(DELETED_AT + timedelta(days=1)) == 6
    assert _days_left(DELETED_AT + timedelta(days=6, hours=23)) == 1
    assert _days_left(DELETED_AT + timedelta(days=7)) == 0
    assert _days_left(DELETED_AT + timedelta(days=9)) == 0


def test_the_days_left_follow_the_retention_passed_in():
    state = ij.delete_state(_job(), tables_left=True, run=None, now=LATER, retention_days=3)
    assert (state["expires_at"], state["days_left"]) == ("2026-11-01T12:00:00+00:00", 3)


def test_a_naive_deleted_at_is_read_as_utc():
    job = _job(deleted_at="2026-10-29T12:00:00")
    assert ij.expires_at(job["deleted_at"], RETENTION_DAYS) == DELETED_AT + timedelta(days=RETENTION_DAYS)


def test_a_job_whose_delete_job_is_running_is_deleting_even_with_its_tables_already_gone():
    run = {"run_id": 9, "state": "RUNNING", "result": None, "message": None, "url": "u"}
    state = ij.delete_state(_job(), tables_left=False, run=run, now=LATER, retention_days=RETENTION_DAYS)
    assert (state["state"], state["reason"], state["run"]) == (ij.DELETING, None, run)


def test_a_queued_or_pending_delete_job_is_deleting():
    for life_cycle in ("QUEUED", "PENDING", "TERMINATING"):
        run = {"run_id": 9, "state": life_cycle, "result": None, "message": None}
        state = ij.delete_state(_job(), tables_left=True, run=run, now=LATER, retention_days=RETENTION_DAYS)
        assert state["state"] == ij.DELETING


def test_a_succeeded_delete_with_no_tables_left_is_gone():
    run = {"run_id": 9, "state": "TERMINATED", "result": "SUCCESS", "message": None}
    state = ij.delete_state(_job(), tables_left=False, run=run, now=LATER, retention_days=RETENTION_DAYS)
    assert (state["state"], state["reason"]) == (ij.DELETE_GONE, None)


def test_a_succeeded_delete_that_left_tables_is_failed_with_the_reason_says_so():
    run = {"run_id": 9, "state": "TERMINATED", "result": "SUCCESS", "message": None}
    state = ij.delete_state(_job(), tables_left=True, run=run, now=LATER, retention_days=RETENTION_DAYS)
    assert (state["state"], state["reason"]) == (
        ij.DELETE_FAILED, "the delete job finished, but tables are still there")


def test_a_failed_delete_shows_the_run_message_whether_or_not_tables_remain():
    run = {"run_id": 9, "state": "TERMINATED", "result": "FAILED", "message": "table x would not drop"}
    for tables_left in (True, False):
        state = ij.delete_state(_job(), tables_left=tables_left, run=run, now=LATER, retention_days=RETENTION_DAYS)
        assert (state["state"], state["reason"]) == (ij.DELETE_FAILED, "table x would not drop")


def test_a_failed_delete_without_a_message_names_its_result():
    run = {"run_id": 9, "state": "INTERNAL_ERROR", "result": None, "message": None}
    state = ij.delete_state(_job(), tables_left=True, run=run, now=LATER, retention_days=RETENTION_DAYS)
    assert state["reason"] == "the delete job ended INTERNAL_ERROR"


def test_a_start_that_failed_is_failed_with_its_reason_and_has_no_run():
    job = _job(delete_start_error="the delete job didn't start: 403 forbidden")
    state = ij.delete_state(job, tables_left=True, run=None, now=JUST_NOW, retention_days=RETENTION_DAYS)
    assert (state["state"], state["reason"], state["run"]) == (
        ij.DELETE_FAILED, "the delete job didn't start: 403 forbidden", None)


def test_a_recorded_start_error_wins_over_a_run_that_came_after_it():
    job = _job(delete_start_error="the delete job didn't start: 403 forbidden")
    run = {"run_id": 9, "state": "RUNNING", "result": None, "message": None}
    assert ij.delete_state(job, tables_left=True, run=run, now=LATER, retention_days=RETENTION_DAYS)["state"] == \
        ij.DELETE_FAILED


def test_the_kept_wording_counts_days_and_says_when_it_is_due():
    assert ij.kept_text(0) == "due for purge"
    assert ij.kept_text(1) == "1 day left"
    assert ij.kept_text(3) == "3 days left"


def test_the_status_cell_of_a_kept_deleting_or_failed_delete():
    kept = {"state": ij.KEPT, "reason": None, "days_left": 4}
    assert ij.delete_status(kept) == {"label": "Deleted", "tone": "neutral", "detail": ["4 days left"], "cause": None}
    running = {"state": ij.DELETING, "reason": None, "run": {"run_id": 9}}
    assert ij.delete_status(running) == {"label": "Deleting", "tone": "run", "detail": ["its delete job is running"],
                                         "cause": None}
    failed = {"state": ij.DELETE_FAILED, "reason": "403 forbidden", "run": None}
    assert ij.delete_status(failed) == {"label": "Delete failed", "tone": "bad", "detail": ["403 forbidden"],
                                        "cause": None}
    assert ij.delete_status({"state": ij.DELETE_FAILED, "reason": None})["detail"] == []


def _days_left(now):
    return ij.delete_state(_job(), True, None, now, RETENTION_DAYS)["days_left"]


def _job(**options):
    """A deleted schedule row, with the options a Delete records."""
    deleted_at = options.pop("deleted_at", DELETED_AT.isoformat())
    return {"id": 1, "name": "East hourly", "deleted_at": deleted_at,
            "options": {"delete_drop_targets": True, **options}}
