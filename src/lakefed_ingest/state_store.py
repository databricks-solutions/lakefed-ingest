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
import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import table_retry

SCHEMA = "lakefed_ingest"
CONTROL_TABLE = f"{SCHEMA}.control"
CHECKPOINT_TABLE = f"{SCHEMA}.checkpoint"
RESEED_TABLE = f"{SCHEMA}.reseed_queue"
RUN_TABLE = f"{SCHEMA}.ingest_run"        # observability: one row per ingestion run
EVENT_TABLE = f"{SCHEMA}.ingest_event"    # observability: one row per (run, table)
DATABASE_CHECK_TABLE = f"{SCHEMA}.database_check"   # one check stamp per unchanged source database
CHECK_WORK_TABLE = f"{SCHEMA}.check_work"           # the check job's work list, per run it starts
DATABRICKS_RUN_TABLE = f"{SCHEMA}.databricks_run"   # the Databricks runs the operations app tracks (its poller)

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
#   2 = discovery, control_change, checkpoint identities, freshness SLA, schedules (operations app)
#   3, 4 = the check job (initial checks, launches)
#   6 = schedule.options (a set start time). It is 6, not 5: the start-time branch numbered it 5 before it met
#       the check job's 3 and 4, and a database migrated to that 5 must still get 3 and 4.
#   7 = discovery.src_connection (discovery through a UC connection)
#   8 = indexes for the app's run history reads (runs by time, recent failures, each table's latest first load)
#   9 = app_task, table_upload, table_upload_line, job_selection (the operations app's background writes and uploads)
#   10 = databricks_run: the Databricks runs the operations app tracks (its poller writes them)
#   11 = discovery.listing (how a discovery listed its databases, for a later re-discovery)
#   12 = app_task.progress (a by-database write's progress)
#   13 = rollup_database, rollup_group (the monitoring rollups) and an index on control by group and database
#   14 = ingest_event.error_kind and error_class (how a failed attempt was classified, for the failing-tables list)
#   15 = databricks_run.cancel_requested_at (a Cancel run is recorded as cancelled, not failed)
#   16 = jdbc_connection (JDBC connections entered by hand in the operations app: a host, port, secret scope and key)
#   17 = jdbc_login_write (who wrote each JDBC login into the dedicated secret scope, and whether it rotated one)
SCHEMA_VERSION = 17
# Advisory lock that serializes migrations (the setup job and ingestion runs of any collection): two
# int keys, so it can't collide with a collection's one-key lock whatever the collection is named,
# and a class of its own (not SOURCE_LOCK_CLASS).
SCHEMA_LOCK = (0x6C667363, 0)
# A migration statement that waits for a table's lock gives up after LOCK_TIMEOUT_S, and the whole transaction is
# rolled back. The migration then tries again after a backoff (migrate_backoff_s), up to MIGRATE_ATTEMPTS times
# (about five minutes), so a waiting ALTER never holds up the loads queued behind it for long.
LOCK_TIMEOUT_S = 3
MIGRATE_ATTEMPTS = 10
# A migrator waits this long for another migrator's attempt (the schema lock) before it gives up at once with
# MigrationLockTimeout: long enough for a slow attempt to finish, and a bound for one that stalled in its transaction.
SCHEMA_LOCK_WAIT_S = 300
LOCK_NOT_AVAILABLE = "55P03"  # SQLSTATE: lock_timeout expired (psycopg's LockNotAvailable)


def schema_fingerprint(ddl: str) -> str:
    """sha256 of the DDL's statements (comments and whitespace ignored) and RENAMES: what the
    SCHEMA_VERSION pin test compares."""
    import hashlib
    body = "\n".join(" ".join(s.split()) for s in split_sql_statements(ddl)) + "\n" + repr(RENAMES)
    return hashlib.sha256(body.encode()).hexdigest()


def migrate_backoff_s(attempt: int) -> float:
    """Seconds to wait after the ``attempt``-th attempt failed on a lock: 5, 10, 20, then 40 at most."""
    return min(40.0, 5.0 * 2 ** (attempt - 1))


def is_lock_timeout(error) -> bool:
    """True for a lock wait that ran out of ``lock_timeout`` (SQLSTATE 55P03), which a retry can outlast."""
    return getattr(error, "sqlstate", None) == LOCK_NOT_AVAILABLE


class MigrationLockTimeout(RuntimeError):
    """The migration gave up: a running load kept holding the schema's tables, or another migration the schema lock.
    The transaction was rolled back."""


# Updates that backfill or rename once, keyed by the whole statement (lower case, whitespace collapsed) with a
# query that returns a row when the update would change one. The query must find every row the update would
# touch: an update with no rows to change is left out. A statement edited away from its key runs as before.
UPDATE_PROBES = {
    "update lakefed_ingest.checkpoint cc set control_id = m.id from (select src_database, src_schema, src_table, "
    "min(id) as id from lakefed_ingest.control group by src_database, src_schema, src_table having count(*) = 1) m "
    "where cc.control_id is null and cc.src_database = m.src_database and cc.src_schema = m.src_schema "
    "and cc.src_table = m.src_table":
        "select 1 from lakefed_ingest.checkpoint where control_id is null limit 1",
    "update lakefed_ingest.checkpoint set phase = 'incremental' where phase = 'cdc'":
        "select 1 from lakefed_ingest.checkpoint where phase = 'cdc' limit 1",
    "update lakefed_ingest.ingest_event set phase = 'incremental' where phase = 'cdc'":
        "select 1 from lakefed_ingest.ingest_event where phase = 'cdc' limit 1",
}
ALTER_TYPE_NAMES = {"text": "text", "bigint": "bigint", "int": "integer"}   # as the catalog spells them
ANY_OBJECT = ("*",)   # what a statement the rules don't recognize may change: nothing after it is left out


@dataclass(slots=True, kw_only=True)
class SchemaCatalog:
    """The lakefed_ingest schema as the catalog shows it before a migration's statements run."""
    schema_exists: bool = False
    relations: set = field(default_factory=set)     # names of tables, indexes and sequences
    tables: set = field(default_factory=set)        # names of tables
    columns: dict = field(default_factory=dict)     # (table, column) -> (data type, default or None, not null)
    constraints: set = field(default_factory=set)   # (table, constraint name)


_CREATE = re.compile(r"^create (?:unique )?(schema|table|index) if not exists ([\w.]+)")
_UPDATE = re.compile(r"^update ([\w.]+) ")
_ALTER_TABLE = re.compile(r"^alter table ([\w.]+) (.+)$")
_ADD_COLUMN = re.compile(r"\badd column if not exists (\w+)")
_ADD = re.compile(r"\badd\b")
_DROP_CONSTRAINT = re.compile(r"^drop constraint if exists (\w+)$")
_ADD_CONSTRAINT = re.compile(r"^add constraint (\w+)\b")
_ALTER_COLUMN = re.compile(r"^alter column (\w+) (.+)$")
_SET_DEFAULT = re.compile(r"^set default (.+)$")
_ALTER_TYPE = re.compile(r"^type (\w+)( using .+)?$")


def pending_statements(statements, catalog: SchemaCatalog, has_rows=lambda query: True) -> tuple:
    """Split the DDL ``statements`` into (to run, left out) for a database with this ``catalog``.

    A statement is left out only when the catalog shows it already done, so a database that is fully
    migrated runs just the constraint re-declarations and a fresh database runs the whole file. The
    statements are judged in order: one that runs changes the objects it names, and a later statement
    about any of them runs too. ``has_rows(query)`` answers an UPDATE_PROBES query.
    """
    changed = set()
    to_run, left_out = [], []
    for statement in statements:
        skip, touched = _judge(" ".join(statement.split()).lower(), catalog, changed, has_rows)
        if skip:
            left_out.append(statement)
        else:
            to_run.append(statement)
            changed |= touched
    return to_run, left_out


def _judge(text: str, catalog: SchemaCatalog, changed: set, has_rows) -> tuple:
    """(left out, objects the statement changes when it runs) for one normalized, lower-case statement."""
    if ANY_OBJECT in changed:
        return False, set()
    created = _CREATE.match(text)
    if created:
        kind, name = created.group(1), _bare(created.group(2))
        if kind == "schema":
            return catalog.schema_exists, set()
        return name in catalog.relations and ("relation", name) not in changed, {("relation", name)}
    updated = _UPDATE.match(text)
    if updated:
        probe = UPDATE_PROBES.get(text)
        skip = probe is not None and _bare(updated.group(1)) in catalog.tables and not has_rows(probe)
        return skip, set()
    altered = _ALTER_TABLE.match(text)
    if altered:
        return _judge_alter_table(_bare(altered.group(1)), altered.group(2), catalog, changed)
    return False, {ANY_OBJECT}


def _judge_alter_table(table: str, body: str, catalog: SchemaCatalog, changed: set) -> tuple:
    exists = table in catalog.tables
    dropped = _DROP_CONSTRAINT.match(body)
    if dropped:
        key = ("constraint", table, dropped.group(1))
        return exists and (table, dropped.group(1)) not in catalog.constraints and key not in changed, {key}
    added = _ADD_CONSTRAINT.match(body)
    if added:
        # Re-declared on every upgrade: whether an existing definition matches can't be read from the catalog.
        return False, {("constraint", table, added.group(1))}
    columns = _ADD_COLUMN.findall(body)
    if columns and len(columns) == len(_ADD.findall(body)):
        skip = exists and all((table, column) in catalog.columns and ("column", table, column) not in changed
                              for column in columns)
        return skip, {("column", table, column) for column in columns}
    altered = _ALTER_COLUMN.match(body)
    if altered:
        key = ("column", table, altered.group(1))
        state = catalog.columns.get((table, altered.group(1)))
        skip = exists and state is not None and key not in changed and _already_set(altered.group(2), state)
        return skip, {key}
    return False, {ANY_OBJECT}


def _already_set(change: str, state: tuple) -> bool:
    """True when the column already has the default, nullability or type that ``change`` would give it."""
    data_type, default, not_null = state
    if change == "drop default":
        return default is None
    if change == "drop not null":
        return not not_null
    value = _SET_DEFAULT.match(change)
    if value:
        return default == value.group(1)
    kind = _ALTER_TYPE.match(change)
    if kind:
        return data_type == ALTER_TYPE_NAMES.get(kind.group(1))
    return False


def _bare(name: str) -> str:
    """A schema-qualified name's object name (``lakefed_ingest.control`` -> ``control``)."""
    return name.split(".")[-1]


def catalog_relations_sql() -> str:
    """Every relation of the schema, with its kind (r = table, p = partitioned table). Param: schema."""
    return ("select c.relname, c.relkind from pg_catalog.pg_class c "
            "join pg_catalog.pg_namespace n on n.oid = c.relnamespace where n.nspname = %s")


def catalog_columns_sql() -> str:
    """Every column of the schema's relations: name, type as the catalog spells it, default and not-null.
    pg_catalog rather than information_schema, which hides the columns the role has no privilege on.
    Param: schema."""
    return ("select c.relname, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), "
            "pg_catalog.pg_get_expr(d.adbin, d.adrelid), a.attnotnull from pg_catalog.pg_attribute a "
            "join pg_catalog.pg_class c on c.oid = a.attrelid "
            "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
            "left join pg_catalog.pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum "
            "where n.nspname = %s and a.attnum > 0 and not a.attisdropped")


def catalog_constraints_sql() -> str:
    """Every constraint of the schema's tables. Param: schema."""
    return ("select c.relname, con.conname from pg_catalog.pg_constraint con "
            "join pg_catalog.pg_class c on c.oid = con.conrelid "
            "join pg_catalog.pg_namespace n on n.oid = c.relnamespace where n.nspname = %s")


def _read_catalog(cur, schema_exists: bool) -> SchemaCatalog:
    """The schema's catalog, in three queries on the open migration transaction."""
    cur.execute(catalog_relations_sql(), (SCHEMA,))
    kinds = cur.fetchall()
    cur.execute(catalog_columns_sql(), (SCHEMA,))
    columns = cur.fetchall()
    cur.execute(catalog_constraints_sql(), (SCHEMA,))
    constraints = cur.fetchall()
    return SchemaCatalog(
        schema_exists=schema_exists,
        relations={name for name, _ in kinds},
        tables={name for name, kind in kinds if kind in ("r", "p")},
        columns={(table, column): (data_type, default, bool(not_null))
                 for table, column, data_type, default, not_null in columns},
        constraints={(table, name) for table, name in constraints},
    )


def _has_rows(cur, query: str) -> bool:
    """True when an UPDATE_PROBES query finds a row."""
    cur.execute(query)
    return cur.fetchone() is not None


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


def source_identity(cfg: dict) -> str:
    """Where a control row reads from: engine, server, port (adapter default when NULL), database,
    schema, table. Lower-cased so cosmetic case edits don't look like a different source."""
    import source_adapters as sa
    adapter = sa.get_adapter(cfg.get("src_type"))
    host = str(cfg.get("src_host") or cfg.get("src_connection") or "").strip().lower()
    port = int(cfg.get("src_port") or adapter.default_port)
    parts = [str(cfg.get(k) or "").strip().lower() for k in ("src_database", "src_schema", "src_table")]
    return f"{adapter.src_type}://{host}:{port}/" + "/".join(parts)


def sink_identity(cfg: dict) -> str:
    """Where a control row writes: the sink table, plus the client slice of a consolidated sink."""
    sink = ".".join(str(cfg.get(k) or "").strip().lower() for k in ("sink_catalog", "sink_schema", "sink_table"))
    cid = str(cfg.get("client_id") or "").strip()
    return f"{sink}#{cid}" if cid else sink


def merge_key(cfg: dict) -> Optional[str]:
    """The MERGE key (normalized primary_key), or None when the row has none."""
    cols = [c.strip().lower() for c in str(cfg.get("primary_key") or "").split(",") if c.strip()]
    return ",".join(cols) or None


def identity_values(cfg: dict) -> tuple:
    """(source_identity, sink_identity, merge_key) recorded on every checkpoint write."""
    return (source_identity(cfg), sink_identity(cfg), merge_key(cfg))


def database_source(cfg: dict) -> str:
    """The server, port and credential a control row reads its database through, as one string: the
    key a per-database check stamp is kept under, with ``src_database`` and the collection. A blank
    port reads the source type's default port (the one its adapter connects on). The exception is a
    connection-backed row (no ``src_host``, so the host and port come from the UC connection): the
    connection's own port is not in the control row, so its blank port stays 0. A blank host or
    credential falls back to the connection, exactly as ``database_source_sql`` spells it in SQL, so
    readers can join a table to its database's stamp."""
    import source_adapters as sa
    host = str(cfg.get("src_host") or cfg.get("src_connection") or "").lower()
    credential = cfg.get("secret_key") or cfg.get("src_connection") or ""
    port = int(cfg.get("src_port") or 0)
    if not port and (cfg.get("src_host") or not cfg.get("src_connection")):
        port = sa.default_port(cfg.get("src_type"))
    return f"{host}|{port}|{credential}"


def database_source_sql(alias: str) -> str:
    """``database_source`` of the control row ``alias`` in SQL."""
    import source_adapters as sa
    whens = " ".join(f"when '{src_type}' then {sa.get_adapter(src_type).default_port}" for src_type in sa.SRC_TYPES)
    default_port = f"(case coalesce(nullif(lower(trim({alias}.src_type)), ''), '{sa.SQLSERVER}') {whens} else 0 end)"
    connection_backed = f"(nullif({alias}.src_host, '') is null and nullif({alias}.src_connection, '') is not null)"
    return (f"lower(coalesce(nullif({alias}.src_host, ''), nullif({alias}.src_connection, ''), '')) || '|' || "
            f"coalesce(nullif({alias}.src_port, 0), "
            f"case when {connection_backed} then 0 else {default_port} end) || '|' || "
            f"coalesce(nullif({alias}.secret_key, ''), nullif({alias}.src_connection, ''), '')")


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
            f"pending_position, pending_run_id, last_success_at, source_identity, sink_identity, merge_key "
            f"from {CHECKPOINT_TABLE} where control_id = any(%s)")


def upsert_checkpoint_sql() -> str:
    """Insert-or-update a checkpoint (keyed by control_id): set strategy + position, phase/status,
    record the select_list the sink was written with (kept when NULL is passed), and record the
    source / sink / merge key it was written for (identity_values) so a later control-row edit that
    points elsewhere is detected and reseeded instead of silently leaving a gap. Clears a pending
    append (see ``record_pending_sql``): the checkpoint now says where the sink is.

    Params: control_id, src_database, src_schema, src_table, strategy, position, phase, status,
    detail, select_list, source_identity, sink_identity, merge_key.
    """
    return (
        f"insert into {CHECKPOINT_TABLE} "
        "(control_id, src_database, src_schema, src_table, strategy, position, phase, status, detail, "
        "select_list, source_identity, sink_identity, merge_key, updated_at) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
        "on conflict (control_id) do update set "
        "strategy = excluded.strategy, position = excluded.position, phase = excluded.phase, "
        "status = excluded.status, detail = excluded.detail, "
        f"select_list = coalesce(excluded.select_list, checkpoint.select_list), "
        "pending_position = null, pending_run_id = null, "
        "source_identity = excluded.source_identity, sink_identity = excluded.sink_identity, "
        "merge_key = excluded.merge_key, updated_at = now()"
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
    when not yet known — legacy rows), source_identity, sink_identity, merge_key (each recorded only
    when not yet known; the planner reseeds a table whose recorded value differs), control_id.
    """
    return (
        f"update {CHECKPOINT_TABLE} set "
        "position = greatest(coalesce(position, '0')::bigint, %s::bigint)::text, "
        "status = 'ok', detail = null, "
        "select_list = coalesce(%s, select_list), ct_begin_version = coalesce(ct_begin_version, %s), "
        "source_identity = coalesce(source_identity, %s), sink_identity = coalesce(sink_identity, %s), "
        "merge_key = coalesce(merge_key, %s), last_success_at = now(), updated_at = now() "
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
    """Open an ingest_run row (status 'running'); returns the new run_id. total starts as the tables the
    run plans to take (a run on a work list takes fewer than its collection has), until finish_run sets the
    final count. work_id: the work list the check job started it for ("<launch_id>:<collection>"), so the
    group runs of one launch can be told apart, or "check-<job run>" on the check job's own record of a check;
    None for a run started otherwise."""
    return (
        f"insert into {RUN_TABLE} "
        "(task_collection, job_run_id, cluster_id, parallelism, total, work_id, status) "
        "values (%s, %s, %s, %s, %s, %s, 'running') returning run_id"
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
    number of earlier attempts of the table in this run (``table_retry``). Only a running event is finished: a table
    the cancel or a sweep marked keeps that status when its engine finishes late."""
    return (
        f"update {EVENT_TABLE} set "
        "finished_at = now(), "
        "duration_ms = (extract(epoch from (now() - started_at)) * 1000)::bigint, "
        "status = %s, stage = %s, rows_read = %s, rows_merged = %s, "
        "position_from = %s, position_to = %s, error = %s, error_kind = %s, error_class = %s, "
        "action = coalesce(%s, action), pool = coalesce(%s, pool), retry_count = coalesce(%s, retry_count) "
        "where id = %s and status = 'running'"
    )


def record_event_failed_sql() -> str:
    """Insert an already-FINISHED failed ingest_event row (bulk, via executemany) — used when a whole
    source database fails change detection, or a batched append's member fails, so its tables are
    visible without per-table work. The status is 'failed', or 'cancelled' for a cancel (failure_status).
    Params: run_id, task_collection, control_id, src_database, src_schema, src_table,
    sink_fqn, action, phase, stage, error, status."""
    return (
        f"insert into {EVENT_TABLE} "
        "(run_id, task_collection, control_id, src_database, src_schema, src_table, sink_fqn, "
        "action, phase, stage, error, status, finished_at, duration_ms) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), 0)"
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


def get_database_checks_sql() -> str:
    """A collection's per-database check stamps. Param: task_collection."""
    return (f"select source, src_database, checked_version, checked_at from {DATABASE_CHECK_TABLE} "
            "where task_collection = %s")


def stamp_database_sql() -> str:
    """One database checked and found unchanged: its CT version and the time, in place of a
    last_success_at on each of its tables (see planner.unchanged_writes). The latest check wins.

    Params: task_collection, source, src_database, checked_version, tables, run_id.
    """
    return (
        f"insert into {DATABASE_CHECK_TABLE} "
        "(task_collection, source, src_database, checked_version, tables, run_id, checked_at) "
        "values (%s, %s, %s, %s, %s, %s, now()) "
        "on conflict (task_collection, source, src_database) do update set "
        "checked_version = excluded.checked_version, tables = excluded.tables, run_id = excluded.run_id, "
        "checked_at = now()"
    )


# ---- monitoring rollups ---------------------------------------------------------------------------
# The operations app's pages read these instead of scanning every control row: one row per database (the grain of
# the app's database_states_sql) and one per ingestion group. A write recomputes its databases from their own rows
# (one aggregate, by index), so the counts are exact and never kept by deltas. A database with no rows left is
# zeroed, not deleted (the app has no DELETE privilege). The app's queries.rollup_* keep this text equal (a test).

ROLLUP_DATABASE_TABLE = f"{SCHEMA}.rollup_database"
ROLLUP_GROUP_TABLE = f"{SCHEMA}.rollup_group"
# Databases per statement: one statement's arrays and its aggregate stay small at 22,000 databases.
ROLLUP_CHUNK = 200


def _rollup_server_sql() -> str:
    """The server a control row reaches, as the app's queries.SERVER spells it (alias t = control)."""
    default_port = ("case lower(coalesce(t.src_type, 'sqlserver')) when 'oracle' then '1521' "
                    "when 'postgresql' then '5432' when 'mysql' then '3306' else '1433' end")
    endpoint = f"lower(t.src_host) || ':' || coalesce(nullif(t.src_port, 0)::text, {default_port})"
    return (f"(case when t.src_connection is not null and (t.use_remote_query or t.src_host is null) "
            f"then lower(t.src_connection) when t.src_host is not null then {endpoint} else '' end)")


def rollup_database_rewrite_sql() -> str:
    """Recompute the rollup rows of some databases of one collection, in one statement. Params: task_collection
    per database (text[]), src_database per database (text[]). Each grain row (server, connection, host, instance
    label) of a database gets its counts from the database's own control rows and checkpoints; a grain row the
    database no longer has is zeroed. Enabled-table counts only, as the app's database_states_sql."""
    return f"""
with keys as (
    select k.task_collection, k.src_database
    from unnest(%s::text[], %s::text[]) as k(task_collection, src_database)),
states as (
    select t.task_collection, coalesce(t.src_database, '') as src_database, {_rollup_server_sql()} as server,
           coalesce(t.src_connection, '') as src_connection, coalesce(lower(t.src_host), '') as src_host,
           coalesce(t.source_instance_group, '') as instance_label, t.task_enabled, t.freshness_sla_minutes,
           t.created_at, coalesce(cp.status = 'failed', false) as failing,
           (cp.control_id is null or cp.phase = 'seeding') as first_load,
           greatest(cp.last_success_at, case when cp.strategy = 'ct' and cp.phase = 'incremental'
                    and cp.position ~ '^[0-9]+$' then case when cp.position::bigint >= dc.checked_version
                    then dc.checked_at end end) as last_success_at
    from keys k
    join {CONTROL_TABLE} t on t.task_collection = k.task_collection and coalesce(t.src_database, '') = k.src_database
    left join {CHECKPOINT_TABLE} cp on cp.control_id = t.id
    left join {DATABASE_CHECK_TABLE} dc on dc.task_collection = t.task_collection
        and dc.src_database = t.src_database and dc.source = {database_source_sql('t')}),
agg as (
    select task_collection, src_database, server, src_connection, src_host, instance_label,
           count(*) as control_rows, count(*) filter (where task_enabled) as tables,
           count(*) filter (where task_enabled and freshness_sla_minutes is null) as no_sla_tables,
           count(*) filter (where task_enabled and failing) as failing,
           count(*) filter (where task_enabled and not failing and first_load) as first_load,
           count(*) filter (where task_enabled and not failing and not first_load) as loaded,
           min(last_success_at) filter (where task_enabled) as oldest_success_at,
           max(last_success_at) filter (where task_enabled) as newest_success_at,
           max(last_success_at) as newest_any_success_at,
           min(coalesce(last_success_at, created_at) + make_interval(mins => freshness_sla_minutes))
               filter (where task_enabled and freshness_sla_minutes is not null and not failing
                       and not first_load) as earliest_due_at
    from states
    group by task_collection, src_database, server, src_connection, src_host, instance_label),
written as (
    insert into {ROLLUP_DATABASE_TABLE} (task_collection, src_database, server, src_connection, src_host,
        instance_label, tables, no_sla_tables, failing, first_load, loaded, oldest_success_at, newest_success_at,
        newest_any_success_at, earliest_due_at, updated_at, control_rows)
    select task_collection, src_database, server, src_connection, src_host, instance_label, tables, no_sla_tables,
           failing, first_load, loaded, oldest_success_at, newest_success_at, newest_any_success_at,
           earliest_due_at, now(), control_rows
    from agg
    on conflict (task_collection, src_database, server, src_connection, src_host, instance_label) do update set
        tables = excluded.tables, no_sla_tables = excluded.no_sla_tables, failing = excluded.failing,
        first_load = excluded.first_load, loaded = excluded.loaded,
        oldest_success_at = excluded.oldest_success_at, newest_success_at = excluded.newest_success_at,
        newest_any_success_at = excluded.newest_any_success_at, earliest_due_at = excluded.earliest_due_at,
        control_rows = excluded.control_rows, updated_at = now()
    returning 1),
zeroed as (
    update {ROLLUP_DATABASE_TABLE} r set tables = 0, no_sla_tables = 0, failing = 0, first_load = 0, loaded = 0,
           oldest_success_at = null, newest_success_at = null, newest_any_success_at = null,
           earliest_due_at = null, control_rows = 0, updated_at = now()
    from keys k
    where r.task_collection = k.task_collection and r.src_database = k.src_database
      and not exists (select 1 from agg a where a.task_collection = r.task_collection
                      and a.src_database = r.src_database and a.server = r.server
                      and a.src_connection = r.src_connection and a.src_host = r.src_host
                      and a.instance_label = r.instance_label)
    returning 1)
select (select count(*) from written) as written, (select count(*) from zeroed) as zeroed"""


def rollup_group_rewrite_sql() -> str:
    """Recompute the rollup rows of some ingestion groups from their database rows. Param: task_collection per group
    (text[]). A group with no database rows is zeroed, as its rows are. Databases count those with an enabled table."""
    return f"""
with groups as (
    select g.task_collection from unnest(%s::text[]) as g(task_collection)),
agg as (
    select g.task_collection,
           coalesce(sum(r.tables), 0) as tables, coalesce(sum(r.no_sla_tables), 0) as no_sla_tables,
           coalesce(sum(r.failing), 0) as failing, coalesce(sum(r.first_load), 0) as first_load,
           coalesce(sum(r.loaded), 0) as loaded,
           count(distinct (r.server, r.src_database)) filter (where r.tables > 0) as databases,
           count(distinct r.server) filter (where r.tables > 0) as servers,
           min(r.oldest_success_at) as oldest_success_at, max(r.newest_success_at) as newest_success_at
    from groups g
    left join {ROLLUP_DATABASE_TABLE} r on r.task_collection = g.task_collection
    group by g.task_collection)
insert into {ROLLUP_GROUP_TABLE} (task_collection, tables, no_sla_tables, failing, first_load, loaded, databases,
    servers, oldest_success_at, newest_success_at, updated_at)
select task_collection, tables, no_sla_tables, failing, first_load, loaded, databases, servers,
       oldest_success_at, newest_success_at, now()
from agg
on conflict (task_collection) do update set
    tables = excluded.tables, no_sla_tables = excluded.no_sla_tables, failing = excluded.failing,
    first_load = excluded.first_load, loaded = excluded.loaded, databases = excluded.databases,
    servers = excluded.servers, oldest_success_at = excluded.oldest_success_at,
    newest_success_at = excluded.newest_success_at, updated_at = now()"""


def delete_rollups_sql(table: str) -> str:
    """Remove a deleted group's rollup rows (the delete job only; the app never deletes). Param: task_collection[]."""
    return f"delete from {table} where task_collection = any(%s)"


class RollupQueue:
    """The databases of one collection whose rollup rows are due a rewrite, written in batches as their work ends.

    Worker threads call ``add`` as each database's work finishes. A batch is written when it is full or the last
    write is older than ``max_age_s``, so the pages follow a long run; ``flush`` writes what is left at the end.
    A failed write keeps its databases queued, so the next write (or the run's end) tries them again. The write
    itself runs outside the lock, so the workers finishing meanwhile are not held up by it."""

    def __init__(self, store, task_collection: str, batch_size: int = ROLLUP_CHUNK, max_age_s: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        self._store = store
        self._task_collection = task_collection
        self._batch_size = max(1, int(batch_size))
        self._max_age_s = max_age_s
        self._clock = clock
        self._pending: set = set()
        self._last_write = clock()
        self._lock = threading.Lock()

    def add(self, src_databases) -> None:
        with self._lock:
            self._pending.update(str(name or "") for name in src_databases)
            names = self._take_if_due_locked()
        if names:
            self._write(names)

    def flush(self) -> None:
        with self._lock:
            names = self._take_locked()
        if names:
            self._write(names)

    def _take_if_due_locked(self) -> List[str]:
        if self._pending and (len(self._pending) >= self._batch_size
                              or self._clock() - self._last_write >= self._max_age_s):
            return self._take_locked()
        return []

    def _take_locked(self) -> List[str]:
        """Empty the pending set and return its names. The clock restarts here, not after the write, so a slow
        write doesn't make the next add write a batch of one."""
        if not self._pending:
            return []
        names = sorted(self._pending)
        self._pending.clear()
        self._last_write = self._clock()
        return names

    def _write(self, names: List[str]) -> None:
        try:
            self._store.rewrite_rollups(self._task_collection, names)
        except BaseException:
            with self._lock:
                self._pending.update(names)
            raise


def deleted_only_collections_sql() -> str:
    """The groups only deleted operations-app jobs hold: a member of a deleted job and no member of a live one (a
    removed member counts as none). Their control rows wait for the delete job, or a Restore, so no check takes
    them. In name order."""
    return (f"select distinct d.task_collection from {SCHEMA}.schedule_member d "
            f"join {SCHEMA}.schedule ds on ds.id = d.schedule_id where ds.deleted_at is not null "
            f"and not exists (select 1 from {SCHEMA}.schedule_member m "
            f"join {SCHEMA}.schedule s on s.id = m.schedule_id "
            f"where m.task_collection = d.task_collection and m.removed_at is null and s.deleted_at is null) "
            f"order by d.task_collection")


def enabled_collections_sql() -> str:
    """The collections with at least one enabled control row, except those only deleted operations-app jobs hold, in
    name order."""
    return (f"select distinct task_collection from {CONTROL_TABLE} where task_enabled "
            f"and task_collection not in ({deleted_only_collections_sql()}) order by task_collection")


def schedule_tables_per_group_sql() -> str:
    """An operations-app ingestion job's tables per group (its options), or no row when it has none. Param: schedule
    id."""
    return (f"select (options ->> 'tables_per_group')::bigint from {SCHEMA}.schedule "
            "where id = %s and options ? 'tables_per_group'")


def schedule_collections_sql() -> str:
    """The groups of one operations-app ingestion job (schedule) that is not deleted: its members not removed, in
    name order. Param: schedule id."""
    return (f"select m.task_collection from {SCHEMA}.schedule_member m join {SCHEMA}.schedule s on s.id = m.schedule_id "
            "where m.schedule_id = %s and m.removed_at is null and s.deleted_at is null order by m.task_collection")


def typed_logins_sql() -> str:
    """For each secret key of one scope with a login typed into the operations app: the JDBC connection its newest
    login was typed for, and that connection's host, port and source type (null when no such connection is stored).
    Param: secret_scope."""
    return (f"select distinct on (w.secret_key) w.secret_key, w.connection, c.src_host, c.src_port, c.src_type "
            f"from {SCHEMA}.jdbc_login_write w left join {SCHEMA}.jdbc_connection c on c.name = w.connection "
            "where w.secret_scope = %s order by w.secret_key, w.entered_at desc, w.id desc")


def record_check_work_sql() -> str:
    """One database with work for a run the check job starts. Params: work_id, task_collection, source,
    src_database."""
    return (f"insert into {CHECK_WORK_TABLE} (work_id, task_collection, source, src_database) "
            "values (%s, %s, %s, %s) on conflict do nothing")


def get_check_work_sql() -> str:
    """The databases with work of one collection's run. Params: work_id, task_collection."""
    return f"select source, src_database from {CHECK_WORK_TABLE} where work_id = %s and task_collection = %s"


def get_check_work_all_sql() -> str:
    """Every database of one work list, whatever its collection: a run of a cycle's share can span collections.
    Param: work_id."""
    return f"select task_collection, source, src_database from {CHECK_WORK_TABLE} where work_id = %s"


def prune_check_work_sql() -> str:
    """Work lists older than a number of days, deleted. Param: days."""
    return f"delete from {CHECK_WORK_TABLE} where created_at < now() - make_interval(days => %s)"


def finish_run_sql() -> str:
    """Finalize an ingest_run row with roll-up counts. Only a running run is finished: one the app cancelled, or a
    sweep closed, keeps that status when its engine finishes late."""
    return (
        f"update {RUN_TABLE} set "
        "finished_at = now(), status = %s, total = %s, ok = %s, failed = %s, "
        "skipped = %s, reseeded = %s, detail = %s "
        "where run_id = %s and status = 'running'"
    )


ABANDONED_RUN_REASON = "the run ended without finishing (cancelled, or its cluster went away)"

# Cancel: a run or table the user stopped is 'cancelled', never 'failed' (the counts, the failing tables and the
# failure e-mail read failures only). The rules below are the SQL's too (stopping_run_sql, close_abandoned_runs_sql):
# a Databricks run is stopping when the app asked for the cancel (cancel_requested_at), when Databricks is
# terminating it, or when it ended CANCELED; a run is stopping once its own record says cancelled.
CANCELLED = "cancelled"


def run_is_stopping(run_status, databricks_run) -> bool:
    """Whether a group run is being (or has been) cancelled: its own status is cancelled, or its Databricks run (a
    dict of databricks_run's columns, or None when the poller has not recorded it) was asked to stop, is terminating,
    or ended CANCELED. A load that fails while the run is stopping is a cancel, not a failure."""
    run = databricks_run or {}
    return (run_status == CANCELLED or run.get("cancel_requested_at") is not None
            or run.get("life_cycle_state") == "TERMINATING" or run.get("result_state") == "CANCELED")


def failure_status(error, stopping) -> str:
    """The status a table's failed attempt is recorded with: cancelled when its run is stopping or Spark reports a
    cancelled job (table_retry.is_cancellation), else failed. ``error`` is an exception or its text."""
    return CANCELLED if stopping or table_retry.is_cancellation(error if error is not None else "") else "failed"


def abandoned_run_status(databricks_run) -> str:
    """The status a run that ended without finishing is closed with: cancelled when its Databricks run ended CANCELED
    or was asked to stop, else failed (its cluster went away, or it died)."""
    run = databricks_run or {}
    cancelled = run.get("result_state") == "CANCELED" or run.get("cancel_requested_at") is not None
    return CANCELLED if cancelled else "failed"


def _databricks_run_of_sql(run: str) -> str:
    """Matches the databricks_run row of an ingest_run (``run`` is its alias) on job_run_id, when that is a number."""
    return f"d.run_id = (case when {run}.job_run_id ~ '^[0-9]+$' then {run}.job_run_id::bigint end)"


def stopping_run_sql() -> str:
    """Whether an ingest run is stopping: the same rules as run_is_stopping, in one row. Params: run_id.
    Returns no row for a run the table doesn't hold."""
    return (
        f"select r.status = 'cancelled' or exists (select 1 from {DATABRICKS_RUN_TABLE} d "
        f"where {_databricks_run_of_sql('r')} "
        "and (d.cancel_requested_at is not null or d.life_cycle_state = 'TERMINATING' or d.result_state = 'CANCELED')) "
        f"from {RUN_TABLE} r where r.run_id = %s"
    )


def close_abandoned_runs_sql() -> str:
    """Close a collection's other runs still 'running', and their events still 'running', as failed, or as
    cancelled when the run was cancelled (abandoned_run_status): a cancel is not a failure.
    Only a run holding the collection's lock may call it: no other sweep of the collection is then
    alive, so an open run is one that was cancelled or lost its cluster and will never finish its rows.
    A table that was waiting for its retry (table_retries) when the run died has a last attempt
    'retried' and no later one: it is closed the same way too, keeping its own error and timing.
    Params: detail, task_collection, run_id, error (appended to a retried attempt's), error.
    Returns (runs closed, events closed)."""
    return (
        f"with dead as (update {RUN_TABLE} r set status = case when exists (select 1 from {DATABRICKS_RUN_TABLE} d "
        f"where {_databricks_run_of_sql('r')} "
        "and (d.result_state = 'CANCELED' or d.cancel_requested_at is not null)) then 'cancelled' else 'failed' end, "
        "finished_at = now(), detail = %s "
        "where r.task_collection = %s and r.status = 'running' and r.run_id <> %s returning r.run_id, r.status), "
        f"events as (update {EVENT_TABLE} e set status = dead.status, finished_at = coalesce(e.finished_at, now()), "
        "duration_ms = coalesce(e.duration_ms, (extract(epoch from (now() - e.started_at)) * 1000)::bigint), "
        "error = case when e.status = 'retried' then concat(e.error, ' | ', %s::text) else %s end "
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


# A run given a work list (by the check) holds its collection's lock SHARED, on the same key, plus one exclusive lock
# per database it was given, so runs of one collection on different databases go at once, and two runs never take
# one database. Everything that needs the collection to itself (a run without a work list, the check, the app's
# edits and Delete, which try the same key exclusively) is refused while any of them holds it, and the other way
# round: a work-list run can't start while one of those holds it.
DATABASE_LOCK_NAMESPACE = "lakefed_ingest.database:"


def database_lock_key(task_collection: str, source: str, src_database: str) -> str:
    """The key of one database's run lock within a collection (the grain of a check stamp)."""
    return f"{DATABASE_LOCK_NAMESPACE}{task_collection}|{source}|{src_database}"


def try_lock_collection_shared_sql() -> str:
    """Non-blocking: true iff this session now holds the collection's lock shared."""
    return "select pg_try_advisory_lock_shared(hashtextextended(%s, 0))"


def unlock_collection_shared_sql() -> str:
    return "select pg_advisory_unlock_shared(hashtextextended(%s, 0))"


def try_lock_databases_sql() -> str:
    """Non-blocking, one statement: each database lock key with whether this session now holds it. Param: keys
    (text[])."""
    return ("select k, pg_try_advisory_lock(hashtextextended(k, 0)) "
            "from unnest(%s::text[]) with ordinality as u(k, n) order by n")


def unlock_databases_sql() -> str:
    """Param: keys (text[])."""
    return "select pg_advisory_unlock(hashtextextended(k, 0)) from unnest(%s::text[]) as u(k)"


# The group rollup is recounted from its database rows by each run that changed them. Two runs of one group may
# commit at once, and each recount reads the database rows as they were before the other committed, so the later
# write could leave the group one database stale. A transaction lock on the group's key, taken before the recount,
# makes the second wait for the first and then read its rows.
ROLLUP_LOCK_NAMESPACE = "lakefed_ingest.rollup:"


def rollup_group_lock_sql() -> str:
    """Wait for, and hold until commit, the recount locks of some groups, in order. Param: task_collection per group
    (text[])."""
    return ("select pg_advisory_xact_lock(hashtextextended(%s || g, 0)) "
            "from (select distinct g from unnest(%s::text[]) as u(g) order by g) as ordered")


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
        # Every worker thread shares this connection. The lock is held by ``_cursor`` for each statement and
        # each transaction, so a transaction keeps every other thread's statements out until it ends.
        self._lock = threading.RLock()

    @contextmanager
    def _cursor(self):
        """A cursor for one statement, or for one transaction (``cur.connection.transaction()``). The lock is held
        for the whole ``with`` block, so a transaction's statements and its commit or rollback run as one unit: no
        other thread's statement can land inside it, and a failed transaction never rolls back someone else's write.
        psycopg already runs one statement at a time, so this only extends that to the transaction's span."""
        with self._lock:
            if self._reconnect is not None and (self._conn.closed or self._conn.broken):
                self._conn = self._reconnect()
            with self._conn.cursor() as cur:
                yield cur

    @property
    def connection(self):
        """The underlying psycopg connection (for raw DDL / bulk inserts). Statements sent through it skip the lock
        ``_cursor`` takes, so use it from one thread only."""
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

    def migrate(self, ddl: str, force: bool = False, lock_timeout_s: int = LOCK_TIMEOUT_S,
                sleep: Callable[[float], None] = time.sleep) -> Optional[dict]:
        """Bring the schema to SCHEMA_VERSION: the pending RENAMES, then the statements of
        lakebase_schema.sql (``ddl``) that the catalog doesn't show already in place (``pending_statements``),
        then record the version, all in ONE transaction, so a failure (a constraint existing rows violate)
        leaves the database as it was.

        Returns None when the database is already at SCHEMA_VERSION, unless ``force`` (the setup job,
        which re-applies every time); else {'from': version before, 'renames': [statements run],
        'ran': statements run, 'skipped': statements left out}. Every statement waits at most
        ``lock_timeout_s`` for a table's lock. A wait that runs out rolls the transaction back and the
        migration tries again after a backoff, up to MIGRATE_ATTEMPTS times; then it raises
        MigrationLockTimeout (nothing changed). A transaction-level advisory lock serializes migrators:
        one that waited finds the version recorded and returns None. Refuses a database newer than this
        code: re-applying older DDL could undo the newer migration.
        """
        for attempt in range(1, MIGRATE_ATTEMPTS + 1):
            try:
                return self._migrate_once(ddl, force, lock_timeout_s)
            except Exception as error:
                if not is_lock_timeout(error):
                    raise
                if attempt == MIGRATE_ATTEMPTS:
                    raise MigrationLockTimeout(
                        f"could not migrate the Lakebase schema to version {SCHEMA_VERSION}: a running load held "
                        f"the lakefed_ingest tables (each of {MIGRATE_ATTEMPTS} attempts waited "
                        f"{int(lock_timeout_s)} s for them). Nothing changed. Run the setup job again.") from error
                sleep(migrate_backoff_s(attempt))

    def _migrate_once(self, ddl: str, force: bool, lock_timeout_s: int) -> Optional[dict]:
        """One migration attempt, in one transaction (see ``migrate``)."""
        with self._cursor() as cur, cur.connection.transaction():
            # Postgres applies lock_timeout to this wait too: a migrator queued behind another's attempt waits for
            # that attempt up to SCHEMA_LOCK_WAIT_S, not lock_timeout_s, and past it gives up without retrying.
            cur.execute(f"set local lock_timeout = '{SCHEMA_LOCK_WAIT_S}s'")
            try:
                cur.execute("select pg_advisory_xact_lock(%s::int, %s::int)", SCHEMA_LOCK)
            except Exception as error:
                if not is_lock_timeout(error):
                    raise
                raise MigrationLockTimeout(
                    f"could not migrate the Lakebase schema to version {SCHEMA_VERSION}: another migration held the "
                    f"schema lock for more than {SCHEMA_LOCK_WAIT_S} s. Nothing changed. Run the setup job again.") \
                    from error
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
            # Read after the renames, so the catalog names what the DDL below refers to.
            catalog = _read_catalog(cur, schema_exists=bool(present))
            to_run, left_out = pending_statements(split_sql_statements(ddl), catalog,
                                                  has_rows=lambda query: _has_rows(cur, query))
            for stmt in to_run:
                cur.execute(stmt)
            cur.execute(f"insert into {SCHEMA}.schema_version (version) values (%s) on conflict (version) "
                        "do update set applied_at = now(), applied_by = current_user", (SCHEMA_VERSION,))
        return {"from": before, "renames": renames, "ran": len(to_run), "skipped": len(left_out)}

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
                            "pending_run_id": r[8], "last_success_at": r[9], "source_identity": r[10],
                            "sink_identity": r[11], "merge_key": r[12]} for r in rows}

    def advance_checkpoint(self, cfg: dict, position, phase=INCREMENTAL, status="ok", detail=None,
                           select_list=None, strategy=None):
        """Persist a new checkpoint for control row ``cfg``. Call ONLY after the bronze write has
        committed (cross-system at-least-once: re-reading one window on crash is harmless).
        ``position`` is stored as text (None -> NULL); ``strategy`` defaults to the row's load_type.
        ``select_list`` records the columns the sink was written with (None keeps the old value)."""
        with self._cursor() as cur:
            cur.execute(upsert_checkpoint_sql(),
                        (*task_identity(cfg), strategy or strategy_name(cfg), position_text(position),
                         phase, status, detail, select_list, *identity_values(cfg)))

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
                   cfg.get("select_list"), *identity_values(cfg)) for cfg, pos, phase, detail in items]
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
        select_list_or_None[, ct_begin_version_or_None[, (source, sink, merge_key) identity]]), ...].
        Returns the number of rows submitted."""
        def ident(r):
            return tuple(r[4]) if len(r) > 4 and r[4] else (None, None, None)
        params = [(int(r[1]), r[2], (r[3] if len(r) > 3 else None), *ident(r), int(r[0])) for r in rows]
        if not params:
            return 0
        with self._cursor() as cur:
            cur.executemany(bulk_advance_unchanged_sql(), params)
        return len(params)

    def get_database_checks(self, task_collection: str) -> dict:
        """{(source, src_database): {'version', 'checked_at'}}: the collection's database stamps."""
        with self._cursor() as cur:
            cur.execute(get_database_checks_sql(), (task_collection,))
            rows = cur.fetchall()
        return {(r[0], r[1]): {"version": int(r[2]), "checked_at": r[3]} for r in rows}

    def enabled_collections(self) -> list:
        """The collections with at least one enabled control row (a check over every collection), not those only
        a deleted operations-app job holds."""
        with self._cursor() as cur:
            cur.execute(enabled_collections_sql())
            return [row[0] for row in cur.fetchall()]

    def schedule_tables_per_group(self, schedule_id: int) -> Optional[int]:
        """The job's tables per group, the size a cycle's runs are shared out by; None when it has none."""
        with self._cursor() as cur:
            cur.execute(schedule_tables_per_group_sql(), (int(schedule_id),))
            row = cur.fetchone()
            return int(row[0]) if row and row[0] else None

    def deleted_only_collections(self) -> list:
        """The groups only deleted operations-app jobs hold: a check named by hand leaves them out."""
        with self._cursor() as cur:
            cur.execute(deleted_only_collections_sql())
            return [row[0] for row in cur.fetchall()]

    def schedule_collections(self, schedule_id: int) -> list:
        """The groups of an operations-app ingestion job, as the check job reads them when started with the
        job's id (a long list of groups does not fit in one job parameter). None of a deleted job."""
        with self._cursor() as cur:
            cur.execute(schedule_collections_sql(), (int(schedule_id),))
            return [row[0] for row in cur.fetchall()]

    def typed_logins(self, scope: str) -> dict:
        """The logins typed into the operations app in this scope, by secret key: the connection each key's newest
        login was typed for, with its host, port and source type (typed_logins_sql). Empty on a database without the
        app's login tables, which then holds no typed login."""
        with self._cursor() as cur:
            cur.execute(f"select to_regclass('{SCHEMA}.jdbc_login_write') is not null "
                        f"and to_regclass('{SCHEMA}.jdbc_connection') is not null")
            if not cur.fetchone()[0]:
                return {}
            cur.execute(typed_logins_sql(), (scope,))
            return {key: {"connection": connection, "src_host": host, "src_port": port, "src_type": src_type}
                    for key, connection, host, port, src_type in cur.fetchall()}

    def record_check_work(self, work_id: str, task_collection: str, databases) -> None:
        """Record the databases with work, [(source, src_database)], of the run the check job is about
        to start for this collection under ``work_id``."""
        params = [(work_id, task_collection, source, database) for source, database in databases]
        if params:
            with self._cursor() as cur:
                cur.executemany(record_check_work_sql(), params)

    def get_check_work(self, work_id: str, task_collection: str) -> set:
        """{(source, src_database)}: the databases the check job found work in for this run."""
        with self._cursor() as cur:
            cur.execute(get_check_work_sql(), (work_id, task_collection))
            return {(source, database) for source, database in cur.fetchall()}

    def get_check_work_all(self, work_id: str) -> set:
        """{(task_collection, source, src_database)}: every database of a work list, across its collections."""
        with self._cursor() as cur:
            cur.execute(get_check_work_all_sql(), (work_id,))
            return {(collection, source, database) for collection, source, database in cur.fetchall()}

    def prune_check_work(self, days: int = 7) -> int:
        """Delete work lists older than ``days``; returns how many rows went."""
        with self._cursor() as cur:
            cur.execute(prune_check_work_sql(), (int(days),))
            return cur.rowcount or 0

    def stamp_databases(self, task_collection: str, stamps, run_id) -> None:
        """Write one check stamp per unchanged database: stamps = [(source, src_database, version,
        tables)], from ``planner.unchanged_writes``. ``run_id`` is the ingest run that checked them, or
        None for the check job, which has no ingest run."""
        params = [(task_collection, source, database, int(version), int(tables),
                   None if run_id is None else int(run_id))
                  for source, database, version, tables in stamps]
        if params:
            with self._cursor() as cur:
                cur.executemany(stamp_database_sql(), params)

    def rewrite_rollups(self, task_collection: str, src_databases) -> None:
        """Recompute the monitoring rollups of some source databases of a collection, then the collection's group row,
        in one transaction. Databases come from the collection's rows (a name's rows on every server)."""
        names = sorted({str(name or "") for name in src_databases})
        if not names:
            return
        with self._cursor() as cur, cur.connection.transaction():
            for at in range(0, len(names), ROLLUP_CHUNK):
                chunk = names[at:at + ROLLUP_CHUNK]
                cur.execute(rollup_database_rewrite_sql(), ([task_collection] * len(chunk), chunk))
            cur.execute(rollup_group_lock_sql(), (ROLLUP_LOCK_NAMESPACE, [task_collection]))
            cur.execute(rollup_group_rewrite_sql(), ([task_collection],))

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
                        (*ident, strategy_name(cfg), None, SEEDING, "reseed_queued", reason, None,
                         *identity_values(cfg)))

    # ---- Observability / telemetry -----------------------------------------------------
    def start_run(self, task_collection, job_run_id, cluster_id, parallelism, planned=None, work_id=None) -> int:
        """Open an ingest_run row; returns its run_id. planned: the tables the run will take, if known;
        work_id: the check job's work list it runs, if any, or "check-<job run>" for the check's own record."""
        with self._cursor() as cur:
            cur.execute(start_run_sql(),
                        (task_collection, job_run_id, cluster_id, int(parallelism),
                         None if planned is None else int(planned), work_id or None))
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
                            action=None, pool=None, retry_count=None, error_kind=None, error_class=None) -> None:
        """Finalize an ingest_event row; on 'ok' also stamp checkpoint.last_success_at.
        Positions are stored as text. ``action`` (optional) corrects the provisional action set at
        start (e.g. 'reseed'); ``pool`` (optional) records the FAIR pool(s) the table's units ran in;
        ``retry_count`` (optional) the table's earlier attempts in this run. Status 'retried' is an
        attempt that failed and is tried again. ``error_kind`` and ``error_class`` say how a failure was
        classified (table_retry.classify, table_retry.error_class)."""
        with self._cursor() as cur:
            cur.execute(record_event_finish_sql(), (
                status, stage, rows_read, rows_merged, position_text(position_from),
                position_text(position_to),
                (error[:2000] if isinstance(error, str) else error), error_kind, error_class,
                action, pool, retry_count, int(event_id),
            ))
            if status == "ok":
                cur.execute(touch_last_success_sql(), (int(event_id),))

    def stopping_run(self, run_id) -> bool:
        """Whether the run is being cancelled (stopping_run_sql; run_is_stopping's rules)."""
        with self._cursor() as cur:
            cur.execute(stopping_run_sql(), (int(run_id),))
            row = cur.fetchone()
        return bool(row and row[0])

    def record_failed_tables(self, run_id, items, stage="detect", action=None) -> int:
        """Bulk-record tables that failed before per-table work (their source database was
        unreachable during change detection, or the planner rejected the row: stage 'plan'), or in
        a batched append: one finished ingest_event row each. The row is 'failed', or 'cancelled' when the
        run is stopping or Spark cancelled the job (failure_status); only a failed table gets its checkpoint
        status set to 'failed' (phase/position untouched), a cancelled one keeps its checkpoint as it was.
        items = [(cfg, cp, error)]; ``action`` defaults to 'full' / 'increment' by the row's strategy."""
        if not items:
            return 0
        stopping = self.stopping_run(run_id)
        events, statuses = [], []
        for cfg, cp, err in items:
            sink = _sink_label(cfg)
            e = (err or "")[:2000]
            action_ = action or ("full" if strategy_name(cfg) == "full" else "increment")
            status = failure_status(err, stopping)
            events.append((int(run_id), cfg.get("task_collection"), cfg.get("id"),
                           cfg.get("src_database"), cfg.get("src_schema"), cfg.get("src_table"),
                           sink, action_, (cp or {}).get("phase"), stage, e, status))
            if status == "failed":
                statuses.append((*task_identity(cfg), strategy_name(cfg), "failed", e[:500]))
        with self._cursor() as cur:
            cur.executemany(record_event_failed_sql(), events)
            if statuses:
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

    def try_lock_collection_shared(self, task_collection: str) -> bool:
        """True iff this connection now holds the collection's lock shared (non-blocking): a work-list run's."""
        with self._cursor() as cur:
            cur.execute(try_lock_collection_shared_sql(), (collection_lock_key(task_collection),))
            return bool(cur.fetchone()[0])

    def unlock_collection_shared(self, task_collection: str) -> None:
        with self._cursor() as cur:
            cur.execute(unlock_collection_shared_sql(), (collection_lock_key(task_collection),))

    def try_lock_databases(self, keys) -> set:
        """Try each database lock key (non-blocking); returns the keys this connection now holds."""
        keys = list(dict.fromkeys(keys))
        if not keys:
            return set()
        with self._cursor() as cur:
            cur.execute(try_lock_databases_sql(), (keys,))
            return {key for key, taken in cur.fetchall() if taken}

    def unlock_databases(self, keys) -> None:
        keys = list(dict.fromkeys(keys))
        if keys:
            with self._cursor() as cur:
                cur.execute(unlock_databases_sql(), (keys,))

    def alone_on_collection(self, task_collection: str) -> bool:
        """For a run holding the collection's lock shared: whether no other session holds it, so the collection's
        other open runs are dead and may be closed. It tries the lock exclusively (its own shared hold doesn't
        conflict with itself) and lets go at once if it got it."""
        if not self.try_lock_collection(task_collection):
            return False
        self.unlock_collection(task_collection)
        return True

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
            with self._lock:
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
