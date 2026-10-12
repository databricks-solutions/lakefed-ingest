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

import re
from typing import List, Optional

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
    "client_id",
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


# --------------------------------------------------------------------------------------
# SQL builders (parameterized, %s placeholders) — unit-tested for shape, no DB needed.
# --------------------------------------------------------------------------------------
def mixed_sinks_sql() -> str:
    """Sinks (lower-cased catalog, schema, table) claimed by a dedicated row (client_id NULL) AND
    other rows — across ALL collections, enabled or not (a disabled row still owns its data)."""
    return (f"select lower(sink_catalog), lower(sink_schema), lower(sink_table) from {CONTROL_TABLE} "
            "group by 1, 2, 3 having count(*) filter (where client_id is null) > 0 and count(*) > 1")


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
    return (f"select control_id, position, phase, status, select_list, strategy, ct_begin_version "
            f"from {CHECKPOINT_TABLE} where control_id = any(%s)")


def upsert_checkpoint_sql() -> str:
    """Insert-or-update a checkpoint (keyed by control_id): set strategy + position, phase/status,
    and record the select_list the sink was written with (kept when NULL is passed).

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
        "updated_at = now()"
    )


def bulk_advance_unchanged_sql() -> str:
    """Skip-no-change bookkeeping for ONE unchanged CT table (run via executemany over all of them).

    Advances the checkpoint to the database's version captured BEFORE change detection, never
    backwards (greatest, compared as numbers — positions are text), only for CT rows in the cdc
    phase, and stamps freshness. Advancing quiet tables matters: CT cleanup keeps raising each
    table's min valid version even when the table never changes, so a checkpoint left behind would
    trigger a false reseed once retention passes.
    Params: CT version (captured), select_list (or NULL to keep), CT begin_version (recorded only
    when not yet known — legacy rows), control_id.
    """
    return (
        f"update {CHECKPOINT_TABLE} set "
        "position = greatest(coalesce(position, '0')::bigint, %s::bigint)::text, "
        "status = 'ok', detail = null, "
        "select_list = coalesce(%s, select_list), ct_begin_version = coalesce(ct_begin_version, %s), "
        "last_success_at = now(), updated_at = now() "
        "where control_id = %s and phase = 'incremental' and strategy = 'ct'"
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
    """Finalize an ingest_event row: finished_at/duration + outcome + metrics."""
    return (
        f"update {EVENT_TABLE} set "
        "finished_at = now(), "
        "duration_ms = (extract(epoch from (now() - started_at)) * 1000)::bigint, "
        "status = %s, stage = %s, rows_read = %s, rows_merged = %s, "
        "position_from = %s, position_to = %s, error = %s, "
        "action = coalesce(%s, action), pool = coalesce(%s, pool) "
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


# --------------------------------------------------------------------------------------
# Lakebase accessor — thin psycopg wrapper. Runs on cluster/job compute only.
# --------------------------------------------------------------------------------------
class StateStore:
    """Connection + typed operations over the Lakebase hot-state tables.

    ``config`` keys: host, port, dbname, user, password (Lakebase OAuth token or role
    password), and optional sslmode (default 'require').
    """

    def __init__(self, conn):
        self._conn = conn

    @property
    def connection(self):
        """The underlying psycopg connection (for raw DDL / bulk inserts)."""
        return self._conn

    @classmethod
    def connect_lakebase(cls, instance_name: str, dbname: str, user: str = None,
                         host: str = None, application_name: str = None) -> "StateStore":
        """Connect using a short-lived Databricks-minted Lakebase OAuth token (no stored password).

        Resolves the instance DNS and the run principal via the Databricks SDK and mints a fresh
        credential each call, so there is no password secret to manage or rotate. Runs on cluster
        compute (the run identity must be a Postgres role on the instance).
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
        cred = w.database.generate_database_credential(
            request_id=str(uuid.uuid4()), instance_names=[instance_name]
        )
        conn = psycopg.connect(
            host=host, port=5432, dbname=dbname, user=user, password=cred.token,
            sslmode="require", autocommit=True,
            **({"application_name": application_name} if application_name else {}),
        )
        return cls(conn)

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

    def get_task(self, task_id) -> dict:
        """Return one config row as a dict keyed by CONTROL_COLUMNS (empty dict if not found)."""
        with self._conn.cursor() as cur:
            cur.execute(get_task_sql(), (int(task_id),))
            row = cur.fetchone()
        return dict(zip(CONTROL_COLUMNS, row)) if row else {}

    def list_task_configs(self, task_collection: str) -> list:
        """Return the full config rows (dicts keyed by CONTROL_COLUMNS) for a collection's
        enabled tasks. The sweep loads these once, then ingests them in-process (threaded),
        instead of one job run per table."""
        with self._conn.cursor() as cur:
            cur.execute(list_task_configs_sql(), (task_collection,))
            return [dict(zip(CONTROL_COLUMNS, row)) for row in cur.fetchall()]

    def mixed_sinks(self) -> set:
        """{(catalog, schema, table)} lower-cased — see ``mixed_sinks_sql``."""
        with self._conn.cursor() as cur:
            cur.execute(mixed_sinks_sql())
            return {tuple(r) for r in cur.fetchall()}

    def get_checkpoint(self, control_id) -> dict:
        """Return {'position', 'phase', 'status', 'strategy'} for a control task, or seeding
        defaults. ``position`` is the raw text; the owning strategy decodes it."""
        with self._conn.cursor() as cur:
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
        with self._conn.cursor() as cur:
            cur.execute(get_checkpoints_sql(), (ids,))
            rows = cur.fetchall()
        return {int(r[0]): {"position": r[1], "phase": r[2], "status": r[3], "select_list": r[4],
                            "strategy": r[5], "ct_begin_version": r[6]} for r in rows}

    def advance_checkpoint(self, cfg: dict, position, phase=INCREMENTAL, status="ok", detail=None,
                           select_list=None, strategy=None):
        """Persist a new checkpoint for control row ``cfg``. Call ONLY after the bronze write has
        committed (cross-system at-least-once: re-reading one window on crash is harmless).
        ``position`` is stored as text (None -> NULL); ``strategy`` defaults to the row's load_type.
        ``select_list`` records the columns the sink was written with (None keeps the old value)."""
        with self._conn.cursor() as cur:
            cur.execute(upsert_checkpoint_sql(),
                        (*task_identity(cfg), strategy or strategy_name(cfg), position_text(position),
                         phase, status, detail, select_list))

    def bulk_advance_unchanged(self, rows) -> int:
        """Advance many unchanged CT tables in one executemany: rows = [(control_id, ct_version,
        select_list_or_None[, ct_begin_version_or_None]), ...]. Returns the number of rows submitted."""
        params = [(int(r[1]), r[2], (r[3] if len(r) > 3 else None), int(r[0])) for r in rows]
        if not params:
            return 0
        with self._conn.cursor() as cur:
            cur.executemany(bulk_advance_unchanged_sql(), params)
        return len(params)

    def record_ct_begin_version(self, cfg: dict, begin_version) -> None:
        """Record the table's CT begin_version on its checkpoint (seed time, or first time seen)."""
        if begin_version is None:
            return
        with self._conn.cursor() as cur:
            cur.execute(record_ct_begin_version_sql(), (int(begin_version), int(cfg["id"])))

    def set_status(self, cfg: dict, status, detail=None):
        with self._conn.cursor() as cur:
            cur.execute(set_status_sql(), (*task_identity(cfg), strategy_name(cfg), status, detail))

    def enqueue_reseed(self, cfg: dict, reason="retention_miss"):
        """Mark the table for a full reseed and flip its phase back to seeding (position cleared)."""
        ident = task_identity(cfg)
        with self._conn.cursor() as cur:
            cur.execute(enqueue_reseed_sql(), (*ident, reason))
            cur.execute(upsert_checkpoint_sql(),
                        (*ident, strategy_name(cfg), None, SEEDING, "reseed_queued", reason, None))

    # ---- Observability / telemetry -----------------------------------------------------
    def start_run(self, task_collection, job_run_id, cluster_id, parallelism) -> int:
        """Open an ingest_run row; returns its run_id."""
        with self._conn.cursor() as cur:
            cur.execute(start_run_sql(),
                        (task_collection, job_run_id, cluster_id, int(parallelism)))
            return int(cur.fetchone()[0])

    def record_event_start(self, run_id, cfg, action, phase, pool) -> int:
        """Open an ingest_event row for one (run, table) from a control-row dict; returns id."""
        sink_fqn = f"{cfg.get('sink_catalog')}.{cfg.get('sink_schema')}.{cfg.get('sink_table')}"
        with self._conn.cursor() as cur:
            cur.execute(record_event_start_sql(), (
                int(run_id), cfg.get("task_collection"), cfg.get("id"),
                cfg.get("src_database"), cfg.get("src_schema"), cfg.get("src_table"),
                sink_fqn, action, phase, pool,
            ))
            return int(cur.fetchone()[0])

    def record_event_finish(self, event_id, status, stage=None, rows_read=None, rows_merged=None,
                            position_from=None, position_to=None, error=None,
                            action=None, pool=None) -> None:
        """Finalize an ingest_event row; on 'ok' also stamp checkpoint.last_success_at.
        Positions are stored as text. ``action`` (optional) corrects the provisional action set at
        start (e.g. 'reseed'); ``pool`` (optional) records the FAIR pool(s) the table's units ran in."""
        with self._conn.cursor() as cur:
            cur.execute(record_event_finish_sql(), (
                status, stage, rows_read, rows_merged, position_text(position_from),
                position_text(position_to),
                (error[:2000] if isinstance(error, str) else error), action, pool, int(event_id),
            ))
            if status == "ok":
                cur.execute(touch_last_success_sql(), (int(event_id),))

    def record_failed_tables(self, run_id, items, stage="detect") -> int:
        """Bulk-record tables that failed before per-table work (their source database was
        unreachable during change detection, or the planner rejected the row — stage 'plan'): one
        finished 'failed' ingest_event row each and the checkpoint status set to 'failed'
        (phase/position untouched). items = [(cfg, cp, error)]."""
        if not items:
            return 0
        events, statuses = [], []
        for cfg, cp, err in items:
            sink = f"{cfg.get('sink_catalog')}.{cfg.get('sink_schema')}.{cfg.get('sink_table')}"
            e = (err or "")[:2000]
            action = "full" if strategy_name(cfg) == "full" else "increment"
            events.append((int(run_id), cfg.get("task_collection"), cfg.get("id"),
                           cfg.get("src_database"), cfg.get("src_schema"), cfg.get("src_table"),
                           sink, action, (cp or {}).get("phase"), stage, e))
            statuses.append((*task_identity(cfg), strategy_name(cfg), "failed", e[:500]))
        with self._conn.cursor() as cur:
            cur.executemany(record_event_failed_sql(), events)
            cur.executemany(set_status_sql(), statuses)
        return len(items)

    def finish_run(self, run_id, total, ok, failed, skipped=0, reseeded=0,
                   status="completed", detail=None) -> None:
        """Finalize an ingest_run row with roll-up counts."""
        with self._conn.cursor() as cur:
            cur.execute(finish_run_sql(), (
                status, total, ok, failed, skipped, reseeded, detail, int(run_id),
            ))

    # ---- per-collection run lock (hold on a DEDICATED connection for the whole sweep) -------
    def try_lock_collection(self, task_collection: str) -> bool:
        """True iff this connection now holds the collection's lock (non-blocking)."""
        with self._conn.cursor() as cur:
            cur.execute(try_lock_collection_sql(), (collection_lock_key(task_collection),))
            return bool(cur.fetchone()[0])

    def unlock_collection(self, task_collection: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(unlock_collection_sql(), (collection_lock_key(task_collection),))

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
