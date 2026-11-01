"""A new job's preparation as preparing.py says it: its states and what each refuses, the record Create keeps, the
databases set aside and the actions on them, and the minutes left while its tables are read. Pure, no app run."""
from datetime import datetime, timezone

import pytest

import preparing as p

NOW = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
SETTINGS = {"load_type": "ct", "fan_in": True, "sink_catalog": "main", "sink_schema": "bronze", "sink_table": "{table}"}


def _record(state=p.PREPARING, stage=p.READING, **extra):
    record = p.new_record("ann@example.com", SETTINGS, {"tables_per_group": 5000}, {"secret_scope": "s", "x": "1"},
                          {"kind": "discovery", "source_id": "d1,d2"}, turn_on=True, now=NOW, state=state, stage=stage)
    return {**record, **extra}


def _options(record):
    return {p.OPTION: record}


def test_a_job_with_no_record_is_ready_and_refuses_nothing():
    for options in (None, {}, {"cluster": {}}):
        assert p.state_of(options) == p.READY and p.is_ready(options)
        assert p.run_refusal(options) is None and p.turn_on_refusal(options) is None
        assert p.tables_refusal(options) is None and p.settings_refusal(options) is None
        assert p.status_cell(options) is None


def test_the_record_keeps_what_create_was_asked_for_and_by_whom():
    record = _record()
    assert record["maker"] == "ann@example.com" and record["made_at"] == NOW.isoformat()
    assert record["settings"] == SETTINGS
    assert record["sizing"] == {"tables_per_group": 5000, "groups_per_server": None}
    assert record["scope"] == {"secret_scope": "s"}             # only the scope and trust, not the tuning
    assert record["source"] == {"kind": "discovery", "source_id": "d1,d2"}
    assert (record["turn_on"], record["turn_on_by"]) == (True, "ann@example.com")
    assert p.new_record("ann", {}, {}, {}, {}, turn_on=False)["turn_on_by"] is None


def test_todays_create_makes_a_ready_job_that_keeps_its_record():
    record = p.new_record("ann", SETTINGS, {}, {}, {"kind": "upload", "source_id": "u1"}, turn_on=False)
    assert record["state"] == p.READY and record["stage"] is None and p.is_ready(_options(record))


def test_a_preparing_job_refuses_runs_and_its_schedule_and_says_its_stage():
    options = _options(_record())
    assert p.run_refusal(options) == ("the job isn't ready yet (preparing: reading tables): Run now is available when "
                                      "the job is ready")
    assert "its schedule is available when the job is ready, and turns on then" in p.turn_on_refusal(options)
    assert "what it ingests can be changed when the job is ready" in p.tables_refusal(options)
    writing = _options(p.moved(_record(), stage=p.WRITING))
    assert "(preparing: writing the job's tables)" in p.run_refusal(writing)
    syncing = _options(p.moved(_record(), stage=p.SYNCING))
    assert "(preparing: making the job's Databricks jobs)" in p.run_refusal(syncing)


def test_table_settings_are_editable_while_reading_and_wait_for_ready_once_the_write_has_begun():
    reading = _record()
    assert p.settings_refusal(_options(reading)) is None
    writing = p.moved(reading, stage=p.WRITING)
    assert writing["writing_started"] is True
    assert "editable when the job is ready" in p.settings_refusal(_options(writing))
    back = p.moved(writing, stage=p.READING)          # read again after a set-aside database: still begun
    assert back["writing_started"] is True and p.settings_refusal(_options(back))
    assert p.settings_refusal(_options(p.ready(writing))) is None


def test_moving_the_record_merges_progress_and_ready_has_no_stage():
    record = p.moved(_record(), progress={"read": 3, "databases": 10}, now=NOW)
    record = p.moved(record, progress={"read": 5}, now=NOW)
    assert record["progress"] == {"read": 5, "databases": 10} and record["updated_at"] == NOW.isoformat()
    done = p.ready(record, kept_paused="no Can Manage Run", now=NOW)
    assert done["state"] == p.READY and done["stage"] is None and done["ready_at"] == NOW.isoformat()
    assert done["kept_paused"] == "no Can Manage Run"
    with pytest.raises(ValueError):
        p.moved(record, state="running")
    with pytest.raises(ValueError):
        p.moved(record, stage="loading")


def test_the_scope_is_kept_while_preparing():
    assert p.scope_changed({"secret_scope": "a", "parallelism": "8"}, {"secret_scope": "a", "parallelism": "16"}) == []
    changed = p.scope_changed({"secret_scope": "a"}, {"secret_scope": "b", "trust_server_certificate": "true"})
    assert changed == ["secret_scope", "trust_server_certificate"]
    assert "secret_scope, trust_server_certificate are kept" in p.scope_refusal(_options(_record()), changed)
    assert p.scope_refusal(_options(_record()), []) is None
    assert p.scope_refusal({}, changed) is None


def _unreadable(database, server="SQL_A"):
    return p.entry(server, database, p.UNREADABLE, "Login failed for user", now=NOW)


def _clash(database, server="sql_a"):
    return p.entry(server, database, p.CLASH, "another table already writes main.bronze.orders",
                   target="main.bronze.orders", job="Sales nightly", now=NOW)


def test_a_database_set_aside_waits_for_a_choice_and_the_job_needs_you():
    record = p.with_set_aside(p.moved(_record(), stage=p.WRITING), [_unreadable("eng_1"), _clash("eng_2")], now=NOW)
    options = _options(record)
    assert p.state_of(options) == p.NEEDS_YOU and p.stage_of(options) == p.WRITING
    assert p.words(options) == "needs you: 2 databases set aside"
    assert p.run_refusal(options).startswith("the job isn't ready yet (needs you: 2 databases set aside)")
    assert p.status_cell(options) == {"label": "Needs you", "tone": "warn", "cause": None,
                                      "detail": ["2 databases set aside"]}
    key = p.database_key("sql_a", "ENG_1")
    assert key in record["set_aside"] and record["set_aside"][key]["why"] == "Login failed for user"


def test_one_the_login_cant_open_in_a_whole_server_choice_is_only_left_out_and_listed():
    record = p.with_set_aside(_record(), [p.entry("sql_a", "hr", p.CLOSED, "no access", now=NOW)])
    assert p.state_of(_options(record)) == p.PREPARING
    assert p.view(_options(record))["counts"]["closed"] == 1


def test_read_them_again_marks_only_the_unreadable_and_the_job_prepares_again():
    record = p.with_set_aside(p.moved(_record(), stage=p.WRITING), [_unreadable("eng_1"), _unreadable("eng_3")])
    once, marked = p.read_again(record, [p.database_key("sql_a", "eng_1")], "bob", now=NOW)
    assert marked == 1 and p.state_of(_options(once)) == p.NEEDS_YOU
    assert once["set_aside"][p.database_key("sql_a", "eng_1")]["again"] == {"by": "bob", "at": NOW.isoformat()}
    every, marked = p.read_again(record, None, "bob", now=NOW)
    assert marked == 2 and p.state_of(_options(every)) == p.PREPARING and p.stage_of(_options(every)) == p.READING
    clash = p.with_set_aside(_record(), [_clash("eng_2")])
    assert p.read_again(clash, None, "bob")[1] == 0


def test_leave_them_out_takes_them_out_of_the_choice_and_the_job_goes_on():
    record = p.with_set_aside(_record(), [_unreadable("eng_1"), _clash("eng_2")])
    out, left = p.leave_out(record, [p.database_key("sql_a", "eng_2"), p.database_key("sql_a", "nope")], "bob", now=NOW)
    assert left == 1 and p.state_of(_options(out)) == p.NEEDS_YOU
    assert p.left_out_keys(_options(out)) == {p.database_key("sql_a", "eng_2")}
    assert out["left_out"][p.database_key("sql_a", "eng_2")]["left_out"] == {"by": "bob", "at": NOW.isoformat()}
    out, left = p.leave_out(out, None, "bob")
    assert left == 1 and p.state_of(_options(out)) == p.PREPARING and out["set_aside"] == {}
    again = p.with_set_aside(out, [_unreadable("eng_1")])      # left out, it stays out
    assert again["set_aside"] == {}


def test_change_the_target_names_changes_only_the_templates_and_marks_the_clashes_to_plan_again():
    record = p.with_set_aside(p.moved(_record(), stage=p.WRITING), [_clash("eng_2"), _unreadable("eng_1")])
    out, marked = p.change_targets(record, {"sink_schema": "bronze_{server}", "load_type": "full"}, "bob", now=NOW)
    assert marked == 1 and out["settings"] == {**SETTINGS, "sink_schema": "bronze_{server}"}
    assert out["set_aside"][p.database_key("sql_a", "eng_2")]["replan"]["by"] == "bob"
    assert p.state_of(_options(out)) == p.NEEDS_YOU          # the unreadable one still waits
    out, _ = p.leave_out(out, None, "bob")
    assert p.state_of(_options(out)) == p.PREPARING and p.stage_of(_options(out)) == p.WRITING


def test_the_prepare_task_clears_a_database_once_it_is_taken():
    record = p.with_set_aside(_record(), [_unreadable("eng_1")])
    record, _ = p.read_again(record, None, "bob")
    record = p.cleared(record, [p.database_key("sql_a", "eng_1")])
    assert record["set_aside"] == {} and p.state_of(_options(record)) == p.PREPARING


def test_a_ready_job_stays_ready_whatever_is_set_aside():
    record = p.with_set_aside(p.ready(_record()), [_unreadable("eng_1")])
    assert p.state_of(_options(record)) == p.READY


def test_a_rediscovery_leaves_out_the_databases_the_job_left_out():
    record, _ = p.leave_out(p.with_set_aside(_record(), [_unreadable("Eng_1")]), None, "bob")
    rows = [{"server": "sql_a", "src_database": "eng_1"}, {"server": "sql_a:1433", "src_database": "ENG_1"},
            {"server": "sql_a", "src_database": "eng_2"}]
    assert p.without_left_out(rows, _options(record)) == [{"server": "sql_a", "src_database": "eng_2"}]
    assert p.without_left_out(rows, {}) == rows


def test_the_minutes_left_wait_for_twenty_databases_or_thirty_seconds():
    assert p.minutes_left(left=900, finished=19, finished_recently=19, elapsed_s=29) is None
    assert p.minutes_left(left=900, finished=20, finished_recently=20, elapsed_s=10) is not None
    assert p.minutes_left(left=900, finished=5, finished_recently=5, elapsed_s=30) is not None
    assert p.minutes_left(left=0, finished=100, finished_recently=10, elapsed_s=100) is None
    assert p.minutes_left(left=10, finished=0, finished_recently=0, elapsed_s=300) is None


def test_the_minutes_left_are_the_databases_left_over_the_recent_rate_blended_and_rounded():
    # 600 finished over 10 minutes (1 a second), 120 of them in the last 2 minutes (1 a second): 1,200 left is 20 min.
    assert p.minutes_left(left=1200, finished=600, finished_recently=120, elapsed_s=600) == 20
    # The source slowed to half its pace in the last 2 minutes: the recent rate weighs more, so 32 min, about 30.
    assert p.minutes_left(left=1200, finished=600, finished_recently=60, elapsed_s=600) == 30
    # Shorter than the window, the reading so far is the window.
    assert p.minutes_left(left=60, finished=30, finished_recently=30, elapsed_s=60) == 2
    # Stalled for the whole window: only the rate since the start counts, at its weight.
    assert p.minutes_left(left=120, finished=600, finished_recently=0, elapsed_s=600) == 8


def test_minutes_are_rounded_more_coarsely_the_more_there_are():
    assert [p.rounded_minutes(m) for m in (0.2, 1.4, 6.6, 14.4)] == [1, 1, 7, 14]
    assert [p.rounded_minutes(m) for m in (15, 17.4, 17.6, 59)] == [15, 15, 20, 60]
    assert [p.rounded_minutes(m) for m in (60, 64, 66, 125)] == [60, 60, 70, 130]


def test_the_page_view_counts_the_reads_and_lists_what_is_set_aside():
    reads = {"databases": 3000, "pending": 1760, "done": 1238, "failed": 2, "tables": 58280, "recent": 240,
             "elapsed_s": 600.0}
    record = p.with_set_aside(_record(), [_unreadable("eng_1"), _unreadable("eng_2")])
    shown = p.view(_options(record), reads)
    assert shown["state"] == p.NEEDS_YOU and shown["turn_on"] is True and shown["recorded"] is True
    assert shown["reading"] == {"databases": 3000, "read": 1240, "done": 1238, "failed": 2, "pending": 1760,
                                "tables": 58280, "percent": 41, "minutes_left": 15}
    assert shown["counts"] == {"set_aside": 2, "waiting": 2, "waiting_unreadable": 2, "waiting_clash": 0,
                               "unreadable": 2, "clash": 0, "closed": 0, "left_out": 0}
    assert shown["targets"] == {"sink_catalog": "main", "sink_schema": "bronze", "sink_table": "{table}"}
    assert [e["database"] for e in shown["set_aside"]] == ["eng_1", "eng_2"]
    assert p.view({}, reads)["reading"] is None and p.view({})["recorded"] is False


def test_the_view_lists_at_most_LISTED_databases_but_counts_them_all(monkeypatch):
    monkeypatch.setattr(p, "LISTED", 2)
    record = p.with_set_aside(_record(), [_unreadable(f"eng_{i}") for i in range(5)])
    shown = p.view(_options(record))
    assert len(shown["set_aside"]) == 2 and shown["counts"]["set_aside"] == 5


def test_the_jobs_list_says_preparing_with_its_stage_and_the_last_reported_progress():
    record = p.moved(_record(), progress={"percent": 41, "minutes_left": 12})
    assert p.status_cell(_options(record)) == {"label": "Preparing", "tone": "run", "cause": None,
                                                "detail": ["reading tables", "41%", "about 12 min left"]}
    assert p.status_cell(_options(_record()))["detail"] == ["reading tables"]


def test_percent_is_whole_and_reaches_100_only_when_all_is_done():
    assert (p.percent(0, 0), p.percent(1, 3), p.percent(2999, 3000), p.percent(3000, 3000)) == (0, 33, 99, 100)


def test_pause_while_preparing_keeps_the_schedule_off_once_ready():
    record = p.with_turn_on(_record(), False, "bob", now=NOW)
    assert record["turn_on"] is False and record["turn_on_by"] is None
    assert p.summary(_options(record)) == {"state": p.PREPARING, "stage": p.READING,
                                           "words": "preparing: reading tables", "turn_on": False, "waiting": 0}


def test_an_unknown_kind_of_set_aside_database_is_refused():
    with pytest.raises(ValueError):
        p.entry("sql_a", "eng_1", "lost", "why")
