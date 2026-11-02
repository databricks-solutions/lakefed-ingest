"""Ingestion jobs and job runs as the pages show them (ingestion_jobs.py), from fixed reads."""
import json
from datetime import datetime, timedelta, timezone

import ingestion_jobs as ij

UTC = timezone.utc
NOW = datetime(2026, 10, 26, 14, 10, tzinfo=UTC)
ADDED = datetime(2026, 10, 1, tzinfo=UTC)

SCHEDULES = [dict(id=1, name="East hourly", cadence_minutes=60, timezone="UTC", paused=False, updated_at=ADDED),
             dict(id=2, name="Nightly", cadence_minutes=1440, timezone="UTC", paused=True,
                  updated_at=datetime(2026, 10, 26, 9, 2, tzinfo=UTC))]
MEMBERS = [dict(schedule_id=1, task_collection="c1", offset_minutes=0, added_at=ADDED, removed_at=None),
           dict(schedule_id=1, task_collection="c2", offset_minutes=30, added_at=ADDED, removed_at=None),
           dict(schedule_id=2, task_collection="c3", offset_minutes=0, added_at=ADDED, removed_at=None)]


def state(group, tables, current=0, late=0, failing=0, server="srv1", database=None):
    """A database's rollup row: its loaded tables, and whether it is late."""
    return dict(server=server, task_collection=group, src_database=database or f"{group}_db", tables=tables,
                state_loaded=current + late, late_database=late > 0, state_first_load=0, state_failing=failing,
                no_sla_tables=0)


STATES = [state("c1", 10, current=8, failing=2), state("c2", 5, current=5), state("c3", 4, late=4),
          state("c4", 3, current=3, server="srv2")]


def run(run_id, group, started, status="completed", minutes=10, **extra):
    return dict(run_id=run_id, task_collection=group, status=status, started_at=started.isoformat(),
                finished_at=None if status == "running" else (started + timedelta(minutes=minutes)).isoformat(),
                total=extra.pop("total", None if status == "running" else 10), ok=extra.pop("ok", None),
                skipped=extra.pop("skipped", 0), failed=extra.pop("failed", 0), detail=None, **extra)


RUNS = [run(9, "c1", datetime(2026, 10, 26, 13, 0, 20, tzinfo=UTC), status="failed", failed=2, ok=8, skipped=6,
            work_started_at=datetime(2026, 10, 26, 13, 1, tzinfo=UTC), work_finished_at=datetime(2026, 10, 26, 13, 3, tzinfo=UTC)),
        run(8, "c2", datetime(2026, 10, 26, 13, 30, 10, tzinfo=UTC), total=5, ok=5, skipped=5),
        run(10, "c1", datetime(2026, 10, 26, 14, 0, 20, tzinfo=UTC), status="running")]
RUNNING = [dict(run_id=10, task_collection="c1", enabled_tables=10, tables_checked=4, tables_ok=2, tables_running=1,
                tables_failed=0, work_started_at=None, ok_last_5m=0, elapsed_s=580, p50_s=600),
           dict(run_id=11, task_collection="c4", enabled_tables=3, tables_checked=0, tables_ok=0, tables_running=0,
                tables_failed=0, work_started_at=None, ok_last_5m=0, elapsed_s=60, p50_s=None)]
STATS = {"c1": dict(p50_s=600, p95_s=900), "c2": dict(p50_s=300, p95_s=400), "c3": dict(p50_s=None)}


def view():
    return ij.jobs_view(SCHEDULES, MEMBERS, STATES, RUNNING, RUNS, STATS, NOW,
                        causes_by_job={1: [dict(label="Could not connect to the source database", tables=2)]})


def test_jobs_come_failing_first_with_their_status_and_last_job_run():
    v = view()
    east, nightly = v["jobs"]
    assert (east["name"], nightly["name"]) == ("East hourly", "Nightly")
    assert east["status"]["label"] == "Running" and "2 tables failing" in east["status"]["detail"]
    assert east["status"]["cause"] == "Could not connect to the source database"
    last = east["last_job_run"]
    assert (last["slot"], last["outcome"]["label"], last["failed_tables"]) == ("2026-10-26T13:00:00+00:00",
                                                                               "Succeeded with failures", 2)
    assert last["took_s"] == 2390 and last["loaded"] == 2 + 0 and last["ran"] == 2
    assert (nightly["status"]["label"], nightly["status"]["detail"]) == ("Late: 1 database", ["paused"])


def test_a_running_job_run_counts_every_tables_of_the_job_and_its_waiting_groups():
    current = view()["jobs"][0]["running_job_run"]
    assert current["progress"] == {"total": 15, "unchanged": 2, "loaded": 2, "in_flight": 1, "failed": 0, "not_run": 0}
    assert (current["waiting"], current["running_group_runs"]) == (1, 1)
    assert current["expected_end"] == "2026-10-26T14:35:00+00:00"     # c2 starts at 14:30 and typically takes 5 min
    assert current["typical_s"] == 30 * 60 + 300


def test_a_paused_job_with_late_tables_needs_no_attention_and_a_group_in_no_job_is_listed_apart():
    v = view()
    assert [j["name"] for j in v["attention"]] == ["East hourly"]
    assert v["not_in_job"]["groups"] == ["c4"]
    assert [r["run_id"] for r in v["running_not_in_job"]] == [11]
    assert {k: v["totals"][k] for k in ("jobs", "running_jobs", "failing_jobs", "late_jobs")} == \
        {"jobs": 2, "running_jobs": 1, "failing_jobs": 1, "late_jobs": 1}


def test_a_jobs_runs_in_a_row_with_failed_tables():
    assert ij.consecutive_failed([dict(outcome={"label": "Running"}, failed_tables=0),
                                  dict(outcome={"label": "Succeeded with failures"}, failed_tables=3),
                                  dict(outcome={"label": "Succeeded with failures"}, failed_tables=1),
                                  dict(outcome={"label": "Succeeded"}, failed_tables=0),
                                  dict(outcome={"label": "Succeeded with failures"}, failed_tables=1)]) == 2


def test_a_group_run_knows_its_job_runs():
    found = ij.job_runs_of_run(dict(task_collection="c2", started_at="2026-10-26T13:31:00+00:00"), SCHEDULES, MEMBERS)
    assert found == [{"job_id": 1, "job_name": "East hourly", "slot": "2026-10-26T13:00:00+00:00"}]
    assert ij.job_runs_of_run(dict(task_collection="c4", started_at="2026-10-26T13:31:00+00:00"), SCHEDULES, MEMBERS) == []


def test_a_check_knows_only_the_check_schedules_job_run_it_started_in():
    checks = dict(SCHEDULES[0], id=3, name="Checked hourly", job_variant="check")
    members = MEMBERS + [dict(MEMBERS[1], schedule_id=3)]
    check = dict(task_collection="c2", started_at="2026-10-26T13:05:00+00:00", work_id="check-555")
    assert ij.job_runs_of_run(check, SCHEDULES + [checks], members) == [
        {"job_id": 3, "job_name": "Checked hourly", "slot": "2026-10-26T13:00:00+00:00"}]
    # A late check of one check job run, its first check at 13:59:50: the job run that check started, even for a
    # member removed after that trigger.
    late = dict(check, started_at="2026-10-26T14:00:20+00:00")
    removed = MEMBERS + [dict(MEMBERS[1], schedule_id=3, removed_at=datetime(2026, 10, 26, 13, 59, 55, tzinfo=UTC))]
    first = {"c2": "2026-10-26T13:59:50+00:00"}
    assert ij.job_runs_of_run(late, SCHEDULES + [checks], removed, first_started=first) == [
        {"job_id": 3, "job_name": "Checked hourly", "slot": "2026-10-26T13:00:00+00:00"}]
    # One launch over groups of two schedules: each schedule's job run is from its own members' first start, and a
    # default schedule goes by the run's own start.
    launched = dict(task_collection="c2", started_at="2026-10-26T13:31:00+00:00", work_id="900:c2")
    first = {"c1": "2026-10-26T12:59:00+00:00", "c2": "2026-10-26T13:31:00+00:00"}
    assert ij.job_runs_of_run(launched, SCHEDULES + [checks], members, first_started=first) == [
        {"job_id": 1, "job_name": "East hourly", "slot": "2026-10-26T13:00:00+00:00"},
        {"job_id": 3, "job_name": "Checked hourly", "slot": "2026-10-26T13:00:00+00:00"}]


def test_a_group_run_running_longer_than_the_window_still_shows_as_running():
    old_start = NOW - timedelta(days=2)
    running = [dict(RUNNING[0], run_id=20, elapsed_s=2 * 86400, started_at=old_start.isoformat())]
    runs = RUNS[:2] + [run(20, "c1", old_start, status="running")]
    v = ij.jobs_view(SCHEDULES, MEMBERS, STATES, running, runs, STATS, NOW)
    assert [r["run_ids"] for r in v["running_job_runs"]] == [[20], []]     # the 14:00 one waits for c2


def test_a_paused_job_with_failing_tables_shows_failing_and_says_it_is_paused():
    paused_since = datetime(2026, 10, 26, 9, 2, tzinfo=UTC)
    got = ij.status(dict(state_failing=3, late_databases=9, paused=True, updated_at=paused_since), None, None,
                    "Timed out", [])
    assert (got["label"], got["detail"], got["paused_since"], got["cause"]) == ("Failing: 3 tables", ["paused"], paused_since, "Timed out")
    late = ij.status(dict(state_failing=0, late_databases=9, paused=True, updated_at=paused_since), None, None, None,
                     [])
    assert (late["label"], late["detail"]) == ("Late: 9 databases", ["paused"])


def test_a_window_starting_mid_slot_never_cuts_a_job_run_short():
    start = datetime(2026, 10, 26, 13, 10, tzinfo=UTC)     # after c1's 13:00 trigger, before c2's 13:30
    end = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)
    placed = ij.place(SCHEDULES[0], MEMBERS, RUNS, start, end, NOW)
    assert [(jr["slot"].hour, len(jr["members"])) for jr in placed] == [(14, 2)]
    whole = ij.place(SCHEDULES[0], MEMBERS, RUNS, datetime(2026, 10, 26, 13, 0, tzinfo=UTC), end, NOW)
    assert [(jr["slot"].hour, len(jr["members"])) for jr in whole] == [(14, 2), (13, 2)]


def test_a_member_removed_later_keeps_its_runs_in_earlier_job_runs():
    removed = [dict(m, removed_at=datetime(2026, 10, 26, 13, 45, tzinfo=UTC)) if m["task_collection"] == "c2" else m
               for m in MEMBERS]
    placed = ij.place(SCHEDULES[0], removed, RUNS, datetime(2026, 10, 26, 13, 0, tzinfo=UTC), NOW, NOW)
    by_hour = {jr["slot"].hour: [m["task_collection"] for m in jr["members"]] for jr in placed}
    assert by_hour == {14: ["c1"], 13: ["c1", "c2"]}
    assert ij.job_runs_of_run(dict(task_collection="c2", started_at="2026-10-26T13:31:00+00:00"), SCHEDULES, removed) == \
        [{"job_id": 1, "job_name": "East hourly", "slot": "2026-10-26T13:00:00+00:00"}]
    assert ij.job_runs_of_run(dict(task_collection="c2", started_at="2026-10-26T14:31:00+00:00"), SCHEDULES, removed) == []


def test_an_extra_run_counts_in_the_outcome_but_not_as_a_member():
    jr = {"slot": datetime(2026, 10, 26, 13, tzinfo=UTC), "members": [
        dict(task_collection="c1", offset_minutes=0, fire_at=datetime(2026, 10, 26, 13, tzinfo=UTC), state="run",
             run=run(9, "c1", datetime(2026, 10, 26, 13, 0, 20, tzinfo=UTC), ok=10, skipped=10),
             extra=[run(12, "c1", datetime(2026, 10, 26, 13, 40, tzinfo=UTC), status="failed", failed=1, ok=9, skipped=9)])]}
    s = ij.summarize(SCHEDULES[0], jr, {}, {"c1": 10}, {}, NOW)
    assert (s["outcome"]["label"], s["ran"], s["members"], s["extra"]) == ("Succeeded with failures", 1, 1, 1)
    live = dict(jr, members=[dict(jr["members"][0], extra=[run(12, "c1", datetime(2026, 10, 26, 13, 40, tzinfo=UTC), status="running")])])
    running = ij.summarize(SCHEDULES[0], live, {}, {"c1": 10}, {}, NOW)
    assert running["outcome"]["label"] == "Running"
    assert (running["finished_group_runs"], running["running_group_runs"]) == (1, 1)     # c1's own run finished


def test_a_finished_group_runs_reseeds_queued_are_not_loaded_in_its_progress():
    member = dict(task_collection="c1", state="run", run=dict(status="completed", total=10, ok=10, skipped=4, reseeded=3, failed=0))
    assert ij._progress(member, {}, 10) == {"total": 10, "unchanged": 7, "loaded": 3, "in_flight": 0, "failed": 0,
                                            "not_run": 0}


def test_a_paused_jobs_triggers_never_wait_or_start():
    """A paused job fires nothing, so its members in the current slot are not run, and it is not running."""
    paused = [dict(SCHEDULES[0], paused=True)]
    v = ij.jobs_view(paused, MEMBERS[:2], STATES, [], [], STATS, datetime(2026, 10, 26, 14, 5, tzinfo=UTC))
    job = v["jobs"][0]
    assert job["running_job_run"] is None and job["status"]["label"] != "Running"
    assert job["last_job_run"] is None      # a trigger that did not run is never its last job run


def test_a_group_triggered_with_no_run_yet_is_starting_and_waits_to_start():
    at = datetime(2026, 10, 26, 14, 5, tzinfo=UTC)
    job = ij.jobs_view(SCHEDULES[:1], MEMBERS[:2], STATES, [], [], STATS, at)["jobs"][0]
    assert job["status"]["label"] == "Running" and job["running_job_run"]["waiting"] == 2
    current = ij.current_and_past(SCHEDULES[0], MEMBERS[:2], [], at - timedelta(hours=1), at)[0]
    rows = ij.member_views(current, {}, {"c1": 10, "c2": 5}, at)
    assert [(r["task_collection"], r["state"]) for r in rows] == [("c1", "starting"), ("c2", "waiting")]
    assert rows[0]["status"]["label"] == "Starting"


def test_a_paused_jobs_last_job_run_is_its_last_real_one_not_a_missed_trigger():
    """Its missed triggers are Not run: the last job run shown, and the failed-in-a-row count, skip them."""
    paused = [dict(SCHEDULES[0], paused=True)]
    job = ij.jobs_view(paused, MEMBERS[:2], STATES, [], [r for r in RUNS if r["run_id"] != 10], STATS, NOW)["jobs"][0]
    assert (job["last_job_run"]["slot"], job["last_job_run"]["outcome"]["label"]) == (
        "2026-10-26T13:00:00+00:00", "Succeeded with failures")
    assert job["failed_in_a_row"] == 1
    never = ij.jobs_view(paused, MEMBERS[:2], STATES, [], [], STATS, NOW)["jobs"][0]
    assert never["last_job_run"] is None


def test_a_job_runs_checks_are_lines_of_their_own_kept_out_of_its_group_run_numbers():
    check = dict(run(20, "c1", datetime(2026, 10, 26, 13, 0, 5, tzinfo=UTC), minutes=1, total=10, ok=10, skipped=10),
                 work_id="check-555", detail=json.dumps({"check": {"work": [], "unchanged_tables": 10,
                                                                    "unchanged_databases": 1}}))
    group_run = dict(RUNS[1])
    jr = {"slot": datetime(2026, 10, 26, 13, tzinfo=UTC), "checks": [check], "members": [
        dict(task_collection="c1", offset_minutes=0, fire_at=datetime(2026, 10, 26, 13, tzinfo=UTC), state="unchanged",
             run=None, extra=[]),
        dict(task_collection="c2", offset_minutes=30, fire_at=datetime(2026, 10, 26, 13, 30, tzinfo=UTC), state="run",
             run=group_run, extra=[])]}
    s = ij.summarize(SCHEDULES[0], jr, {}, {"c1": 10, "c2": 5}, {}, NOW)
    assert (s["ran"], s["clusters"], s["run_ids"]) == (1, len({group_run.get("cluster_id")} - {None}), [8])
    assert s["took_s"] == round((datetime.fromisoformat(group_run["finished_at"])
                                 - datetime.fromisoformat(group_run["started_at"])).total_seconds())
    assert s["checks"] == [{"run_id": 20, "task_collection": "c1", "status": "completed", "unchanged_tables": 10,
                            "databases_with_work": 0}]
    rows = ij.member_views(jr, {}, {"c1": 10, "c2": 5}, NOW)
    check_rows = [r for r in rows if r.get("check")]
    assert [(r["task_collection"], r["status"]["label"]) for r in check_rows] == [("c1", "Checked: no changes")]
    assert [r["state"] for r in rows if not r.get("check")] == ["unchanged", "run"]
    # Its check vouched for c1's tables: they count as checked unchanged, not as still to go.
    c2 = ij._progress(jr["members"][1], {}, 5)
    assert (s["progress"]["total"], s["progress"]["unchanged"]) == (10 + c2["total"], 10 + c2["unchanged"])


def test_a_last_job_run_older_than_the_window_is_the_one_found_before_it():
    """Paused overnight: the window holds only triggers that did not run, so the last real job run comes from
    before it (main reads it), never a missed trigger."""
    paused = [dict(SCHEDULES[0], paused=True)]
    earlier = {"slot": "2026-10-25T21:00:00+00:00", "outcome": {"label": "Succeeded", "tone": "ok", "note": None},
               "failed_tables": 0, "took_s": 300}
    job = ij.jobs_view(paused, MEMBERS[:2], STATES, [], [], STATS, NOW, earlier_last_by_job={1: earlier})["jobs"][0]
    assert job["last_job_run"] == earlier
    assert ij.last_finished([{"outcome": {"label": "Not run"}}, {"outcome": {"label": "Running"}}]) is None


def test_the_last_job_run_before_the_window_is_the_newest_slot_not_the_newest_start():
    # A late run of an older slot can start after a run of a newer one: the newest slot that ran wins, and the
    # failure streak counts on from it over the job runs found with it.
    job = dict(SCHEDULES[0])
    late_older = dict(task_collection="c2", started_at="2026-10-26T13:20:00+00:00")     # c2 fires at :30: 12:00's
    newer = dict(task_collection="c1", started_at="2026-10-26T13:05:00+00:00")          # c1 fires at :00: 13:00's
    found = ij.newest_slots([late_older, newer], job, MEMBERS)
    assert found == [datetime(2026, 10, 26, 13, tzinfo=UTC), datetime(2026, 10, 26, 12, tzinfo=UTC)]
    # Found that way, only its newest job run is read: its failures in a row are at least that one.
    found = ij.earlier_last([{"outcome": {"label": "Succeeded with failures"}, "failed_tables": 2}])
    assert (found["failed_in_a_row"], found["failed_in_a_row_at_least"]) == (1, True)
    assert ij.earlier_last([{"outcome": {"label": "Not run"}, "failed_tables": 0}]) is None


def test_a_check_started_run_counts_the_tables_its_check_found_unchanged_and_a_group_that_did_not_run_is_not_run():
    # The check listed one database (3 tables) of a 13-table group: the run read 3, the check vouched for the other 10.
    member = {"state": "run", "run": {"status": "completed", "total": 3, "ok": 3, "failed": 0, "work_id": "9:c1",
                                      "reseeded": 0, "loaded": 1}}
    got = ij._progress(member, {}, 13)
    assert (got["total"], got["unchanged"] + got["loaded"], got["not_run"]) == (13, 13, 0)
    assert ij._progress({"state": "no run"}, {}, 13) == {"total": 13, "unchanged": 0, "loaded": 0, "in_flight": 0,
                                                         "failed": 0, "not_run": 13}
