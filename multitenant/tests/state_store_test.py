"""Unit tests for the pure decision helpers and SQL builders in state_store.py.

No database or psycopg needed — psycopg is imported lazily inside StateStore.connect.
"""
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

def test_get_checkpoint_sql_targets_checkpoint_table():
    sql = ss.get_checkpoint_sql()
    assert ss.CHECKPOINT_TABLE in sql
    assert sql.count("%s") == 3  # db, schema, table


def test_upsert_checkpoint_is_idempotent_on_pk():
    sql = ss.upsert_checkpoint_sql()
    assert "on conflict (src_database, src_schema, src_table) do update" in sql
    assert sql.count("%s") == 7  # db, schema, table, version, phase, status, detail


def test_enqueue_reseed_targets_reseed_table():
    sql = ss.enqueue_reseed_sql()
    assert ss.RESEED_TABLE in sql
    assert sql.count("%s") == 4  # db, schema, table, reason


def test_set_status_sql_upserts():
    sql = ss.set_status_sql()
    assert "on conflict" in sql
    assert sql.count("%s") == 5  # db, schema, table, status, detail


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
    # status, stage, rows_read, rows_merged, ct_version_from, ct_version_to, error, id
    assert sql.count("%s") == 8


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
