"""A job's preparation record and the reads of its claimed drafts against a real Postgres with the Lakebase schema:
set_prepare_sql writes the record whole and keeps the job's other options, and job_reads_sql counts only the rows the
job's Create claimed, leaving out a dropped database, another job's rows and a listing's, with the reads finished in
the last window and how long the reading has gone; main._job_reads finds them from the job's selection.

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
