"""Lakebase hot-state accessor for multi-tenant ingestion.

Multi-tenant ingestion is strategy-agnostic at the framework level; Change Tracking (CT) is
the first ingestion strategy. Per ADR-0004, the per-(db,table) checkpoint, phase/status, and
reseed queue live in Lakebase (OLTP Postgres), alongside the cold `control` config. This module
is imported by the cluster-side notebooks (copy_data_sweep, get_task, get_checkpoint,
checkpoint_seed, mark_reseed, advance_checkpoint) which own all writes to the hot store — a DBSQL
warehouse cannot write to Lakebase.

The pure decision helpers and SQL builders at the top have no database dependency and are
unit-tested in multitenant/tests/state_store_test.py. ``psycopg`` is imported lazily inside
``StateStore.connect`` so the module (and its tests) load without the driver installed.

Naming: `control` and the task-list fetch (list_task_configs) are framework-level and
strategy-agnostic; the checkpoint/reseed surface (ct_checkpoint, reseed_queue, is_reseed_needed,
phase) is specific to the CT strategy.
"""

from __future__ import annotations

from typing import Optional

SCHEMA = "lakefed_ingest_mt"
CONTROL_TABLE = f"{SCHEMA}.control"
CHECKPOINT_TABLE = f"{SCHEMA}.ct_checkpoint"
RESEED_TABLE = f"{SCHEMA}.reseed_queue"

SEEDING = "seeding"
CDC = "cdc"

# Columns selected from the control table, in order. get_task returns a dict keyed by these
# names; the cluster get_task notebook publishes each as a task value for downstream tasks.
CONTROL_COLUMNS = [
    "id", "job_name", "task_collection", "src_type", "src_connection", "src_database",
    "src_catalog", "src_schema", "src_table", "sink_catalog", "sink_schema", "sink_table",
    "enable_iceberg_reads", "primary_key", "sink_cluster_cols", "load_type", "load_partitioned",
    "select_list", "use_remote_query", "ct_enabled", "source_instance_group", "tier",
    "priority", "warehouse_id", "task_enabled",
]


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
    """Advance the per-table phase state machine: seeding -> cdc (cdc is terminal)."""
    return CDC if current_phase == SEEDING else CDC


def decide_action(phase: str, checkpoint_version: Optional[int],
                  min_valid_version: Optional[int]) -> str:
    """Decide what the per-table state machine should do this cycle.

    Returns one of:
      - 'seed'      : phase is seeding (first onboarding, or re-seed after retention loss).
      - 'reseed'    : phase is cdc but CT retention lapsed (checkpoint < min valid) -> re-seed.
      - 'increment' : phase is cdc and the checkpoint is still valid -> read CHANGES + MERGE.

    In the seeding branch ``min_valid_version`` is irrelevant and may be None. In the cdc branch
    the caller fetches it (ct_engine.min_valid_version) first, then calls this to split
    reseed vs increment via is_reseed_needed.
    """
    if phase == SEEDING:
        return "seed"
    return "reseed" if is_reseed_needed(checkpoint_version, min_valid_version) else "increment"


# --------------------------------------------------------------------------------------
# SQL builders (parameterized, %s placeholders) — unit-tested for shape, no DB needed.
# --------------------------------------------------------------------------------------
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
    return (
        f"select ct_version, phase, status from {CHECKPOINT_TABLE} "
        "where src_database = %s and src_schema = %s and src_table = %s"
    )


def upsert_checkpoint_sql() -> str:
    """Insert-or-update a checkpoint. Advancing the version and setting phase/status."""
    return (
        f"insert into {CHECKPOINT_TABLE} "
        "(src_database, src_schema, src_table, ct_version, phase, status, detail, updated_at) "
        "values (%s, %s, %s, %s, %s, %s, %s, now()) "
        "on conflict (src_database, src_schema, src_table) do update set "
        "ct_version = excluded.ct_version, phase = excluded.phase, "
        "status = excluded.status, detail = excluded.detail, updated_at = now()"
    )


def set_status_sql() -> str:
    """Update only status/detail (used for failure isolation / quarantine)."""
    return (
        f"insert into {CHECKPOINT_TABLE} "
        "(src_database, src_schema, src_table, status, detail, updated_at) "
        "values (%s, %s, %s, %s, %s, now()) "
        "on conflict (src_database, src_schema, src_table) do update set "
        "status = excluded.status, detail = excluded.detail, updated_at = now()"
    )


def enqueue_reseed_sql() -> str:
    return (
        f"insert into {RESEED_TABLE} (src_database, src_schema, src_table, reason) "
        "values (%s, %s, %s, %s)"
    )


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
                         host: str = None) -> "StateStore":
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

    def get_checkpoint(self, db: str, schema: str, table: str) -> dict:
        """Return {'ct_version', 'phase', 'status'} or seeding defaults when absent."""
        with self._conn.cursor() as cur:
            cur.execute(get_checkpoint_sql(), (db, schema, table))
            row = cur.fetchone()
        if row is None:
            return {"ct_version": 0, "phase": SEEDING, "status": "ok"}
        return {"ct_version": int(row[0]), "phase": row[1], "status": row[2]}

    def advance_version(self, db, schema, table, ct_version, phase=CDC, status="ok", detail=None):
        """Persist a new checkpoint. Call ONLY after the bronze MERGE has committed
        (cross-system at-least-once: re-reading one window on crash is harmless)."""
        with self._conn.cursor() as cur:
            cur.execute(
                upsert_checkpoint_sql(),
                (db, schema, table, int(ct_version), phase, status, detail),
            )

    def set_status(self, db, schema, table, status, detail=None):
        with self._conn.cursor() as cur:
            cur.execute(set_status_sql(), (db, schema, table, status, detail))

    def enqueue_reseed(self, db, schema, table, reason="retention_miss"):
        """Mark the table for a full reseed and flip its phase back to seeding."""
        with self._conn.cursor() as cur:
            cur.execute(enqueue_reseed_sql(), (db, schema, table, reason))
            cur.execute(set_status_sql(), (db, schema, table, "reseed_queued", reason))
            cur.execute(
                upsert_checkpoint_sql(),
                (db, schema, table, 0, SEEDING, "reseed_queued", reason),
            )
