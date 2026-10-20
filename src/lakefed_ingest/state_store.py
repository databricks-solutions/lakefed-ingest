"""Lakebase state accessor for lakefed_ingest (control, checkpoints, reseed queue, telemetry).

The framework is strategy-agnostic: ``control.load_type`` selects a load strategy
(``load_strategies``) and ``control.src_type`` a source adapter (``source_adapters``). Per ADR-0004,
the per-(db,table) checkpoint, phase/status, and reseed queue live in Lakebase (OLTP Postgres),
alongside the cold `control` config. This module is imported by the cluster-side notebooks
(ingest, apply_lakebase_schema), which own all writes to the hot store. Warehouse-free: everything
runs on the cluster.

Checkpoints are keyed by the control task id (``control.id``), not by db/schema/table, so
same-named databases on different servers never share a checkpoint. A checkpoint is generic:
``strategy`` names the strategy that wrote it and ``position`` (text) is that strategy's resume
position (CT: the database CT version).

The pure decision helpers and SQL builders at the top have no database dependency and are
unit-tested in tests/state_store_test.py. ``psycopg`` is imported lazily inside
``StateStore.connect`` so the module (and its tests) load without the driver installed.

Naming: `control`, `checkpoint`, and the telemetry tables (`ingest_run`, `ingest_event`) are
framework-level; the reseed surface (reseed_queue, is_reseed_needed, phases seeding/incremental) is used by
the CT strategy.
"""

from __future__ import annotations

import json
import re
import threading
import zlib
from typing import Callable, List, Optional

SCHEMA = "lakefed_ingest"
CONTROL_TABLE = f"{SCHEMA}.control"
CHECKPOINT_TABLE = f"{SCHEMA}.checkpoint"
RESEED_TABLE = f"{SCHEMA}.reseed_queue"
RUN_TABLE = f"{SCHEMA}.ingest_run"        # observability: one row per ingestion run
EVENT_TABLE = f"{SCHEMA}.ingest_event"    # observability: one row per (run, table)

SEEDING = "seeding"
INCREMENTAL = "incremental"  # ct / cursor: seeded, applying increments
LOADED = "loaded"    # full: last reload committed (no resumable position)

# Columns selected from the control table, in order. list_task_configs / get_task return dicts
# keyed by these names (the ``cfg`` the sweep and ct_engine consume).
CONTROL_COLUMNS = [
    "id", "job_name", "task_collection", "src_type", "src_connection", "src_host", "src_port",
    "secret_key", "src_database",
    "src_catalog", "src_schema", "src_table", "sink_catalog", "sink_schema", "sink_table",
    "enable_iceberg_reads", "primary_key", "sink_cluster_cols", "load_type", "load_partitioned",
    "partition_col", "partition_size_mb",
    "select_list", "use_remote_query", "ct_enabled", "cursor_column", "cdc_capture_instance",
    "jdbc_options", "source_instance_group", "tier", "priority", "warehouse_id", "task_enabled",
    "client_id", "write_mode", "client_attrs",
]


def strategy_name(cfg: dict) -> str:
    """The load strategy a control row's checkpoint belongs to (``control.load_type``; default ct)."""
    return (cfg.get("load_type") or "ct").strip().lower()


def position_text(value) -> Optional[str]:
    """Checkpoint positions are stored as text (strategy-owned encoding); None stays NULL."""
    return None if value is None else str(value)


# --------------------------------------------------------------------------------------
# Schema migration helpers for apply_lakebase_schema.ipynb (pure; unit-tested).
# --------------------------------------------------------------------------------------
# Renames from the CT-only naming to the generic one. Postgres has no IF EXISTS form for column
# renames (and table/index renames fail when the target exists), so the applier inspects the catalog
# and runs only the renames still pending — re-applying is a no-op. Applied BEFORE lakebase_schema.sql,
# so its create-if-not-exists statements find the renamed objects (and their data) in place.
#   (kind, table (columns only), old name, new name)
RENAMES = [
    ("table", None, "ct_checkpoint", "checkpoint"),
    ("table", None, "sweep_run", "ingest_run"),
    ("column", "checkpoint", "ct_version", "position"),
    ("column", "ingest_run", "sweep_id", "run_id"),
    ("column", "ingest_event", "sweep_id", "run_id"),
    ("column", "ingest_event", "ct_version_from", "position_from"),
    ("column", "ingest_event", "ct_version_to", "position_to"),
    ("index", None, "ct_checkpoint_control_pk", "checkpoint_control_pk"),
    ("index", None, "ct_checkpoint_control_uq", "checkpoint_control_uq"),
    ("index", None, "sweep_run_pkey", "ingest_run_pkey"),
    ("index", None, "sweep_run_collection_idx", "ingest_run_collection_idx"),
    ("index", None, "ingest_event_sweep_idx", "ingest_event_run_idx"),
    ("sequence", None, "sweep_run_sweep_id_seq", "ingest_run_run_id_seq"),
]


def rename_statements(relations, columns, schema: str = SCHEMA) -> List[str]:
    """The pending RENAMES as SQL, given the current catalog of ``schema``.

    ``relations`` = names of the schema's relations (tables, indexes, sequences — pg_class);
    ``columns`` = {(table, column)}. A rename runs only when the old name exists and the new one
    does not; table renames are simulated so the column renames that follow see the new table name.
    """
    rels, cols = set(relations), set(columns)
    out = []
    for kind, table, old, new in RENAMES:
        if kind == "column":
            if (table, old) in cols and (table, new) not in cols:
                out.append(f"alter table {schema}.{table} rename column {old} to {new}")
                cols = (cols - {(table, old)}) | {(table, new)}
        elif old in rels and new not in rels:
            out.append(f"alter {kind} {schema}.{old} rename to {new}")
            rels = (rels - {old}) | {new}
            if kind == "table":
                cols = {((new if t == old else t), c) for t, c in cols}
    return out


def split_sql_statements(ddl: str) -> List[str]:
    """Split lakebase_schema.sql into statements: strip ``--`` comments first (they may contain ';'),
    then split on ';'. Safe for this controlled DDL (no ';' or '--' inside string literals, no
    DO $$ blocks)."""
    no_comments = "\n".join(re.sub(r"--.*$", "", line) for line in ddl.splitlines())
    return [s.strip() for s in no_comments.split(";") if s.strip()]


# Schema version. The version applied is recorded in `schema_version`, so an ingestion run whose code
# is newer than its database applies the pending migration itself (StateStore.migrate): upgrading is a
# deploy. Bump SCHEMA_VERSION with every change to lakebase_schema.sql or RENAMES; a unit test pins the
# DDL's fingerprint to it, so a schema change can't ship without a bump.
#   0 = no version recorded (deployed before versions existed)   1 = first recorded version
SCHEMA_VERSION = 1
# Advisory lock that serializes migrations (the setup job and ingestion runs of any collection): two
# int keys, so it can't collide with a collection's one-key lock whatever the collection is named,
# and a class of its own (not SOURCE_LOCK_CLASS).
SCHEMA_LOCK = (0x6C667363, 0)


def schema_fingerprint(ddl: str) -> str:
    """sha256 of the DDL's statements (comments and whitespace ignored) and RENAMES: what the
    SCHEMA_VERSION pin test compares."""
    import hashlib
    body = "\n".join(" ".join(s.split()) for s in split_sql_statements(ddl)) + "\n" + repr(RENAMES)
    return hashlib.sha256(body.encode()).hexdigest()


# --------------------------------------------------------------------------------------
# Pure decision helpers (no DB) — unit-tested directly.
# --------------------------------------------------------------------------------------
def is_reseed_needed(checkpoint_version: Optional[int], min_valid_version: Optional[int]) -> bool:
    """True when the checkpoint is too old to read CHANGES from (CT retention lapsed).

    SQL Server's CHANGETABLE(CHANGES tbl, @v) only returns rows when
    ``@v >= CHANGE_TRACKING_MIN_VALID_VERSION(object_id)``. If the checkpoint has fallen
    behind that minimum, changes have been purged and the table must be fully reseeded.

    A NULL/None ``min_valid_version`` means CT is not enabled (or the object is unknown)
    -> reseed. A None checkpoint means we have never seeded -> reseed.
    """
    if min_valid_version is None:
        return True
    if checkpoint_version is None:
        return True
    return int(checkpoint_version) < int(min_valid_version)


def next_phase(current_phase: str) -> str:
    """Advance the per-table phase state machine: seeding -> incremental (terminal)."""
    return INCREMENTAL


def decide_action(phase: str, checkpoint_version: Optional[int],
                  min_valid_version: Optional[int]) -> str:
    """Decide what the per-table state machine should do this cycle.

    Returns one of:
      - 'seed'      : phase is seeding (first onboarding, or re-seed after retention loss).
      - 'reseed'    : phase is incremental but CT retention lapsed (checkpoint < min valid) -> re-seed.
      - 'increment' : phase is incremental and the checkpoint is still valid -> read CHANGES + MERGE.

    In the seeding branch ``min_valid_version`` is irrelevant and may be None. In the cdc branch
    the caller fetches it (ct_engine.min_valid_version) first, then calls this to split
    reseed vs increment via is_reseed_needed.
    """
    if phase == SEEDING:
        return "seed"
    return "reseed" if is_reseed_needed(checkpoint_version, min_valid_version) else "increment"


def select_list_columns(select_list: Optional[str]) -> List[str]:
    """Normalized (lower-case, trimmed) column names of a comma-separated select_list."""
    return [c.strip().lower() for c in (select_list or "").split(",") if c.strip()]


def select_list_added_columns(control_select_list: str,
                              recorded_select_list: Optional[str]) -> Optional[List[str]]:
    """Columns the control row's select_list has that the checkpoint's recorded select_list lacks.

    Returns None when nothing was recorded yet (legacy checkpoint -> caller falls back to checking
    the sink's columns once). Only ADDED columns matter: CT returns changed rows only, so a new
    column needs a reseed to backfill history; reordering is harmless (writes map BY NAME) and a
    removed column just stops being written (bronze keeps it; existing columns are never dropped).
    """
    if recorded_select_list is None:
        return None
    have = set(select_list_columns(recorded_select_list))
    return [c for c in select_list_columns(control_select_list) if c not in have]


def task_identity(cfg: dict) -> tuple:
    """(control_id, src_database, src_schema, src_table) for a control-row dict — the key + the
    readable source identity written alongside every checkpoint/status/reseed row."""
    return (int(cfg["id"]), cfg["src_database"], cfg["src_schema"], cfg["src_table"])


def _sink_label(cfg: dict) -> str:
    """catalog.schema.table of the row's sink, as telemetry records it (``ingest_event.sink_fqn``)."""
    return f"{cfg.get('sink_catalog')}.{cfg.get('sink_schema')}.{cfg.get('sink_table')}"


# --------------------------------------------------------------------------------------
# SQL builders (parameterized, %s placeholders) — unit-tested for shape, no DB needed.
# --------------------------------------------------------------------------------------
def mixed_sinks_sql() -> str:
    """Sinks (lower-cased catalog, schema, table) claimed by a dedicated row (client_id NULL) AND
    other rows, across ALL collections, enabled or not (a disabled row still owns its data). Starts
    from the dedicated rows (partial index control_dedicated_sink_idx) and probes each one's sink in
    control_sink_client_uq, so every sweep doesn't scan a control table that holds millions of
    consolidated rows."""
    return (f"select lower(d.sink_catalog), lower(d.sink_schema), lower(d.sink_table) from {CONTROL_TABLE} d "
            f"where d.client_id is null and exists (select 1 from {CONTROL_TABLE} o "
            "where lower(o.sink_catalog) = lower(d.sink_catalog) and lower(o.sink_schema) = lower(d.sink_schema) "
            "and lower(o.sink_table) = lower(d.sink_table) and o.id <> d.id)")


def list_task_configs_sql() -> str:
    """Full control rows for a collection's enabled tasks, ordered by tier then priority.

    The consolidated sweep loads a whole task_collection in one query, then iterates the tables
    in-process (threaded) rather than fanning out one job run per table. Strategy-agnostic:
    selects every enabled task in the collection (per-task strategy is decided later, in the
    sweep's per-table branch). Param: task_collection.
    """
    cols = ", ".join(CONTROL_COLUMNS)
    return (
        f"select {cols} from {CONTROL_TABLE} "
        "where task_collection = %s and task_enabled = true "
        "order by tier nulls last, priority nulls last, id"
    )


def get_task_sql() -> str:
    """Fetch one config row by id (columns in CONTROL_COLUMNS order)."""
    return f"select {', '.join(CONTROL_COLUMNS)} from {CONTROL_TABLE} where id = %s"


def get_checkpoint_sql() -> str:
    """Checkpoint for one control task. Param: control_id."""
    return f"select position, phase, status, strategy from {CHECKPOINT_TABLE} where control_id = %s"


def get_checkpoints_sql() -> str:
    """Checkpoints for MANY control tasks in one round trip (the sweep's planning phase).
    Param: list of control ids (bound as a Postgres array)."""
    return (f"select control_id, position, phase, status, select_list, strategy, ct_begin_version, "
            f"pending_position, pending_run_id, last_success_at from {CHECKPOINT_TABLE} "
            "where control_id = any(%s)")


def upsert_checkpoint_sql() -> str:
    """Insert-or-update a checkpoint (keyed by control_id): set strategy + position, phase/status,
    and record the select_list the sink was written with (kept when NULL is passed). Clears a
    pending append (see ``record_pending_sql``): the checkpoint now says where the sink is.

    Params: control_id, src_database, src_schema, src_table, strategy, position, phase, status,
    detail, select_list.
    """
    return (
        f"insert into {CHECKPOINT_TABLE} "
        "(control_id, src_database, src_schema, src_table, strategy, position, phase, status, detail, "
        "select_list, updated_at) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
        "on conflict (control_id) do update set "
        "strategy = excluded.strategy, position = excluded.position, phase = excluded.phase, "
        "status = excluded.status, detail = excluded.detail, "
        f"select_list = coalesce(excluded.select_list, checkpoint.select_list), "
        "pending_position = null, pending_run_id = null, "
        "updated_at = now()"
    )


def record_pending_sql() -> str:
    """Before an append write: the position it brings the sink to and the run writing it. A new row
    (first seed) starts in phase seeding; an existing one keeps its position, phase and status.

    Params: control_id, src_database, src_schema, src_table, strategy, pending_position, pending_run_id.
    """
    return (
        f"insert into {CHECKPOINT_TABLE} "
        "(control_id, src_database, src_schema, src_table, strategy, pending_position, pending_run_id, "
        "updated_at) values (%s, %s, %s, %s, %s, %s, %s, now()) "
        "on conflict (control_id) do update set "
        "pending_position = excluded.pending_position, pending_run_id = excluded.pending_run_id, "
        "updated_at = now()"
    )


def bulk_advance_unchanged_sql() -> str:
    """Skip-no-change bookkeeping for ONE unchanged CT table (run via executemany over all of them).

    Advances the checkpoint to the database's version captured BEFORE change detection, never
    backwards (greatest, compared as numbers — positions are text), only for CT rows in the cdc
    phase, and stamps freshness. Advancing quiet tables matters: CT cleanup keeps raising each
    table's min valid version even when the table never changes, so a checkpoint left behind would
    trigger a false reseed once retention passes. A table with a pending append is never advanced
    here: the planner sends it to the worker, which checks whether that append committed.
    Params: CT version (captured), select_list (or NULL to keep), CT begin_version (recorded only
    when not yet known — legacy rows), control_id.
    """
    return (
        f"update {CHECKPOINT_TABLE} set "
        "position = greatest(coalesce(position, '0')::bigint, %s::bigint)::text, "
        "status = 'ok', detail = null, "
        "select_list = coalesce(%s, select_list), ct_begin_version = coalesce(ct_begin_version, %s), "
        "last_success_at = now(), updated_at = now() "
        "where control_id = %s and phase = 'incremental' and strategy = 'ct' and pending_run_id is null"
    )


def record_ct_begin_version_sql() -> str:
    """Params: begin_version, control_id."""
    return f"update {CHECKPOINT_TABLE} set ct_begin_version = %s, updated_at = now() where control_id = %s"


def set_status_sql() -> str:
    """Update only status/detail (failure isolation / quarantine), keyed by control_id. A new row
    (failure before the first checkpoint) records the row's strategy; an existing row keeps its own.

    Params: control_id, src_database, src_schema, src_table, strategy, status, detail.
    """
    return (
        f"insert into {CHECKPOINT_TABLE} "
        "(control_id, src_database, src_schema, src_table, strategy, status, detail, updated_at) "
        "values (%s, %s, %s, %s, %s, %s, %s, now()) "
        "on conflict (control_id) do update set "
        "status = excluded.status, detail = excluded.detail, updated_at = now()"
    )


def enqueue_reseed_sql() -> str:
    """Params: control_id, src_database, src_schema, src_table, reason."""
    return (
        f"insert into {RESEED_TABLE} (control_id, src_database, src_schema, src_table, reason) "
        "values (%s, %s, %s, %s, %s)"
    )


# --------------------------------------------------------------------------------------
# Observability / telemetry builders. One ingestion run ingests many tables in one job task, so
# there is no per-table Jobs-UI task; these record per-table progress to Lakebase (concurrent
# point writes), queryable live through the Lakebase UC catalog.
# --------------------------------------------------------------------------------------
def start_run_sql() -> str:
    """Open an ingest_run row (status 'running'); returns the new run_id."""
    return (
        f"insert into {RUN_TABLE} "
        "(task_collection, job_run_id, cluster_id, parallelism, status) "
        "values (%s, %s, %s, %s, 'running') returning run_id"
    )


def record_event_start_sql() -> str:
    """Open an ingest_event row for one (run, table), status 'running'; returns id."""
    return (
        f"insert into {EVENT_TABLE} "
        "(run_id, task_collection, control_id, src_database, src_schema, src_table, "
        "sink_fqn, action, phase, pool, status) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'running') returning id"
    )


def record_event_finish_sql() -> str:
    """Finalize an ingest_event row: finished_at/duration + outcome + metrics. ``retry_count`` is the
    number of earlier attempts of the table in this run (``table_retry``)."""
    return (
        f"update {EVENT_TABLE} set "
        "finished_at = now(), "
        "duration_ms = (extract(epoch from (now() - started_at)) * 1000)::bigint, "
        "status = %s, stage = %s, rows_read = %s, rows_merged = %s, "
        "position_from = %s, position_to = %s, error = %s, "
        "action = coalesce(%s, action), pool = coalesce(%s, pool), retry_count = coalesce(%s, retry_count) "
        "where id = %s"
    )


def record_event_failed_sql() -> str:
    """Insert an already-FINISHED failed ingest_event row (bulk, via executemany) — used when a whole
    source database fails change detection, so its tables are visible as failed without per-table
    work. Params: run_id, task_collection, control_id, src_database, src_schema, src_table,
    sink_fqn, action, phase, stage, error."""
    return (
        f"insert into {EVENT_TABLE} "
        "(run_id, task_collection, control_id, src_database, src_schema, src_table, sink_fqn, "
        "action, phase, stage, error, status, finished_at, duration_ms) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'failed', now(), 0)"
    )


def record_event_finished_sql() -> str:
    """Insert an already-FINISHED ingest_event row (bulk, via executemany) for the members of a batched
    append, which ran as one unit. Params: run_id, task_collection, control_id, src_database,
    src_schema, src_table, sink_fqn, action, phase, stage, status, position_from, position_to, pool,
    error, duration_ms (for started_at), duration_ms."""
    return (
        f"insert into {EVENT_TABLE} "
        "(run_id, task_collection, control_id, src_database, src_schema, src_table, sink_fqn, "
        "action, phase, stage, status, position_from, position_to, pool, error, "
        "started_at, finished_at, duration_ms) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "now() - %s * interval '1 millisecond', now(), %s)"
    )


def touch_last_success_many_sql() -> str:
    """Stamp checkpoint.last_success_at for many control ids. Param: array of control ids."""
    return f"update {CHECKPOINT_TABLE} set last_success_at = now() where control_id = any(%s)"


def touch_last_success_sql() -> str:
    """Stamp checkpoint.last_success_at from a successful ingest_event (freshness/lag)."""
    return (
        f"update {CHECKPOINT_TABLE} cc set last_success_at = now() "
        f"from {EVENT_TABLE} e "
        "where e.id = %s and cc.control_id = e.control_id"
    )


def finish_run_sql() -> str:
    """Finalize an ingest_run row with roll-up counts."""
    return (
        f"update {RUN_TABLE} set "
        "finished_at = now(), status = %s, total = %s, ok = %s, failed = %s, "
        "skipped = %s, reseeded = %s, detail = %s "
        "where run_id = %s"
    )


ABANDONED_RUN_REASON = "the run ended without finishing (cancelled, or its cluster went away)"


def close_abandoned_runs_sql() -> str:
    """Close a collection's other runs still 'running', and their events still 'running', as failed.
    Only a run holding the collection's lock may call it: no other sweep of the collection is then
    alive, so an open run is one that was cancelled or lost its cluster and will never finish its rows.
    A table that was waiting for its retry (table_retries) when the run died has a last attempt
    'retried' and no later one: it is closed as failed too, keeping its own error and timing.
    Params: detail, task_collection, run_id, error (appended to a retried attempt's), error.
    Returns (runs closed, events closed)."""
    return (
        f"with dead as (update {RUN_TABLE} set status = 'failed', finished_at = now(), detail = %s "
        "where task_collection = %s and status = 'running' and run_id <> %s returning run_id), "
        f"events as (update {EVENT_TABLE} e set status = 'failed', finished_at = coalesce(e.finished_at, now()), "
        "duration_ms = coalesce(e.duration_ms, (extract(epoch from (now() - e.started_at)) * 1000)::bigint), "
        "error = case when e.status = 'retried' then concat(e.error, ' | ', %s) else %s end "
        "from dead where e.run_id = dead.run_id and (e.status = 'running' or ("
        f"e.status = 'retried' and not exists (select 1 from {EVENT_TABLE} later "
        "where later.run_id = e.run_id and later.control_id = e.control_id and later.id > e.id))) "
        "returning e.id) "
        "select (select count(*) from dead), (select count(*) from events)"
    )


# --------------------------------------------------------------------------------------
# Per-collection run lock. Sweeps of DIFFERENT task_collections run concurrently (that is the
# horizontal-scale pattern); two sweeps of the SAME collection must not overlap (e.g. a schedule
# firing while the previous run is still going). A session-level Postgres advisory lock keyed by
# the collection provides that, and it is released automatically when the holding connection
# drops — a crashed run can never leave a stale lock behind. 64-bit hash => negligible collisions.
# Changing the namespace changes every key: don't let a run of an older build (namespace
# 'lakefed_ingest.sweep:') overlap a run of this one for the same collection during an upgrade.
# --------------------------------------------------------------------------------------
LOCK_NAMESPACE = "lakefed_ingest:"


def collection_lock_key(task_collection: str) -> str:
    """Namespaced advisory-lock key for a task_collection."""
    return LOCK_NAMESPACE + str(task_collection)


def lock_application_name(task_collection: str, job_run_id: str = "") -> str:
    """Label for the lock connection, visible in pg_stat_activity (Postgres caps it at 63 chars)."""
    return f"lakefed_ingest:{task_collection}:{job_run_id}"[:63]


def try_lock_collection_sql() -> str:
    """Non-blocking: returns true iff this session acquired the collection's lock."""
    return "select pg_try_advisory_lock(hashtextextended(%s, 0))"


def unlock_collection_sql() -> str:
    """Release the collection's session-level advisory lock."""
    return "select pg_advisory_unlock(hashtextextended(%s, 0))"


# Per-source query caps (job parameter max_queries_per_source): every run holds a session-level SHARED
# advisory lock per source it reads, on its collection-lock connection, so the holders of a source's
# lock are the runs reading it now, and a run that stops leaves the count with its session. The lock
# has two int keys, a fixed class and a hash of the source (the two-key space is separate from the
# one-key collection locks).
SOURCE_LOCK_CLASS = 0x6C666973


def source_lock_key(source: str) -> int:
    """A source's lock key: a stable non-negative 31-bit hash of its name (``ct_engine.source_key``)."""
    return zlib.crc32(source.encode("utf-8")) & 0x7FFFFFFF


def join_source_sql() -> str:
    return "select pg_advisory_lock_shared(%s::int, %s::int)"


def source_runs_sql() -> str:
    """How many sessions hold each of the given sources' shared locks: rows (lock key, runs)."""
    return ("select objid::bigint, count(*) from pg_locks where locktype = 'advisory' and granted "
            "and mode = 'ShareLock' and objsubid = 2 and classid::bigint = %s "
            "and database = (select oid from pg_database where datname = current_database()) "
            "and objid::bigint = any(%s) group by objid")


def source_shares(cap: int, keys: dict, runs: dict) -> dict:
    """{source: this run's share of ``cap``}: the cap divided by the runs reading the source now (this
    run among them), at least 1. ``keys``: {source: lock key}; ``runs``: {lock key: runs}."""
    return {source: max(1, int(cap) // max(1, runs.get(key, 1))) for source, key in keys.items()}


# --------------------------------------------------------------------------------------
# Lakebase accessor — thin psycopg wrapper. Runs on cluster/job compute only.
# --------------------------------------------------------------------------------------
class StateStore:
    """Connection + typed operations over the Lakebase hot-state tables.

    ``config`` keys: host, port, dbname, user, password (Lakebase OAuth token or role
    password), and optional sslmode (default 'require').
    """

    def __init__(self, conn, reconnect: Optional[Callable[[], object]] = None):
        self._conn = conn
        # Optional factory for a fresh connection: a dropped connection (network blip, failover,
        # idle timeout) is replaced before the next statement instead of failing the rest of the run.
        # The statement that hit the drop still fails (and its table retries next run), so nothing
        # is ever executed twice. Never set for the lock connection: a dropped lock must stay dropped.
        self._reconnect = reconnect
        self._reconnect_lock = threading.Lock()

    def _cursor(self):
        if self._reconnect is not None and (self._conn.closed or self._conn.broken):
            with self._reconnect_lock:
                if self._conn.closed or self._conn.broken:
                    self._conn = self._reconnect()
        return self._conn.cursor()

    @property
    def connection(self):
        """The underlying psycopg connection (for raw DDL / bulk inserts)."""
        return self._conn

    @classmethod
    def connect_lakebase(cls, instance_name: str, dbname: str, user: str = None,
                         host: str = None, application_name: str = None,
                         reconnect: bool = False) -> "StateStore":
        """Connect using a short-lived Databricks-minted Lakebase OAuth token (no stored password).

        Resolves the instance DNS and the run principal via the Databricks SDK and mints a fresh
        credential each call, so there is no password secret to manage or rotate. Runs on cluster
        compute (the run identity must be a Postgres role on the instance).

        Token expiry is checked only at login, so an open connection outlives its token. With
        ``reconnect`` a dropped connection is reopened with a freshly minted token.
        """
        import uuid
        import psycopg
        from databricks.sdk import WorkspaceClient

        # Requires databricks-sdk with the Database (Lakebase) API — the cluster tasks pin a
        # recent version (databricks-sdk==0.145.0) since the DBR-bundled sdk may predate it.
        w = WorkspaceClient()
        if host is None:
            host = w.database.get_database_instance(name=instance_name).read_write_dns
        if user is None:
            user = w.current_user.me().user_name
        def _open():
            cred = w.database.generate_database_credential(
                request_id=str(uuid.uuid4()), instance_names=[instance_name]
            )
            return psycopg.connect(
                host=host, port=5432, dbname=dbname, user=user, password=cred.token,
                sslmode="require", autocommit=True,
                **({"application_name": application_name} if application_name else {}),
            )
        return cls(_open(), reconnect=_open if reconnect else None)

    @classmethod
    def connect(cls, config: dict) -> "StateStore":
        """Connect with explicit static credentials (secret-scope fallback / local testing)."""
        import psycopg  # lazy: keeps the module importable without the driver (e.g. in tests)

        conn = psycopg.connect(
            host=config["host"],
            port=int(config.get("port", 5432)),
            dbname=config["dbname"],
            user=config["user"],
            password=config["password"],
            sslmode=config.get("sslmode", "require"),
            autocommit=True,
        )
        return cls(conn)

    @staticmethod
    def _applied_version(cur) -> int:
        cur.execute("select to_regclass(%s)", (f"{SCHEMA}.schema_version",))
        if cur.fetchone()[0] is None:
            return 0
        cur.execute(f"select coalesce(max(version), 0) from {SCHEMA}.schema_version")
        return int(cur.fetchone()[0])

    def schema_version(self) -> int:
        """The newest schema version applied to this database; 0 if none was recorded (a database
        set up before versions existed, or an empty one)."""
        with self._cursor() as cur:
            return self._applied_version(cur)

    def migrate(self, ddl: str, force: bool = False, lock_timeout_s: int = 60) -> Optional[dict]:
        """Bring the schema to SCHEMA_VERSION: the pending RENAMES, then every statement of
        lakebase_schema.sql (``ddl``), then record the version, all in ONE transaction, so a failure
        (a constraint existing rows violate, a lock timeout) leaves the database as it was.

        Returns None when the database is already at SCHEMA_VERSION, unless ``force`` (the setup job,
        which re-applies every time); else {'from': version before, 'renames': [statements run]}.
        A transaction-level advisory lock serializes migrators: one that waited finds the version
        recorded and returns None. The DDL waits at most ``lock_timeout_s`` per table for statements
        of running jobs or the app. Refuses a database newer than this code: re-applying older DDL
        could undo the newer migration.
        """
        with self._cursor() as cur, cur.connection.transaction():
            cur.execute("select pg_advisory_xact_lock(%s::int, %s::int)", SCHEMA_LOCK)
            cur.execute(f"set local lock_timeout = '{int(lock_timeout_s)}s'")
            before = self._applied_version(cur)
            if before > SCHEMA_VERSION:
                raise RuntimeError(f"the Lakebase schema is at version {before}, newer than this code's "
                                   f"{SCHEMA_VERSION}: deploy the newer code instead")
            if before == SCHEMA_VERSION and not force:
                return None
            # Instances created before the schema was renamed: move lakefed_ingest_mt (all tables and
            # data) to lakefed_ingest first.
            cur.execute("select nspname from pg_namespace where nspname in ('lakefed_ingest_mt', %s)", (SCHEMA,))
            present = {r[0] for r in cur.fetchall()}
            if "lakefed_ingest_mt" in present and SCHEMA not in present:
                cur.execute(f"alter schema lakefed_ingest_mt rename to {SCHEMA}")
            cur.execute("select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                        "where n.nspname = %s", (SCHEMA,))
            relations = {r[0] for r in cur.fetchall()}
            cur.execute("select table_name, column_name from information_schema.columns where table_schema = %s",
                        (SCHEMA,))
            renames = rename_statements(relations, {(r[0], r[1]) for r in cur.fetchall()})
            for stmt in renames:
                cur.execute(stmt)
            for stmt in split_sql_statements(ddl):
                cur.execute(stmt)
            cur.execute(f"insert into {SCHEMA}.schema_version (version) values (%s) on conflict (version) "
                        "do update set applied_at = now(), applied_by = current_user", (SCHEMA_VERSION,))
        return {"from": before, "renames": renames}

    def get_task(self, task_id) -> dict:
        """Return one config row as a dict keyed by CONTROL_COLUMNS (empty dict if not found)."""
        with self._cursor() as cur:
            cur.execute(get_task_sql(), (int(task_id),))
            row = cur.fetchone()
        return dict(zip(CONTROL_COLUMNS, row)) if row else {}

    def list_task_configs(self, task_collection: str) -> list:
        """Return the full config rows (dicts keyed by CONTROL_COLUMNS) for a collection's
        enabled tasks. The sweep loads these once, then ingests them in-process (threaded),
        instead of one job run per table."""
        with self._cursor() as cur:
            cur.execute(list_task_configs_sql(), (task_collection,))
            rows = cur.fetchall()
        # Most values repeat across a collection's rows (one select_list per source table for
        # thousands of databases, the sink names, the server): share one string object per value so
        # a collection of hundreds of thousands of rows doesn't hold a copy per row.
        shared: dict = {}
        def share(v):
            return shared.setdefault(v, v) if isinstance(v, str) else v
        return [dict(zip(CONTROL_COLUMNS, map(share, row))) for row in rows]

    def mixed_sinks(self) -> set:
        """{(catalog, schema, table)} lower-cased — see ``mixed_sinks_sql``."""
        with self._cursor() as cur:
            cur.execute(mixed_sinks_sql())
            return {tuple(r) for r in cur.fetchall()}

    def get_checkpoint(self, control_id) -> dict:
        """Return {'position', 'phase', 'status', 'strategy'} for a control task, or seeding
        defaults. ``position`` is the raw text; the owning strategy decodes it."""
        with self._cursor() as cur:
            cur.execute(get_checkpoint_sql(), (int(control_id),))
            row = cur.fetchone()
        if row is None:
            return {"position": None, "phase": SEEDING, "status": "ok", "strategy": None}
        return {"position": row[0], "phase": row[1], "status": row[2], "strategy": row[3]}

    def get_checkpoints(self, control_ids) -> dict:
        """{control_id: {'position', 'phase', 'status', 'select_list', 'strategy'}} for every id that
        has a checkpoint, in ONE query (ids without one are simply absent -> seed)."""
        ids = [int(i) for i in control_ids]
        if not ids:
            return {}
        with self._cursor() as cur:
            cur.execute(get_checkpoints_sql(), (ids,))
            rows = cur.fetchall()
        return {int(r[0]): {"position": r[1], "phase": r[2], "status": r[3], "select_list": r[4],
                            "strategy": r[5], "ct_begin_version": r[6], "pending_position": r[7],
                            "pending_run_id": r[8], "last_success_at": r[9]} for r in rows}

    def advance_checkpoint(self, cfg: dict, position, phase=INCREMENTAL, status="ok", detail=None,
                           select_list=None, strategy=None):
        """Persist a new checkpoint for control row ``cfg``. Call ONLY after the bronze write has
        committed (cross-system at-least-once: re-reading one window on crash is harmless).
        ``position`` is stored as text (None -> NULL); ``strategy`` defaults to the row's load_type.
        ``select_list`` records the columns the sink was written with (None keeps the old value)."""
        with self._cursor() as cur:
            cur.execute(upsert_checkpoint_sql(),
                        (*task_identity(cfg), strategy or strategy_name(cfg), position_text(position),
                         phase, status, detail, select_list))

    def record_pending(self, cfg: dict, position, run_id) -> None:
        """Record an append write about to start (see ``record_pending_sql``). Call BEFORE the write."""
        with self._cursor() as cur:
            cur.execute(record_pending_sql(), (*task_identity(cfg), strategy_name(cfg),
                                               position_text(position), int(run_id)))

    # ---- bulk forms, for the members of a batched append (one executemany each) --------------
    def record_pending_many(self, items, run_id) -> None:
        """``record_pending`` for many rows: items = [(cfg, position)]."""
        params = [(*task_identity(cfg), strategy_name(cfg), position_text(pos), int(run_id)) for cfg, pos in items]
        if params:
            with self._cursor() as cur:
                cur.executemany(record_pending_sql(), params)

    def advance_many(self, items) -> None:
        """``advance_checkpoint`` (status ok, recording each row's select_list) for many rows:
        items = [(cfg, position, phase, detail)]. Call ONLY after their write committed."""
        params = [(*task_identity(cfg), strategy_name(cfg), position_text(pos), phase, "ok", detail,
                   cfg.get("select_list")) for cfg, pos, phase, detail in items]
        if params:
            with self._cursor() as cur:
                cur.executemany(upsert_checkpoint_sql(), params)

    def record_ct_begin_versions(self, items) -> None:
        """``record_ct_begin_version`` for many rows: items = [(cfg, begin_version)] (None skipped)."""
        params = [(int(bv), int(cfg["id"])) for cfg, bv in items if bv is not None]
        if params:
            with self._cursor() as cur:
                cur.executemany(record_ct_begin_version_sql(), params)

    def record_finished_events(self, run_id, items, duration_ms=0) -> None:
        """One finished ingest_event per item; 'ok' ones also stamp checkpoint.last_success_at.
        items = [(cfg, action, phase, stage, status, position_from, position_to, pool, error)]."""
        rows, ok = [], []
        for cfg, action, phase, stage, status, p_from, p_to, pool, error in items:
            rows.append((int(run_id), cfg.get("task_collection"), cfg.get("id"), cfg.get("src_database"),
                         cfg.get("src_schema"), cfg.get("src_table"), _sink_label(cfg), action, phase, stage,
                         status, position_text(p_from), position_text(p_to), pool,
                         None if error is None else str(error)[:2000], int(duration_ms), int(duration_ms)))
            if status == "ok":
                ok.append(int(cfg["id"]))
        if not rows:
            return
        with self._cursor() as cur:
            cur.executemany(record_event_finished_sql(), rows)
            if ok:
                cur.execute(touch_last_success_many_sql(), (ok,))

    def bulk_advance_unchanged(self, rows) -> int:
        """Advance many unchanged CT tables in one executemany: rows = [(control_id, ct_version,
        select_list_or_None[, ct_begin_version_or_None]), ...]. Returns the number of rows submitted."""
        params = [(int(r[1]), r[2], (r[3] if len(r) > 3 else None), int(r[0])) for r in rows]
        if not params:
            return 0
        with self._cursor() as cur:
            cur.executemany(bulk_advance_unchanged_sql(), params)
        return len(params)

    def record_ct_begin_version(self, cfg: dict, begin_version) -> None:
        """Record the table's CT begin_version on its checkpoint (seed time, or first time seen)."""
        if begin_version is None:
            return
        with self._cursor() as cur:
            cur.execute(record_ct_begin_version_sql(), (int(begin_version), int(cfg["id"])))

    def set_status(self, cfg: dict, status, detail=None):
        with self._cursor() as cur:
            cur.execute(set_status_sql(), (*task_identity(cfg), strategy_name(cfg), status, detail))

    def enqueue_reseed(self, cfg: dict, reason="retention_miss"):
        """Mark the table for a full reseed and flip its phase back to seeding (position cleared)."""
        ident = task_identity(cfg)
        with self._cursor() as cur:
            cur.execute(enqueue_reseed_sql(), (*ident, reason))
            cur.execute(upsert_checkpoint_sql(),
                        (*ident, strategy_name(cfg), None, SEEDING, "reseed_queued", reason, None))

    # ---- Observability / telemetry -----------------------------------------------------
    def start_run(self, task_collection, job_run_id, cluster_id, parallelism) -> int:
        """Open an ingest_run row; returns its run_id."""
        with self._cursor() as cur:
            cur.execute(start_run_sql(),
                        (task_collection, job_run_id, cluster_id, int(parallelism)))
            return int(cur.fetchone()[0])

    def record_event_start(self, run_id, cfg, action, phase, pool) -> int:
        """Open an ingest_event row for one (run, table) from a control-row dict; returns id."""
        sink_fqn = f"{cfg.get('sink_catalog')}.{cfg.get('sink_schema')}.{cfg.get('sink_table')}"
        with self._cursor() as cur:
            cur.execute(record_event_start_sql(), (
                int(run_id), cfg.get("task_collection"), cfg.get("id"),
                cfg.get("src_database"), cfg.get("src_schema"), cfg.get("src_table"),
                sink_fqn, action, phase, pool,
            ))
            return int(cur.fetchone()[0])

    def record_event_finish(self, event_id, status, stage=None, rows_read=None, rows_merged=None,
                            position_from=None, position_to=None, error=None,
                            action=None, pool=None, retry_count=None) -> None:
        """Finalize an ingest_event row; on 'ok' also stamp checkpoint.last_success_at.
        Positions are stored as text. ``action`` (optional) corrects the provisional action set at
        start (e.g. 'reseed'); ``pool`` (optional) records the FAIR pool(s) the table's units ran in;
        ``retry_count`` (optional) the table's earlier attempts in this run. Status 'retried' is an
        attempt that failed and is tried again."""
        with self._cursor() as cur:
            cur.execute(record_event_finish_sql(), (
                status, stage, rows_read, rows_merged, position_text(position_from),
                position_text(position_to),
                (error[:2000] if isinstance(error, str) else error), action, pool, retry_count, int(event_id),
            ))
            if status == "ok":
                cur.execute(touch_last_success_sql(), (int(event_id),))

    def record_failed_tables(self, run_id, items, stage="detect", action=None) -> int:
        """Bulk-record tables that failed before per-table work (their source database was
        unreachable during change detection, or the planner rejected the row: stage 'plan'), or in
        a batched append: one finished 'failed' ingest_event row each and the checkpoint status set to
        'failed' (phase/position untouched). items = [(cfg, cp, error)]; ``action`` defaults to
        'full' / 'increment' by the row's strategy."""
        if not items:
            return 0
        events, statuses = [], []
        for cfg, cp, err in items:
            sink = _sink_label(cfg)
            e = (err or "")[:2000]
            action_ = action or ("full" if strategy_name(cfg) == "full" else "increment")
            events.append((int(run_id), cfg.get("task_collection"), cfg.get("id"),
                           cfg.get("src_database"), cfg.get("src_schema"), cfg.get("src_table"),
                           sink, action_, (cp or {}).get("phase"), stage, e))
            statuses.append((*task_identity(cfg), strategy_name(cfg), "failed", e[:500]))
        with self._cursor() as cur:
            cur.executemany(record_event_failed_sql(), events)
            cur.executemany(set_status_sql(), statuses)
        return len(items)

    def finish_run(self, run_id, total, ok, failed, skipped=0, reseeded=0,
                   status="completed", detail=None) -> None:
        """Finalize an ingest_run row with roll-up counts."""
        with self._cursor() as cur:
            cur.execute(finish_run_sql(), (
                status, total, ok, failed, skipped, reseeded, detail, int(run_id),
            ))

    def close_abandoned_runs(self, task_collection, run_id) -> tuple:
        """Close the collection's runs that ended without finishing (see ``close_abandoned_runs_sql``);
        ``run_id`` is the caller's own run, which must hold the collection's lock. Returns (runs, events)."""
        detail = json.dumps({"closed_by_run": int(run_id), "reason": ABANDONED_RUN_REASON})
        error = f"{ABANDONED_RUN_REASON}; closed by run {int(run_id)}"
        with self._cursor() as cur:
            cur.execute(close_abandoned_runs_sql(), (detail, task_collection, int(run_id), error, error))
            runs, events = cur.fetchone()
        return int(runs), int(events)

    # ---- per-collection run lock (hold on a DEDICATED connection for the whole sweep) -------
    def try_lock_collection(self, task_collection: str) -> bool:
        """True iff this connection now holds the collection's lock (non-blocking)."""
        with self._cursor() as cur:
            cur.execute(try_lock_collection_sql(), (collection_lock_key(task_collection),))
            return bool(cur.fetchone()[0])

    def unlock_collection(self, task_collection: str) -> None:
        with self._cursor() as cur:
            cur.execute(unlock_collection_sql(), (collection_lock_key(task_collection),))

    def join_sources(self, sources) -> dict:
        """Hold a shared lock per source until this connection closes (see ``SOURCE_LOCK_CLASS``);
        returns {source: lock key}."""
        keys = {source: source_lock_key(source) for source in sorted(set(sources))}
        with self._cursor() as cur:
            for key in keys.values():
                cur.execute(join_source_sql(), (SOURCE_LOCK_CLASS, key))
        return keys

    def source_runs(self, keys) -> dict:
        """{lock key: runs holding it now}, for the given source lock keys."""
        with self._cursor() as cur:
            cur.execute(source_runs_sql(), (SOURCE_LOCK_CLASS, [int(k) for k in keys]))
            return {int(key): int(runs) for key, runs in cur.fetchall()}

    def is_alive(self) -> bool:
        """True iff the connection is still usable. A dropped lock connection means the
        session-level advisory lock it held has been released."""
        try:
            if self._conn.closed:
                return False
            with self._conn.cursor() as cur:
                cur.execute("select 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        """Close the connection (also releases any session-level advisory locks it holds)."""
        try:
            self._conn.close()
        except Exception:
            pass
