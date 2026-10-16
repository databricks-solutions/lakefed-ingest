"""Schedules: cron and offsets, next runs, wrapper job settings, drift, warnings, and sync with a fake client."""
from datetime import datetime, timezone

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


def test_describe():
    assert sch.describe(15) == "every 15 minutes"
    assert sch.describe(15, 7) == "every 15 minutes at :07 past"
    assert sch.describe(60, 5) == "hourly at :05"
    assert sch.describe(1440, 90, "UTC") == "daily at 01:30 (UTC)"


def test_validate():
    ok = dict(name="Prod 15", cadence_minutes=15, timezone="UTC", parameters={"parallelism": "32"},
              job_variant="default", members=["c1", "c2"], notify_emails="a@x.com, b@y.org")
    assert sch.validate(ok, ["c1", "c2"], ["default"]) == []
    bad = dict(ok, name="", cadence_minutes=7, timezone="Mars/Base", parameters={"parallelism": "0", "x": "1"},
               job_variant="large", members=["c1", "c1", "zz"], notify_emails="nope")
    errs = " | ".join(sch.validate(bad, ["c1"], ["default"]))
    for part in ("name:", "cadence_minutes", "timezone", "parameters.parallelism", "'x' isn't", "job_variant",
                 "listed twice", "unknown collections zz", "notify_emails"):
        assert part in errs
    assert "at least one collection" in " ".join(sch.validate(dict(ok, members=[]), [], ["default"]))


def test_job_settings_and_drift():
    s = sch.job_settings(SCHED, "c1", 7, 555, view_group="users")
    assert s["name"] == "lakefed_ingest schedule: Every 15 min / c1"
    assert s["schedule"] == {"quartz_cron_expression": "0 7/15 * * * ?", "timezone_id": "UTC", "pause_status": "UNPAUSED"}
    assert s["max_concurrent_runs"] == 1 and s["queue"] == {"enabled": False}
    assert s["tasks"] == [{"task_key": "ingest", "run_job_task": {
        "job_id": 555, "job_parameters": {"task_collection": "c1", "parallelism": "32"}}}]
    assert s["tags"] == {sch.TAG_SCHEDULE: "7", sch.TAG_COLLECTION: "c1"}
    assert s["email_notifications"] == {"on_failure": ["ops@example.com"]}
    assert s["access_control_list"] == [{"group_name": "users", "permission_level": "CAN_VIEW"}]
    assert sch.drift(s, s) == []
    edited = dict(s, schedule=dict(s["schedule"], pause_status="PAUSED"), max_concurrent_runs=5)
    assert sch.drift(edited, s) == ["paused", "max_concurrent_runs"]
    assert "job_parameters" in sch.drift(sch.job_settings(dict(SCHED, parameters={}), "c1", 7, 555), s)
    assert sch.job_settings(dict(SCHED, paused=True), "c1", 0, 1)["schedule"]["pause_status"] == "PAUSED"


def test_warnings():
    assert sch.warnings(15, 300, 60) == []
    assert "overrun" in sch.warnings(15, 960, None)[0]
    assert sch.warnings(15, 780, None)[0].startswith("near overrun")
    assert "can't meet SLA" in sch.warnings(15, 600, 20)[-1]
    assert "2 trigger(s) skipped" in sch.warnings(15, None, None, skipped=2)[0]


def test_skipped_triggers():
    runs = [{"state": {"life_cycle_state": "SKIPPED"}}, {"state": {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS"}},
            {"status": {"state": "SKIPPED"}}]
    assert sch.skipped_triggers(runs) == 2


class FakeJobs(sch.JobsClient):
    def __init__(self, jobs=None):
        self.jobs, self.calls, self.next_id = dict(jobs or {}), [], 1000

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
