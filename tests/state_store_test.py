"""Unit tests for the pure decision helpers and SQL builders in state_store.py.

No database or psycopg needed — psycopg is imported lazily inside StateStore.connect.
"""
import json
import re
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

import state_store as ss


# ---- is_reseed_needed --------------------------------------------------------
# A full reseed is required when Change-Tracking retention has lapsed: the checkpoint
# version is older than the source's minimum valid version. Missing values (CT not
# enabled, or never seeded) also force a reseed.

def test_reseed_needed_when_checkpoint_behind_min_valid():
    assert ss.is_reseed_needed(5, 10) is True


def test_no_reseed_when_checkpoint_at_or_above_min_valid():
    assert ss.is_reseed_needed(10, 10) is False
    assert ss.is_reseed_needed(12, 10) is False


def test_reseed_needed_when_min_valid_missing():
    # NULL min valid version => CT not enabled / unknown object => reseed.
    assert ss.is_reseed_needed(100, None) is True


def test_reseed_needed_when_never_seeded():
    assert ss.is_reseed_needed(None, 0) is True


def test_reseed_accepts_string_versions():
    # Values arrive from SQL task outputs as strings; comparison must be numeric.
    assert ss.is_reseed_needed("5", "10") is True
    assert ss.is_reseed_needed("10", "5") is False


# ---- next_phase --------------------------------------------------------------
def test_next_phase_seeding_to_incremental():
    assert ss.next_phase(ss.SEEDING) == ss.INCREMENTAL


def test_next_phase_incremental_is_terminal():
    assert ss.next_phase(ss.INCREMENTAL) == ss.INCREMENTAL


# ---- config (control table) builders -----------------------------------------
# All metadata is consolidated in Lakebase; config reads run on the cluster. Task selection is
# strategy-agnostic: it selects every enabled task in the collection (per-task strategy is
# decided later, in the sweep's per-table branch).

def test_list_task_configs_sql_selects_full_rows_filters_and_orders():
    sql = ss.list_task_configs_sql()
    assert ss.CONTROL_TABLE in sql
    for col in ss.CONTROL_COLUMNS:
        assert col in sql                              # selects the full config row
    assert "task_collection = %s" in sql and "task_enabled = true" in sql
    # strategy-agnostic selection: the WHERE must not hardcode a strategy predicate
    # (load_type/ct_enabled are selected columns, so check the predicates, not the bare names).
    assert "load_type = 'ct'" not in sql and "ct_enabled = true" not in sql
    assert "order by tier nulls last, priority nulls last, id" in sql
    assert sql.count("%s") == 1  # task_collection


def test_get_task_sql_selects_all_control_columns():
    sql = ss.get_task_sql()
    assert ss.CONTROL_TABLE in sql
    for col in ss.CONTROL_COLUMNS:
        assert col in sql
    assert sql.count("%s") == 1  # id


# ---- SQL builders ------------------------------------------------------------
# Builders return parameterized (%s) statements against the Lakebase hot-state tables.

def test_get_checkpoint_sql_keyed_by_control_id():
    sql = ss.get_checkpoint_sql()
    assert ss.CHECKPOINT_TABLE in sql
    assert "where control_id = %s" in sql
    assert sql.count("%s") == 1  # control_id


def test_upsert_checkpoint_conflicts_on_control_id():
    # Keyed by control id so same-named DBs on different servers never share a checkpoint.
    sql = ss.upsert_checkpoint_sql()
    assert "on conflict (control_id) do update" in sql
    assert "src_database, src_schema, src_table" in sql   # readable identity still written
    # control_id, db, schema, table, strategy, position, phase, status, detail, select_list,
    # source_identity, sink_identity, merge_key
    assert sql.count("%s") == 13
    assert "source_identity = excluded.source_identity, sink_identity = excluded.sink_identity" in sql
    assert "strategy = excluded.strategy, position = excluded.position" in sql
    assert "select_list = coalesce(excluded.select_list, checkpoint.select_list)" in sql


def test_enqueue_reseed_targets_reseed_table():
    sql = ss.enqueue_reseed_sql()
    assert ss.RESEED_TABLE in sql
    assert "control_id" in sql
    assert sql.count("%s") == 5  # control_id, db, schema, table, reason


def test_set_status_sql_upserts_on_control_id():
    sql = ss.set_status_sql()
    assert "on conflict (control_id)" in sql
    assert sql.count("%s") == 7  # control_id, db, schema, table, strategy, status, detail
    # an existing row keeps its own strategy / position; only status + detail change
    assert "strategy = excluded" not in sql and "position" not in sql


def test_task_identity_from_control_row():
    cfg = {"id": "7", "src_database": "db1", "src_schema": "dbo", "src_table": "t"}
    assert ss.task_identity(cfg) == (7, "db1", "dbo", "t")


def test_touch_last_success_joins_on_control_id():
    sql = ss.touch_last_success_sql()
    assert "cc.control_id = e.control_id" in sql


def test_control_columns_include_jdbc_and_secret_fields():
    for col in ("src_host", "src_port", "secret_key"):
        assert col in ss.CONTROL_COLUMNS
        assert col in ss.list_task_configs_sql()


# ---- Lakebase DDL (string checks) --------------------------------------------
_DDL = (Path(__file__).parent.parent / "src" / "lakefed_ingest" / "lakebase_schema.sql").read_text()


def test_ddl_ct_constraint_no_longer_requires_remote_query():
    # The CT constraint is re-declared idempotently and requires a resolvable host, not the
    # governed remote_query transport.
    assert "drop constraint if exists valid_ct_metadata" in _DDL
    con = _DDL[_DDL.index("add constraint valid_ct_metadata"):]
    con = con[:con.index(";")]
    assert "use_remote_query" not in con
    assert "src_host is not null or src_connection is not null" in con


def test_ddl_a_read_through_a_connection_needs_the_connection():
    assert "drop constraint if exists valid_remote_query" in _DDL
    con = _DDL[_DDL.index("add constraint valid_remote_query"):]
    assert con[:con.index(";")].split("check", 1)[1].split() == [
        "(", "not", "use_remote_query", "or", "src_connection", "is", "not", "null", ")"]


def test_ddl_migrates_checkpoint_to_control_id():
    assert "constraint checkpoint_control_pk primary key (control_id)" in _DDL
    assert "alter table lakefed_ingest.checkpoint add column if not exists control_id" in _DDL
    assert "drop constraint if exists ct_checkpoint_pkey" in _DDL
    assert "create unique index if not exists checkpoint_control_uq" in _DDL


def test_ddl_adds_jdbc_columns_idempotently():
    for col in ("src_host", "src_port", "secret_key"):
        assert f"alter table lakefed_ingest.control add column if not exists {col}" in _DDL


def test_ddl_has_no_dollar_quoted_blocks():
    # apply_lakebase_schema.ipynb splits statements on ';' — DO $$ ... $$ blocks would break it.
    code = "\n".join(l for l in _DDL.splitlines() if not l.strip().startswith("--"))
    assert "$$" not in code


# ---- decide_action -----------------------------------------------------------
# The consolidated sweep uses this pure helper to drive the per-table state machine.

def test_decide_action_seeding_phase_seeds():
    assert ss.decide_action(ss.SEEDING, None, None) == "seed"
    assert ss.decide_action(ss.SEEDING, 5, 3) == "seed"  # min_valid irrelevant while seeding


def test_decide_action_cdc_retention_lapsed_reseeds():
    # checkpoint has fallen below the source minimum valid version -> reseed.
    assert ss.decide_action(ss.INCREMENTAL, 5, 10) == "reseed"


def test_decide_action_cdc_valid_increments():
    assert ss.decide_action(ss.INCREMENTAL, 10, 5) == "increment"
    assert ss.decide_action(ss.INCREMENTAL, 10, 10) == "increment"


def test_decide_action_cdc_missing_min_valid_reseeds():
    # CT not enabled / unknown object (min_valid None) -> reseed.
    assert ss.decide_action(ss.INCREMENTAL, 10, None) == "reseed"


# ---- observability / telemetry builders --------------------------------------
# One ingest_run row per run, one ingest_event row per (run, table): INSERT at start,
# UPDATE at finish. These re-create per-table visibility that the single-task run hides.

def test_start_run_sql_inserts_running_and_returns_id():
    sql = ss.start_run_sql()
    assert ss.RUN_TABLE == "lakefed_ingest.ingest_run" and ss.RUN_TABLE in sql
    assert "'running'" in sql
    assert "returning run_id" in sql
    assert sql.count("%s") == 6  # task_collection, job_run_id, cluster_id, parallelism, total (planned), work_id
    assert "work_id" in sql


def test_a_run_records_the_work_list_it_was_started_for():
    assert "alter table lakefed_ingest.ingest_run add column if not exists work_id text" in _DDL
    assert "ingest_run_work_idx" in _DDL
    import json as _json
    notebook = _json.loads((Path(ss.__file__).parent / "ingest.ipynb").read_text())
    source = "\n".join("".join(c["source"]) for c in notebook["cells"])
    assert "planned=len(configs), work_id=work_id or None)" in source


def test_record_event_start_sql_inserts_running_and_returns_id():
    sql = ss.record_event_start_sql()
    assert ss.EVENT_TABLE in sql
    assert "'running'" in sql
    assert "returning id" in sql
    assert "(run_id, task_collection" in sql
    # run_id, task_collection, control_id, src_database, src_schema, src_table,
    # sink_fqn, action, phase, pool
    assert sql.count("%s") == 10


def test_record_event_finish_sql_updates_event_by_id():
    sql = ss.record_event_finish_sql()
    assert sql.startswith(f"update {ss.EVENT_TABLE} set")
    assert "finished_at = now()" in sql and "duration_ms" in sql
    assert "where id = %s" in sql
    # status, stage, rows_read, rows_merged, position_from, position_to, error, error_kind, error_class,
    # action, pool, retry_count, id
    assert sql.count("%s") == 13
    assert "error_kind = %s, error_class = %s" in sql
    assert "position_from = %s, position_to = %s" in sql
    assert "action = coalesce(%s, action)" in sql and "pool = coalesce(%s, pool)" in sql
    assert "retry_count = coalesce(%s, retry_count)" in sql
    assert sql.endswith("where id = %s and status = 'running'")


def test_touch_last_success_sql_stamps_checkpoint_from_event():
    sql = ss.touch_last_success_sql()
    assert ss.CHECKPOINT_TABLE in sql and ss.EVENT_TABLE in sql
    assert "last_success_at = now()" in sql
    assert sql.count("%s") == 1  # event_id


def test_finish_run_sql_updates_run_by_id():
    sql = ss.finish_run_sql()
    assert sql.startswith(f"update {ss.RUN_TABLE} set")
    assert "finished_at = now()" in sql
    assert sql.endswith("where run_id = %s and status = 'running'")
    # status, total, ok, failed, skipped, reseeded, detail, run_id
    assert sql.count("%s") == 8


def test_close_abandoned_runs_sql_closes_the_collections_other_open_runs_and_their_open_events():
    # A run that was cancelled or lost its cluster never finishes its ingest_run row or its in-flight
    # ingest_event rows. The next run holds the collection's lock, so any other open run of the
    # collection is dead: close it as failed, with its events, in one statement.
    sql = ss.close_abandoned_runs_sql()
    run_update, event_update = sql.split("), events as (")
    assert f"update {ss.RUN_TABLE} r set status = case when exists" in run_update
    assert ("and (d.result_state = 'CANCELED' or d.cancel_requested_at is not null)) "
            "then 'cancelled' else 'failed' end") in run_update
    assert "where r.task_collection = %s and r.status = 'running' and r.run_id <> %s" in run_update
    assert f"update {ss.EVENT_TABLE} e set status = dead.status" in event_update
    assert "e.run_id = dead.run_id and (e.status = 'running'" in event_update
    # A table waiting for its retry when the run died: its last attempt is 'retried' and no later one
    # exists, so it is closed as failed too, keeping its error and timing.
    assert ("e.status = 'retried' and not exists (select 1 from " + ss.EVENT_TABLE + " later "
            "where later.run_id = e.run_id and later.control_id = e.control_id and later.id > e.id)") in event_update
    assert "finished_at = coalesce(e.finished_at, now())" in event_update
    assert "when e.status = 'retried' then concat(e.error, ' | ', %s::text)" in event_update
    assert sql.rstrip().endswith("select (select count(*) from dead), (select count(*) from events)")
    # detail, task_collection, run_id, error (twice: a running event's, appended to a retried one's)
    assert sql.count("%s") == 5


def test_close_abandoned_runs_sql_closes_a_cancelled_run_as_cancelled_and_leaves_others_failed():
    # Only a run whose Databricks run was cancelled (ended CANCELED, or asked to stop) closes as cancelled; its events
    # follow their run (dead.status). Any other dead run closes as failed, as before.
    sql = ss.close_abandoned_runs_sql()
    assert sql.count("'cancelled'") == 1 and sql.count("'failed'") == 1
    assert f"{ss.DATABRICKS_RUN_TABLE} d" in sql
    assert "d.run_id = (case when r.job_run_id ~ '^[0-9]+$' then r.job_run_id::bigint end)" in sql


def test_stopping_run_sql_reads_the_run_and_its_databricks_run_by_job_run_id():
    sql = ss.stopping_run_sql()
    assert sql.count("%s") == 1 and sql.rstrip().endswith(f"from {ss.RUN_TABLE} r where r.run_id = %s")
    assert "r.status = 'cancelled'" in sql
    assert f"from {ss.DATABRICKS_RUN_TABLE} d" in sql
    assert "d.run_id = (case when r.job_run_id ~ '^[0-9]+$' then r.job_run_id::bigint end)" in sql
    for condition in ("d.cancel_requested_at is not null", "d.life_cycle_state = 'TERMINATING'",
                      "d.result_state = 'CANCELED'"):
        assert condition in sql


def test_stopping_run_binds_the_run_id_and_reads_the_flag():
    for got, stopping in ((True, True), (False, False), (None, False)):
        conn = FakeConn(fetch=[(got,)] if got is not None else [])
        assert ss.StateStore(conn).stopping_run(9) is stopping
        assert conn.calls == [(ss.stopping_run_sql(), (9,))]


def test_record_event_failed_takes_its_status_as_a_twelfth_parameter():
    sql = ss.record_event_failed_sql()
    assert "'failed'" not in sql and "'cancelled'" not in sql
    assert sql.count("%s") == 12 and "error, status, finished_at, duration_ms" in sql


def test_record_failed_tables_a_cancelled_run_records_cancelled_events_and_leaves_checkpoints_alone():
    conn = FakeConn(fetch=[(True,)])
    cfg = dict(CFG, task_collection="tc", sink_catalog="c", sink_schema="s", sink_table="t")
    ss.StateStore(conn).record_failed_tables(3, [(cfg, {"phase": "incremental"}, "connection reset")])
    stopping_sql, (ev_sql, ev_rows) = conn.calls
    assert stopping_sql[0] == ss.stopping_run_sql()
    assert ev_sql == ss.record_event_failed_sql()
    assert [row[-1] for row in ev_rows] == ["cancelled"]
    assert all(sql != ss.set_status_sql() for sql, _ in conn.calls)


def test_record_failed_tables_a_spark_cancel_in_a_live_run_is_cancelled_and_a_real_error_is_failed():
    conn = FakeConn()
    cfg = dict(CFG, task_collection="tc", sink_catalog="c", sink_schema="s", sink_table="t")
    items = [(cfg, None, "[SPARK_JOB_CANCELLED] Job 4 cancelled"), (dict(cfg, id=8), None, "login failed")]
    ss.StateStore(conn).record_failed_tables(3, items)
    _, (_, ev_rows), (st_sql, st_rows) = conn.calls
    assert [row[-1] for row in ev_rows] == ["cancelled", "failed"]
    assert st_sql == ss.set_status_sql() and [row[0] for row in st_rows] == [8]
    assert st_rows[0][5:] == ("failed", "login failed")


# ---- per-collection run lock --------------------------------------------------------------
# Different task_collections sweep concurrently; the same collection must not overlap. A
# session-level advisory lock keyed by a namespaced, 64-bit hash of the collection name.

def test_collection_lock_key_is_namespaced():
    assert ss.collection_lock_key("t1") == ss.LOCK_NAMESPACE + "t1"
    assert ss.collection_lock_key("t1") != ss.collection_lock_key("t2")


def test_try_lock_collection_sql_is_non_blocking_64bit():
    sql = ss.try_lock_collection_sql()
    assert "pg_try_advisory_lock" in sql        # non-blocking: a duplicate run skips, never waits
    assert "hashtextextended" in sql            # 64-bit key => negligible cross-collection collisions
    assert sql.count("%s") == 1


def test_unlock_collection_sql_matches_lock_key():
    sql = ss.unlock_collection_sql()
    assert "pg_advisory_unlock" in sql and "hashtextextended" in sql
    assert sql.count("%s") == 1


def test_lock_application_name_is_labeled_and_capped():
    name = ss.lock_application_name("smoke_test", "12345")
    assert name == "lakefed_ingest:smoke_test:12345"
    assert len(ss.lock_application_name("c" * 200, "r" * 50)) == 63   # Postgres limit


# ---- partitioned seed control settings ----------------------------------------------------
def test_control_columns_include_partition_settings():
    for col in ("load_partitioned", "partition_col", "partition_size_mb"):
        assert col in ss.CONTROL_COLUMNS


def test_partitioning_ddl_and_idempotent_migration():
    assert "partition_col         text" in _DDL and "partition_size_mb     int" in _DDL
    assert "add column if not exists partition_col" in _DDL
    assert "add column if not exists partition_size_mb" in _DDL
    assert "drop constraint if exists valid_partitioning" in _DDL
    assert ("not load_partitioned or (partition_col is not null and "
            "coalesce(partition_size_mb, 0) > 0)") in _DDL


# ---- StateStore methods against a fake connection ----------------------------------------
# Builders are shape-tested above; these check the BOUND PARAMETER ORDER each method sends, so a
# swapped argument can't silently corrupt checkpoints, locks, or telemetry.

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.conn.raise_on_execute:
            raise RuntimeError("connection lost")
        self.conn.calls.append((sql, params))

    def executemany(self, sql, seq):
        if self.conn.raise_on_execute:
            raise RuntimeError("connection lost")
        self.conn.calls.append((sql, list(seq)))

    def fetchone(self):
        return self.conn.fetch.pop(0) if self.conn.fetch else None

    def fetchall(self):
        rows, self.conn.fetch = list(self.conn.fetch), []
        return rows


class FakeConn:
    def __init__(self, fetch=None, closed=False, raise_on_execute=False, raise_on_close=False):
        self.calls, self.fetch = [], list(fetch or [])
        self.closed, self.raise_on_execute, self.raise_on_close = closed, raise_on_execute, raise_on_close

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        if self.raise_on_close:
            raise RuntimeError("already gone")
        self.closed = True


CFG = {"id": 7, "src_database": "d", "src_schema": "s", "src_table": "t"}


def test_advance_checkpoint_binds_identity_strategy_then_text_position():
    conn = FakeConn(); ss.StateStore(conn).advance_checkpoint(CFG, 42)
    # strategy defaults to the row's load_type (ct when unset); the CT version is stored as text
    assert conn.calls == [(ss.upsert_checkpoint_sql(),
                           (7, "d", "s", "t", "ct", "42", "incremental", "ok", None, None,
                            *ss.identity_values(CFG)))]


def test_advance_checkpoint_full_strategy_has_no_position():
    conn = FakeConn()
    ss.StateStore(conn).advance_checkpoint(dict(CFG, load_type="full"), None, phase=ss.LOADED)
    assert conn.calls[0][1][4:7] == ("full", None, "loaded")


def test_set_status_binds_identity_then_status():
    conn = FakeConn(); ss.StateStore(conn).set_status(CFG, "failed", "boom")
    assert conn.calls == [(ss.set_status_sql(), (7, "d", "s", "t", "ct", "failed", "boom"))]


def test_enqueue_reseed_queues_then_flips_phase_to_seeding():
    conn = FakeConn(); ss.StateStore(conn).enqueue_reseed(CFG)
    assert conn.calls == [
        (ss.enqueue_reseed_sql(), (7, "d", "s", "t", "retention_miss")),
        (ss.upsert_checkpoint_sql(),
         (7, "d", "s", "t", "ct", None, "seeding", "reseed_queued", "retention_miss", None,
          *ss.identity_values(CFG))),
    ]


def test_get_checkpoint_binds_control_id_and_defaults_to_seeding():
    conn = FakeConn(fetch=[None]); cp = ss.StateStore(conn).get_checkpoint("7")
    assert conn.calls == [(ss.get_checkpoint_sql(), (7,))]
    assert cp == {"position": None, "phase": "seeding", "status": "ok", "strategy": None}
    conn = FakeConn(fetch=[("287493", "incremental", "ok", "ct")])
    assert ss.StateStore(conn).get_checkpoint(7) == {"position": "287493", "phase": "incremental",
                                                     "status": "ok", "strategy": "ct"}


def test_try_lock_collection_reads_result_and_binds_key():
    for got in (True, False):
        conn = FakeConn(fetch=[(got,)])
        assert ss.StateStore(conn).try_lock_collection("c") is got
        assert conn.calls == [(ss.try_lock_collection_sql(), (ss.collection_lock_key("c"),))]


def test_close_abandoned_runs_binds_detail_collection_run_then_error_and_returns_counts():
    conn = FakeConn(fetch=[(1, 48)])
    assert ss.StateStore(conn).close_abandoned_runs("c", 9) == (1, 48)
    (sql, params), = conn.calls
    assert sql == ss.close_abandoned_runs_sql()
    detail, collection, run_id, retried_error, error = params
    assert json.loads(detail) == {"closed_by_run": 9, "reason": ss.ABANDONED_RUN_REASON}
    assert (collection, run_id) == ("c", 9)
    assert error == retried_error == f"{ss.ABANDONED_RUN_REASON}; closed by run 9"


def test_is_alive_false_when_closed_or_erroring_true_otherwise():
    assert ss.StateStore(FakeConn(closed=True)).is_alive() is False
    assert ss.StateStore(FakeConn(raise_on_execute=True)).is_alive() is False
    assert ss.StateStore(FakeConn()).is_alive() is True


def test_close_swallows_exceptions():
    ss.StateStore(FakeConn(raise_on_close=True)).close()   # must not raise


def test_record_event_finish_ok_stamps_last_success():
    conn = FakeConn()
    ss.StateStore(conn).record_event_finish(9, "ok", stage="merge", rows_merged=3,
                                            position_from=1, position_to=2, action="increment")
    assert conn.calls == [
        (ss.record_event_finish_sql(), ("ok", "merge", None, 3, "1", "2", None, None, None, "increment", None, None, 9)),
        (ss.touch_last_success_sql(), (9,)),
    ]


def test_record_event_finish_failed_truncates_error_and_skips_stamp():
    conn = FakeConn()
    ss.StateStore(conn).record_event_finish(9, "failed", stage="read", error="x" * 3000)
    assert len(conn.calls) == 1
    assert len(conn.calls[0][1][6]) == 2000          # error param truncated


def test_record_event_finish_skipped_reseed_does_not_stamp():
    conn = FakeConn()
    ss.StateStore(conn).record_event_finish(9, "skipped", stage="checkpoint", action="reseed")
    assert len(conn.calls) == 1 and conn.calls[0][1][9] == "reseed"


def test_record_event_finish_records_pool():
    conn = FakeConn()
    ss.StateStore(conn).record_event_finish(9, "ok", stage="merge", pool="pool3,pool17")
    assert conn.calls[0][1][10] == "pool3,pool17" and conn.calls[0][1][12] == 9


def test_record_event_finish_records_a_retried_attempt_without_stamping():
    conn = FakeConn()
    ss.StateStore(conn).record_event_finish(9, "retried", stage="seed", error="x", retry_count=1)
    assert len(conn.calls) == 1 and conn.calls[0][1][0] == "retried" and conn.calls[0][1][11] == 1


def test_record_event_finish_records_how_a_failure_was_classified():
    conn = FakeConn()
    ss.StateStore(conn).record_event_finish(9, "failed", stage="read", error="x", error_kind="transient",
                                            error_class="FAILED_JDBC.CONNECTION")
    assert conn.calls[0][1][7:9] == ("transient", "FAILED_JDBC.CONNECTION")


# ---- skip-no-change: bulk checkpoint load, select_list recording, bulk advance -------------

def test_get_checkpoints_one_query_any_array():
    sql = ss.get_checkpoints_sql()
    assert "control_id = any(%s)" in sql and "select_list" in sql and sql.count("%s") == 1


def test_get_checkpoints_maps_rows_by_control_id():
    conn = FakeConn(fetch=[(7, "100", "incremental", "ok", "a, b", "ct", 3, "150", 12, "2026-10-14", "s1", "k1", "id"),
                           (8, None, "loaded", "ok", None, "full", None, None, None, None, None, None, None)])
    got = ss.StateStore(conn).get_checkpoints(["7", 8])
    assert conn.calls == [(ss.get_checkpoints_sql(), ([7, 8],))]
    assert got == {7: {"position": "100", "phase": "incremental", "status": "ok", "select_list": "a, b",
                       "strategy": "ct", "ct_begin_version": 3, "pending_position": "150", "pending_run_id": 12,
                       "last_success_at": "2026-10-14", "source_identity": "s1", "sink_identity": "k1", "merge_key": "id"},
                   8: {"position": None, "phase": "loaded", "status": "ok", "select_list": None,
                       "strategy": "full", "ct_begin_version": None, "pending_position": None,
                       "pending_run_id": None, "last_success_at": None,
                       "source_identity": None, "sink_identity": None, "merge_key": None}}
    assert ss.StateStore(FakeConn()).get_checkpoints([]) == {}


def test_advance_checkpoint_records_select_list():
    conn = FakeConn(); ss.StateStore(conn).advance_checkpoint(CFG, 42, select_list="a, b")
    assert conn.calls[0][1][9] == "a, b"


def test_bulk_advance_never_moves_backwards_and_only_cdc():
    sql = ss.bulk_advance_unchanged_sql()
    # positions are text: compare as numbers, never lexically ('9' > '10')
    assert "position = greatest(coalesce(position, '0')::bigint, %s::bigint)::text" in sql
    assert "where control_id = %s and phase = 'incremental' and strategy = 'ct'" in sql
    assert "last_success_at = now()" in sql and "select_list = coalesce(%s, select_list)" in sql
    assert "ct_begin_version = coalesce(ct_begin_version, %s)" in sql   # never overwrite a recorded one
    assert sql.count("%s") == 7


def test_bulk_advance_binds_each_rows_own_version():
    # Rows from two databases carry their OWN database's version (CT versions are per database).
    conn = FakeConn()
    n = ss.StateStore(conn).bulk_advance_unchanged([(7, 500, None), (9, 12, "a, b", 4)])
    assert n == 2
    assert conn.calls == [(ss.bulk_advance_unchanged_sql(), [(500, None, None, None, None, None, 7), (12, "a, b", 4, None, None, None, 9)])]
    assert ss.StateStore(FakeConn()).bulk_advance_unchanged([]) == 0


def test_select_list_added_columns():
    assert ss.select_list_added_columns("a, b", None) is None                 # legacy: not recorded
    assert ss.select_list_added_columns("b,A", "a, b") == []                  # reorder/case: no reseed
    assert ss.select_list_added_columns("a, b, C", "a,b") == ["c"]            # added -> reseed
    assert ss.select_list_added_columns("a", "a, b") == []                    # removed: no reseed


def test_record_failed_tables_bulk_events_and_status():
    conn = FakeConn()
    cfg = dict(CFG, task_collection="tc", sink_catalog="c", sink_schema="s", sink_table="t")
    n = ss.StateStore(conn).record_failed_tables(3, [(cfg, {"phase": "incremental"}, "db unreachable")])
    assert n == 1
    _, (ev_sql, ev_rows), (st_sql, st_rows) = conn.calls
    assert ev_sql == ss.record_event_failed_sql()
    assert ev_rows == [(3, "tc", 7, "d", "s", "t", "c.s.t", "increment", "incremental", "detect", "db unreachable",
                        "failed")]
    assert st_sql == ss.set_status_sql() and st_rows == [(7, "d", "s", "t", "ct", "failed", "db unreachable")]


def test_record_failed_tables_full_rows_and_plan_stage():
    conn = FakeConn()
    cfg = dict(CFG, load_type="full", task_collection="tc", sink_catalog="c", sink_schema="s", sink_table="t")
    ss.StateStore(conn).record_failed_tables(3, [(cfg, None, "unsupported")], stage="plan")
    _, (_, ev_rows), (_, st_rows) = conn.calls
    assert ev_rows[0][7:10] == ("full", None, "plan") and st_rows[0][4] == "full"


# ---- generic checkpoint + renames: guarded migration and the applier's statement splitter ------

def test_rename_statements_old_instance_renames_everything_once():
    rels = {"control", "ct_checkpoint", "sweep_run", "ingest_event", "ct_checkpoint_control_uq",
            "sweep_run_pkey", "sweep_run_collection_idx", "ingest_event_sweep_idx", "sweep_run_sweep_id_seq"}
    cols = {("ct_checkpoint", "ct_version"), ("ct_checkpoint", "phase"), ("sweep_run", "sweep_id"),
            ("ingest_event", "sweep_id"), ("ingest_event", "ct_version_from"),
            ("ingest_event", "ct_version_to")}
    got = ss.rename_statements(rels, cols)
    assert got[:2] == ["alter table lakefed_ingest.ct_checkpoint rename to checkpoint",
                       "alter table lakefed_ingest.sweep_run rename to ingest_run"]
    # column renames address the NEW table names (table renames are simulated first)
    assert "alter table lakefed_ingest.checkpoint rename column ct_version to position" in got
    assert "alter table lakefed_ingest.ingest_run rename column sweep_id to run_id" in got
    assert "alter table lakefed_ingest.ingest_event rename column ct_version_to to position_to" in got
    assert "alter index lakefed_ingest.ct_checkpoint_control_uq rename to checkpoint_control_uq" in got
    assert "alter sequence lakefed_ingest.sweep_run_sweep_id_seq rename to ingest_run_run_id_seq" in got
    # an index that never existed on this instance is simply skipped
    assert not any("ct_checkpoint_control_pk" in s for s in got)


def test_rename_statements_is_noop_when_migrated_or_fresh():
    rels = {"control", "checkpoint", "ingest_run", "ingest_event", "checkpoint_control_pk"}
    cols = {("checkpoint", "position"), ("ingest_run", "run_id"), ("ingest_event", "run_id"),
            ("ingest_event", "position_from"), ("ingest_event", "position_to")}
    assert ss.rename_statements(rels, cols) == []
    assert ss.rename_statements(set(), set()) == []


def test_rename_statements_never_renames_onto_an_existing_object():
    # both names present (e.g. a half-applied manual fix): leave it for a human, don't fail the apply
    rels = {"ct_checkpoint", "checkpoint"}
    cols = {("ct_checkpoint", "ct_version"), ("checkpoint", "position")}
    assert ss.rename_statements(rels, cols) == []


def test_split_sql_statements_strips_comments_with_semicolons():
    ddl = "create table a (x int); -- note; with a semicolon\n\n-- whole-line; comment\nalter table a add y int ;\n"
    assert ss.split_sql_statements(ddl) == ["create table a (x int)", "alter table a add y int"]


def test_split_sql_statements_on_the_real_schema_file():
    stmts = ss.split_sql_statements(_DDL)
    assert stmts[0] == "create schema if not exists lakefed_ingest"
    assert all("--" not in s for s in stmts) and len(stmts) > 40


# ---- schema version and migration --------------------------------------------------------------

# The DDL fingerprint each SCHEMA_VERSION was released with. Changed lakebase_schema.sql or RENAMES?
# Bump state_store.SCHEMA_VERSION and add its fingerprint here: deployments then migrate on their
# next ingestion run.
SCHEMA_FINGERPRINTS = {
    1: "c035b85032e0df245775f4858dcc2e450ae737e885f326071c7b8546527a96e1",
    2: "8e514a425a7684f353c288677bff216cfe32421980263c197ba453bc10ac989f",
    3: "b748cce2a28ea27b16807b344ebc891908bfed92658f67b6cf4ed360af3d1551",
    4: "f105421638e334fbf50e4f7ee40469d129a03b4e22e7074f751b3dbad21b6d73",
    # 5 was the start-time branch's own number before it met 3 and 4; never reused.
    6: "8bdb0c74e5aaeeefafe658951e872a64494746e1eb60e1750a0db86df3851232",
    7: "853692ef91037d70967740372efac90d2c505ae3a290bc1a6637adb906b5f895",
    8: "62773bba7e6ece8e8d8675445067fc6069a9a5ea1a52670a464712f24e2f11e3",
    9: "634be82ce6ad19ac81eb83a26f00abdb072f12c28aed2a02b00a523caeb6bfca",
    10: "32ad0886a86f0bcadd26fca1fc27499bc5c309e6f52de665d6c137e407173fa9",
    11: "557b634c85d1a753d82c7e0ca145209f3b56e1a957d833ad38469e0c97f65999",
    12: "3c4da8d33b1512088b091acb8548088962d3220648a7d497ff4378c043f269f8",
    13: "02ab6eceb01e8c75509c88f496d08f187e6cbf746cc0019f3477977968854cc0",
    14: "f125d8ae5701120edba627816947a339119e6d8d6d1c4690632b975e52f6ddf7",
    15: "9ee71a536e5e38ff4c1222cc650c8eb2f1cab4dc6fbbfaebd49473c24640ab40",
    16: "7ce59a107931d2ee279e1b52c60746fe7feab54cb3bd70cb504b6d898e01835b",
    17: "15076f535e703623034842daa101fc7d70b2af8b356451abec6515b6e75de287",
}


def test_schema_version_is_bumped_with_every_ddl_change():
    got = ss.schema_fingerprint(_DDL)
    assert SCHEMA_FINGERPRINTS.get(ss.SCHEMA_VERSION) == got, (
        f"lakebase_schema.sql or RENAMES changed: bump state_store.SCHEMA_VERSION to "
        f"{max(SCHEMA_FINGERPRINTS) + 1} and add {{{max(SCHEMA_FINGERPRINTS) + 1}: {got!r}}} to SCHEMA_FINGERPRINTS")
    assert ss.SCHEMA_VERSION == max(SCHEMA_FINGERPRINTS)


def test_schema_fingerprint_ignores_comments_and_whitespace():
    assert ss.schema_fingerprint("create table a (x int); -- note\n") == ss.schema_fingerprint("create  table a\n(x int);")
    assert ss.schema_fingerprint("create table a (x int);") != ss.schema_fingerprint("create table a (x bigint);")


def test_the_failing_tables_index_is_a_plain_non_partial_index_on_control_and_started_at():
    statements = [" ".join(s.split()) for s in ss.split_sql_statements(_DDL)]
    index_statements = [s for s in statements if "ingest_event_control_started_idx" in s]
    assert index_statements == [
        "create index if not exists ingest_event_control_started_idx "
        "on lakefed_ingest.ingest_event (control_id, started_at)"
    ]


def test_ddl_creates_the_schema_version_table_and_runs_in_a_transaction():
    stmts = ss.split_sql_statements(_DDL)
    assert any(s.startswith("create table if not exists lakefed_ingest.schema_version") for s in stmts)
    assert not any("concurrently" in s.lower() for s in stmts)       # not allowed in a transaction


# Every upgrade re-applies the WHOLE lakebase_schema.sql (plus RENAMES) in one transaction, from
# whatever version the database is at, so every statement must be idempotent, additive and valid on
# any earlier version's schema (docs/lakebase_schema.md, "Changing the schema"). These tests check
# the statement forms with no database: a statement that would fail or do harm when re-applied fails here
# instead of failing the next upgrade on every deployment.

# Statements that change existing columns or rows, each reviewed as a no-op once applied and valid from
# any earlier version (a re-applied type change to the same type doesn't rewrite the table: checked on
# Lakebase, Postgres 16). Matched by the start of the normalized statement. A new statement of these
# kinds fails test_ddl_statements_are_safe_to_re_apply until it's reviewed and listed here.
REVIEWED_NON_ADDITIVE = (
    "alter table lakefed_ingest.control alter column src_port drop default",
    "alter table lakefed_ingest.control alter column primary_key drop not null",
    "alter table lakefed_ingest.control alter column job_name drop not null",
    "alter table lakefed_ingest.control alter column use_remote_query set default false",
    "update lakefed_ingest.checkpoint cc set control_id = m.id from",            # where cc.control_id is null
    "alter table lakefed_ingest.checkpoint alter column position drop default",
    "alter table lakefed_ingest.checkpoint alter column position drop not null",
    "alter table lakefed_ingest.checkpoint alter column position type text using position::text",
    "update lakefed_ingest.checkpoint set phase = 'incremental' where phase = 'cdc'",
    "alter table lakefed_ingest.ingest_event alter column position_from type text using position_from::text",
    "alter table lakefed_ingest.ingest_event alter column position_to type text using position_to::text",
    "update lakefed_ingest.ingest_event set phase = 'incremental' where phase = 'cdc'",
)
_NON_ADDITIVE = re.compile(r"^(update|insert|delete)\b|\balter column\b|\bdrop (default|not null)\b|\bset not null\b")


def ddl_problems(statements, reviewed=REVIEWED_NON_ADDITIVE):
    """Why each statement isn't safe to re-apply on any earlier version's schema ([] = all safe)."""
    out, dropped = [], set()
    for raw in statements:
        s = " ".join(raw.split()).lower()
        short = s[:100]
        if re.search(r"\brename\b", s):
            out.append(f"rename (put it in state_store.RENAMES, which runs only pending renames): {short}")
        elif re.match(r"(drop|truncate)\b", s) or re.search(r"\bdrop (column|table|schema|index|view)\b", s):
            out.append(f"destructive (needs a per-version migration step, not the cumulative re-apply): {short}")
        elif s.startswith("create"):
            if not re.match(r"create (schema|table|(unique )?index) if not exists\b|create or replace (view|function)\b", s):
                out.append(f"create without if not exists / or replace: {short}")
        elif s.startswith("alter table"):
            table = s.split()[2]
            for name in re.findall(r"\bdrop constraint if exists (\w+)", s):
                dropped.add((table, name))
            for name in re.findall(r"\badd constraint (\w+)", s):
                if (table, name) not in dropped:
                    out.append(f"add constraint {name} without an earlier drop constraint if exists {name}: {short}")
            if re.search(r"\badd (?!column if not exists\b|constraint\b)", s):
                out.append(f"add without column if not exists: {short}")
            if _NON_ADDITIVE.search(s) and not s.startswith(reviewed):
                out.append(f"changes an existing column; review it and add it to REVIEWED_NON_ADDITIVE: {short}")
        elif _NON_ADDITIVE.search(s):
            if not s.startswith(reviewed):
                out.append(f"changes existing rows; make it a no-op once applied, then add it to REVIEWED_NON_ADDITIVE: {short}")
        elif not s.startswith("comment on"):
            out.append(f"unrecognized statement kind: {short}")
    return out


def test_ddl_statements_are_safe_to_re_apply():
    assert ddl_problems(ss.split_sql_statements(_DDL)) == []
    # Every reviewed statement still exists (remove entries for statements taken out of the file).
    stmts = [" ".join(s.split()).lower() for s in ss.split_sql_statements(_DDL)]
    assert [r for r in REVIEWED_NON_ADDITIVE if not any(s.startswith(r) for s in stmts)] == []


@pytest.mark.parametrize("stmt, why", [
    ("create table lakefed_ingest.t (a int)", "create without if not exists"),
    ("create index t_idx on lakefed_ingest.t (a)", "create without if not exists"),
    ("alter table lakefed_ingest.t add column b int", "add without column if not exists"),
    ("alter table lakefed_ingest.t add b int", "add without column if not exists"),
    ("alter table lakefed_ingest.t add constraint c check (a > 0)", "without an earlier drop constraint if exists c"),
    ("alter table lakefed_ingest.t drop column a", "destructive"),
    ("drop table if exists lakefed_ingest.t", "destructive"),
    ("truncate lakefed_ingest.t", "destructive"),
    ("alter table lakefed_ingest.t rename column a to b", "rename"),
    ("alter table lakefed_ingest.t alter column a type bigint", "REVIEWED_NON_ADDITIVE"),
    ("alter table lakefed_ingest.t alter column a set not null", "REVIEWED_NON_ADDITIVE"),
    ("update lakefed_ingest.t set a = 1", "REVIEWED_NON_ADDITIVE"),
    ("delete from lakefed_ingest.t where a = 1", "REVIEWED_NON_ADDITIVE"),
    ("grant select on lakefed_ingest.t to app", "unrecognized"),
])
def test_ddl_check_flags_statements_that_are_unsafe_to_re_apply(stmt, why):
    problems = ddl_problems([stmt])
    assert len(problems) == 1 and why in problems[0], problems


def test_ddl_check_accepts_the_idempotent_forms():
    assert ddl_problems([
        "create table if not exists lakefed_ingest.t (a int)",
        "create unique index if not exists t_uq on lakefed_ingest.t (a)",
        "alter table lakefed_ingest.t add column if not exists b int, add column if not exists c text",
        "alter table lakefed_ingest.t drop constraint if exists c",
        "alter table lakefed_ingest.t add constraint c check (a > 0)",
        "comment on table lakefed_ingest.t is 'x'",
    ]) == []


class FakeLockTimeout(Exception):
    """What psycopg raises when lock_timeout runs out: SQLSTATE 55P03 (LockNotAvailable)."""
    sqlstate = "55P03"


class MigrationDb:
    """A scripted Postgres for migrate(): answers the catalog queries, records statements and whether they
    ran inside the transaction. ``lock_timeouts`` makes the first attempts fail on their first DDL statement,
    as a lock wait that runs out does; ``probe_rows`` are substrings of the UPDATE_PROBES queries that find a row.
    ``schema_lock_held_s`` is how long another migrator holds the schema lock: the wait for it runs out when the
    transaction's lock_timeout is shorter, as Postgres applies lock_timeout to advisory-lock waits too, and else
    the lock is taken once the other migrator is done."""

    def __init__(self, version=None, schemas=("lakefed_ingest",), relations=(), columns=(), fail_on=None,
                 catalog=None, probe_rows=(), lock_timeouts=0, schema_lock_held_s=0):
        self.version, self.schemas, self.relations, self.columns = version, set(schemas), set(relations), set(columns)
        self.catalog = catalog or ss.SchemaCatalog(
            schema_exists=bool(schemas), relations=set(relations), tables=set(relations),
            columns={(table, column): ("text", None, False) for table, column in columns})
        self.fail_on, self.probe_rows, self.lock_timeouts = fail_on, probe_rows, lock_timeouts
        self.schema_lock_held_s, self.lock_timeout_s = schema_lock_held_s, None
        self.statements, self.in_tx, self.committed, self.rows = [], False, None, []
        self.attempts, self.outcomes = 0, []
        self.connection = self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def transaction(self):
        db = self

        class Tx:
            def __enter__(self):
                db.in_tx, db.lock_timeout_s = True, None
                db.attempts += 1

            def __exit__(self, exc_type, *exc):
                db.in_tx, db.committed = False, exc_type is None
                db.outcomes.append(db.committed)
                return False
        return Tx()

    def execute(self, sql, params=None):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("check constraint violated")
        if sql.startswith("set local lock_timeout"):
            self.lock_timeout_s = int(sql.split("'")[1].rstrip("s"))
        if sql.startswith("select pg_advisory_xact_lock"):
            self.lock = params
            if self.lock_timeout_s is not None and self.lock_timeout_s < self.schema_lock_held_s:
                self.statements.append((sql, self.in_tx))
                raise FakeLockTimeout("canceling statement due to lock timeout")
        self.statements.append((sql, self.in_tx))
        if self.attempts <= self.lock_timeouts and sql.startswith(("alter", "create", "update", "insert")):
            raise FakeLockTimeout("canceling statement due to lock timeout")
        self.rows = []
        if sql.startswith("select to_regclass"):
            self.rows = [(None if self.version is None else "schema_version",)]
        elif sql.startswith("select coalesce(max(version)"):
            self.rows = [(self.version,)]
        elif sql.startswith("select nspname"):
            self.rows = [(s,) for s in self.schemas]
        elif "c.relkind" in sql:
            self.rows = [(name, "r" if name in self.catalog.tables else "i") for name in sorted(self.catalog.relations)]
        elif sql.startswith("select c.relname from"):
            self.rows = [(r,) for r in sorted(self.relations)]
        elif "a.attname" in sql:
            self.rows = [(t, c, typ, default, not_null)
                         for (t, c), (typ, default, not_null) in self.catalog.columns.items()]
        elif "con.conname" in sql:
            self.rows = sorted(self.catalog.constraints)
        elif sql.startswith("select table_name"):
            self.rows = list(self.columns)
        elif sql.startswith("select 1 from") and any(p in sql for p in self.probe_rows):
            self.rows = [(1,)]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def ran(self):
        return [s for s, _ in self.statements if not s.startswith(("select", "set local"))]


def test_schema_version_is_zero_before_versions_were_recorded():
    assert ss.StateStore(MigrationDb()).schema_version() == 0
    assert ss.StateStore(MigrationDb(version=1)).schema_version() == 1


def test_migrate_from_an_unversioned_database_applies_renames_ddl_and_version_in_one_transaction():
    db = MigrationDb(relations={"ct_checkpoint", "checkpoint_control_pk"}, columns={("ct_checkpoint", "ct_version")})
    out = ss.StateStore(db).migrate("create table if not exists lakefed_ingest.x (a int); alter table lakefed_ingest.x add b int;")
    assert out == {"from": 0, "ran": 2, "skipped": 0,
                   "renames": ["alter table lakefed_ingest.ct_checkpoint rename to checkpoint",
                               "alter table lakefed_ingest.checkpoint rename column ct_version to position"]}
    assert db.ran()[2:] == ["create table if not exists lakefed_ingest.x (a int)", "alter table lakefed_ingest.x add b int",
                            "insert into lakefed_ingest.schema_version (version) values (%s) on conflict (version) "
                            "do update set applied_at = now(), applied_by = current_user"]
    assert all(in_tx for _, in_tx in db.statements) and db.committed is True
    # Serialized by the schema lock, taken before the version is read; the table waits are bounded.
    first = [s for s, _ in db.statements[:4]]
    assert first[:3] == [f"set local lock_timeout = '{ss.SCHEMA_LOCK_WAIT_S}s'",
                         "select pg_advisory_xact_lock(%s::int, %s::int)", "set local lock_timeout = '3s'"]
    assert first[3].startswith("select to_regclass")
    # Two int keys: never the one-key lock of a collection (even one named "schema"), nor a source lock.
    assert db.lock == ss.SCHEMA_LOCK and len(db.lock) == 2 and db.lock[0] != ss.SOURCE_LOCK_CLASS


def test_migrate_is_a_noop_at_the_current_version_unless_forced():
    db = MigrationDb(version=ss.SCHEMA_VERSION)
    assert ss.StateStore(db).migrate("create table t (a int);") is None and db.ran() == []
    out = ss.StateStore(db).migrate("create table t (a int);", force=True)  # the setup job
    assert out == {"from": ss.SCHEMA_VERSION, "renames": [], "ran": 1, "skipped": 0}
    assert "create table t (a int)" in db.ran()


def test_migrate_moves_the_preview_schema_first():
    db = MigrationDb(schemas={"lakefed_ingest_mt"})
    ss.StateStore(db).migrate("select 1;")
    assert db.ran()[0] == "alter schema lakefed_ingest_mt rename to lakefed_ingest"


def test_migrate_failure_rolls_back_and_a_newer_database_is_refused():
    db = MigrationDb(fail_on="valid_remote_query")
    with pytest.raises(RuntimeError):
        ss.StateStore(db).migrate("alter table c add constraint valid_remote_query check (x);")
    assert db.committed is False and not any("schema_version (version)" in s for s in db.ran())
    newer = MigrationDb(version=ss.SCHEMA_VERSION + 1)
    with pytest.raises(RuntimeError, match="newer than this code"):
        ss.StateStore(newer).migrate("create table t (a int);", force=True)
    assert newer.ran() == [] and newer.committed is False


# ---- skipping statements the catalog shows are already in place ----------------------------------------------------

def test_a_fresh_database_gets_the_whole_file():
    statements = ss.split_sql_statements(_DDL)
    assert ss.pending_statements(statements, ss.SchemaCatalog()) == (statements, [])


def test_a_migrated_database_reruns_only_the_constraint_re_declarations():
    # A re-declared constraint can't be compared with its stored definition from the catalog, so it always runs.
    statements = ss.split_sql_statements(_DDL)
    to_run, left_out = ss.pending_statements(statements, _migrated_catalog(statements), has_rows=lambda query: False)
    # The drops of old names (absent now) are left out; the drops and adds of the names the file declares run.
    declared = {name for s in statements for name in re.findall(r"add constraint (\w+)", s)}
    expected = [s for s in statements if " add constraint " in s
                or any(name in declared for name in re.findall(r"drop constraint if exists (\w+)", s))]
    assert to_run == expected and len(left_out) == len(statements) - len(expected) and len(expected) == 28
    assert all(s not in to_run for s in statements if s.startswith("create"))


def test_skips_objects_and_columns_the_catalog_shows_in_place():
    catalog = ss.SchemaCatalog(
        schema_exists=True, relations={"t", "t_idx"}, tables={"t"},
        columns={("t", "a"): ("integer", None, False), ("t", "b"): ("text", "'x'::text", True)},
        constraints={("t", "c_old")})
    stmts = ["create schema if not exists lakefed_ingest",
             "create table if not exists lakefed_ingest.t (a int)",
             "create unique index if not exists t_idx on lakefed_ingest.t (a)",
             "alter table lakefed_ingest.t add column if not exists a int",
             "alter table lakefed_ingest.t add column if not exists a int, add column if not exists b text",
             "alter table lakefed_ingest.t drop constraint if exists c_gone",
             "alter table lakefed_ingest.t drop constraint if exists c_old",
             "alter table lakefed_ingest.t add column if not exists c int"]
    to_run, left_out = ss.pending_statements(stmts, catalog)
    assert left_out == stmts[:6] and to_run == [stmts[6], stmts[7]]


def test_column_states_decide_the_alter_column_statements():
    catalog = ss.SchemaCatalog(
        schema_exists=True, relations={"t"}, tables={"t"},
        columns={("t", "d"): ("text", None, True), ("t", "e"): ("text", "false", False),
                 ("t", "f"): ("integer", "'x'::text", False), ("t", "g"): ("text", None, False)})
    stmts = [
        "alter table lakefed_ingest.t alter column d drop default",          # no default: skip
        "alter table lakefed_ingest.t alter column d drop not null",         # not null: runs
        "alter table lakefed_ingest.t alter column e drop not null",         # nullable: skip
        "alter table lakefed_ingest.t alter column e set default false",     # already false: skip
        "alter table lakefed_ingest.t alter column f set default false",     # runs
        "alter table lakefed_ingest.t alter column f drop default",          # runs
        "alter table lakefed_ingest.t alter column g type text using g::text",   # already text: skip
        "alter table lakefed_ingest.t alter column f type bigint using f::bigint",  # runs
        "alter table lakefed_ingest.t alter column missing drop default",    # no such column: runs
    ]
    to_run, _ = ss.pending_statements(stmts, catalog)
    assert to_run == [stmts[1], stmts[4], stmts[5], stmts[7], stmts[8]]


def test_a_statement_that_runs_changes_what_later_statements_see():
    catalog = ss.SchemaCatalog(schema_exists=True, relations={"t"}, tables={"t"},
                               columns={("t", "e"): ("text", "false", False)})
    stmts = ["alter table lakefed_ingest.t alter column e drop default",
             "alter table lakefed_ingest.t alter column e set default false"]
    # The column's default is false now, but the first statement removes it: the second must run.
    assert ss.pending_statements(stmts, catalog) == (stmts, [])


def test_an_unrecognized_statement_runs_and_everything_after_it_runs_too():
    catalog = ss.SchemaCatalog(schema_exists=True, relations={"t"}, tables={"t"})
    stmts = ["grant select on lakefed_ingest.t to reader",
             "create table if not exists lakefed_ingest.t (a int)"]
    assert ss.pending_statements(stmts, catalog) == (stmts, [])


def test_update_probes_are_statements_of_the_ddl():
    normalized = {" ".join(s.split()).lower() for s in ss.split_sql_statements(_DDL)}
    assert [key for key in ss.UPDATE_PROBES if key not in normalized] == []


def test_an_update_runs_only_when_its_probe_finds_rows():
    catalog = ss.SchemaCatalog(schema_exists=True, relations={"checkpoint"}, tables={"checkpoint"})
    update = "update lakefed_ingest.checkpoint set phase = 'incremental' where phase = 'cdc'"
    probes = []

    def has_rows(query):
        probes.append(query)
        return False
    assert ss.pending_statements([update], catalog, has_rows=has_rows) == ([], [update])
    assert probes == [ss.UPDATE_PROBES[update.lower()]]
    assert ss.pending_statements([update], catalog) == ([update], [])        # no probe answer: runs
    absent = ss.SchemaCatalog(schema_exists=True)
    assert ss.pending_statements([update], absent, has_rows=has_rows) == ([update], [])   # table not there: runs
    assert len(probes) == 1


def test_a_migration_that_finds_the_schema_done_runs_only_the_constraints_and_records_the_counts():
    statements = ss.split_sql_statements(_DDL)
    db = MigrationDb(version=ss.SCHEMA_VERSION - 1, catalog=_migrated_catalog(statements))
    out = ss.StateStore(db).migrate(_DDL)
    assert out["ran"] == 28 and out["skipped"] == len(statements) - 28 and out["renames"] == []
    assert db.committed is True


# ---- the lock wait: bounded per statement, retried with backoff, then a plain message -----------------------------

def test_a_lock_timeout_is_retried_with_backoff_until_the_migration_commits():
    db = MigrationDb(version=ss.SCHEMA_VERSION - 1, lock_timeouts=2)
    sleeps = []
    out = ss.StateStore(db).migrate("create table t (a int);", sleep=sleeps.append)
    assert out is not None and sleeps == [5.0, 10.0]
    assert db.outcomes == [False, False, True] and db.attempts == 3
    assert db.ran()[-1].startswith("insert into lakefed_ingest.schema_version")


def test_a_migrator_waiting_for_the_schema_lock_is_not_cut_short_by_the_table_lock_timeout():
    db = MigrationDb(version=ss.SCHEMA_VERSION - 1, schema_lock_held_s=30)
    sleeps = []
    out = ss.StateStore(db).migrate("create table t (a int);", sleep=sleeps.append)
    assert out is not None and sleeps == [] and db.outcomes == [True]


def test_the_schema_lock_wait_has_its_own_bound_and_the_tables_keep_theirs():
    db = MigrationDb(version=ss.SCHEMA_VERSION - 1)
    ss.StateStore(db).migrate("create table t (a int);", sleep=lambda s: None)
    ran = [sql for sql, _ in db.statements]
    wait, lock, tables, ddl = (next(i for i, sql in enumerate(ran) if sql.startswith(start)) for start in (
        f"set local lock_timeout = '{ss.SCHEMA_LOCK_WAIT_S}s'", "select pg_advisory_xact_lock",
        f"set local lock_timeout = '{ss.LOCK_TIMEOUT_S}s'", "create table t"))
    assert wait < lock < tables < ddl


def test_a_migration_held_past_the_schema_lock_bound_ends_waiting_at_once_and_changes_nothing():
    # Another migrator stalled in its transaction (a hung run): the waiter gives up after SCHEMA_LOCK_WAIT_S, with
    # MigrationLockTimeout (the ingest and check runs end migration_waiting), instead of waiting with it.
    db = MigrationDb(version=ss.SCHEMA_VERSION - 1, schema_lock_held_s=ss.SCHEMA_LOCK_WAIT_S + 1)
    sleeps = []
    with pytest.raises(ss.MigrationLockTimeout, match="another migration held the schema lock") as caught:
        ss.StateStore(db).migrate("create table t (a int);", sleep=sleeps.append)
    assert "Run the setup job again." in str(caught.value) and isinstance(caught.value.__cause__, FakeLockTimeout)
    assert sleeps == [] and db.attempts == 1 and db.outcomes == [False]
    assert "create table t (a int)" not in db.ran()


def test_a_migration_that_keeps_timing_out_gives_up_with_a_plain_message_and_changes_nothing():
    db = MigrationDb(version=ss.SCHEMA_VERSION - 1, lock_timeouts=ss.MIGRATE_ATTEMPTS)
    sleeps = []
    with pytest.raises(ss.MigrationLockTimeout, match="a running load held the lakefed_ingest tables") as caught:
        ss.StateStore(db).migrate("create table t (a int);", sleep=sleeps.append)
    assert "Run the setup job again." in str(caught.value) and isinstance(caught.value.__cause__, FakeLockTimeout)
    assert len(sleeps) == ss.MIGRATE_ATTEMPTS - 1 and db.outcomes == [False] * ss.MIGRATE_ATTEMPTS
    assert not any("schema_version (version)" in s for s in db.ran())


def test_the_retries_wait_about_five_minutes_in_all():
    waits = [ss.migrate_backoff_s(attempt) for attempt in range(1, ss.MIGRATE_ATTEMPTS)]
    total = sum(waits) + ss.MIGRATE_ATTEMPTS * ss.LOCK_TIMEOUT_S
    assert waits[:4] == [5.0, 10.0, 20.0, 40.0] and max(waits) == 40.0
    assert 240 <= total <= 330


def test_only_a_lock_timeout_is_retried():
    assert ss.is_lock_timeout(FakeLockTimeout()) is True
    assert ss.is_lock_timeout(RuntimeError("boom")) is False
    error = RuntimeError("check constraint violated")
    error.sqlstate = "23514"
    assert ss.is_lock_timeout(error) is False
    sleeps = []
    db = MigrationDb(fail_on="valid_remote_query")
    with pytest.raises(RuntimeError, match="check constraint violated"):
        ss.StateStore(db).migrate("alter table c add constraint valid_remote_query check (x);", sleep=sleeps.append)
    assert sleeps == [] and db.attempts == 1


def _migrated_catalog(statements):
    """The catalog of a database that has the whole file applied, in the order the file applies it: each object
    it creates, each column with the default, nullability and type the file leaves it with, each constraint."""
    catalog = ss.SchemaCatalog(schema_exists=True)
    for raw in statements:
        s = " ".join(raw.split())
        created = re.match(r"create table if not exists lakefed_ingest\.(\w+) \((.*)\)$", s)
        if created:
            catalog.relations.add(created.group(1))
            catalog.tables.add(created.group(1))
            for part in created.group(2).split(","):
                words = part.split()
                if len(words) > 1 and words[0] not in ("primary", "constraint", "unique", "check"):
                    catalog.columns[(created.group(1), words[0])] = _column_state(" ".join(words[1:]))
            continue
        index = re.match(r"create (?:unique )?index if not exists (\w+)", s)
        if index:
            catalog.relations.add(index.group(1))
            continue
        added = re.match(r"alter table lakefed_ingest\.(\w+) add column if not exists (\w+) (.*)$", s)
        if added:
            catalog.columns.setdefault((added.group(1), added.group(2)), _column_state(added.group(3)))
            continue
        constraint = re.match(r"alter table lakefed_ingest\.(\w+) add constraint (\w+)", s)
        if constraint:
            catalog.constraints.add((constraint.group(1), constraint.group(2)))
            continue
        altered = re.match(r"alter table lakefed_ingest\.(\w+) alter column (\w+) (.*)$", s)
        if altered:
            key, change = (altered.group(1), altered.group(2)), altered.group(3)
            data_type, default, not_null = catalog.columns[key]
            if change == "drop default":
                default = None
            elif change == "drop not null":
                not_null = False
            elif change.startswith("set default "):
                default = change[len("set default "):]
            elif change.startswith("type "):
                data_type = change.split()[1]
            catalog.columns[key] = (data_type, default, not_null)
    return catalog


def _column_state(definition: str) -> tuple:
    """(data type, default or None, not null) of a column definition's words after its name."""
    words = definition.split()
    default = re.search(r"\bdefault (\S+)", definition)
    return ({"int": "integer"}.get(words[0], words[0]), default.group(1) if default else None, "not null" in definition)



def test_ddl_generic_checkpoint_and_telemetry_columns():
    for frag in ("create table if not exists lakefed_ingest.checkpoint",
                 "strategy       text        not null default 'ct'",
                 "alter column position type text using position::text",
                 "add column if not exists strategy text not null default 'ct'",
                 "phase in ('seeding', 'incremental', 'loaded')",
                 "create table if not exists lakefed_ingest.ingest_run",
                 "run_id          bigserial   primary key",
                 "position_from   text", "position_to     text"):
        assert frag in _DDL, frag
    code = "\n".join(l.split("--")[0] for l in _DDL.splitlines())
    for old in ("ct_checkpoint (", "sweep_run (", "sweep_id ", "ct_version "):
        assert old not in code, old


def test_ddl_control_strategy_constraints_and_columns():
    for col in ("cursor_column", "cdc_capture_instance", "jdbc_options"):
        assert f"alter table lakefed_ingest.control add column if not exists {col} text" in _DDL
        assert col in ss.CONTROL_COLUMNS
    assert "alter column primary_key drop not null" in _DDL          # full loads need no PK
    assert "load_type in ('ct', 'cdc', 'cursor', 'full')" in _DDL
    assert "src_type in ('sqlserver', 'oracle', 'postgresql', 'mysql')" in _DDL
    assert "drop constraint if exists control_load_type_check" in _DDL  # the old inline check
    cur = _DDL[_DDL.index("add constraint valid_cursor_metadata"):]
    cur = cur[:cur.index(";")]
    assert "cursor_column" in cur and "primary_key" in cur


def test_strategy_name_and_position_text():
    assert ss.strategy_name({}) == "ct" and ss.strategy_name({"load_type": " FULL "}) == "full"
    assert ss.position_text(None) is None and ss.position_text(12) == "12"


def test_record_ct_begin_version():
    conn = FakeConn(); ss.StateStore(conn).record_ct_begin_version(CFG, 495)
    assert conn.calls == [(ss.record_ct_begin_version_sql(), (495, int(CFG["id"])))]
    conn = FakeConn(); ss.StateStore(conn).record_ct_begin_version(CFG, None)
    assert conn.calls == []


def test_mixed_sinks_query_starts_from_dedicated_rows_and_maps_tuples():
    sql = ss.mixed_sinks_sql()
    # Every collection, enabled or not; no GROUP BY over the whole control table.
    assert "task_enabled" not in sql and "group by" not in sql
    assert "where d.client_id is null and exists (" in sql and "and o.id <> d.id)" in sql
    assert "lower(o.sink_table) = lower(d.sink_table)" in sql
    conn = FakeConn(fetch=[("main", "bronze", "orders")])
    assert ss.StateStore(conn).mixed_sinks() == {("main", "bronze", "orders")}


def test_ddl_indexes_dedicated_rows_by_sink():
    assert ("create index if not exists control_dedicated_sink_idx\n"
            "    on lakefed_ingest.control (lower(sink_catalog), lower(sink_schema), lower(sink_table))\n"
            "    where client_id is null;") in _DDL


def test_control_columns_include_client_id():
    assert "client_id" in ss.CONTROL_COLUMNS


class _FakeConn:
    def __init__(self, name):
        self.name, self.closed, self.broken = name, False, False
    def cursor(self):
        return nullcontext(self.name)


def test_dropped_connection_is_replaced_before_the_next_statement():
    opened = []
    def reopen():
        opened.append(_FakeConn(f"c{len(opened) + 2}"))
        return opened[-1]
    store = ss.StateStore(_FakeConn("c1"), reconnect=reopen)
    with store._cursor() as cur:                           # healthy: no new connection
        assert cur == "c1" and not opened
    store._conn.broken = True
    with store._cursor() as cur:                           # dropped: reopened once
        assert cur == "c2" and len(opened) == 1
    with store._cursor() as cur:
        assert cur == "c2"


def test_without_reconnect_a_dropped_connection_stays_dropped():
    # The lock connection: losing it means the advisory lock is gone, which must not be hidden.
    store = ss.StateStore(_FakeConn("c1"))
    store._conn.closed = True
    with store._cursor() as cur:
        assert cur == "c1"
    assert store.is_alive() is False


# ---- append write mode: pending appends -----------------------------------------------------------

def test_record_pending_binds_identity_position_and_run():
    conn = FakeConn(); ss.StateStore(conn).record_pending(CFG, 150, "12")
    assert conn.calls == [(ss.record_pending_sql(), (7, "d", "s", "t", "ct", "150", 12))]
    sql = ss.record_pending_sql()
    assert ("on conflict (control_id) do update set pending_position = excluded.pending_position, "
            "pending_run_id = excluded.pending_run_id") in sql
    assert "phase" not in sql and "status" not in sql        # an existing checkpoint keeps both


def test_checkpoint_advance_clears_a_pending_append_and_bulk_advance_skips_it():
    assert "pending_position = null, pending_run_id = null" in ss.upsert_checkpoint_sql()
    assert ss.bulk_advance_unchanged_sql().endswith("and pending_run_id is null")


def test_ddl_write_mode_client_attrs_and_pending_columns():
    assert "control add column if not exists write_mode text not null default 'merge'" in _DDL
    assert "control add column if not exists client_attrs text" in _DDL
    assert "write_mode" in ss.CONTROL_COLUMNS and "client_attrs" in ss.CONTROL_COLUMNS
    con = _DDL[_DDL.index("add constraint valid_write_mode"):]
    con = con[:con.index(";")]
    assert "write_mode in ('merge', 'append')" in con and "load_type in ('ct', 'full')" in con
    assert "jsonb_typeof(client_attrs::jsonb) = 'object'" in _DDL
    assert "checkpoint add column if not exists pending_position text" in _DDL
    assert "checkpoint add column if not exists pending_run_id bigint" in _DDL


# ---- batched appends: bulk checkpoint and telemetry writes ---------------------------------------

def test_bulk_batch_operations_bind_one_row_per_member():
    conn = FakeConn(); store = ss.StateStore(conn)
    a, b = dict(CFG, select_list="a, b"), dict(CFG, id=8, select_list="a")
    store.record_pending_many([(a, 150), (b, 160)], 42)
    store.advance_many([(a, 150, "incremental", None), (b, None, "loaded", "full load complete")])
    store.record_ct_begin_versions([(a, 7), (b, None)])
    store.record_pending_many([], 42)                       # nothing to send: no round trip
    assert conn.calls == [
        (ss.record_pending_sql(), [(7, "d", "s", "t", "ct", "150", 42), (8, "d", "s", "t", "ct", "160", 42)]),
        (ss.upsert_checkpoint_sql(), [(7, "d", "s", "t", "ct", "150", "incremental", "ok", None, "a, b",
                                       *ss.identity_values(a)),
                                      (8, "d", "s", "t", "ct", None, "loaded", "ok", "full load complete", "a",
                                       *ss.identity_values(b))]),
        (ss.record_ct_begin_version_sql(), [(7, 7)]),
    ]


def test_bulk_batch_operations_fill_every_placeholder():
    # The bulk writes reuse the single-row statements. If one of those gains a parameter, the bulk
    # rows must gain it too, or every batched write raises after its data committed.
    conn = FakeConn(); store = ss.StateStore(conn)
    a = dict(CFG, select_list="a, b")
    store.record_pending_many([(a, 150)], 42)
    store.advance_many([(a, 150, "incremental", None)])
    store.record_ct_begin_versions([(a, 7)])
    for sql, rows in conn.calls:
        assert all(len(row) == sql.count("%s") for row in rows), sql


def test_record_finished_events_in_bulk_and_stamps_success():
    conn = FakeConn()
    cfg = dict(CFG, task_collection="tc", sink_catalog="c", sink_schema="s", sink_table="t")
    ss.StateStore(conn).record_finished_events(3, [
        (cfg, "increment", "incremental", "merge", "ok", 100, 150, "batch2", None),
        (dict(cfg, id=8), "increment", "incremental", "merge", "failed", 100, None, "batch2", "boom")],
        duration_ms=1234)
    (ev_sql, rows), (stamp_sql, stamp_params) = conn.calls
    assert ev_sql == ss.record_event_finished_sql() and "now() - %s * interval '1 millisecond'" in ev_sql
    assert ev_sql.count("%s") == len(rows[0]) == 17
    assert rows[0] == (3, "tc", 7, "d", "s", "t", "c.s.t", "increment", "incremental", "merge", "ok", "100", "150",
                       "batch2", None, 1234, 1234)
    assert rows[1][10] == "failed" and rows[1][14] == "boom"
    assert stamp_sql == ss.touch_last_success_many_sql() and stamp_params == ([7],)


def test_record_failed_tables_action_override():
    conn = FakeConn()
    cfg = dict(CFG, task_collection="tc", sink_catalog="c", sink_schema="s", sink_table="t")
    ss.StateStore(conn).record_failed_tables(3, [(cfg, None, "x")], stage="checkpoint", action="seed")
    _, (_, ev_rows), _ = conn.calls
    assert ev_rows[0][7:10] == ("seed", None, "checkpoint")


def test_list_task_configs_shares_repeated_strings():
    def row(i):
        values = {"id": i, "select_list": "".join(["id, ", "v"]), "src_database": f"db{i}"}
        return tuple(values.get(c) for c in ss.CONTROL_COLUMNS)
    rows = [row(1), row(2)]
    sl = ss.CONTROL_COLUMNS.index("select_list")
    assert rows[0][sl] is not rows[1][sl]
    got = ss.StateStore(FakeConn(fetch=rows)).list_task_configs("tc")
    assert got[0]["select_list"] == "id, v" and got[0]["select_list"] is got[1]["select_list"]
    assert [g["src_database"] for g in got] == ["db1", "db2"] and got[1]["id"] == 2


# ---- per-source query caps: shared locks on the lock connection ------------------------------------

def test_source_shares_divide_the_cap_between_the_runs_reading_a_source():
    keys = {"connection:a": 11, "connection:b": 22}
    assert ss.source_shares(64, keys, {11: 4}) == {"connection:a": 16, "connection:b": 64}  # b: this run alone
    assert ss.source_shares(10, keys, {11: 40, 22: 3}) == {"connection:a": 1, "connection:b": 3}   # at least 1


def test_source_lock_keys_are_stable_31_bit_hashes():
    key = ss.source_lock_key("connection:sales_conn")
    assert key == ss.source_lock_key("connection:sales_conn") and 0 <= key < 2 ** 31
    assert key != ss.source_lock_key("connection:other") and 0 < ss.SOURCE_LOCK_CLASS < 2 ** 31


def test_join_sources_holds_one_shared_lock_per_source_and_counts_their_holders():
    conn = FakeConn()
    store = ss.StateStore(conn)
    keys = store.join_sources(["connection:b", "connection:a", "connection:b"])
    assert list(keys) == ["connection:a", "connection:b"]
    assert conn.calls == [("select pg_advisory_lock_shared(%s::int, %s::int)", (ss.SOURCE_LOCK_CLASS, keys[s]))
                          for s in ("connection:a", "connection:b")]
    conn.calls, conn.fetch = [], [(keys["connection:a"], 3)]
    assert store.source_runs(keys.values()) == {keys["connection:a"]: 3}
    (sql, params), = conn.calls
    assert "mode = 'ShareLock'" in sql and "classid::bigint = %s" in sql and "objid::bigint = any(%s)" in sql
    assert params == (ss.SOURCE_LOCK_CLASS, list(keys.values()))

def test_identity_values_normalize_case_port_and_key():
    cfg = {"src_type": "sqlserver", "src_host": "Srv", "src_port": None, "src_database": "DB", "src_schema": "dbo",
           "src_table": "T", "sink_catalog": "Main", "sink_schema": "B", "sink_table": "T", "primary_key": " Id, K "}
    assert ss.identity_values(cfg) == ("sqlserver://srv:1433/db/dbo/t", "main.b.t", "id,k")
    assert ss.sink_identity(dict(cfg, client_id="Tenant-1")) == "main.b.t#Tenant-1"
    assert ss.merge_key(dict(cfg, primary_key=None)) is None


def test_bulk_advance_records_identity_only_when_unknown():
    sql = ss.bulk_advance_unchanged_sql()
    assert "sink_identity = coalesce(sink_identity, %s)" in sql and sql.count("%s") == 7
    conn = FakeConn()
    ss.StateStore(conn).bulk_advance_unchanged([(7, 5, None, None, ("s", "k", "m")), (8, 6, None)])
    assert conn.calls[0][1] == [(5, None, None, "s", "k", "m", 7), (6, None, None, None, None, None, 8)]



# ---- per-database check stamps ------------------------------------------------------------------

def test_database_source_reads_a_blank_port_as_the_source_types_default():
    for src_type, port in (("sqlserver", 1433), ("oracle", 1521), ("postgresql", 5432), ("mysql", 3306)):
        blank = ss.database_source({"src_type": src_type, "src_host": "Srv", "src_port": None, "secret_key": "k"})
        assert blank == f"srv|{port}|k"
        assert ss.database_source({"src_type": src_type, "src_host": "Srv", "src_port": 0, "secret_key": "k"}) == blank
        jdbc = {"src_type": src_type, "src_host": "Srv", "src_port": port, "secret_key": "k"}
        assert ss.database_source(jdbc) == blank
    mysql = {"src_type": "mysql", "src_host": "Srv", "src_port": 3307, "secret_key": "k"}
    assert ss.database_source(mysql) == "srv|3307|k"
    assert ss.database_source({"src_host": "Srv", "src_port": None, "secret_key": "k"}) == "srv|1433|k"
    assert ss.database_source({"src_type": "db2", "src_host": "Srv", "src_port": None, "secret_key": "k"}) == "srv|0|k"
    # SQL trim() removes spaces only, so a tab is not part of the type name: an unknown type in both copies
    tab = {"src_type": "\toracle", "src_host": "Srv", "src_port": None, "secret_key": "k"}
    assert ss.database_source(tab) == "srv|0|k"
    padded = {"src_type": " oracle ", "src_host": "Srv", "src_port": None, "secret_key": "k"}
    assert ss.database_source(padded) == "srv|1521|k"


def test_a_connection_backed_row_keeps_a_blank_port_as_zero():
    # Its host comes from the UC connection, whose own port the control row does not carry.
    conn = {"src_type": "mysql", "src_connection": "conn", "src_port": None, "secret_key": None}
    assert ss.database_source(conn) == "conn|0|conn"
    assert ss.database_source({**conn, "src_port": 3307}) == "conn|3307|conn"
    assert ss.database_source({**conn, "src_host": "Srv"}) == "srv|3306|conn"


def test_database_source_names_the_server_port_and_credential_the_way_the_app_reads_it():
    jdbc = {"src_host": "Srv.Example.net", "src_port": 1433, "secret_key": "k-db1", "src_database": "db1"}
    conn = {"src_connection": "lfc_conn", "use_remote_query": True, "src_database": "db1"}
    assert ss.database_source(jdbc) == "srv.example.net|1433|k-db1"
    assert ss.database_source(conn) == "lfc_conn|0|lfc_conn"
    assert ss.database_source({**jdbc, "src_port": None, "secret_key": ""}) == "srv.example.net|1433|"
    # the same identity in SQL, for readers that join control rows to their database's stamp
    expr = ss.database_source_sql("t")
    assert "lower(coalesce(nullif(t.src_host, ''), nullif(t.src_connection, ''), ''))" in expr
    assert "coalesce(nullif(t.src_port, 0), case when (nullif(t.src_host, '') is null" in expr
    assert "coalesce(nullif(t.secret_key, ''), nullif(t.src_connection, ''), '')" in expr


def test_ddl_database_check_is_keyed_by_collection_source_and_database():
    for frag in ("create table if not exists lakefed_ingest.database_check",
                 "checked_version bigint      not null",
                 "constraint database_check_pk primary key (task_collection, source, src_database)"):
        assert frag in _DDL


def test_stamp_databases_upserts_one_row_per_database_and_reads_them_back():
    conn = FakeConn()
    store = ss.StateStore(conn)
    store.stamp_databases("coll", [("lfc_conn|0|lfc_conn", "db1", 41, 1000), ("lfc_conn|0|lfc_conn", "db2", 8, 999)],
                          run_id=12)
    (sql, params), = conn.calls
    assert sql == ss.stamp_database_sql()
    assert "on conflict (task_collection, source, src_database) do update" in sql and "checked_at = now()" in sql
    assert params == [("coll", "lfc_conn|0|lfc_conn", "db1", 41, 1000, 12), ("coll", "lfc_conn|0|lfc_conn", "db2", 8, 999, 12)]
    store.stamp_databases("coll", [], run_id=12)
    assert len(conn.calls) == 1   # nothing to write: no statement
    store.stamp_databases("coll", [("s", "db3", 9, 5)], run_id=None)   # the check job: no ingest run
    assert conn.calls[-1][1] == [("coll", "s", "db3", 9, 5, None)]
    conn.calls = conn.calls[:1]

    conn.calls, conn.fetch = [], [("s1", "db1", 41, "t1")]
    assert store.get_database_checks("coll") == {("s1", "db1"): {"version": 41, "checked_at": "t1"}}
    assert conn.calls == [(ss.get_database_checks_sql(), ("coll",))]


# ---- the check job's work list ------------------------------------------------------------------

def test_the_work_list_is_written_once_per_database_and_read_back_for_one_run():
    conn = FakeConn()
    store = ss.StateStore(conn)
    store.record_check_work("w1", "coll", [("s1", "db1"), ("s1", "db2")])
    (sql, params), = conn.calls
    assert sql == ss.record_check_work_sql() and "on conflict do nothing" in sql
    assert params == [("w1", "coll", "s1", "db1"), ("w1", "coll", "s1", "db2")]
    store.record_check_work("w1", "coll", [])
    assert len(conn.calls) == 1   # nothing to write: no statement

    conn.calls, conn.fetch = [], [("s1", "db1"), ("s1", "db2")]
    assert store.get_check_work("w1", "coll") == {("s1", "db1"), ("s1", "db2")}
    assert conn.calls == [(ss.get_check_work_sql(), ("w1", "coll"))]


def test_old_work_lists_are_pruned():
    sql = ss.prune_check_work_sql()
    assert sql.startswith("delete from lakefed_ingest.check_work where created_at <") and "%s" in sql


def test_a_check_without_a_group_takes_every_collection_with_an_enabled_row():
    conn = FakeConn(fetch=[("c1",), ("c2",)])
    assert ss.StateStore(conn).enabled_collections() == ["c1", "c2"]
    assert conn.calls == [(ss.enabled_collections_sql(), None)]
    assert "where task_enabled" in ss.enabled_collections_sql() and "order by" in ss.enabled_collections_sql()


def test_a_check_without_a_group_leaves_out_the_groups_only_a_deleted_job_holds():
    sql = ss.enabled_collections_sql()
    assert f"task_collection not in ({ss.deleted_only_collections_sql()})" in sql
    deleted_only = ss.deleted_only_collections_sql()
    assert "ds.deleted_at is not null" in deleted_only and "s.deleted_at is null" in deleted_only
    assert "m.removed_at is null" in deleted_only and "not exists" in deleted_only


def test_the_deleted_only_groups_are_read_on_their_own():
    conn = FakeConn(fetch=[("g2",)])
    assert ss.StateStore(conn).deleted_only_collections() == ["g2"]
    assert conn.calls == [(ss.deleted_only_collections_sql(), None)]


def test_the_check_reads_an_ingestion_jobs_groups_by_its_id_and_none_of_a_deleted_one():
    sql = ss.schedule_collections_sql()
    assert "m.schedule_id = %s" in sql and "m.removed_at is null" in sql and "s.deleted_at is null" in sql
    assert sql.count("%s") == 1


# ---- one connection shared by many threads: a transaction keeps other statements out of it ------------------------
# Every worker and check thread writes through the StateStore's one connection. The fake follows psycopg's rules for
# that case: a transaction opened while another is open becomes a savepoint, and a transaction that exits with an
# error rolls back what it ran. Threads wait on events (WAIT_S is only a ceiling), so each run takes the same order.

WAIT_S = 1.0


def test_a_failed_rollup_leaves_the_checkpoint_and_pending_writes_of_other_workers_committed():
    rollup_open, pending_done, checkpoint_done = threading.Event(), threading.Event(), threading.Event()

    def on_execute(thread, sql):
        if thread != "rollup":
            return
        if sql == ss.rollup_database_rewrite_sql():
            rollup_open.set()
            _wait_all([pending_done, checkpoint_done])     # the other workers run while the rollup's transaction is open
        elif sql == ss.rollup_group_rewrite_sql():
            raise RuntimeError("lakebase unreachable")

    conn = _SharedConnection(on_execute)
    store = ss.StateStore(conn)
    rollup, rollup_errors = _start("rollup", store.rewrite_rollups, "c1", ["db1"])
    assert rollup_open.wait(WAIT_S)
    pending, _ = _start("worker-b", store.record_pending, CFG, 9, 4, done=pending_done)
    checkpoint, _ = _start("worker-c", store.advance_checkpoint, CFG, 9, done=checkpoint_done)
    for thread in (rollup, pending, checkpoint):
        thread.join(timeout=10)

    assert [str(error) for error in rollup_errors] == ["lakebase unreachable"]
    assert _transactions_run_one_at_a_time(conn.events)
    assert [thread for thread, _ in conn.rolled_back] == ["rollup"] * len(conn.rolled_back)
    assert {thread for kind, thread, *_ in conn.events if kind == "execute"} >= {"worker-b", "worker-c"}


def test_rollups_of_two_collections_never_share_a_transaction():
    x_open, y_done = threading.Event(), threading.Event()

    def on_execute(thread, sql):
        if thread == "collection-x" and sql == ss.rollup_database_rewrite_sql():
            x_open.set()
            y_done.wait(WAIT_S)                            # collection y finishes its rollup while x's is open

    conn = _SharedConnection(on_execute)
    store = ss.StateStore(conn)
    collection_x, x_errors = _start("collection-x", store.rewrite_rollups, "x", ["d1"])
    assert x_open.wait(WAIT_S)
    collection_y, y_errors = _start("collection-y", store.rewrite_rollups, "y", ["d2"], done=y_done)
    for thread in (collection_x, collection_y):
        thread.join(timeout=10)

    assert x_errors == [] and y_errors == []
    assert not any(kind == "savepoint" for kind, *_ in conn.events)
    assert _transactions_run_one_at_a_time(conn.events)
    assert [kind for kind, *_ in conn.events if kind in ("begin", "commit")] == ["begin", "commit", "begin", "commit"]


class _SharedConnection:
    """A psycopg-style connection that several threads use at once. ``events`` logs each transaction step and each
    executed statement as (kind, thread, ...) in the order they ran. ``on_execute(thread, sql)`` runs before each
    statement and may block or raise. ``rolled_back`` lists the (thread, sql) of the statements a rollback undid."""

    def __init__(self, on_execute=lambda thread, sql: None):
        self.events, self.rolled_back = [], []
        self.on_execute = on_execute
        self._depth, self._open = 0, []

    def cursor(self):
        return _SharedCursor(self)

    @contextmanager
    def transaction(self):
        thread = threading.current_thread().name
        self.events.append(("savepoint" if self._depth else "begin", thread))
        self._depth += 1
        try:
            yield
        except BaseException:
            self._finish(thread, "rollback")
            raise
        self._finish(thread, "commit")

    def _finish(self, thread, kind):
        self._depth -= 1
        if self._depth == 0:
            self.events.append((kind, thread))
            if kind == "rollback":
                self.rolled_back.extend(self._open)
            self._open = []
        else:
            self.events.append(("release", thread))

    def execute(self, sql):
        thread = threading.current_thread().name
        self.on_execute(thread, sql)
        self.events.append(("execute", thread, sql))
        if self._depth:
            self._open.append((thread, sql))


class _SharedCursor:
    def __init__(self, conn):
        self.conn, self.connection = conn, conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.execute(sql)

    def executemany(self, sql, seq):
        self.conn.execute(sql)

    def fetchone(self):
        return None

    def fetchall(self):
        return []


def _transactions_run_one_at_a_time(events) -> bool:
    """True iff no transaction opened inside another, and between a transaction's begin and its end every statement
    belongs to the thread that opened it."""
    owner = None
    for event in events:
        kind, thread = event[0], event[1]
        if kind == "savepoint":
            return False
        if kind == "begin":
            if owner is not None:
                return False
            owner = thread
        elif kind in ("commit", "rollback"):
            if thread != owner:
                return False
            owner = None
        elif kind == "execute" and owner is not None and thread != owner:
            return False
    return owner is None


def _start(name, target, *args, done=None):
    """Run ``target(*args)`` on a named thread; returns (thread, errors). ``done`` is set when the call ends."""
    errors = []

    def run():
        try:
            target(*args)
        except Exception as error:
            errors.append(error)
        finally:
            if done is not None:
                done.set()

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread, errors


def _wait_all(events):
    for event in events:
        event.wait(WAIT_S)


# ---- cancel: a stopped run is cancelled, not failed --------------------------------------------

def test_a_run_is_stopping_when_its_own_status_or_its_databricks_run_says_so():
    assert ss.run_is_stopping("cancelled", None)
    assert not ss.run_is_stopping("running", None)
    assert not ss.run_is_stopping("running", {"life_cycle_state": "RUNNING", "result_state": None,
                                              "cancel_requested_at": None})
    assert ss.run_is_stopping("running", {"life_cycle_state": "RUNNING", "cancel_requested_at": "2026-10-30T15:00"})
    assert ss.run_is_stopping("running", {"life_cycle_state": "TERMINATING", "cancel_requested_at": None})
    assert ss.run_is_stopping("running", {"life_cycle_state": "TERMINATED", "result_state": "CANCELED"})
    assert not ss.run_is_stopping("running", {"life_cycle_state": "TERMINATED", "result_state": "FAILED"})


def test_a_failed_attempt_is_cancelled_when_its_run_is_stopping_or_spark_cancelled_the_job():
    assert ss.failure_status(RuntimeError("Connection reset"), stopping=True) == "cancelled"
    assert ss.failure_status("[SPARK_JOB_CANCELLED] cancelled", stopping=False) == "cancelled"
    assert ss.failure_status(RuntimeError("Login failed for user"), stopping=False) == "failed"
    assert ss.failure_status(None, stopping=False) == "failed"


def test_an_abandoned_run_is_cancelled_only_when_databricks_ended_it_cancelled_or_was_asked_to_stop():
    assert ss.abandoned_run_status({"life_cycle_state": "TERMINATED", "result_state": "CANCELED"}) == "cancelled"
    assert ss.abandoned_run_status({"life_cycle_state": "RUNNING", "cancel_requested_at": "2026-10-30"}) == "cancelled"
    assert ss.abandoned_run_status({"life_cycle_state": "TERMINATED", "result_state": "FAILED"}) == "failed"
    assert ss.abandoned_run_status(None) == "failed"

