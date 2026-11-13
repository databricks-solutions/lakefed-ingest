"""The check job's own records (queries.check_records_since_sql), which the job page reads to show step 1 finished while the
check's Databricks task still waits for its group runs: run against a real Postgres.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (a libpq URI to any database there) where
the test may create and drop its own database."""
import os
import sys
import uuid
from pathlib import Path

import pytest
import queries as q

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]
CHECK_STARTED = "2026-11-11 16:52:12+00"


@pytest.fixture(scope="module")
def pg():
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_app_check_records_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    conn = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=name), autocommit=True)
    try:
        for statement in state_store.split_sql_statements((REPO / "src/lakefed_ingest/lakebase_schema.sql").read_text()):
            conn.execute(statement)
        yield conn
    finally:
        conn.close()
        admin.execute(f"drop database {name}")
        admin.close()


def _record(conn, collection, work_id, status, started="2026-11-11 16:52:13+00", finished=None):
    conn.execute("insert into lakefed_ingest.ingest_run (task_collection, work_id, status, started_at, finished_at) "
                 "values (%s, %s, %s, %s, %s)", (collection, work_id, status, started, finished))


def _read(conn, groups):
    cur = conn.execute(q.check_records_since_sql(), (groups, CHECK_STARTED))
    names = [d.name for d in cur.description]
    return dict(zip(names, cur.fetchone()))


def test_it_counts_only_the_check_records_of_the_groups_since_the_check_started(pg):
    _record(pg, "g1", "check-111", "completed", finished="2026-11-11 16:52:15+00")
    _record(pg, "g1", "check-111", "completed", finished="2026-11-11 16:52:19+00")
    _record(pg, "g2", "check-111", "completed", finished="2026-11-11 16:52:17+00")
    _record(pg, "g1", "111:g1", "running")                                      # a group run's own record
    _record(pg, "g1", None, "completed", finished="2026-11-11 16:52:40+00")      # a run started by hand
    _record(pg, "g3", "check-111", "completed", finished="2026-11-11 16:52:30+00")  # another job's group
    _record(pg, "g1", "check-099", "completed", started="2026-11-11 16:00:00+00",
            finished="2026-11-11 16:00:09+00")                                  # an earlier check
    found = _read(pg, ["g1", "g2"])
    assert (found["records"], found["running"], found["failed"]) == (3, 0, 0)
    assert str(found["finished_at"]) == "2026-11-11 16:52:19+00:00"


def test_a_record_still_running_or_ended_any_way_but_completed_is_counted_apart(pg):
    for status in ("running", "failed", "cancelled", "skipped"):
        _record(pg, "g4", "check-222", status)
    found = _read(pg, ["g4"])
    assert (found["records"], found["running"], found["failed"]) == (4, 1, 3)


def test_a_check_with_no_record_yet_reads_as_none_finished(pg):
    found = _read(pg, ["g5"])
    assert (found["records"], found["running"], found["failed"], found["finished_at"]) == (0, 0, 0, None)
