"""A job's Delete against a real Postgres: the app's dialog counts (queries.delete_groups_sql, delete_targets_sql) and
the delete job's transaction (delete_job.delete_rows), which must name the same groups and leave history alone.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server where the test may create and drop its own database."""
import os
import sys
import uuid
from pathlib import Path

import pytest
import queries as q

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def pg():
    psycopg = pytest.importorskip("psycopg")
    sys.path.insert(0, str(REPO / "src" / "lakefed_ingest"))
    import state_store
    name = f"lakefed_app_delete_{uuid.uuid4().hex[:8]}"
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


def _job(conn, name, groups, deleted=False):
    sid = conn.execute("insert into lakefed_ingest.schedule (name, cadence_minutes, deleted_at) values (%s, 60, "
                       + ("now()" if deleted else "null") + ") returning id", (name,)).fetchone()[0]
    for group in groups:
        conn.execute("insert into lakefed_ingest.schedule_member (schedule_id, task_collection, removed_at) values "
                     "(%s, %s, " + ("now()" if deleted else "null") + ")", (sid, group))
    return sid


def _row(conn, group, table, sink=None, client=None):
    """A control row; a target table shared by two rows is a consolidated one, a client_id each."""
    return conn.execute(
        "insert into lakefed_ingest.control (task_collection, src_host, src_port, secret_key, src_database, src_schema, "
        "src_table, sink_catalog, sink_schema, sink_table, primary_key, select_list, client_id) values "
        "(%s, 'h', 1433, 'k1', 'db', 'dbo', %s, 'm', 'l', %s, 'id', 'id', %s) returning id",
        (group, table, sink or f"{group}_{table}", client)).fetchone()[0]


def test_the_dialog_and_the_delete_job_agree_and_history_stays(pg):
    import delete_job
    sid = _job(pg, "Hourly", ["g1", "g2"])
    _job(pg, "Other", ["g2", "g3"])
    first = _row(pg, "g1", "a")
    _row(pg, "g1", "b", sink="FAN_IN", client="x")
    _row(pg, "g3", "c", sink="fan_in", client="y")
    pg.execute("insert into lakefed_ingest.checkpoint (control_id, src_database, src_schema, src_table, phase) values "
               "(%s, 'db', 'dbo', 'a', 'incremental')", (first,))
    groups = [r[0] for r in pg.execute(q.delete_groups_sql(), (sid, sid)).fetchall()]
    assert groups == ["g1"]                                         # g2 is in Other too
    tables, targets, kept, names = pg.execute(q.delete_targets_sql(), (groups, groups, groups)).fetchone()
    assert (tables, targets, kept, names) == (2, 1, 1, ["m.l.FAN_IN"])
    pg.execute("update lakefed_ingest.schedule set deleted_at = now() where id = %s", (sid,))
    pg.execute("update lakefed_ingest.schedule_member set removed_at = now() where schedule_id = %s", (sid,))
    with pg.cursor() as cur, cur.connection.transaction():
        result = delete_job.delete_rows(cur, sid)
    assert result["groups"] == ["g1"] and result["deleted"]["control"] == 2 and result["deleted"]["checkpoint"] == 1
    assert result["targets"] == {"drop": ["`m`.`l`.`g1_a`"], "kept": ["`m`.`l`.`FAN_IN`"]}
    left = pg.execute("select task_collection from lakefed_ingest.control order by 1").fetchall()
    assert left == [("g3",)]


def test_a_run_holding_a_groups_lock_refuses_the_delete_and_the_deletion_is_listed(pg):
    import psycopg
    import delete_job
    import state_store
    sid = _job(pg, "Hourly", ["g1"], deleted=True)
    _row(pg, "g1", "a")
    pg.execute(q.set_schedule_flag_sql(), (["delete_drop_targets"], True, sid))
    # The ingestion run's session lock, on a connection of its own, built from the address (a dsn has no password).
    run = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=pg.info.dbname), autocommit=True)
    try:
        run.execute(state_store.try_lock_collection_sql(), (state_store.collection_lock_key("g1"),))
        with pytest.raises(delete_job.RunGoing):
            with pg.cursor() as cur, cur.connection.transaction():
                delete_job.delete_rows(cur, sid)
    finally:
        run.close()
    assert pg.execute("select count(*) from lakefed_ingest.control").fetchone()[0] == 1
    assert [r[0] for r in pg.execute(q.deleted_jobs_sql(), (10, 0)).fetchall()] == [sid]    # not purged: still kept
    pg.execute(q.set_schedule_flag_sql(), (["delete_finished"], True, sid))
    assert pg.execute(q.deleted_jobs_sql(), (10, 0)).fetchall() == []                       # purged: off the list


def _draft_rows(conn, did, marks, started="now()"):
    conn.execute("insert into lakefed_ingest.discovery (discovery_id, src_database, status, listing, started_at) values "
                 f"(%s, 'eng_001', 'ok', %s::jsonb, {started}), (%s, 'eng_002', 'ok', %s::jsonb, {started})",
                 (did, marks, did, marks))
    conn.execute("insert into lakefed_ingest.discovered_table (discovery_id, src_database, src_schema, src_table) values "
                 "(%s, 'eng_001', 'dbo', 't1'), (%s, 'eng_002', 'dbo', 't1')", (did, did))


def test_the_purge_takes_discarded_and_left_drafts_and_never_one_a_job_took(pg):
    import delete_job
    import queries as q
    old = "now() - interval '3 hours'"
    _draft_rows(pg, "discarded", '{"draft": true, "touched_at": "2026-10-30T00:00:00+00:00", "discarded": "x"}')
    _draft_rows(pg, "left", '{"draft": true}', started=old)
    _draft_rows(pg, "fresh", '{"draft": true}')
    _draft_rows(pg, "taken", '{"job_id": 7}', started=old)
    assert pg.execute(q.drafts_to_purge_sql(), (7200,)).fetchall()      # the app sees one is due
    with pg.cursor() as cur, pg.transaction():
        purged = delete_job.purge_drafts(cur, 7200)
    assert purged == {"drafts": 2, "databases": 4, "tables": 4}
    left = pg.execute("select distinct discovery_id from lakefed_ingest.discovery order by 1").fetchall()
    assert [d for (d,) in left] == ["fresh", "taken"]
    tables = pg.execute("select distinct discovery_id from lakefed_ingest.discovered_table order by 1").fetchall()
    assert [d for (d,) in tables] == ["fresh", "taken"]
    assert pg.execute(q.drafts_to_purge_sql(), (7200,)).fetchall() == []


def test_removing_one_table_of_a_live_job_takes_only_its_state_and_keeps_its_target_and_job(pg):
    import delete_job
    gone = _row(pg, "g1", "a")
    kept = _row(pg, "g1", "b")
    sid = _job(pg, "Live", ["g1"])
    for control_id, table in ((gone, "a"), (kept, "b")):
        pg.execute("insert into lakefed_ingest.checkpoint (control_id, src_database, src_schema, src_table, phase) "
                   "values (%s, 'db', 'dbo', %s, 'incremental')", (control_id, table))
    pg.execute("insert into lakefed_ingest.reseed_queue (control_id, src_database, src_schema, src_table, reason) "
               "values (%s, 'db', 'dbo', 'a', 'retention_miss'), (%s, 'db', 'dbo', 'b', 'retention_miss')",
               (gone, kept))
    pg.execute("insert into lakefed_ingest.ingest_event (control_id, task_collection, src_database, src_schema, "
               "src_table, action, status) values (%s, 'g1', 'db', 'dbo', 'a', 'increment', 'ok')", (gone,))
    _roll_up(pg, ["g1"], [("g1", "db")])
    assert pg.execute("select control_rows, tables from lakefed_ingest.rollup_database "
                      "where task_collection = 'g1'").fetchall() == [(2, 2)]

    with pg.cursor() as cur, cur.connection.transaction():
        removed = delete_job.delete_table_rows(cur, sid, [gone])
    assert removed["control_ids"] == [gone] and removed["missing"] == []
    assert removed["deleted"] == {"reseed_queue": 1, "checkpoint": 1, "control": 1}
    assert removed["databases"] == 1
    assert removed["targets"] == {"drop": [], "kept": ["`m`.`l`.`g1_a`"]}

    assert pg.execute("select id from lakefed_ingest.control order by id").fetchall() == [(kept,)]
    assert pg.execute("select control_id from lakefed_ingest.checkpoint").fetchall() == [(kept,)]
    assert pg.execute("select control_id from lakefed_ingest.reseed_queue").fetchall() == [(kept,)]
    assert pg.execute("select count(*) from lakefed_ingest.ingest_event where control_id = %s",
                      (gone,)).fetchone() == (1,)
    assert pg.execute("select deleted_at from lakefed_ingest.schedule where id = %s", (sid,)).fetchone() == (None,)
    assert pg.execute("select count(*) from lakefed_ingest.schedule_member where schedule_id = %s "
                      "and removed_at is null", (sid,)).fetchone() == (1,)
    assert pg.execute("select control_rows, tables from lakefed_ingest.rollup_database "
                      "where task_collection = 'g1'").fetchall() == [(1, 1)]
    assert pg.execute("select tables from lakefed_ingest.rollup_group "
                      "where task_collection = 'g1'").fetchall() == [(1,)]


def test_a_run_holding_the_tables_group_refuses_its_removal_and_nothing_goes(pg):
    import psycopg
    import delete_job
    import state_store
    sid = _job(pg, "Live", ["g1"])
    gone = _row(pg, "g1", "a")
    run = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=pg.info.dbname), autocommit=True)
    try:
        run.execute(state_store.try_lock_collection_sql(), (state_store.collection_lock_key("g1"),))
        with pytest.raises(delete_job.RunGoing):
            with pg.cursor() as cur, cur.connection.transaction():
                delete_job.delete_table_rows(cur, sid, [gone])
    finally:
        run.close()
    assert pg.execute("select count(*) from lakefed_ingest.control").fetchone() == (1,)


def _roll_up(conn, groups, databases):
    """The monitoring rollups as the engine leaves them after a write: each database, then each group."""
    import state_store
    conn.execute(state_store.rollup_database_rewrite_sql(), ([g for g, _ in databases], [d for _, d in databases]))
    conn.execute(state_store.rollup_group_rewrite_sql(), (groups,))
