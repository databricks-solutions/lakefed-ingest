"""A job's preparation record and the reads of its claimed drafts against a real Postgres with the Lakebase schema:
set_prepare_sql writes the record whole and keeps the job's other options, and job_reads_sql counts only the rows the
job's Create claimed, leaving out a dropped database, another job's rows and a listing's, with the reads finished in
the last window and how long the reading has gone; main._job_reads finds them from the job's selection. Then the
prepare task's statements: its claim (once, by one instance at a time, again once its heartbeat is stale), heartbeat,
fence and end, the look for jobs to prepare again, the set-aside writes on the claimed rows (paged, read again, left
out, stopped and prepared again), and what Create checks by database key and a pass finds written. Create's own
transaction and a whole preparation run as the app's role in app_role_pg_test.py.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import main  # noqa: E402
import preparing as p  # noqa: E402
import queries as q  # noqa: E402

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def owner(monkeypatch):
    """A database of the test's own with the Lakebase schema, the app's Lakebase calls pointed at it (through the
    pool), and an owner connection for the setup and the asserts."""
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_prepare_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    target = psycopg.conninfo.make_conninfo(URI, dbname=name)
    monkeypatch.setattr(main.db, "connect", lambda: psycopg.connect(target, autocommit=True))
    main.db.close_idle()
    conn = psycopg.connect(target, autocommit=True)
    try:
        for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
            conn.execute(statement)
        yield conn
    finally:
        main.db.close_idle()
        conn.close()
        admin.execute(f"drop database {name} with (force)")
        admin.close()


def _job(conn, options=None) -> int:
    return conn.execute("insert into lakefed_ingest.schedule (name, cadence_minutes, options) values (%s, 60, %s::jsonb) "
                        "returning id", (f"job {uuid.uuid4().hex[:6]}", json.dumps(options or {}))).fetchone()[0]


def _options(conn, sid) -> dict:
    return conn.execute("select options from lakefed_ingest.schedule where id = %s", (sid,)).fetchone()[0]


def _row(conn, did, database, status, listing, tables=None, started="10 minutes", finished=None):
    conn.execute("insert into lakefed_ingest.discovery (discovery_id, src_type, src_connection, src_database, status, "
                 "tables, listing, started_at, finished_at) values (%s, 'sqlserver', 'sql_a', %s, %s, %s, %s::jsonb, "
                 "now() - %s::interval, case when %s::text is null then null else now() - %s::interval end)",
                 (did, database, status, tables, json.dumps(listing), started, finished, finished))


def test_the_record_is_written_whole_and_the_other_options_are_kept(owner):
    sid = _job(owner, {"rediscover_hour": 3, "target_names": "unity_catalog"})
    record = p.new_record("ann@example.com", {"load_type": "ct"}, {}, {}, {"kind": "discovery", "source_id": "d1"},
                          turn_on=True, state=p.PREPARING, stage=p.READING)
    owner.execute(q.set_prepare_sql(), (json.dumps(record), sid))
    options = _options(owner, sid)
    assert options["rediscover_hour"] == 3 and options["target_names"] == "unity_catalog"
    assert options[p.OPTION] == record
    moved = p.moved(record, stage=p.WRITING)
    owner.execute(q.set_prepare_sql(), (json.dumps(moved), sid))
    assert _options(owner, sid)[p.OPTION]["stage"] == p.WRITING
    empty = _job(owner)
    owner.execute("update lakefed_ingest.schedule set options = null where id = %s", (empty,))
    owner.execute(q.set_prepare_sql(), (json.dumps(record), empty))
    assert _options(owner, empty) == {p.OPTION: record}


def test_the_reads_count_only_the_rows_the_jobs_create_claimed(owner):
    sid = _job(owner)
    claimed = {"via": "warehouse", "job_id": sid}
    _row(owner, "d1", "eng_1", "ok", claimed, tables=47, finished="30 seconds")
    _row(owner, "d1", "eng_2", "ok", claimed, tables=40, finished="5 minutes")
    _row(owner, "d1", "eng_3", "failed", claimed, finished="20 seconds")
    _row(owner, "d1", "eng_4", "running", claimed)
    _row(owner, "d1", "eng_5", "running", {**claimed, "dropped": "2026-11-01T20:00:00+00:00"})
    _row(owner, "d1", "eng_6", "ok", {"via": "warehouse", "job_id": sid + 1}, tables=9, finished="10 seconds")
    _row(owner, "d1", "eng_7", "listed", claimed)
    _row(owner, "d2", "eng_8", "running", claimed, started="1 minute")
    cur = owner.execute(q.job_reads_sql(), (p.RATE_WINDOW_S, ["d1", "d2"], str(sid)))
    reads = dict(zip([d.name for d in cur.description], cur.fetchone()))
    assert {k: reads[k] for k in ("databases", "pending", "done", "failed", "tables", "recent")} == {
        "databases": 5, "pending": 2, "done": 2, "failed": 1, "tables": 87, "recent": 2}
    assert 590 <= reads["elapsed_s"] <= 700
    shown = p.reading_view(reads)
    assert (shown["databases"], shown["read"], shown["tables"]) == (5, 3, 87)


def test_the_jobs_reads_are_found_from_its_selection(owner):
    sid = _job(owner)
    _row(owner, "d1", "eng_1", "ok", {"via": "warehouse", "job_id": sid}, tables=47, finished="30 seconds")
    owner.execute("insert into lakefed_ingest.job_selection (job_id, scope, server, filter, source_kind, source_id) "
                  "values (%s, 'server', 'sql_a', '', 'discovery', 'd1')", (sid,))
    job = main._load_schedule(sid)[0]
    reads = main._job_reads(job)
    assert (reads["databases"], reads["done"], reads["tables"]) == (1, 1, 47)


# ---- the prepare task: its claim, its heartbeat, its fence, its end, and the look for jobs to prepare again ----------

def _task(conn, sid, status="running", progress=None, heartbeat="0 seconds", finished=None) -> int:
    return conn.execute("insert into lakefed_ingest.app_task (kind, request_id, job_id, status, progress, heartbeat_at, "
                        "finished_at) values ('prepare', %s, %s, %s, %s::jsonb, now() - %s::interval, "
                        "case when %s::text is null then null else now() - %s::interval end) returning id",
                        (uuid.uuid4().hex, sid, status, json.dumps(progress) if progress else None, heartbeat,
                         finished, finished)).fetchone()[0]


def _claim(conn, sid, owner, backoff=0):
    return conn.execute(q.claim_prepare_sql(), (owner, sid, 120, backoff)).fetchall()


def test_a_prepare_task_is_claimed_once_while_its_heartbeat_is_fresh_and_again_once_it_is_stale(owner):
    sid = _job(owner)
    task = _task(owner, sid)
    assert _claim(owner, sid, "a") == [(task,)]                      # Create just made it: no thread held it yet
    assert _claim(owner, sid, "b") == []                             # a's heartbeat is fresh
    assert owner.execute(q.prepare_beat_sql(), (task, "a")).fetchall() == [(task,)]
    owner.execute("update lakefed_ingest.app_task set heartbeat_at = now() - interval '1 hour' where id = %s", (task,))
    assert _claim(owner, sid, "b") == [(task,)]                      # a's thread is gone with its app
    assert owner.execute(q.prepare_beat_sql(), (task, "a")).fetchall() == []
    assert owner.execute(q.prepare_fence_sql(), (task, "a")).fetchall() == []
    assert owner.execute(q.prepare_fence_sql(), (task, "b")).fetchall() == [(task,)]
    owner.execute(q.finish_prepare_sql(), ("succeeded", None, sid, "a", "a"))     # not a's to end any more
    assert owner.execute("select status from lakefed_ingest.app_task where id = %s", (task,)).fetchone() == ("running",)
    owner.execute(q.finish_prepare_sql(), ("failed", "the Jobs API is down", sid, "b", "b"))
    assert _claim(owner, sid, "c", backoff=120) == []                # failed just now: the backoff holds it
    assert _claim(owner, sid, "c") == [(task,)]                      # a user's own ask has none
    owner.execute(q.finish_prepare_sql(), ("failed", "stopped by ann", sid, None, None))
    assert owner.execute("select status, error from lakefed_ingest.app_task where id = %s", (task,)).fetchone() == (
        "failed", "stopped by ann")


def test_two_instances_claiming_one_task_at_once_get_it_once(owner):
    """The claim's FOR UPDATE SKIP LOCKED: while one instance's claim is not committed, the other's skips the row, and
    once it is, finds the heartbeat fresh."""
    sid = _job(owner)
    task = _task(owner, sid)
    first = main.db.connect()
    second = main.db.connect()
    try:
        first.autocommit = False
        assert first.execute(q.claim_prepare_sql(), ("a", sid, 120, 0)).fetchall() == [(task,)]
        assert second.execute(q.claim_prepare_sql(), ("b", sid, 120, 0)).fetchall() == []
        first.commit()
        assert second.execute(q.claim_prepare_sql(), ("b", sid, 120, 0)).fetchall() == []
        assert owner.execute("select progress ->> 'owner' from lakefed_ingest.app_task where id = %s",
                             (task,)).fetchone() == ("a",)
    finally:
        first.close()
        second.close()


def test_the_look_for_jobs_to_prepare_again_finds_only_the_ones_no_thread_holds(owner):
    preparing = {p.OPTION: {"state": p.PREPARING, "stage": p.WRITING}}
    stale = _job(owner, preparing)
    _task(owner, stale, progress={"owner": "gone"}, heartbeat="1 hour")
    fresh = _job(owner, preparing)
    _task(owner, fresh, progress={"owner": "here"})
    failed = _job(owner, preparing)
    _task(owner, failed, status="failed", finished="1 hour")
    just_failed = _job(owner, preparing)
    _task(owner, just_failed, status="failed", finished="5 seconds")
    stopped = _job(owner, {p.OPTION: {"state": p.PREPARING, "stopped": {"by": "ann"}}})
    _task(owner, stopped, status="failed", finished="1 hour")
    needs_you = _job(owner, {p.OPTION: {"state": p.NEEDS_YOU}})
    _task(owner, needs_you, status="succeeded", finished="1 hour")
    deleted = _job(owner, preparing)
    _task(owner, deleted, progress={"owner": "gone"}, heartbeat="1 hour")
    owner.execute("update lakefed_ingest.schedule set deleted_at = now() where id = %s", (deleted,))
    found = [r[0] for r in owner.execute(q.preparing_jobs_sql(), (120, 120)).fetchall()]
    assert found == sorted([stale, failed])


def test_a_jobs_prepare_task_is_left_out_of_its_writes_list_its_stale_end_and_deletes_wait(owner):
    sid = _job(owner)
    _task(owner, sid, progress={"owner": "gone"}, heartbeat="1 hour")
    fresh = _task(owner, _job(owner), progress={"owner": "here"})
    owner.execute(q.stale_tasks_sql(), ("u", "r", "p", 120, None, sid))
    assert owner.execute("select status from lakefed_ingest.app_task where job_id = %s", (sid,)).fetchall() == [
        ("running",)]
    assert owner.execute(q.job_tasks_sql(), (sid,)).fetchall() == []
    job = owner.execute("select job_id from lakefed_ingest.app_task where id = %s", (fresh,)).fetchone()[0]
    assert owner.execute(q.running_task_sql(), (job, 120)).fetchall() == []


# ---- the reads of a job's claimed drafts: set aside, read again, left out, stopped, prepared again -------------------

def _claimed_rows(conn, sid):
    claimed = {"via": "warehouse", "job_id": sid}
    _row(conn, "d1", "eng_1", "ok", claimed, tables=47, finished="1 minute")
    _row(conn, "d1", "eng_2", "failed", claimed, finished="1 minute")
    _row(conn, "d1", "eng_3", "failed", {**claimed, "whole_server": True}, finished="1 minute")
    _row(conn, "d1", "eng_4", "failed", {**claimed, "dropped": "2026-11-01T20:00:00+00:00"}, finished="1 minute")
    _row(conn, "d1", "eng_5", "running", {**claimed, "statement_id": "st-5", "claimed_at": "2020-01-01T00:00:00+00:00"})
    _row(conn, "d1", "eng_6", "failed", {"via": "warehouse", "job_id": sid + 1}, finished="1 minute")
    conn.execute("update lakefed_ingest.discovery set error = 'Login failed' where status = 'failed'")


def _listing(conn, database) -> dict:
    return conn.execute("select listing from lakefed_ingest.discovery where discovery_id = 'd1' and src_database = %s",
                        (database,)).fetchone()[0]


def test_the_databases_that_couldnt_be_read_are_paged_from_the_jobs_claimed_rows(owner):
    sid = _job(owner)
    _claimed_rows(owner, sid)
    rows = owner.execute(q.job_failed_reads_sql(), (["d1"], str(sid), 1, 0)).fetchall()
    assert rows == [("sql_a", "eng_2", "Login failed", False, 2)]
    rows = owner.execute(q.job_failed_reads_sql(), (["d1"], str(sid), 10, 1)).fetchall()
    assert rows == [("sql_a", "eng_3", "Login failed", True, 2)]


def test_read_again_resets_only_the_named_failed_rows_for_the_reader(owner):
    sid = _job(owner)
    _claimed_rows(owner, sid)
    reset = owner.execute(q.prepare_read_again_sql(), (["d1"], str(sid), False, ["sql_a"], ["eng_2"])).fetchall()
    assert reset == [("sql_a", "eng_2")]
    status, error, listing = owner.execute(
        "select status, error, listing from lakefed_ingest.discovery where discovery_id = 'd1' "
        "and src_database = 'eng_2'").fetchone()
    assert (status, error, listing.get("statement_id"), listing.get("claimed_at")) == ("running", None, None, None)
    work = owner.execute(q.warehouse_read_work_sql(), (60, 6 * 3600, 7200)).fetchall()
    assert [row[0] for row in work] == ["d1"]
    every = owner.execute(q.prepare_read_again_sql(), (["d1"], str(sid), True, [], [])).fetchall()
    assert every == [("sql_a", "eng_3")]                     # not the dropped one, nor another job's


def test_leave_out_marks_the_rows_so_no_list_or_read_takes_them(owner):
    sid = _job(owner)
    _claimed_rows(owner, sid)
    left = owner.execute(q.prepare_leave_out_reads_sql(), (["d1"], str(sid), False, ["sql_a"], ["eng_2"])).fetchall()
    assert left == [("sql_a", "eng_2", "Login failed", False)] and "left_out" in _listing(owner, "eng_2")
    assert [r[1] for r in owner.execute(q.job_failed_reads_sql(), (["d1"], str(sid), 10, 0)).fetchall()] == ["eng_3"]
    assert owner.execute(q.prepare_read_again_sql(), (["d1"], str(sid), False, ["sql_a"], ["eng_2"])).fetchall() == []


def test_stop_discards_the_reads_going_and_prepare_again_takes_them_back(owner):
    sid = _job(owner)
    _claimed_rows(owner, sid)
    going = owner.execute(q.prepare_stop_reads_sql(), (["d1"], str(sid))).fetchall()
    assert going == [("st-5",)] and "discarded" in _listing(owner, "eng_5")
    assert owner.execute(q.warehouse_read_work_sql(), (60, 6 * 3600, 7200)).fetchall() == []
    owner.execute("update lakefed_ingest.discovery set status = 'failed', error = 'Statement was cancelled' "
                  "where discovery_id = 'd1' and src_database = 'eng_5'")          # its cancelled statement's answer
    assert owner.execute(q.prepare_resume_reads_sql(), (["d1"], str(sid))).fetchall() == [(1,)]
    listing = _listing(owner, "eng_5")
    assert "discarded" not in listing and "statement_id" not in listing
    assert owner.execute("select status from lakefed_ingest.discovery where discovery_id = 'd1' "
                         "and src_database = 'eng_5'").fetchone() == ("running",)


# ---- what Create checks by name, and what a pass finds written --------------------------------------------------------

def _control(conn, group, database, host="h"):
    """A control row of a database on a host (as delete_job_pg_test writes them), with a target of its own."""
    conn.execute("insert into lakefed_ingest.control (task_collection, src_host, src_port, secret_key, src_database, "
                 "src_schema, src_table, sink_catalog, sink_schema, sink_table, primary_key, select_list) values "
                 "(%s, %s, 1433, 'k1', %s, 'dbo', 't', 'm', 'l', %s, 'id', 'id')",
                 (group, host, database, f"t_{uuid.uuid4().hex[:8]}"))


def test_create_finds_a_database_another_job_holds_by_its_key_alone(owner):
    live, gone, purged = _job(owner), _job(owner), _job(owner, {"delete_finished": True})
    for sid, group, database in ((live, "g_live", "eng_1"), (gone, "g_gone", "eng_2"), (purged, "g_purged", "eng_3")):
        owner.execute("insert into lakefed_ingest.schedule_member (schedule_id, task_collection) values (%s, %s)",
                      (sid, group))
        _control(owner, group, database)
    owner.execute("update lakefed_ingest.schedule set deleted_at = now() where id = any(%s)", ([gone, purged],))
    owner.execute("update lakefed_ingest.schedule_member set removed_at = now() where schedule_id = any(%s)",
                  ([gone, purged],))
    key = lambda database: q.database_key({"src_type": "sqlserver", "src_host": "h", "src_port": 1433,
                                            "secret_key": "k1", "src_database": database})
    found = owner.execute(q.database_owners_sql(), ([key("eng_1")],)).fetchall()
    assert [(r[0], r[2]) for r in found] == [("eng_1", False)]
    assert [(r[0], r[2]) for r in owner.execute(q.database_owners_sql(), ([key("eng_2")],)).fetchall()] == [
        ("eng_2", True)]
    assert owner.execute(q.database_owners_sql(), ([key("eng_3"), key("eng_9")],)).fetchall() == []


def test_a_pass_finds_the_databases_the_job_has_written_with_their_groups(owner):
    _control(owner, "g1", "Eng_1")
    _control(owner, "g1", "Eng_1")
    _control(owner, "g2", "eng_2", host="H")
    _control(owner, "other", "eng_3")
    rows = owner.execute(q.job_database_groups_sql(), (["g1", "g2"],)).fetchall()
    assert sorted(rows) == [("h", "eng_1", "g1"), ("h", "eng_2", "g2")]


def test_an_uploads_databases_are_read_one_line_each(owner):
    for line, database, table in ((2, "eng_1", "a"), (3, "eng_1", "b"), (4, "eng_2", "a")):
        owner.execute("insert into lakefed_ingest.table_upload_line (upload_id, line, server, src_database, src_schema, "
                      "src_table, fields, status) values ('u1', %s, 'sql_a', %s, 'dbo', %s, %s::jsonb, 'ok')",
                      (line, database, table, json.dumps({"src_connection": "sql_a", "line": line})))
    rows = owner.execute(q.upload_database_sources_sql(), ("u1",)).fetchall()
    assert [(r[1], r[2]["line"]) for r in rows] == [("eng_1", 2), ("eng_2", 4)]
