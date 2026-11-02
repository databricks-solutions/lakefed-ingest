"""Where Run now and Cancel run stand for an ingestion job, from its active Databricks runs (ingestion_jobs.run_phase):
starting until a run is really going, running, cancelling until every run has ended, and the timeout that says
plainly when Databricks has not confirmed. Plain functions, no database."""
from datetime import datetime, timedelta, timezone

import ingestion_jobs as ij

UTC = timezone.utc
NOW = datetime(2026, 10, 29, 16, 0, tzinfo=UTC)


def test_no_active_run_is_idle():
    assert ij.run_phase([], NOW) == {"phase": ij.IDLE, "since": None, "timed_out": False, "run_id": None,
                                     "run_url": None}


def test_a_pending_or_queued_run_is_starting_until_it_really_runs():
    for life_cycle in ("PENDING", "QUEUED", "BLOCKED"):
        phase = ij.run_phase([_run(7, life_cycle, started=NOW - timedelta(minutes=2))], NOW)
        assert (phase["phase"], phase["timed_out"], phase["run_id"]) == (ij.STARTING, False, 7)


def test_a_run_that_is_running_is_running_with_its_page():
    phase = ij.run_phase([_run(7, "PENDING", started=NOW - timedelta(minutes=3)),
                          _run(8, "RUNNING", started=NOW - timedelta(minutes=1), url="https://h/run/8")], NOW)
    assert (phase["phase"], phase["run_id"], phase["run_url"], phase["timed_out"]) == (
        ij.RUNNING, 8, "https://h/run/8", False)


def test_a_run_still_starting_after_the_limit_says_databricks_has_not_confirmed_it():
    phase = ij.run_phase([_run(7, "PENDING", started=NOW - timedelta(minutes=16))], NOW)
    assert (phase["phase"], phase["timed_out"]) == (ij.STARTING, True)


def test_a_cancel_is_cancelling_until_every_run_has_ended():
    seen = NOW - timedelta(seconds=40)
    phase = ij.run_phase([_run(7, "TERMINATING", started=NOW - timedelta(hours=1), seen=seen),
                          _run(8, "RUNNING", started=NOW - timedelta(hours=1))], NOW)
    assert (phase["phase"], phase["since"], phase["timed_out"]) == (ij.CANCELLING, seen.isoformat(), False)


def test_a_cancel_that_databricks_has_not_confirmed_in_time_times_out_and_says_so():
    seen = NOW - timedelta(minutes=11)
    phase = ij.run_phase([_run(7, "TERMINATING", started=NOW - timedelta(hours=1), seen=seen)], NOW)
    assert (phase["phase"], phase["timed_out"]) == (ij.CANCELLING, True)


def test_a_run_that_is_running_never_times_out():
    phase = ij.run_phase([_run(7, "RUNNING", started=NOW - timedelta(days=2))], NOW)
    assert (phase["phase"], phase["timed_out"]) == (ij.RUNNING, False)


def test_a_cancelled_run_with_its_time_unknown_is_cancelling_without_a_timeout():
    phase = ij.run_phase([_run(7, "TERMINATING", started=None, seen=None)], NOW)
    assert (phase["phase"], phase["since"], phase["timed_out"]) == (ij.CANCELLING, None, False)


def test_naive_times_from_the_table_are_read_as_utc():
    phase = ij.run_phase([_run(7, "TERMINATING", started=None, seen="2026-10-29T15:40:00")], NOW)
    assert (phase["since"], phase["timed_out"]) == ("2026-10-29T15:40:00+00:00", True)


def _run(run_id, life_cycle, started=None, seen=None, url=None):
    """A run as the poller's view shows it, with its start and when its state last changed."""
    return {"run_id": run_id, "state": life_cycle, "started": started.isoformat() if started else None,
            "seen": seen.isoformat() if isinstance(seen, datetime) else seen, "url": url}
