"""Unit tests for the pure decision helpers and SQL builders in state_store.py.

No database or psycopg needed — psycopg is imported lazily inside StateStore.connect.
"""
from pathlib import Path

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
def test_next_phase_seeding_to_cdc():
    assert ss.next_phase(ss.SEEDING) == ss.CDC


def test_next_phase_cdc_is_terminal():
    assert ss.next_phase(ss.CDC) == ss.CDC


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
    assert sql.count("%s") == 8  # control_id, db, schema, table, version, phase, status, detail


def test_enqueue_reseed_targets_reseed_table():
    sql = ss.enqueue_reseed_sql()
    assert ss.RESEED_TABLE in sql
    assert "control_id" in sql
    assert sql.count("%s") == 5  # control_id, db, schema, table, reason


def test_set_status_sql_upserts_on_control_id():
    sql = ss.set_status_sql()
    assert "on conflict (control_id)" in sql
    assert sql.count("%s") == 6  # control_id, db, schema, table, status, detail


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
_DDL = (Path(__file__).parent.parent / "src" / "multitenant" / "lakebase_schema.sql").read_text()


def test_ddl_ct_constraint_no_longer_requires_remote_query():
    # The CT constraint is re-declared idempotently and requires a resolvable host, not the
    # governed remote_query transport.
    assert "drop constraint if exists mt_valid_ct_metadata" in _DDL
    con = _DDL[_DDL.index("add constraint mt_valid_ct_metadata"):]
    con = con[:con.index(";")]
    assert "use_remote_query" not in con
    assert "src_host is not null or src_connection is not null" in con


def test_ddl_migrates_checkpoint_to_control_id():
    assert "constraint ct_checkpoint_control_pk primary key (control_id)" in _DDL
    assert "alter table lakefed_ingest_mt.ct_checkpoint add column if not exists control_id" in _DDL
    assert "drop constraint if exists ct_checkpoint_pkey" in _DDL
    assert "create unique index if not exists ct_checkpoint_control_uq" in _DDL


def test_ddl_adds_jdbc_columns_idempotently():
    for col in ("src_host", "src_port", "secret_key"):
        assert f"alter table lakefed_ingest_mt.control add column if not exists {col}" in _DDL


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
    assert ss.decide_action(ss.CDC, 5, 10) == "reseed"


def test_decide_action_cdc_valid_increments():
    assert ss.decide_action(ss.CDC, 10, 5) == "increment"
    assert ss.decide_action(ss.CDC, 10, 10) == "increment"


def test_decide_action_cdc_missing_min_valid_reseeds():
    # CT not enabled / unknown object (min_valid None) -> reseed.
    assert ss.decide_action(ss.CDC, 10, None) == "reseed"


# ---- observability / telemetry builders --------------------------------------
# One sweep_run row per run, one ingest_event row per (sweep, table): INSERT at start,
# UPDATE at finish. These re-create per-table visibility that the single-task sweep hides.

def test_start_sweep_sql_inserts_running_and_returns_id():
    sql = ss.start_sweep_sql()
    assert ss.SWEEP_TABLE in sql
    assert "'running'" in sql
    assert "returning sweep_id" in sql
    assert sql.count("%s") == 4  # task_collection, job_run_id, cluster_id, parallelism


def test_record_event_start_sql_inserts_running_and_returns_id():
    sql = ss.record_event_start_sql()
    assert ss.EVENT_TABLE in sql
    assert "'running'" in sql
    assert "returning id" in sql
    # sweep_id, task_collection, control_id, src_database, src_schema, src_table,
    # sink_fqn, action, phase, pool
    assert sql.count("%s") == 10


def test_record_event_finish_sql_updates_event_by_id():
    sql = ss.record_event_finish_sql()
    assert sql.startswith(f"update {ss.EVENT_TABLE} set")
    assert "finished_at = now()" in sql and "duration_ms" in sql
    assert "where id = %s" in sql
    # status, stage, rows_read, rows_merged, ct_version_from, ct_version_to, error, action, id
    assert sql.count("%s") == 9
    assert "action = coalesce(%s, action)" in sql


def test_touch_last_success_sql_stamps_checkpoint_from_event():
    sql = ss.touch_last_success_sql()
    assert ss.CHECKPOINT_TABLE in sql and ss.EVENT_TABLE in sql
    assert "last_success_at = now()" in sql
    assert sql.count("%s") == 1  # event_id


def test_finish_sweep_sql_updates_sweep_by_id():
    sql = ss.finish_sweep_sql()
    assert sql.startswith(f"update {ss.SWEEP_TABLE} set")
    assert "finished_at = now()" in sql
    assert "where sweep_id = %s" in sql
    # status, total, ok, failed, skipped, reseeded, detail, sweep_id
    assert sql.count("%s") == 8


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
    assert name == "lakefed_mt_sweep:smoke_test:12345"
    assert len(ss.lock_application_name("c" * 200, "r" * 50)) == 63   # Postgres limit


# ---- partitioned seed control settings ----------------------------------------------------
def test_control_columns_include_partition_settings():
    for col in ("load_partitioned", "partition_col", "partition_size_mb"):
        assert col in ss.CONTROL_COLUMNS


def test_partitioning_ddl_and_idempotent_migration():
    assert "partition_col         text" in _DDL and "partition_size_mb     int" in _DDL
    assert "add column if not exists partition_col" in _DDL
    assert "add column if not exists partition_size_mb" in _DDL
    assert "drop constraint if exists mt_valid_partitioning" in _DDL
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

    def fetchone(self):
        return self.conn.fetch.pop(0) if self.conn.fetch else None


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


def test_advance_version_binds_identity_then_checkpoint():
    conn = FakeConn(); ss.StateStore(conn).advance_version(CFG, 42)
    assert conn.calls == [(ss.upsert_checkpoint_sql(), (7, "d", "s", "t", 42, "cdc", "ok", None))]


def test_set_status_binds_identity_then_status():
    conn = FakeConn(); ss.StateStore(conn).set_status(CFG, "failed", "boom")
    assert conn.calls == [(ss.set_status_sql(), (7, "d", "s", "t", "failed", "boom"))]


def test_enqueue_reseed_queues_then_flips_phase_to_seeding():
    conn = FakeConn(); ss.StateStore(conn).enqueue_reseed(CFG)
    assert conn.calls == [
        (ss.enqueue_reseed_sql(), (7, "d", "s", "t", "retention_miss")),
        (ss.upsert_checkpoint_sql(), (7, "d", "s", "t", 0, "seeding", "reseed_queued", "retention_miss")),
    ]


def test_get_checkpoint_binds_control_id_and_defaults_to_seeding():
    conn = FakeConn(fetch=[None]); cp = ss.StateStore(conn).get_checkpoint("7")
    assert conn.calls == [(ss.get_checkpoint_sql(), (7,))]
    assert cp == {"ct_version": 0, "phase": "seeding", "status": "ok"}
    conn = FakeConn(fetch=[(287493, "cdc", "ok")])
    assert ss.StateStore(conn).get_checkpoint(7) == {"ct_version": 287493, "phase": "cdc", "status": "ok"}


def test_try_lock_collection_reads_result_and_binds_key():
    for got in (True, False):
        conn = FakeConn(fetch=[(got,)])
        assert ss.StateStore(conn).try_lock_collection("c") is got
        assert conn.calls == [(ss.try_lock_collection_sql(), (ss.collection_lock_key("c"),))]


def test_is_alive_false_when_closed_or_erroring_true_otherwise():
    assert ss.StateStore(FakeConn(closed=True)).is_alive() is False
    assert ss.StateStore(FakeConn(raise_on_execute=True)).is_alive() is False
    assert ss.StateStore(FakeConn()).is_alive() is True


def test_close_swallows_exceptions():
    ss.StateStore(FakeConn(raise_on_close=True)).close()   # must not raise


def test_record_event_finish_ok_stamps_last_success():
    conn = FakeConn()
    ss.StateStore(conn).record_event_finish(9, "ok", stage="merge", rows_merged=3,
                                            ct_version_from=1, ct_version_to=2, action="increment")
    assert conn.calls == [
        (ss.record_event_finish_sql(), ("ok", "merge", None, 3, 1, 2, None, "increment", 9)),
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
    assert len(conn.calls) == 1 and conn.calls[0][1][7] == "reseed"
