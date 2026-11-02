"""A deleted job's Restore and its refusals against a real Postgres: the restore statements
(queries.restore_members_sql,
restore_schedule_sql), the checks
a Restore makes (live_name_taken_sql, groups_held_elsewhere_sql, restore_conflicts_sql),
the owner of a new table's database (deleted_database_owners_sql), and the Deleted jobs list (deleted_jobs_sql,
deleted_jobs_count_sql, deleted_groups_sql). The fixture and the control row come from delete_job_pg_test.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server where the test may create and drop its own database."""
import os

import pytest

import queries as q
from delete_job_pg_test import _job, _row, pg  # noqa: F401  (pg is the fixture)

pytestmark = pytest.mark.skipif(not os.environ.get("LAKEFED_APP_TEST_PG"), reason="LAKEFED_APP_TEST_PG not set")
SAME_SOURCE = {"src_type": "sqlserver", "src_host": "h", "src_port": 1433, "src_connection": None, "secret_key": "k1",
               "src_database": "db"}


def test_a_restore_puts_back_the_members_its_delete_removed_and_leaves_the_others_removed(pg):
    sid = _deleted(pg, "Hourly", ["g1", "g2"], earlier=["g3"])
    with pg.transaction():
        pg.execute(q.restore_members_sql(), (sid, sid))
        assert pg.execute(q.restore_schedule_sql(), (sid,)).rowcount == 1
    members = dict(pg.execute("select task_collection, removed_at is null from lakefed_ingest.schedule_member "
                              "where schedule_id = %s", (sid,)).fetchall())
    assert members == {"g1": True, "g2": True, "g3": False}
    assert pg.execute("select deleted_at, paused from lakefed_ingest.schedule where id = %s", (sid,)).fetchone() == (
        None, True)


def test_a_purged_job_matches_no_restore_and_leaves_the_kept_list(pg):
    sid = _deleted(pg, "Hourly", ["g1"])
    assert _kept_ids(pg) == [sid]
    pg.execute(q.set_schedule_flag_sql(), (["delete_finished"], True, sid))
    assert pg.execute(q.restore_schedule_sql(), (sid,)).rowcount == 0
    assert pg.execute(q.deleted_jobs_count_sql()).fetchone()[0] == 0
    assert _kept_ids(pg) == []


def test_the_kept_list_is_newest_deletion_first_and_pages_and_counts_only_what_is_kept(pg):
    older = _deleted(pg, "Older", ["g1"], when="now() - interval '3 days'")
    newer = _deleted(pg, "Newer", ["g2"], when="now() - interval '1 day'")
    purged = _deleted(pg, "Purged", ["g3"], when="now() - interval '2 days'")
    pg.execute(q.set_schedule_flag_sql(), (["delete_finished"], True, purged))
    assert _kept_ids(pg) == [newer, older]
    assert [row[0] for row in pg.execute(q.deleted_jobs_sql(), (1, 1)).fetchall()] == [older]
    assert pg.execute(q.deleted_jobs_count_sql()).fetchone()[0] == 2


def test_the_deleted_groups_are_the_kept_jobs_groups_that_no_live_job_holds(pg):
    _deleted(pg, "Gone", ["g1", "g2"])
    _job(pg, "Live", ["g2"])
    assert [row[0] for row in pg.execute(q.deleted_groups_sql()).fetchall()] == ["g1"]


def test_a_live_job_with_the_name_refuses_the_restore(pg):
    sid = _deleted(pg, "Hourly", ["g1"])
    live = _job(pg, "HOURLY", ["g8"])
    assert pg.execute(q.live_name_taken_sql(), ("Hourly", sid)).fetchall() == [(live,)]
    assert pg.execute(q.live_name_taken_sql(), ("HOURLY", live)).fetchall() == []


def test_a_group_a_live_job_holds_is_named_and_refuses_the_restore(pg):
    sid = _deleted(pg, "Hourly", ["g1", "g2"])
    _job(pg, "Holder", ["g1"])
    assert pg.execute(q.groups_held_elsewhere_sql(), (["g1", "g2"], sid)).fetchall() == [("g1", "Holder")]


def test_a_live_job_sharing_a_database_or_a_target_table_refuses_the_restore(pg):
    _deleted(pg, "Hourly", ["g1"])
    _row(pg, "g1", "a")                                       # database db, sink m.l.g1_a
    _job(pg, "Other", ["g9"])
    _row(pg, "g9", "x", sink="g1_a", client="c9")             # the same database and the same sink table
    assert pg.execute(q.restore_conflicts_sql(), (["g1"], ["g1"])).fetchall() == [
        ("database", "db", "Other"), ("target", "m.l.g1_a", "Other")]


def test_a_new_table_in_a_database_a_kept_job_still_reads_is_refused_as_its_owner(pg):
    sid = _deleted(pg, "Hourly", ["g1"])
    _row(pg, "g1", "a")
    keys = [q.database_key(SAME_SOURCE)]
    assert pg.execute(q.deleted_database_owners_sql(), (keys,)).fetchall() == [("db", "Hourly", sid)]
    pg.execute(q.set_schedule_flag_sql(), (["delete_finished"], True, sid))
    assert pg.execute(q.deleted_database_owners_sql(), (keys,)).fetchall() == []


def _deleted(conn, name, groups, earlier=(), when="now()"):
    """A job deleted at ``when``, its groups' members removed at that same instant (one transaction, so the Restore
    can tell them from earlier removals), and the ``earlier`` groups removed two days before."""
    with conn.transaction():
        sid = conn.execute("insert into lakefed_ingest.schedule (name, cadence_minutes, deleted_at) values (%s, 60, "
                           f"{when}) returning id", (name,)).fetchone()[0]
        for group in groups:
            conn.execute("insert into lakefed_ingest.schedule_member (schedule_id, task_collection, removed_at) "
                         f"values (%s, %s, {when})", (sid, group))
        for group in earlier:
            conn.execute("insert into lakefed_ingest.schedule_member (schedule_id, task_collection, removed_at) "
                         "values (%s, %s, now() - interval '2 days')", (sid, group))
    return sid


def _kept_ids(conn):
    return [row[0] for row in conn.execute(q.deleted_jobs_sql(), (100, 0)).fetchall()]
