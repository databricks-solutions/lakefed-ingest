"""StateStore statements run against a real Postgres, where parameters are typed by the server: a throwaway
database with the Lakebase schema.

Skipped unless LAKEFED_TEST_PG names a Postgres server (a libpq URI to any database there) where the test may
create and drop its own database, for example a local Postgres."""
import json
import os
import uuid
from pathlib import Path

import pytest
import state_store as ss

URI = os.environ.get("LAKEFED_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_TEST_PG not set")
SCHEMA = Path(__file__).resolve().parents[1] / "src" / "lakefed_ingest" / "lakebase_schema.sql"


@pytest.fixture
def conn():
    psycopg = pytest.importorskip("psycopg")
    name = f"lakefed_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    db = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=name), autocommit=True)
    try:
        for statement in ss.split_sql_statements(SCHEMA.read_text()):
            db.execute(statement)
        yield db
    finally:
        db.close()
        admin.execute(f"drop database {name}")
        admin.close()


def test_a_run_closes_its_collections_abandoned_runs_and_their_open_events(conn):
    """The run holding the collection's lock closes its other open runs: the statement is prepared by the server,
    so every parameter must have a type Postgres can work out."""
    conn.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, status) values "
                 "(1, 'c', 'running'), (2, 'c', 'running'), (3, 'other', 'running')")
    conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_table, status, "
                 "error) values (1, 'c', 10, 't10', 'running', null), (1, 'c', 11, 't11', 'retried', 'timed out'), "
                 "(1, 'c', 12, 't12', 'ok', null)")
    store = ss.StateStore(conn)
    assert store.close_abandoned_runs("c", 2) == (1, 2)
    runs = dict(conn.execute("select run_id, status from lakefed_ingest.ingest_run order by run_id").fetchall())
    assert runs == {1: "failed", 2: "running", 3: "running"}
    detail = conn.execute("select detail from lakefed_ingest.ingest_run where run_id = 1").fetchone()[0]
    assert json.loads(detail)["closed_by_run"] == 2
    events = {t: (s, e) for t, s, e in conn.execute(
        "select src_table, status, error from lakefed_ingest.ingest_event order by src_table")}
    assert events["t10"] == ("failed", f"{ss.ABANDONED_RUN_REASON}; closed by run 2")
    assert events["t11"] == ("failed", f"timed out | {ss.ABANDONED_RUN_REASON}; closed by run 2")
    assert events["t12"] == ("ok", None)


@pytest.fixture
def sessions():
    """A throwaway database with the Lakebase schema, and a way to open more sessions on it, as runs are."""
    psycopg = pytest.importorskip("psycopg")
    name = f"lakefed_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    target = psycopg.conninfo.make_conninfo(URI, dbname=name)
    opened = []

    def session():
        opened.append(psycopg.connect(target, autocommit=True))
        return opened[-1]
    try:
        for statement in ss.split_sql_statements(SCHEMA.read_text()):
            session().execute(statement)
        yield session
    finally:
        for c in opened:
            c.close()
        admin.execute(f"drop database {name}")
        admin.close()


def test_work_list_runs_share_a_collection_and_anything_needing_it_alone_is_refused(sessions):
    first, second, edit = (ss.StateStore(sessions()) for _ in range(3))
    assert first.try_lock_collection_shared("g1") and second.try_lock_collection_shared("g1")
    assert edit.try_lock_collection("g1") is False                       # a whole run, the check
    xact = sessions()
    assert xact.execute("select pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                        (ss.collection_lock_key("g1"),)).fetchone()[0] is False      # the app's edits and Delete
    first.unlock_collection_shared("g1")
    second.unlock_collection_shared("g1")
    assert edit.try_lock_collection("g1") is True
    assert first.try_lock_collection_shared("g1") is False               # and the other way round


def test_two_runs_given_one_database_take_it_once_and_the_second_skips_it(sessions):
    first, second = ss.StateStore(sessions()), ss.StateStore(sessions())
    keys = [ss.database_lock_key("g1", "srv", name) for name in ("db1", "db2")]
    assert first.try_lock_databases(keys) == set(keys)
    assert second.try_lock_databases(keys + [ss.database_lock_key("g1", "srv", "db3")]) == \
        {ss.database_lock_key("g1", "srv", "db3")}
    first.unlock_databases(keys)
    assert second.try_lock_databases(keys) == set(keys)


def test_a_run_closes_abandoned_runs_only_when_no_other_run_holds_the_collection(sessions):
    first, second = ss.StateStore(sessions()), ss.StateStore(sessions())
    assert first.try_lock_collection_shared("g1") and second.try_lock_collection_shared("g1")
    assert first.alone_on_collection("g1") is False
    second.unlock_collection_shared("g1")
    assert first.alone_on_collection("g1") is True
    assert second.try_lock_collection_shared("g1") is True               # the exclusive try was let go at once


def test_two_runs_recounting_one_group_at_once_leave_it_exact(sessions):
    """Each run writes its own database's rows and recounts the group; the second recount waits for the first to
    commit, so the group counts both databases."""
    import threading
    setup = sessions()
    setup.execute("insert into lakefed_ingest.control (task_collection, src_host, src_database, src_schema, src_table, "
                  "sink_catalog, sink_schema, sink_table, primary_key, select_list) values "
                  "('g1', 'host-a', 'db1', 'dbo', 't', 'main', 's', 'db1_t', 'id', 'id'), "
                  "('g1', 'host-a', 'db2', 'dbo', 't', 'main', 's', 'db2_t', 'id', 'id')")
    stores = [ss.StateStore(sessions()) for _ in range(2)]
    barrier = threading.Barrier(2)

    def recount(store, database):
        barrier.wait()
        store.rewrite_rollups("g1", [database])
    threads = [threading.Thread(target=recount, args=(s, d)) for s, d in zip(stores, ("db1", "db2"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert setup.execute("select databases, tables from lakefed_ingest.rollup_group where task_collection = 'g1'"
                         ).fetchone() == (2, 2)


def test_a_work_list_spanning_collections_reads_back_whole_and_per_collection(sessions):
    """A run of a cycle's share: its work list is recorded per collection under one work id, and the run reads it
    whole (its parts) while each part reads its own collection's databases."""
    store = ss.StateStore(sessions())
    store.record_check_work("L:run1", "g1", [("srv", "a"), ("srv", "b")])
    store.record_check_work("L:run1", "g2", [("srv", "c")])
    store.record_check_work("L:run2", "g1", [("srv", "d")])
    assert store.get_check_work_all("L:run1") == {("g1", "srv", "a"), ("g1", "srv", "b"), ("g2", "srv", "c")}
    assert store.get_check_work("L:run1", "g2") == {("srv", "c")}


def test_a_check_and_a_run_of_one_collection_go_at_once_on_different_databases(sessions):
    """The check (shared, groups B) and a work-list run both hold the collection; each has its own databases, and a
    whole-collection run is refused while either goes."""
    check, run, whole = (ss.StateStore(sessions()) for _ in range(3))
    assert check.try_lock_collection_shared("g1") and run.try_lock_collection_shared("g1")
    assert run.try_lock_databases([ss.database_lock_key("g1", "srv", "a")])
    held = check.try_lock_databases([ss.database_lock_key("g1", "srv", n) for n in ("a", "b")])
    assert held == {ss.database_lock_key("g1", "srv", "b")}           # the check leaves the run's database out
    assert whole.try_lock_collection("g1") is False


def test_a_failed_attempt_records_how_it_was_classified(conn):
    """The classification columns exist once the schema applies, and record_event_finish writes them."""
    conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_table, status) "
                 "values (1, 'c', 10, 't10', 'running')")
    event_id = conn.execute("select id from lakefed_ingest.ingest_event").fetchone()[0]
    ss.StateStore(conn).record_event_finish(event_id, "failed", stage="read", error="x", error_kind="transient",
                                            error_class="FAILED_JDBC.CONNECTION")
    assert conn.execute("select error_kind, error_class from lakefed_ingest.ingest_event where id = %s",
                        (event_id,)).fetchone() == ("transient", "FAILED_JDBC.CONNECTION")


def test_stopping_run_is_true_for_a_cancelled_run_or_one_asked_to_stop_and_never_fails_on_a_non_numeric_job_run(conn):
    """The cancel rules of run_is_stopping, in one statement: the run's own status, or its Databricks run (matched on
    job_run_id) asked to stop, terminating or ended CANCELED. A job_run_id that is not a number matches nothing."""
    conn.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, job_run_id, status) values "
                 "(1, 'c', '101', 'running'), (2, 'c', '102', 'running'), (3, 'c', 'not-a-number', 'running'), "
                 "(4, 'c', '104', 'cancelled'), (5, 'c', '105', 'running'), (6, 'c', '106', 'running')")
    conn.execute("insert into lakefed_ingest.databricks_run (run_id, job_id, kind, life_cycle_state, result_state, "
                 "cancel_requested_at) values (102, 1, 'group', 'RUNNING', null, now()), "
                 "(105, 1, 'group', 'TERMINATED', 'CANCELED', null), (106, 1, 'group', 'TERMINATING', null, null)")
    store = ss.StateStore(conn)
    got = {run_id: store.stopping_run(run_id) for run_id in (1, 2, 3, 4, 5, 6)}
    assert got == {1: False, 2: True, 3: False, 4: True, 5: True, 6: True}


def test_a_failed_table_in_a_cancelled_run_is_a_cancelled_event_and_keeps_its_checkpoint(conn):
    """A cancel is not a failure: its tables are cancelled events and their checkpoints keep their status. A table
    that fails in a run that is not stopping is failed, and only it gets its checkpoint marked failed."""
    conn.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, job_run_id, status) values "
                 "(7, 'c', '701', 'running'), (8, 'c', '801', 'running')")
    conn.execute("insert into lakefed_ingest.databricks_run (run_id, job_id, kind, life_cycle_state, "
                 "cancel_requested_at) values (701, 1, 'group', 'RUNNING', now())")
    conn.execute("insert into lakefed_ingest.checkpoint (control_id, src_database, src_schema, src_table, strategy, "
                 "status) values (21, 'd', 's', 't21', 'ct', 'ok'), (22, 'd', 's', 't22', 'ct', 'ok')")
    store = ss.StateStore(conn)
    store.record_failed_tables(7, [(_cfg(21), {"phase": "incremental"}, "connection reset")])
    store.record_failed_tables(8, [(_cfg(22), None, "[SPARK_JOB_CANCELLED] Job 9 cancelled"),
                                   (_cfg(23), None, "login failed")])
    events = dict(conn.execute(
        "select control_id, status from lakefed_ingest.ingest_event order by control_id").fetchall())
    assert events == {21: "cancelled", 22: "cancelled", 23: "failed"}
    checkpoints = dict(conn.execute("select control_id, status from lakefed_ingest.checkpoint "
                                    "order by control_id").fetchall())
    assert checkpoints == {21: "ok", 22: "ok", 23: "failed"}


def test_a_sweep_closes_a_cancelled_run_as_cancelled_and_any_other_dead_run_as_failed(conn):
    """The run holding the lock closes the other open runs: a run whose Databricks run was cancelled closes as
    cancelled with its events, a run whose Databricks run failed or is unknown closes as failed."""
    conn.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, job_run_id, status) values "
                 "(1, 'c', '201', 'running'), (2, 'c', '202', 'running'), (3, 'c', '203', 'running'), "
                 "(4, 'c', '204', 'running')")
    conn.execute("insert into lakefed_ingest.databricks_run (run_id, job_id, kind, life_cycle_state, result_state, "
                 "cancel_requested_at) values (201, 1, 'group', 'TERMINATED', 'CANCELED', null), "
                 "(202, 1, 'group', 'RUNNING', null, now()), (203, 1, 'group', 'INTERNAL_ERROR', 'FAILED', null)")
    conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_table, status) "
                 "values (1, 'c', 10, 't10', 'running'), (2, 'c', 11, 't11', 'running'), "
                 "(3, 'c', 12, 't12', 'running')")
    store = ss.StateStore(conn)
    assert store.close_abandoned_runs("c", 4) == (3, 3)
    runs = dict(conn.execute("select run_id, status from lakefed_ingest.ingest_run order by run_id").fetchall())
    assert runs == {1: "cancelled", 2: "cancelled", 3: "failed", 4: "running"}
    events = dict(conn.execute(
        "select src_table, status from lakefed_ingest.ingest_event order by src_table").fetchall())
    assert events == {"t10": "cancelled", "t11": "cancelled", "t12": "failed"}


def test_a_late_run_finish_does_not_overwrite_a_cancelled_run(conn):
    """A run cancelled while its engine was still going keeps its status when the engine finishes late; a run still
    running finishes as usual."""
    conn.execute("insert into lakefed_ingest.ingest_run (run_id, task_collection, status, finished_at) values "
                 "(1, 'c', 'cancelled', now()), (2, 'c', 'running', null)")
    store = ss.StateStore(conn)
    store.finish_run(1, total=3, ok=3, failed=0, status="completed")
    store.finish_run(2, total=3, ok=3, failed=0, status="completed")
    runs = dict(conn.execute("select run_id, status from lakefed_ingest.ingest_run order by run_id").fetchall())
    assert runs == {1: "cancelled", 2: "completed"}


def test_a_late_event_finish_does_not_overwrite_a_cancelled_event(conn):
    """A table the cancel marked keeps that status when its engine's finish arrives late; a table still running
    finishes as usual."""
    conn.execute("insert into lakefed_ingest.ingest_event (run_id, task_collection, control_id, src_table, status) "
                 "values (1, 'c', 10, 't10', 'cancelled'), (1, 'c', 11, 't11', 'running')")
    event_ids = {src_table: event_id for event_id, src_table in conn.execute(
        "select id, src_table from lakefed_ingest.ingest_event").fetchall()}
    store = ss.StateStore(conn)
    store.record_event_finish(event_ids["t10"], "ok", stage="merge", rows_merged=3)
    store.record_event_finish(event_ids["t11"], "ok", stage="merge", rows_merged=3)
    statuses = dict(conn.execute("select src_table, status from lakefed_ingest.ingest_event").fetchall())
    assert statuses == {"t10": "cancelled", "t11": "ok"}


def test_typed_logins_name_each_keys_newest_connection_and_its_host(conn):
    """The engine's guard reads, per key of one scope, the connection its newest login was typed for and that
    connection's host, port and type; a connection that isn't stored has no host, and another scope's keys stay out."""
    conn.execute("insert into lakefed_ingest.jdbc_connection (name, src_type, src_host, src_port, secret_scope, "
                 "secret_key) values ('orders', 'sqlserver', 'orders.example', 1433, 'logins', 'creds-{database}'), "
                 "('evil', 'sqlserver', 'attacker.example', 1433, 'logins', '{database}')")
    for at, (entry, connection, scope, key) in enumerate((("1", "evil", "logins", "creds-sales"),
                                                         ("2", "orders", "logins", "creds-sales"),
                                                         ("3", "gone", "logins", "creds-hr"),
                                                         ("4", "evil", "other", "creds-sales"))):
        conn.execute("insert into lakefed_ingest.jdbc_login_write (id, connection, secret_scope, secret_key, entered_by, "
                     "entered_at, outcome) values (%s, %s, %s, %s, 'ann', now() + make_interval(secs => %s), 'saved')",
                     (entry, connection, scope, key, at))
    typed = ss.StateStore(conn).typed_logins("logins")
    assert typed == {"creds-sales": {"connection": "orders", "src_host": "orders.example", "src_port": 1433,
                                     "src_type": "sqlserver"},
                     "creds-hr": {"connection": "gone", "src_host": None, "src_port": None, "src_type": None}}


def test_typed_logins_are_none_on_a_database_without_the_apps_login_tables():
    psycopg = pytest.importorskip("psycopg")
    name = f"lakefed_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    bare = psycopg.connect(psycopg.conninfo.make_conninfo(URI, dbname=name), autocommit=True)
    try:
        assert ss.StateStore(bare).typed_logins("logins") == {}
    finally:
        bare.close()
        admin.execute(f"drop database {name}")
        admin.close()


def _cfg(control_id):
    return {"id": control_id, "task_collection": "c", "src_database": "d", "src_schema": "s",
            "src_table": f"t{control_id}", "sink_catalog": "cat", "sink_schema": "sch",
            "sink_table": f"t{control_id}", "load_type": "ct"}
