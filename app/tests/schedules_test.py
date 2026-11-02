"""Schedules: cron and offsets, next runs, wrapper job settings, drift, warnings, and sync with a fake client."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import schedules as sch

UTC = timezone.utc
SCHED = dict(id=7, name="Every 15 min", cadence_minutes=15, timezone="UTC", parameters={"parallelism": "32"},
             job_variant="default", stagger=True, paused=False, notify_emails="ops@example.com", deleted_at=None)


def test_quartz_cron_for_every_cadence_kind():
    assert sch.quartz_cron(15, 0) == "0 0/15 * * * ?"
    assert sch.quartz_cron(15, 7) == "0 7/15 * * * ?"
    assert sch.quartz_cron(60, 20) == "0 20 * * * ?"
    assert sch.quartz_cron(120, 30) == "0 30 0/2 * * ?"
    assert sch.quartz_cron(1440, 0) == "0 0 0 * * ?"
    with pytest.raises(ValueError):
        sch.quartz_cron(15, 15)
    with pytest.raises(ValueError):
        sch.quartz_cron(7, 0)


def test_offsets_spread_members_in_name_order_and_stay_in_the_first_hour():
    assert sch.offsets(["b", "a"], 15, True) == {"a": 0, "b": 7}
    assert sch.offsets(["a", "b", "c"], 30, True) == {"a": 0, "b": 10, "c": 20}
    assert sch.offsets(["a", "b"], 1440, True) == {"a": 0, "b": 30}
    assert sch.offsets(["a", "b"], 15, False) == {"a": 0, "b": 0}


def test_fire_times_and_next_run_match_the_cron():
    start = datetime(2026, 10, 16, 10, 2, tzinfo=UTC)
    times = sch.fire_times(15, 7, "UTC", start, datetime(2026, 10, 16, 11, 0, tzinfo=UTC))
    assert [t.minute for t in times] == [7, 22, 37, 52]
    assert sch.next_run(15, 7, "UTC", start) == datetime(2026, 10, 16, 10, 7, tzinfo=UTC)
    assert sch.next_run(1440, 30, "UTC", start) == datetime(2026, 10, 17, 0, 30, tzinfo=UTC)
    # A time zone shifts the wall clock: 02:00 in New York (EDT, UTC-4) is 06:00 UTC.
    assert sch.next_run(1440, 120, "America/New_York", start) == datetime(2026, 10, 17, 6, 0, tzinfo=UTC)
    assert sch.next_run(15, 7, "", start) == sch.next_run(15, 7, "UTC", start)      # a blank zone is UTC


def test_describe():
    # Plain words, as cronstrue reads a cron expression, with the time zone as the Databricks Jobs UI shows it.
    assert sch.describe(15) == "Every 15 minutes (UTC)"
    assert sch.describe(15, 7) == "Every 15 minutes, starting at 7 minutes past the hour (UTC)"
    assert sch.describe(60) == "Every hour, on the hour (UTC)"
    assert sch.describe(60, 5) == "Every hour, at 5 minutes past the hour (UTC)"
    assert sch.describe(1440, 90, "UTC") == "Every day at 01:30 (UTC)"
    assert sch.describe(360, 390, "Europe/London") == "Every 6 hours, starting at 06:30 (Europe/London)"


def test_validate():
    ok = dict(name="Prod 15", cadence_minutes=15, timezone="UTC", parameters={"parallelism": "32"},
              job_variant="default", members=["c1", "c2"], notify_emails="a@x.com, b@y.org")
    assert sch.validate(ok, ["c1", "c2"], ["default"]) == []
    assert sch.validate(dict(ok, name="Prod (EMEA), 15 min"), ["c1", "c2"], ["default"]) == []
    assert sch.validate(dict(ok, name="a/b"), ["c1", "c2"], ["default"])
    bad = dict(ok, name="", cadence_minutes=7, timezone="Mars/Base", parameters={"parallelism": "0", "x": "1"},
               job_variant="large", members=["c1", "c1", "zz"], notify_emails="nope")
    errs = " | ".join(sch.validate(bad, ["c1"], ["default"]))
    for part in ("name:", "cadence_minutes", "timezone", "parameters.parallelism", "'x' isn't", "job_variant",
                 "listed twice", "unknown ingestion groups zz", "notify_emails"):
        assert part in errs
    assert "at least one ingestion group" in " ".join(sch.validate(dict(ok, members=[]), [], ["default"]))


def test_job_settings_and_drift():
    s = sch.job_settings(SCHED, "c1", 7, 555, view_group="users")
    assert s["name"] == "lakefed_ingest schedule: Every 15 min / c1"
    assert s["schedule"] == {"quartz_cron_expression": "0 7/15 * * * ?", "timezone_id": "UTC", "pause_status": "UNPAUSED"}
    assert s["max_concurrent_runs"] == 1 and s["queue"] == {"enabled": False}
    assert s["tasks"] == [{"task_key": "ingest", "run_job_task": {
        "job_id": 555, "job_parameters": {"task_collection": "c1", "parallelism": "32"}}}]
    assert s["tags"] == {sch.TAG_SCHEDULE: "7", sch.TAG_COLLECTION: "c1"}
    assert s["email_notifications"] == {"on_failure": ["ops@example.com"]}
    assert s["notification_settings"] == {"no_alert_for_skipped_runs": True}
    assert s["access_control_list"] == [{"group_name": "users", "permission_level": "CAN_VIEW"}]
    assert sch.drift(s, s) == []
    edited = dict(s, schedule=dict(s["schedule"], pause_status="PAUSED"), max_concurrent_runs=5)
    assert sch.drift(edited, s) == ["paused", "max_concurrent_runs"]
    assert "job_parameters" in sch.drift(sch.job_settings(dict(SCHED, parameters={}), "c1", 7, 555), s)
    assert sch.drift({k: v for k, v in s.items() if k != "notification_settings"}, s) == ["no_alert_for_skipped_runs"]
    owned = sch.job_settings(SCHED, "c1", 7, 555, owner="inst/db")
    assert owned["tags"][sch.TAG_OWNER] == "inst/db" and sch.drift(s, owned) == ["tags"]
    assert sch.job_settings(dict(SCHED, paused=True), "c1", 0, 1)["schedule"]["pause_status"] == "PAUSED"


def test_warnings():
    assert sch.warnings(15, 300, 60) == []
    assert "overrun" in sch.warnings(15, 960, None)[0]
    assert sch.warnings(15, 780, None)[0].startswith("near overrun")
    assert "can't meet the freshness target" in sch.warnings(15, 600, 20)[-1]
    assert "2 trigger(s) skipped" in sch.warnings(15, None, None, skipped=2)[0]


def test_is_skipped():
    runs = [{"state": {"life_cycle_state": "SKIPPED"}}, {"state": {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS"}},
            {"status": {"state": "SKIPPED"}}]
    assert [sch.is_skipped(r) for r in runs] == [True, False, True]


class FakeJobs(sch.JobsClient):
    def __init__(self, jobs=None, runs=None):
        self.jobs, self.calls, self.next_id = dict(jobs or {}), [], 1000
        self.run_list = dict(runs or {})      # job_id -> runs (dicts with run_id, start_time, state)

    def tagged(self, schedule_id, owner):
        tags = lambda s: s.get("tags") or {}
        return {j: tags(s).get(sch.TAG_COLLECTION, "") for j, s in self.jobs.items()
                if owner and tags(s).get(sch.TAG_SCHEDULE) == str(schedule_id) and tags(s).get(sch.TAG_OWNER) == owner}

    def runs(self, job_id, since_ms, until_ms=None):
        self.calls.append(("runs", job_id, since_ms, until_ms))
        return [r for r in self.run_list.get(job_id, [])
                if r["start_time"] >= since_ms and (until_ms is None or r["start_time"] < until_ms)]

    def get(self, job_id):
        return {"job_id": job_id, "settings": self.jobs[job_id]} if job_id in self.jobs else None

    def create(self, settings):
        self.next_id += 1
        self.jobs[self.next_id] = settings
        self.calls.append(("create", self.next_id))
        return self.next_id

    def reset(self, job_id, settings):
        self.jobs[job_id] = settings
        self.calls.append(("reset", job_id))

    def delete(self, job_id):
        self.jobs.pop(job_id, None)
        self.calls.append(("delete", job_id))


def test_sync_creates_updates_recreates_and_deletes():
    current = sch.job_settings(SCHED, "c2", 7, 555)
    edited = dict(current, max_concurrent_runs=3)
    fake = FakeJobs({20: current, 30: edited})
    members = [dict(task_collection="c1", offset_minutes=0, job_id=None),     # never synced
               dict(task_collection="c2", offset_minutes=7, job_id=20),       # in sync
               dict(task_collection="c3", offset_minutes=0, job_id=30),       # edited outside the app
               dict(task_collection="c4", offset_minutes=0, job_id=99)]       # deleted outside the app
    removed = [dict(task_collection="c5", offset_minutes=0, job_id=50)]
    res = {r.task_collection: (r.action, r.job_id) for r in sch.sync(SCHED, members, removed, fake, 555)}
    assert res["c1"][0] == "created" and res["c2"] == ("unchanged", 20)
    assert res["c3"] == ("updated", 30) and res["c4"][0] == "created" and res["c5"] == ("deleted", 50)
    assert fake.jobs[30]["max_concurrent_runs"] == 1
    status = sch.check(SCHED, [dict(m, job_id=res[m["task_collection"]][1]) for m in members], fake, 555)
    assert {k: v["status"] for k, v in status.items()} == {"c1": "in_sync", "c2": "in_sync", "c3": "in_sync", "c4": "in_sync"}


def test_sync_of_a_deleted_schedule_deletes_every_job_and_reports_errors():
    class Failing(FakeJobs):
        def create(self, settings):
            raise RuntimeError("PERMISSION_DENIED")
    fake = Failing({20: {}})
    out = sch.sync(dict(SCHED, deleted_at="2026-10-16"), [], [dict(task_collection="c2", offset_minutes=0, job_id=20)], fake, 555)
    assert [(r.action, r.job_id) for r in out] == [("deleted", 20)]
    err = sch.sync(SCHED, [dict(task_collection="c1", offset_minutes=0, job_id=None)], [], fake, 555)[0]
    assert err.action == "error" and "PERMISSION_DENIED" in err.detail
    assert sch.check(SCHED, [dict(task_collection="c9", offset_minutes=0, job_id=None),
                             dict(task_collection="c8", offset_minutes=0, job_id=77)], fake, 555) == {
        "c9": {"status": "not_synced"}, "c8": {"status": "missing"}}


OWNER = "inst/db"


def test_deleting_a_check_schedule_deletes_its_shared_job_once():
    fake = FakeJobs({50: sch.group_job_settings(GROUP, ["c1", "c2"], 777, owner=OWNER)})
    members = [dict(id=1, task_collection="c1", offset_minutes=0, job_id=50),
               dict(id=2, task_collection="c2", offset_minutes=0, job_id=50)]
    out = sch.sync(dict(GROUP, deleted_at="2026-10-16"), members, [], fake, 777, owner=OWNER)
    assert fake.calls == [("delete", 50)]
    assert [(r.member_id, r.action, r.job_id) for r in out] == [(1, "deleted", 50), (2, "deleted", 50)]


def test_sync_adopts_an_unrecorded_tagged_job_and_deletes_untracked_ones():
    mine = lambda c: sch.job_settings(SCHED, c, 0, 555, owner=OWNER)
    fake = FakeJobs({40: mine("c1"),                                     # created, but its id never recorded
                     41: mine("c1"),                                     # a duplicate (e.g. a double sync)
                     42: mine("c9"),                                     # a collection no longer in the schedule
                     43: sch.job_settings(SCHED, "c1", 0, 555, owner="other/db")})   # another deployment's
    out = sch.sync(SCHED, [dict(id=1, task_collection="c1", offset_minutes=0, job_id=None)], [], fake, 555,
                   owner=OWNER)
    res = {(r.job_id, r.action, r.member_id) for r in out}
    assert res == {(40, "adopted", 1), (41, "deleted", None), (42, "deleted", None)}
    assert set(fake.jobs) == {40, 43} and not [c for c in fake.calls if c[0] == "create"]


def test_sync_of_a_collection_added_back_keeps_one_job():
    # Removed (its delete still pending), then added back as a new member with no job yet.
    fake = FakeJobs({10: sch.job_settings(SCHED, "c1", 0, 555, owner=OWNER)})
    out = sch.sync(SCHED, [dict(id=2, task_collection="c1", offset_minutes=0, job_id=None)],
                   [dict(id=1, task_collection="c1", offset_minutes=0, job_id=10)], fake, 555, owner=OWNER)
    assert {(r.member_id, r.action, r.job_id) for r in out} == {(2, "adopted", 10), (1, "kept", 10)}
    assert set(fake.jobs) == {10}


def test_sync_without_the_ingestion_job_still_deletes():
    fake = FakeJobs({10: sch.job_settings(SCHED, "c1", 0, 555, owner=OWNER),
                     11: sch.job_settings(SCHED, "c2", 0, 555, owner=OWNER)})
    out = sch.sync(SCHED, [dict(id=1, task_collection="c1", offset_minutes=0, job_id=None)],
                   [dict(id=2, task_collection="c2", offset_minutes=0, job_id=11)], fake, None, owner=OWNER)
    assert {(r.member_id, r.action) for r in out} == {(1, "error"), (2, "deleted")}
    assert "no ingestion job" in out[0].detail and set(fake.jobs) == {10}       # c1's job is kept


def test_sync_when_listing_fails_deletes_no_untracked_job():
    class NoList(FakeJobs):
        def tagged(self, schedule_id, owner):
            raise RuntimeError("429")
    fake = NoList({40: sch.job_settings(SCHED, "c9", 0, 555, owner=OWNER)})
    out = sch.sync(SCHED, [dict(id=1, task_collection="c1", offset_minutes=0, job_id=None)], [], fake, 555, owner=OWNER)
    assert [(r.action, r.member_id) for r in out] == [("error", None), ("created", 1)] and 40 in fake.jobs


def test_skipped_runs_lists_incrementally_and_counts_the_whole_window():
    H = 3600 * 1000
    now = 100 * H
    # A 5-minute cadence: 288 runs a day, every other one skipped (more than any page cap).
    runs = [{"run_id": i, "start_time": now - 24 * H + i * 300_000,
             "state": {"life_cycle_state": "SKIPPED" if i % 2 else "TERMINATED"}} for i in range(288)]
    fake = FakeJobs(runs={5: runs})
    cache = sch.SkippedRuns(ttl_s=60)
    assert len(cache.starts(fake, 5, now - 24 * H, now)) == 144
    assert len(cache.starts(fake, 5, now - 24 * H, now + 30_000)) == 144 and len(fake.calls) == 1   # within the ttl
    fake.run_list[5].append({"run_id": 999, "start_time": now + 30_000, "state": {"life_cycle_state": "SKIPPED"}})
    assert len(cache.starts(fake, 5, now - 24 * H, now + 90_000)) == 145
    assert fake.calls[-1] == ("runs", 5, now - sch.SkippedRuns.MARGIN_MS, None)                  # only the new runs
    fake.run_list[5].append({"run_id": 998, "start_time": now - 30 * H, "state": {"life_cycle_state": "SKIPPED"}})
    assert len(cache.starts(fake, 5, now - 48 * H, now + 90_000)) == 146                         # a longer window
    assert fake.calls[-1] == ("runs", 5, now - 48 * H, now - 24 * H)


def test_new_job_parameters_validate_like_the_notebook_parses_them():
    ok = dict(name="p", cadence_minutes=15, timezone="UTC", job_variant="default", members=["c1"],
              parameters={"table_retries": "2", "table_retry_budget": "50", "max_queries_per_source": "8",
                          "max_seeds_per_run": "0", "freshness_interval_s": "300.5", "append_batch_size": "200",
                          "snapshot_isolation": "true", "reseed_on_new_columns": "false"})
    assert sch.validate(ok, ["c1"], ["default"]) == []
    for k, v in (("table_retries", "-1"), ("append_batch_size", "0"), ("freshness_interval_s", "1e3"),
                 ("snapshot_isolation", "yes")):
        assert f"parameters.{k}: invalid value" in " ".join(sch.validate(dict(ok, parameters={k: v}), ["c1"], ["default"]))
    assert set(sch.FLAGS) == {"trust_server_certificate", "snapshot_isolation", "reseed_on_new_columns",
                             "fail_on_table_errors"}


def test_a_table_retry_budget_of_zero_is_valid_and_is_sent_to_the_job():
    schedule = dict(SCHED, members=["c1"], parameters={"table_retry_budget": "0"})
    assert sch.validate(schedule, ["c1"], ["default"]) == []
    job_parameters = sch.job_settings(schedule, "c1", 0, 555)["tasks"][0]["run_job_task"]["job_parameters"]
    assert job_parameters["table_retry_budget"] == "0"


def test_an_unset_table_retry_budget_is_left_out_so_the_job_default_applies():
    job = sch.job_settings(dict(SCHED, parameters={}), "c1", 0, 555)
    assert job["tasks"][0]["run_job_task"]["job_parameters"] == {"task_collection": "c1"}


def test_a_retry_run_gets_the_stored_budget_and_table_retries_and_none_when_none_are_stored():
    stored = sch.run_parameters(dict(SCHED, parameters={"table_retry_budget": "5", "table_retries": "3"}), "c1")
    assert stored == {"task_collection": "c1", "table_retries": "3", "table_retry_budget": "5"}
    unstored = sch.run_parameters(dict(SCHED, parameters={}), "c1")
    assert unstored == {"task_collection": "c1"}
    assert "table_retry_budget" not in unstored and "table_retries" not in unstored


def test_a_retry_run_takes_the_same_parameters_as_the_wrapper_job_it_retries_for():
    schedule = dict(SCHED, parameters={"table_retry_budget": "5", "table_retries": "3", "parallelism": "8"})
    wrapper = sch.job_settings(schedule, "c1", 0, 555)["tasks"][0]["run_job_task"]["job_parameters"]
    assert sch.run_parameters(schedule, "c1") == wrapper


# ---- job runs: group runs placed by cadence slot ------------------------------------------------

def _run(group, started, minutes=10, run_id=None, status="completed"):
    return dict(run_id=run_id, task_collection=group, status=status, started_at=started.isoformat(),
                finished_at=None if status == "running" else (started + timedelta(minutes=minutes)).isoformat())


def _members(*pairs, added=datetime(2026, 1, 1, tzinfo=UTC)):
    return [dict(task_collection=g, offset_minutes=o, added_at=added, removed_at=None) for g, o in pairs]


def test_staggered_members_of_one_hourly_trigger_share_one_job_run():
    runs = [_run("a", datetime(2026, 10, 26, 14, 0, 30, tzinfo=UTC), run_id=1),
            _run("b", datetime(2026, 10, 26, 14, 30, 20, tzinfo=UTC), run_id=2)]
    found = sch.job_runs(60, "UTC", _members(("a", 0), ("b", 30)), runs, datetime(2026, 10, 26, 14, tzinfo=UTC),
                         datetime(2026, 10, 26, 15, tzinfo=UTC), now=datetime(2026, 10, 26, 16, tzinfo=UTC))
    assert len(found) == 1 and found[0]["slot"] == datetime(2026, 10, 26, 14, tzinfo=UTC)
    assert [(m["task_collection"], m["state"], m["run"]["run_id"]) for m in found[0]["members"]] == [("a", "run", 1), ("b", "run", 2)]


def test_a_run_two_minutes_late_belongs_to_its_trigger_and_a_second_one_is_extra():
    runs = [_run("a", datetime(2026, 10, 26, 14, 2, tzinfo=UTC), run_id=1),
            _run("a", datetime(2026, 10, 26, 14, 40, tzinfo=UTC), run_id=2)]
    found = sch.job_runs(60, "UTC", _members(("a", 0)), runs, datetime(2026, 10, 26, 13, tzinfo=UTC),
                         datetime(2026, 10, 26, 15, tzinfo=UTC), now=datetime(2026, 10, 26, 16, tzinfo=UTC))
    by_slot = {j["slot"].hour: j["members"][0] for j in found}
    assert by_slot[14]["run"]["run_id"] == 1 and [r["run_id"] for r in by_slot[14]["extra"]] == [2]
    assert by_slot[13]["state"] == "no run" and [j["slot"].hour for j in found] == [14, 13]


def test_a_run_now_before_the_first_trigger_is_a_job_run_of_its_own():
    members = _members(("a", 0), ("b", 30), added=datetime(2026, 10, 26, 13, 10, tzinfo=UTC))
    runs = [_run("a", datetime(2026, 10, 26, 13, 12, tzinfo=UTC), run_id=1),
            _run("b", datetime(2026, 10, 26, 13, 14, tzinfo=UTC), run_id=2)]
    found = sch.job_runs(60, "UTC", members, runs, datetime(2026, 10, 26, 13, tzinfo=UTC),
                         datetime(2026, 10, 26, 15, tzinfo=UTC), now=datetime(2026, 10, 26, 13, 40, tzinfo=UTC))
    outside = [j for j in found if j["outside"]]
    assert [j["slot"] for j in outside] == [datetime(2026, 10, 26, 13, 12, tzinfo=UTC)]
    assert [(m["task_collection"], m["state"], m["run"]["run_id"]) for m in outside[0]["members"]] == [("a", "run", 1), ("b", "run", 2)]
    assert all(not j["outside"] for j in found if j["slot"] != outside[0]["slot"])


def test_runs_by_hand_far_apart_are_separate_job_runs_and_a_skipped_one_is_left_out():
    members = _members(("a", 0), added=datetime(2026, 10, 26, 13, 1, tzinfo=UTC))
    runs = [_run("a", datetime(2026, 10, 26, 13, 5, tzinfo=UTC), run_id=1),
            _run("a", datetime(2026, 10, 26, 13, 40, tzinfo=UTC), run_id=2),
            _run("a", datetime(2026, 10, 26, 13, 41, tzinfo=UTC), run_id=3, status="skipped")]
    found = sch.job_runs(60, "UTC", members, runs, datetime(2026, 10, 26, 13, tzinfo=UTC),
                         datetime(2026, 10, 26, 14, tzinfo=UTC), now=datetime(2026, 10, 26, 13, 59, tzinfo=UTC))
    assert [(j["slot"].minute, j["members"][0]["run"]["run_id"]) for j in found if j["outside"]] == [(40, 2), (5, 1)]


def test_a_trigger_missed_while_the_previous_run_was_going_is_skipped():
    runs = [_run("a", datetime(2026, 10, 26, 13, 0, tzinfo=UTC), minutes=75, run_id=1)]
    found = sch.job_runs(60, "UTC", _members(("a", 0)), runs, datetime(2026, 10, 26, 14, tzinfo=UTC),
                         datetime(2026, 10, 26, 15, tzinfo=UTC), now=datetime(2026, 10, 26, 16, tzinfo=UTC))
    assert found[0]["members"][0]["state"] == "skipped"


def test_runs_and_triggers_before_a_member_was_added_are_left_out():
    runs = [_run("a", datetime(2026, 10, 26, 13, 0, tzinfo=UTC), run_id=1),
            _run("a", datetime(2026, 10, 26, 14, 0, tzinfo=UTC), run_id=2)]
    members = _members(("a", 0), added=datetime(2026, 10, 26, 13, 30, tzinfo=UTC))
    found = sch.job_runs(60, "UTC", members, runs, datetime(2026, 10, 26, 13, tzinfo=UTC),
                         datetime(2026, 10, 26, 15, tzinfo=UTC), now=datetime(2026, 10, 26, 16, tzinfo=UTC))
    assert [(j["slot"].hour, j["members"][0]["run"]["run_id"]) for j in found] == [(14, 2)]


def test_a_daily_trigger_keeps_its_slot_across_a_daylight_saving_change():
    zone = "Europe/London"     # clocks went back at 02:00 BST on 25 Oct 2026
    runs = [_run("a", datetime(2026, 10, 24, 1, 5, tzinfo=UTC), run_id=1),     # 02:05 BST
            _run("a", datetime(2026, 10, 25, 2, 5, tzinfo=UTC), run_id=2),     # 02:05 GMT
            _run("b", datetime(2026, 10, 25, 2, 35, tzinfo=UTC), run_id=3)]
    found = sch.job_runs(1440, zone, _members(("a", 120), ("b", 150)), runs, datetime(2026, 10, 23, 23, tzinfo=UTC),
                         datetime(2026, 10, 25, 23, tzinfo=UTC), now=datetime(2026, 10, 26, 12, tzinfo=UTC))
    assert [[(m["run"] or {}).get("run_id") for m in j["members"]] for j in found] == [[2, 3], [1, None]]
    assert found[1]["members"][1]["state"] == "no run"


def test_a_member_whose_start_is_still_ahead_is_waiting():
    runs = [_run("a", datetime(2026, 10, 26, 14, 0, 30, tzinfo=UTC), run_id=1, status="running")]
    found = sch.job_runs(60, "UTC", _members(("a", 0), ("b", 30)), runs, datetime(2026, 10, 26, 14, tzinfo=UTC),
                         datetime(2026, 10, 26, 15, tzinfo=UTC), now=datetime(2026, 10, 26, 14, 10, tzinfo=UTC))
    waiting = found[0]["members"][1]
    assert waiting["state"] == "waiting" and waiting["fire_at"] == datetime(2026, 10, 26, 14, 30, tzinfo=UTC)


def test_a_member_triggered_in_the_current_slot_with_no_group_run_yet_is_starting():
    """Its cluster may still be starting, for up to 15 minutes after the trigger; then it is "no run"."""
    found = sch.job_runs(30, "UTC", _members(("a", 0), ("b", 15)), [], datetime(2026, 10, 26, 20, tzinfo=UTC),
                         datetime(2026, 10, 26, 21, tzinfo=UTC), now=datetime(2026, 10, 26, 20, 35, tzinfo=UTC))
    current, past = found[0]["members"], found[1]["members"]
    assert [(m["task_collection"], m["state"]) for m in current] == [("a", "starting"), ("b", "waiting")]
    assert [m["state"] for m in past] == ["no run", "no run"]


def test_minute_and_hourly_schedules_run_in_utc_and_set_times_keep_their_zone():
    # London is a whole hour off UTC, so :00 and :30 fall on the same instants; in UTC no run is skipped on the
    # clock-change nights. A daily or every-N-hours schedule is tied to a local time of day, so it keeps its zone.
    assert sch.timezone_for(30, "Europe/London") == "UTC" and sch.timezone_for(60, "Europe/London") == "UTC"
    assert sch.timezone_for(120, "Europe/London") == "Europe/London"
    assert sch.timezone_for(1440, "Europe/London") == "Europe/London"
    assert sch.timezone_for(1440, "") == "UTC"


def test_a_set_start_time_moves_every_members_offset_and_wraps_within_the_cadence():
    assert sch.offsets(["a", "b"], 1440, True, start_minute=150) == {"a": 150, "b": 180}
    assert sch.offsets(["a"], 360, False, start_minute=390) == {"a": 30}
    assert sch.offsets(["a", "b"], 30, True, start_minute=150) == {"a": 0, "b": 15}   # minutes ignore a start


def test_a_start_time_is_read_whether_the_options_are_stored_as_text_or_as_a_dict():
    assert sch.start_minute({"cadence_minutes": 1440, "options": '{"start_minute": 150}'}) == 150
    assert sch.start_minute({"cadence_minutes": 1440, "options": {"start_minute": 150}}) == 150
    assert sch.start_minute({"cadence_minutes": 30, "options": {"start_minute": 150}}) == 0


def test_a_schedules_options_are_parsed_in_one_place():
    source = Path(sch.__file__).read_text()
    assert source.count("def options_of(") == 1
    body = source[source.index("def start_minute("):]
    assert "json.loads" not in body[:body.index("\ndef ", 1)]


def test_a_daily_schedule_at_a_set_time_fires_then_and_its_job_runs_are_named_by_that_time():
    offsets = sch.offsets(["a", "b"], 1440, True, start_minute=150)
    assert sch.quartz_cron(1440, offsets["a"]) == "0 30 2 * * ?"
    start = datetime(2026, 10, 27, tzinfo=timezone.utc)
    fire = sch.next_run(1440, offsets["b"], "Europe/London", start)
    assert fire == datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)          # 03:00 GMT, the second member
    assert sch.slot_of(fire, sch.stagger_of(offsets["b"], 1440, 150), "Europe/London") == datetime(
        2026, 10, 27, 2, 30, tzinfo=timezone.utc)
    assert sch.describe(1440, 150, "Europe/London") == "Every day at 02:30 (Europe/London)"


def test_a_start_time_is_checked_and_only_set_time_schedules_take_one():
    ok = {"name": "n", "cadence_minutes": 1440, "timezone": "Europe/London", "members": ["c1"], "parameters": {}}
    assert sch.validate(dict(ok, options={"start_minute": 150}), ["c1"], ["default"]) == []
    assert sch.validate(dict(ok, options={"start_minute": 1440}), ["c1"], ["default"]) == [
        "options.start_minute: a minute of the day, 0 to 1439"]
    assert sch.validate(dict(ok, cadence_minutes=30, options={"start_minute": 150}), ["c1"], ["default"]) == [
        "options.start_minute: only daily and every 2 to 12 hours schedules start at a set time"]
    assert sch.validate(dict(ok, options={"color": "red"}), ["c1"], ["default"]) == ["options: unknown key color"]


def test_a_set_time_inside_the_clock_change_gap_does_not_fire_that_night():
    # 29 Mar 2026, London: 01:00 GMT jumps to 02:00 BST, so 01:30 does not exist that night (Quartz skips it).
    fires = sch.fire_times(1440, 90, "Europe/London", datetime(2026, 3, 28, tzinfo=timezone.utc),
                           datetime(2026, 3, 31, tzinfo=timezone.utc))
    assert fires == [datetime(2026, 3, 28, 1, 30, tzinfo=timezone.utc), datetime(2026, 3, 30, 0, 30, tzinfo=timezone.utc)]


def test_a_member_starts_at_its_clock_time_and_the_stagger_range_is_its_stagger():
    assert sch.member_start(1440, 150) == "02:30" and sch.member_start(30, 15) == "15 minutes past the hour"
    assert sch.member_start(60, 0) == "on the hour" and sch.member_start(60, 1) == "1 minute past the hour"
    offs = sch.offsets(["a", "b"], 1440, True, start_minute=1430)
    assert offs == {"a": 1430, "b": 20}
    assert sorted(sch.stagger_of(o, 1440, 1430) for o in offs.values()) == [0, 30]


# ---- the check variant: one wrapper per schedule, starting the check job with every member ---------------

GROUP = dict(SCHED, job_variant=sch.CHECK_VARIANT)


def test_a_check_schedule_has_one_wrapper_that_starts_the_check_job_with_every_member():
    s = sch.group_job_settings(GROUP, ["c2", "c1"], 777, owner=OWNER)
    task = s["tasks"][0]
    assert task["run_job_task"] == {"job_id": 777, "job_parameters": {"schedule_id": "7", "task_collections": "c1,c2",
                                                                      "parallelism": "32"}}
    assert s["schedule"]["quartz_cron_expression"] == "0 0/15 * * * ?"     # no stagger: one start per cycle
    assert s["tags"][sch.TAG_COLLECTION] == sch.GROUP_TAG and s["name"] == sch.group_job_name(GROUP["name"])
    assert s["max_concurrent_runs"] == 1 and s["queue"] == {"enabled": False}


def test_a_check_job_with_more_groups_than_a_parameter_holds_passes_only_its_id():
    groups = [f"engagement_extraction_sql_engagements_c{i:03d}" for i in range(300)]    # about 13,500 characters
    params = sch.group_job_settings(GROUP, groups, 777, owner=OWNER)["tasks"][0]["run_job_task"]["job_parameters"]
    assert params["schedule_id"] == "7" and params["task_collections"] == ""
    assert all(len(v) <= sch.MAX_PARAMETER_CHARS for v in params.values())


# The tag rules Databricks applies to a job's tags, which it copies onto the job's clusters, so the cloud's rules
# hold too. AWS: a key of 1 to 127 and a value of up to 255 characters, each of letters, digits, spaces and
# _ . : / = + - @ only (so no "*"). Azure: a tag name has none of < > % & \ ? /. The fake Jobs API accepts any tag,
# so this is the check against the real rules.
_AWS_TAG_TEXT = re.compile(r"[\w .:/=+\-@]*")
_AZURE_NAME_REFUSED = set("<>%&\\?/")


def _tag_problems(tags: dict) -> list:
    problems = []
    for key, value in tags.items():
        if not 1 <= len(key) <= 127 or not _AWS_TAG_TEXT.fullmatch(key) or set(key) & _AZURE_NAME_REFUSED:
            problems.append(f"key {key!r}")
        if len(value) > 255 or not _AWS_TAG_TEXT.fullmatch(value):
            problems.append(f"value {value!r} of {key}")
    return problems


def test_every_tag_the_app_writes_meets_the_databricks_tag_rules():
    group = "lab_1k_try_lfc_sqlserver_test_c01"            # as new_job.group_name names a group
    default = sch.job_settings(SCHED, group, 0, 555, owner="lakefed-ingest/lakefed_ingest")
    check = sch.group_job_settings(GROUP, [group, "calib_1db"], 777, owner="lakefed-ingest/lakefed_ingest")
    assert _tag_problems(default["tags"]) == [] and _tag_problems(check["tags"]) == []
    assert _tag_problems({sch.TAG_COLLECTION: "*"}) == ["value '*' of lakefed_ingest_collection"]   # what AWS refused


def test_syncing_a_check_schedule_creates_one_job_for_all_members_and_deletes_their_own():
    per_member = lambda c: sch.job_settings(SCHED, c, 0, 555, owner=OWNER)
    fake = FakeJobs({10: per_member("c1"), 11: per_member("c2")})           # before: the default variant
    members = [dict(id=1, task_collection="c1", offset_minutes=0, job_id=10),
               dict(id=2, task_collection="c2", offset_minutes=7, job_id=11)]
    out = sch.sync(GROUP, members, [], fake, 777, owner=OWNER)
    group = [j for j, s in fake.jobs.items() if s["tags"][sch.TAG_COLLECTION] == sch.GROUP_TAG]
    assert len(group) == 1 and set(fake.jobs) == set(group)                 # 10 and 11 are deleted
    by_member = {r.member_id: (r.action, r.job_id) for r in out if r.member_id}
    assert by_member == {1: ("created", group[0]), 2: ("unchanged", group[0])}
    assert sch.check(GROUP, [dict(m, job_id=group[0]) for m in members], fake, 777, owner=OWNER) == {
        "c1": {"status": "in_sync"}, "c2": {"status": "in_sync"}}
    again = sch.sync(GROUP, [dict(m, job_id=group[0]) for m in members], [], fake, 777, owner=OWNER)
    assert {r.action for r in again} == {"unchanged"} and len(fake.jobs) == 1


def test_a_member_added_to_a_check_schedule_updates_the_one_job():
    fake = FakeJobs({50: sch.group_job_settings(GROUP, ["c1"], 777, owner=OWNER)})
    members = [dict(id=1, task_collection="c1", offset_minutes=0, job_id=50),
               dict(id=2, task_collection="c2", offset_minutes=0, job_id=None)]
    out = sch.sync(GROUP, members, [dict(id=3, task_collection="c3", offset_minutes=0, job_id=50)], fake, 777,
                   owner=OWNER)
    assert fake.jobs[50]["tasks"][0]["run_job_task"]["job_parameters"]["task_collections"] == "c1,c2"
    assert {(r.member_id, r.action) for r in out} == {(1, "updated"), (2, "unchanged"), (3, "kept")}
    assert set(fake.jobs) == {50}


def test_switching_back_from_check_gives_each_member_its_own_job_again():
    fake = FakeJobs({50: sch.group_job_settings(GROUP, ["c1", "c2"], 777, owner=OWNER)})
    members = [dict(id=1, task_collection="c1", offset_minutes=0, job_id=50),
               dict(id=2, task_collection="c2", offset_minutes=7, job_id=50)]
    out = sch.sync(SCHED, members, [], fake, 555, owner=OWNER)
    jobs = {r.member_id: r.job_id for r in out if r.member_id}
    assert jobs[1] != jobs[2] and {fake.jobs[j]["tags"][sch.TAG_COLLECTION] for j in jobs.values()} == {"c1", "c2"}
    assert set(fake.jobs) == set(jobs.values())


def test_the_check_variant_is_valid_only_when_the_check_job_is_configured():
    body = dict(name="Group", cadence_minutes=15, timezone="UTC", parameters={}, members=["c1"], job_variant="check")
    assert sch.validate(body, ["c1"], {"default": "1", "check": "2"}) == []
    assert sch.validate(body, ["c1"], {"default": "1"}) == ["job_variant must be one of default"]


def test_group_runs_of_one_launch_are_one_job_run_however_late_they_start():
    late = _run("b", datetime(2026, 10, 26, 15, 40, tzinfo=UTC), run_id=2)      # after b's 15:30 trigger
    late["work_id"] = "900:b"
    first = _run("a", datetime(2026, 10, 26, 14, 1, tzinfo=UTC), run_id=1)
    first["work_id"] = "900:a"
    found = sch.job_runs(60, "UTC", _members(("a", 0), ("b", 30)), [first, late], datetime(2026, 10, 26, 14, tzinfo=UTC),
                         datetime(2026, 10, 26, 16, tzinfo=UTC), now=datetime(2026, 10, 26, 17, tzinfo=UTC))
    by_hour = {j["slot"].hour: [(m["task_collection"], (m["run"] or {}).get("run_id")) for m in j["members"]] for j in found}
    assert by_hour == {14: [("a", 1), ("b", 2)], 15: [("a", None), ("b", None)]}


def _check(group, started, work=(), run_id=None, status="completed"):
    r = _run(group, started, minutes=1, run_id=run_id, status=status)
    r["work_id"] = "check-555"
    r["detail"] = None if status == "running" else json.dumps({"check": {"work": [list(w) for w in work],
                                                                         "unchanged_tables": 10}})
    return r


def test_the_checks_own_records_are_the_job_runs_checks_not_its_group_runs():
    quiet = _check("a", datetime(2026, 10, 26, 14, 0, 5, tzinfo=UTC), run_id=1)
    busy = _check("b", datetime(2026, 10, 26, 14, 0, 6, tzinfo=UTC), work=[("srv|0|srv", "db")], run_id=2)
    ingest = _run("b", datetime(2026, 10, 26, 14, 9, tzinfo=UTC), run_id=3)
    ingest["work_id"] = "900:b"
    found = sch.job_runs(60, "UTC", _members(("a", 0), ("b", 0)), [quiet, busy, ingest],
                         datetime(2026, 10, 26, 14, tzinfo=UTC), datetime(2026, 10, 26, 15, tzinfo=UTC),
                         now=datetime(2026, 10, 26, 16, tzinfo=UTC), check_variant=True)
    members = {m["task_collection"]: m for m in found[0]["members"]}
    assert (members["a"]["state"], members["a"]["run"]) == ("unchanged", None)
    assert (members["b"]["state"], members["b"]["run"]["run_id"], members["b"]["extra"]) == ("run", 3, [])
    assert [(c["task_collection"], c["run_id"]) for c in found[0]["checks"]] == [("a", 1), ("b", 2)]
    running = _check("a", datetime(2026, 10, 26, 14, 0, 5, tzinfo=UTC), run_id=1, status="running")
    found = sch.job_runs(60, "UTC", _members(("a", 0)), [running], datetime(2026, 10, 26, 14, tzinfo=UTC),
                         datetime(2026, 10, 26, 15, tzinfo=UTC), now=datetime(2026, 10, 26, 14, 1, tzinfo=UTC),
                         check_variant=True)
    assert found[0]["members"][0]["state"] == "checking" and found[0]["checks"][0]["run_id"] == 1
    assert sch.is_check({"detail": json.dumps({"check": {"error": "x"}})}) and not sch.is_check({"work_id": "900:b"})


def test_a_default_schedule_takes_no_checks_even_of_its_members():
    # A check started some other way (its own fallback schedule, by hand) is not this schedule's job run.
    check = _check("a", datetime(2026, 10, 26, 14, 5, tzinfo=UTC), run_id=1)
    found = sch.job_runs(60, "UTC", _members(("a", 0)), [check], datetime(2026, 10, 26, 14, tzinfo=UTC),
                         datetime(2026, 10, 26, 15, tzinfo=UTC), now=datetime(2026, 10, 26, 16, tzinfo=UTC))
    assert (found[0]["members"][0]["state"], found[0]["checks"]) == ("no run", [])


def test_a_check_schedule_fires_every_member_at_the_slot_and_keeps_one_check_together():
    # Offsets stored from a staggered schedule do not apply: the one wrapper fires at the slot. A check job run
    # that reaches its last collection after the next trigger still belongs to the slot it started in.
    first = _check("a", datetime(2026, 10, 26, 14, 59, 50, tzinfo=UTC), run_id=1)
    late = _check("b", datetime(2026, 10, 26, 15, 0, 20, tzinfo=UTC), run_id=2)
    ingest = _run("b", datetime(2026, 10, 26, 15, 1, tzinfo=UTC), run_id=3)
    ingest["work_id"] = "900:b"
    found = sch.job_runs(60, "UTC", _members(("a", 0), ("b", 30)), [first, late, ingest],
                         datetime(2026, 10, 26, 14, tzinfo=UTC), datetime(2026, 10, 26, 16, tzinfo=UTC),
                         now=datetime(2026, 10, 26, 17, tzinfo=UTC), check_variant=True)
    slot = {jr["slot"].hour: jr for jr in found}
    assert [c["run_id"] for c in slot[14]["checks"]] == [1, 2] and slot[15]["checks"] == []
    assert {m["task_collection"]: m["offset_minutes"] for m in slot[14]["members"]} == {"a": 0, "b": 0}


def test_a_check_counts_for_a_member_that_belonged_at_the_trigger():
    # Removed just after the trigger, before the check reached it: the check still ran for that job run.
    check = _check("a", datetime(2026, 10, 26, 14, 3, tzinfo=UTC), run_id=1)
    members = _members(("a", 0))
    members[0]["removed_at"] = datetime(2026, 10, 26, 14, 1, tzinfo=UTC)
    found = sch.job_runs(60, "UTC", members, [check], datetime(2026, 10, 26, 14, tzinfo=UTC),
                         datetime(2026, 10, 26, 15, tzinfo=UTC), now=datetime(2026, 10, 26, 16, tzinfo=UTC),
                         check_variant=True)
    assert (found[0]["members"][0]["state"], [c["run_id"] for c in found[0]["checks"]]) == ("unchanged", [1])


def test_a_check_schedules_launched_run_counts_for_a_member_that_belonged_at_the_trigger():
    check = _check("a", datetime(2026, 10, 26, 14, 0, 5, tzinfo=UTC), work=[("srv|0|srv", "db")], run_id=1)
    ingest = _run("a", datetime(2026, 10, 26, 14, 3, tzinfo=UTC), run_id=2)
    ingest["work_id"] = "900:a"
    members = _members(("a", 0))
    members[0]["removed_at"] = datetime(2026, 10, 26, 14, 1, tzinfo=UTC)
    found = sch.job_runs(60, "UTC", members, [check, ingest], datetime(2026, 10, 26, 14, tzinfo=UTC),
                         datetime(2026, 10, 26, 15, tzinfo=UTC), now=datetime(2026, 10, 26, 16, tzinfo=UTC),
                         check_variant=True)
    assert (found[0]["members"][0]["state"], found[0]["members"][0]["run"]["run_id"]) == ("run", 2)


def test_a_default_schedule_judges_a_launched_run_by_its_own_start():
    ingest = _run("a", datetime(2026, 10, 26, 14, 3, tzinfo=UTC), run_id=2)
    ingest["work_id"] = "900:a"
    members = _members(("a", 0))
    members[0]["removed_at"] = datetime(2026, 10, 26, 14, 1, tzinfo=UTC)
    found = sch.job_runs(60, "UTC", members, [ingest], datetime(2026, 10, 26, 14, tzinfo=UTC),
                         datetime(2026, 10, 26, 15, tzinfo=UTC), now=datetime(2026, 10, 26, 16, tzinfo=UTC))
    assert found[0]["members"][0]["run"] is None


def test_a_check_schedule_at_a_set_time_fires_its_one_wrapper_then_and_names_its_job_runs_by_it():
    daily = dict(GROUP, cadence_minutes=1440, timezone="UTC", options={"start_minute": 150})
    assert sch.group_job_settings(daily, ["c1"], 777, owner=OWNER)["schedule"]["quartz_cron_expression"] == "0 30 2 * * ?"
    check = _check("a", datetime(2026, 10, 26, 2, 30, 5, tzinfo=UTC), work=[("srv|0|srv", "db")], run_id=1)
    ingest = _run("a", datetime(2026, 10, 26, 2, 33, tzinfo=UTC), run_id=2)
    ingest["work_id"] = "900:a"
    found = sch.job_runs(1440, "UTC", _members(("a", 150)), [check, ingest], datetime(2026, 10, 26, tzinfo=UTC),
                         datetime(2026, 10, 27, tzinfo=UTC), now=datetime(2026, 10, 26, 4, tzinfo=UTC),
                         start_minute=150, check_variant=True)
    assert found[0]["slot"] == datetime(2026, 10, 26, 2, 30, tzinfo=UTC)
    assert found[0]["members"][0]["run"]["run_id"] == 2 and [c["run_id"] for c in found[0]["checks"]] == [1]


def test_a_check_wrapper_tagged_the_old_way_is_kept_and_retagged_with_a_value_aws_accepts():
    import re
    assert re.fullmatch(r"[\d \w+\-=.:/@]*", sch.GROUP_TAG)        # the Jobs API's tag value rule on AWS
    fake = FakeJobs()
    old = sch.group_job_settings(GROUP, ["c1", "c2"], 777)
    old["tags"][sch.TAG_COLLECTION] = "*"
    jid = fake.create(old)
    members = [dict(id=1, task_collection="c1", job_id=jid), dict(id=2, task_collection="c2", job_id=jid)]
    results = sch.sync(GROUP, members, [], fake, 777)
    assert "updated" in {r.action for r in results} and {r.job_id for r in results} == {jid} and len(fake.jobs) == 1
    assert fake.jobs[jid]["tags"][sch.TAG_COLLECTION] == sch.GROUP_TAG


def test_a_job_never_changed_was_last_updated_by_its_creator_and_an_older_unrecorded_change_is_unknown():
    created = "2026-10-28T16:01:45+00:00"
    assert sch.updated_by({"created_by": "a@x.com", "created_at": created, "updated_at": created}) == "a@x.com"
    assert sch.updated_by({"created_by": "a@x.com", "created_at": created, "updated_at": "2026-10-28T17:00:00+00:00"}) is None
    assert sch.updated_by({"created_by": "a@x.com", "created_at": created, "updated_at": "2026-10-28T17:00:00+00:00",
                           "options": {"updated_by": "b@x.com"}}) == "b@x.com"
