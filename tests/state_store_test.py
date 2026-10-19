"""Unit tests for the pure decision helpers and SQL builders in state_store.py.

No database or psycopg needed — psycopg is imported lazily inside StateStore.connect.
"""
import json
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
    # control_id, db, schema, table, strategy, position, phase, status, detail, select_list
    assert sql.count("%s") == 10
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
    assert sql.count("%s") == 4  # task_collection, job_run_id, cluster_id, parallelism


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
    # status, stage, rows_read, rows_merged, position_from, position_to, error, action, pool, retry_count, id
    assert sql.count("%s") == 11
    assert "position_from = %s, position_to = %s" in sql
    assert "action = coalesce(%s, action)" in sql and "pool = coalesce(%s, pool)" in sql
    assert "retry_count = coalesce(%s, retry_count)" in sql


def test_touch_last_success_sql_stamps_checkpoint_from_event():
    sql = ss.touch_last_success_sql()
    assert ss.CHECKPOINT_TABLE in sql and ss.EVENT_TABLE in sql
    assert "last_success_at = now()" in sql
    assert sql.count("%s") == 1  # event_id


def test_finish_run_sql_updates_run_by_id():
    sql = ss.finish_run_sql()
    assert sql.startswith(f"update {ss.RUN_TABLE} set")
    assert "finished_at = now()" in sql
    assert "where run_id = %s" in sql
    # status, total, ok, failed, skipped, reseeded, detail, run_id
    assert sql.count("%s") == 8


def test_close_abandoned_runs_sql_closes_the_collections_other_open_runs_and_their_open_events():
    # A run that was cancelled or lost its cluster never finishes its ingest_run row or its in-flight
    # ingest_event rows. The next run holds the collection's lock, so any other open run of the
    # collection is dead: close it as failed, with its events, in one statement.
    sql = ss.close_abandoned_runs_sql()
    run_update, event_update = sql.split("), events as (")
    assert f"update {ss.RUN_TABLE} set status = 'failed'" in run_update
    assert "where task_collection = %s and status = 'running' and run_id <> %s" in run_update
    assert f"update {ss.EVENT_TABLE} e set status = 'failed'" in event_update
    assert "e.run_id = dead.run_id and e.status = 'running'" in event_update
    assert sql.rstrip().endswith("select (select count(*) from dead), (select count(*) from events)")
    # detail, task_collection, run_id, error
    assert sql.count("%s") == 4


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
                           (7, "d", "s", "t", "ct", "42", "incremental", "ok", None, None))]


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
         (7, "d", "s", "t", "ct", None, "seeding", "reseed_queued", "retention_miss", None)),
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
    detail, collection, run_id, error = params
    assert json.loads(detail) == {"closed_by_run": 9, "reason": ss.ABANDONED_RUN_REASON}
    assert (collection, run_id) == ("c", 9)
    assert error == f"{ss.ABANDONED_RUN_REASON}; closed by run 9"


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
        (ss.record_event_finish_sql(), ("ok", "merge", None, 3, "1", "2", None, "increment", None, None, 9)),
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


def test_record_event_finish_records_pool():
    conn = FakeConn()
    ss.StateStore(conn).record_event_finish(9, "ok", stage="merge", pool="pool3,pool17")
    assert conn.calls[0][1][8] == "pool3,pool17" and conn.calls[0][1][10] == 9


def test_record_event_finish_records_a_retried_attempt_without_stamping():
    conn = FakeConn()
    ss.StateStore(conn).record_event_finish(9, "retried", stage="seed", error="x", retry_count=1)
    assert len(conn.calls) == 1 and conn.calls[0][1][0] == "retried" and conn.calls[0][1][9] == 1


# ---- skip-no-change: bulk checkpoint load, select_list recording, bulk advance -------------

def test_get_checkpoints_one_query_any_array():
    sql = ss.get_checkpoints_sql()
    assert "control_id = any(%s)" in sql and "select_list" in sql and sql.count("%s") == 1


def test_get_checkpoints_maps_rows_by_control_id():
    conn = FakeConn(fetch=[(7, "100", "incremental", "ok", "a, b", "ct", 3, "150", 12, "2026-10-14"),
                           (8, None, "loaded", "ok", None, "full", None, None, None, None)])
    got = ss.StateStore(conn).get_checkpoints(["7", 8])
    assert conn.calls == [(ss.get_checkpoints_sql(), ([7, 8],))]
    assert got == {7: {"position": "100", "phase": "incremental", "status": "ok", "select_list": "a, b",
                       "strategy": "ct", "ct_begin_version": 3, "pending_position": "150", "pending_run_id": 12,
                       "last_success_at": "2026-10-14"},
                   8: {"position": None, "phase": "loaded", "status": "ok", "select_list": None,
                       "strategy": "full", "ct_begin_version": None, "pending_position": None,
                       "pending_run_id": None, "last_success_at": None}}
    assert ss.StateStore(FakeConn()).get_checkpoints([]) == {}


def test_advance_checkpoint_records_select_list():
    conn = FakeConn(); ss.StateStore(conn).advance_checkpoint(CFG, 42, select_list="a, b")
    assert conn.calls[0][1][-1] == "a, b"


def test_bulk_advance_never_moves_backwards_and_only_cdc():
    sql = ss.bulk_advance_unchanged_sql()
    # positions are text: compare as numbers, never lexically ('9' > '10')
    assert "position = greatest(coalesce(position, '0')::bigint, %s::bigint)::text" in sql
    assert "where control_id = %s and phase = 'incremental' and strategy = 'ct'" in sql
    assert "last_success_at = now()" in sql and "select_list = coalesce(%s, select_list)" in sql
    assert "ct_begin_version = coalesce(ct_begin_version, %s)" in sql   # never overwrite a recorded one
    assert sql.count("%s") == 4


def test_bulk_advance_binds_each_rows_own_version():
    # Rows from two databases carry their OWN database's version (CT versions are per database).
    conn = FakeConn()
    n = ss.StateStore(conn).bulk_advance_unchanged([(7, 500, None), (9, 12, "a, b", 4)])
    assert n == 2
    assert conn.calls == [(ss.bulk_advance_unchanged_sql(), [(500, None, None, 7), (12, "a, b", 4, 9)])]
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
    (ev_sql, ev_rows), (st_sql, st_rows) = conn.calls
    assert ev_sql == ss.record_event_failed_sql() and "'failed'" in ev_sql
    assert ev_rows == [(3, "tc", 7, "d", "s", "t", "c.s.t", "increment", "incremental", "detect", "db unreachable")]
    assert st_sql == ss.set_status_sql() and st_rows == [(7, "d", "s", "t", "ct", "failed", "db unreachable")]


def test_record_failed_tables_full_rows_and_plan_stage():
    conn = FakeConn()
    cfg = dict(CFG, load_type="full", task_collection="tc", sink_catalog="c", sink_schema="s", sink_table="t")
    ss.StateStore(conn).record_failed_tables(3, [(cfg, None, "unsupported")], stage="plan")
    (_, ev_rows), (_, st_rows) = conn.calls
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
        return self.name


def test_dropped_connection_is_replaced_before_the_next_statement():
    opened = []
    def reopen():
        opened.append(_FakeConn(f"c{len(opened) + 2}"))
        return opened[-1]
    store = ss.StateStore(_FakeConn("c1"), reconnect=reopen)
    assert store._cursor() == "c1" and not opened          # healthy: no new connection
    store._conn.broken = True
    assert store._cursor() == "c2" and len(opened) == 1    # dropped: reopened once
    assert store._cursor() == "c2"


def test_without_reconnect_a_dropped_connection_stays_dropped():
    # The lock connection: losing it means the advisory lock is gone, which must not be hidden.
    store = ss.StateStore(_FakeConn("c1"))
    store._conn.closed = True
    assert store._cursor() == "c1" and store.is_alive() is False


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
        (ss.upsert_checkpoint_sql(), [(7, "d", "s", "t", "ct", "150", "incremental", "ok", None, "a, b"),
                                      (8, "d", "s", "t", "ct", None, "loaded", "ok", "full load complete", "a")]),
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
    (_, ev_rows), _ = conn.calls
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
