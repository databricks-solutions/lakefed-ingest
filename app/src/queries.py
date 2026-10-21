"""SQL for the operations app — pure builders over the Lakebase schema (docs/lakebase_schema.md).

Every statement is parameterized (psycopg %s); nothing user-supplied is formatted into SQL. Kept
free of web-framework and database imports so it is unit-tested in app/tests/queries_test.py.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional, Tuple

SCHEMA = "lakefed_ingest"


# ---- freshness SLA ------------------------------------------------------------------------------

# An enabled table with control.freshness_sla_minutes breaches its SLA when its last success (or, if
# it has never succeeded, its control row's creation) is older than the SLA. Tables without an SLA
# are never flagged. Aliases: t = control, cp = checkpoint.
SLA_BREACHED = ("(t.task_enabled and t.freshness_sla_minutes is not null "
                "and coalesce(cp.last_success_at, t.created_at) < now() - make_interval(mins => t.freshness_sla_minutes))")


# ---- overview -----------------------------------------------------------------------------------

def collections_overview_sql() -> str:
    """One row per task_collection: table counts, the latest run, and current health."""
    return f"""
with tables as (
    select t.task_collection,
           count(*) filter (where t.task_enabled)                                   as enabled_tables,
           count(*)                                                                 as all_tables,
           count(*) filter (where t.task_enabled and cp.status = 'failed')          as failing_tables,
           count(*) filter (where t.task_enabled and (cp.control_id is null or cp.phase = 'seeding')) as seeding_tables,
           count(*) filter (where t.task_enabled and t.freshness_sla_minutes is null) as no_sla_tables,
           count(*) filter (where {SLA_BREACHED})                                   as sla_breached_tables,
           max(cp.last_success_at)                                                  as last_success_at
    from {SCHEMA}.control t
    left join {SCHEMA}.checkpoint cp on cp.control_id = t.id
    group by t.task_collection
),
last_run as (
    select distinct on (task_collection) task_collection, run_id, status, started_at, finished_at,
           total, ok, failed, skipped, reseeded
    from {SCHEMA}.ingest_run
    order by task_collection, started_at desc
)
select tb.task_collection, tb.enabled_tables, tb.all_tables, tb.failing_tables, tb.seeding_tables,
       tb.sla_breached_tables, tb.no_sla_tables, tb.last_success_at,
       r.run_id as last_run_id, r.status as last_run_status, r.started_at as last_run_started,
       r.finished_at as last_run_finished, r.ok as last_run_ok, r.failed as last_run_failed
from tables tb
left join last_run r on r.task_collection = tb.task_collection
order by tb.task_collection"""


def running_runs_sql() -> str:
    """Runs still marked running (newest first), with progress: tables finished so far (unchanged tables
    are bulk-advanced without events, so these count tables that needed work) and elapsed time against
    the collection's typical run (median of its last 20 finished runs)."""
    return f"""
select r.run_id, r.task_collection, r.job_run_id, r.started_at, r.parallelism,
       round(extract(epoch from (now() - r.started_at)))::int as elapsed_s,
       (select count(*) from {SCHEMA}.ingest_event e where e.run_id = r.run_id and e.status = 'ok')      as tables_ok,
       (select count(*) from {SCHEMA}.ingest_event e where e.run_id = r.run_id and e.status = 'failed')  as tables_failed,
       (select count(*) from {SCHEMA}.ingest_event e where e.run_id = r.run_id and e.status = 'running') as tables_running,
       (select round(percentile_cont(0.5) within group (order by extract(epoch from (x.finished_at - x.started_at)))::numeric)
          from (select started_at, finished_at from {SCHEMA}.ingest_run x
                where x.task_collection = r.task_collection and x.finished_at is not null and x.status <> 'skipped'
                order by x.started_at desc limit 20) x)                                                as typical_s
from {SCHEMA}.ingest_run r
where r.status = 'running'
order by r.started_at desc"""


# ---- runs / events ------------------------------------------------------------------------------

def runs_sql(collection: Optional[str], limit: int = 100) -> Tuple[str, tuple]:
    """Recent runs, optionally for one collection. Returns (sql, params)."""
    where, params = "", ()
    if collection:
        where, params = "where task_collection = %s ", (collection,)
    sql = (f"select run_id, task_collection, status, started_at, finished_at, "
           f"round(extract(epoch from (coalesce(finished_at, now()) - started_at))::numeric, 0) as duration_s, "
           f"total, ok, failed, skipped, reseeded, job_run_id, parallelism "
           f"from {SCHEMA}.ingest_run {where}order by started_at desc limit %s")
    return sql, params + (int(limit),)


def run_events_sql() -> str:
    """Per-table events of one run, failures first. Param: run_id."""
    return (f"select control_id, src_database, src_schema, src_table, sink_fqn, action, stage, status, "
            f"duration_ms, rows_merged, position_from, position_to, error "
            f"from {SCHEMA}.ingest_event where run_id = %s "
            f"order by (status = 'failed') desc, duration_ms desc nulls last")


def recent_errors_sql() -> str:
    """Failed table events in the last N hours, newest first. Params: hours, limit."""
    return (f"select e.finished_at, e.task_collection, e.run_id, e.control_id, e.src_database, "
            f"e.src_schema, e.src_table, e.stage, e.error "
            f"from {SCHEMA}.ingest_event e "
            f"where e.status = 'failed' and e.started_at > now() - make_interval(hours => %s) "
            f"order by e.started_at desc limit %s")


# ---- tables (control + checkpoint) --------------------------------------------------------------

TABLE_FILTERS = ("all", "failing", "sla_breached", "no_sla", "seeding", "disabled")


def tables_sql(collection: Optional[str], search: Optional[str], status_filter: str,
               limit: int) -> Tuple[str, tuple]:
    """Control rows joined with their checkpoint. Returns (sql, params)."""
    if status_filter not in TABLE_FILTERS:
        raise ValueError(f"status_filter must be one of {TABLE_FILTERS}")
    conds, params = [], []
    if collection:
        conds.append("t.task_collection = %s")
        params.append(collection)
    if search:
        conds.append("(t.src_database || '.' || t.src_schema || '.' || t.src_table || ' ' || "
                     "t.sink_catalog || '.' || t.sink_schema || '.' || t.sink_table || ' ' || "
                     "coalesce(t.client_id, '')) ilike %s")
        params.append(f"%{search}%")
    if status_filter == "failing":
        conds.append("cp.status = 'failed'")
    elif status_filter == "sla_breached":
        conds.append(SLA_BREACHED)
    elif status_filter == "no_sla":
        conds.append("t.task_enabled and t.freshness_sla_minutes is null")
    elif status_filter == "seeding":
        conds.append("(cp.control_id is null or cp.phase = 'seeding')")
    elif status_filter == "disabled":
        conds.append("not t.task_enabled")
    where = ("where " + " and ".join(conds) + " ") if conds else ""
    sql = (f"select t.id, t.task_collection, t.task_enabled, t.src_type, t.load_type, "
           f"t.src_database, t.src_schema, t.src_table, "
           f"t.sink_catalog || '.' || t.sink_schema || '.' || t.sink_table as sink, t.client_id, "
           f"cp.phase, cp.status, cp.last_success_at, "
           f"round(extract(epoch from (now() - cp.last_success_at)) / 60)::int as lag_min, "
           f"t.freshness_sla_minutes as sla_min, {SLA_BREACHED} as sla_breached, "
           f"cp.position, cp.detail "
           f"from {SCHEMA}.control t left join {SCHEMA}.checkpoint cp on cp.control_id = t.id "
           f"{where}order by t.task_collection, t.src_database, t.src_schema, t.src_table limit %s")
    return sql, tuple(params) + (int(limit),)


def collections_sql() -> str:
    return f"select distinct task_collection from {SCHEMA}.control order by 1"


def set_enabled_sql() -> str:
    """Params: enabled (bool), control ids (list)."""
    return f"update {SCHEMA}.control set task_enabled = %s where id = any(%s)"


# A reseed flip also clears the status (a requested reseed isn't a failing table, and the seed budget
# defers failed seeds) and any pending append: the job settles a pending append before it looks at the
# phase, so a left-over one would move the table back to incremental and undo the reseed. The job's
# own reseed (state_store.upsert_checkpoint_sql) clears them the same way.
_RESEED_SET = "phase = 'seeding', status = 'ok', pending_position = null, pending_run_id = null, updated_at = now()"


def force_reseed_sql() -> str:
    """Flip checkpoints back to seeding (the next run re-copies those tables). Param: control ids."""
    return (f"update {SCHEMA}.checkpoint set {_RESEED_SET}, detail = 'reseed requested from the app' "
            f"where control_id = any(%s)")


def log_reseed_sql() -> str:
    """Record manual reseeds in the reseed log. Params: reason, control ids."""
    return (f"insert into {SCHEMA}.reseed_queue (control_id, src_database, src_schema, src_table, reason) "
            f"select id, src_database, src_schema, src_table, %s from {SCHEMA}.control where id = any(%s)")


# ---- add a control row --------------------------------------------------------------------------

LOAD_TYPES = ("ct", "cursor", "full")
SRC_TYPES = ("sqlserver", "oracle", "postgresql", "mysql")
CONTROL_INSERT_COLUMNS = (
    "task_collection", "src_type", "src_host", "src_port", "secret_key", "src_database", "src_schema",
    "src_table", "select_list", "primary_key", "load_type", "cursor_column", "sink_catalog",
    "sink_schema", "sink_table", "client_id", "load_partitioned", "partition_col", "partition_size_mb",
    "freshness_sla_minutes", "src_connection", "use_remote_query", "write_mode", "client_attrs", "jdbc_options",
)
WRITE_MODES = ("merge", "append")
_ATTR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")     # ct_engine.client_attrs


def _truthy(v) -> bool:
    return v if isinstance(v, bool) else str(v or "").strip().lower() in ("true", "1", "yes", "y")


def _json_object(v):
    """A JSON object given as text or a dict -> the dict; None when blank; raises ValueError otherwise."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    obj = json.loads(v) if isinstance(v, str) else v
    if not isinstance(obj, dict):
        raise ValueError("not a JSON object")
    return obj


def client_attrs_errors(attrs, select_list) -> List[str]:
    """The ingestion job's client_attrs rules (ct_engine.client_attrs), plus no clash with select_list."""
    try:
        obj = _json_object(attrs)
    except ValueError:
        return ["client_attrs must be a JSON object {column: value}"]
    errs, seen = [], set()
    selected = {c.strip().lower() for c in str(select_list or "").split(",") if c.strip()}
    for name, value in (obj or {}).items():
        if not _ATTR_NAME.match(str(name)) or str(name).startswith("__"):
            errs.append(f"client_attrs column '{name}': use letters, digits and _ (1-128 characters, not starting with __)")
        elif name.lower() in seen:
            errs.append(f"client_attrs column '{name}' is given twice")
        elif name.lower() in selected:
            errs.append(f"client_attrs column '{name}' is also in select_list")
        if isinstance(value, (dict, list)):
            errs.append(f"client_attrs column '{name}': the value must be a string, number, boolean or null")
        seen.add(str(name).lower())
    return errs


def validate_control_row(row: dict) -> List[str]:
    """Form-level checks with clear messages (the database CHECK constraints are the backstop)."""
    errs = []
    remote = _truthy(row.get("use_remote_query"))
    # A UC-connection read needs the connection and no host or secret; a JDBC read needs both.
    source = ("src_connection",) if remote else ("src_host", "secret_key")
    for col in ("task_collection",) + source + ("src_database", "src_schema", "src_table",
                                                "select_list", "sink_catalog", "sink_schema", "sink_table"):
        if col == "src_host" and str(row.get("src_connection") or "").strip():
            continue                     # JDBC falls back to the connection's host
        if not str(row.get(col) or "").strip():
            errs.append(f"{col} is required" + (" when use_remote_query is set" if col == "src_connection" else ""))
    if row.get("src_type") not in SRC_TYPES:
        errs.append(f"src_type must be one of {', '.join(SRC_TYPES)}")
    lt = row.get("load_type")
    if lt not in LOAD_TYPES:
        errs.append(f"load_type must be one of {', '.join(LOAD_TYPES)}")
    if lt in ("ct", "cursor") and not str(row.get("primary_key") or "").strip():
        errs.append(f"primary_key is required for load_type {lt}")
    if lt == "cursor" and not str(row.get("cursor_column") or "").strip():
        errs.append("cursor_column is required for load_type cursor")
    if lt in ("ct", "cursor") and row.get("src_type") != "sqlserver":
        errs.append(f"load_type {lt} is only supported for src_type sqlserver")
    if "*" in str(row.get("select_list") or ""):
        errs.append("select_list must list explicit columns (* isn't supported)")
    if row.get("load_partitioned") and (not row.get("partition_col") or not row.get("partition_size_mb")):
        errs.append("partitioned loads need partition_col and partition_size_mb")
    sla = row.get("freshness_sla_minutes")
    if sla not in (None, ""):
        try:
            ok = int(sla) > 0 and float(sla) == int(sla)
        except (TypeError, ValueError):
            ok = False
        if not ok:
            errs.append("freshness_sla_minutes must be a whole number of minutes greater than 0 (blank = no SLA)")
    cid = row.get("client_id")
    if cid not in (None, "") and not re.match(r"^[A-Za-z0-9_.:-]{1,128}$", str(cid)):
        errs.append("client_id may only use letters, digits and _ . : - (1-128 characters)")
    if "__client_id__" in {c.strip().lower() for c in str(row.get("select_list") or "").split(",")}:
        errs.append("select_list must not include __client_id__ (consolidated sinks add it)")
    opts = row.get("jdbc_options")
    if opts not in (None, ""):
        try:
            ok = isinstance(json.loads(opts) if isinstance(opts, str) else opts, dict)
        except ValueError:
            ok = False
        if not ok:
            errs.append("jdbc_options must be a JSON object")
    mode = str(row.get("write_mode") or "merge").strip().lower()
    if mode not in WRITE_MODES:
        errs.append("write_mode must be merge or append")
    elif mode == "append" and lt not in ("ct", "full"):
        errs.append(f"write_mode append supports load_type ct and full, not {lt}")
    errs += client_attrs_errors(row.get("client_attrs"), row.get("select_list"))
    return errs


def insert_control_sql() -> str:
    cols = ", ".join(CONTROL_INSERT_COLUMNS)
    marks = ", ".join(["%s"] * len(CONTROL_INSERT_COLUMNS))
    return f"insert into {SCHEMA}.control ({cols}) values ({marks}) returning id"


def control_params(row: dict) -> tuple:
    """Insert params in CONTROL_INSERT_COLUMNS order; blank strings become NULL (the NOT NULL columns
    write_mode, use_remote_query and load_partitioned get their defaults when omitted; JSON objects are
    stored as text)."""
    def clean(c, v):
        if c in ("use_remote_query", "load_partitioned"):
            return _truthy(v)
        if c in ("client_attrs", "jdbc_options") and isinstance(v, dict):
            return json.dumps(v)
        if isinstance(v, str):
            v = v.strip()
            v = v or None
        if c == "write_mode":
            return (v or "merge").lower()
        return v
    return tuple(clean(c, row.get(c)) for c in CONTROL_INSERT_COLUMNS)


# ---- bulk import (table discovery) --------------------------------------------------------------

DEFAULT_PARTITION_THRESHOLD_MB = 1024   # tables at least this large get a partitioned seed / full load
DEFAULT_PARTITION_SIZE_MB = 512


def discoveries_sql() -> str:
    """Recent discoveries, one row each. Param: limit."""
    return (f"select discovery_id, min(started_at) as started_at, max(finished_at) as finished_at, "
            f"min(src_host) as src_host, count(*) as databases, "
            f"count(*) filter (where status = 'ok') as ok, count(*) filter (where status = 'failed') as failed, "
            f"count(*) filter (where status = 'running') as running, coalesce(sum(tables), 0) as tables, "
            f"min(requested_by) as requested_by "
            f"from {SCHEMA}.discovery group by discovery_id order by min(started_at) desc limit %s")


def discovery_databases_sql() -> str:
    """Per-database status of one discovery. Param: discovery_id."""
    return (f"select src_database, status, tables, error, started_at, finished_at "
            f"from {SCHEMA}.discovery where discovery_id = %s order by src_database")


def discovered_tables_sql() -> str:
    """Discovered tables of one discovery, with the source connection and any existing control row
    for the same source table (``registered_id``). Param: discovery_id."""
    return (f"select dt.*, d.src_type, d.src_host, d.src_port, d.secret_key, "
            f"(select min(c.id) from {SCHEMA}.control c where lower(c.src_host) = lower(d.src_host) "
            f"   and lower(c.src_database) = lower(dt.src_database) and lower(c.src_schema) = lower(dt.src_schema) "
            f"   and lower(c.src_table) = lower(dt.src_table)) as registered_id "
            f"from {SCHEMA}.discovered_table dt join {SCHEMA}.discovery d "
            f"  on d.discovery_id = dt.discovery_id and d.src_database = dt.src_database "
            f"where dt.discovery_id = %s order by dt.src_database, dt.src_schema, dt.src_table")


def parse_databases(text: Optional[str]) -> List[str]:
    """Database names from a comma- / newline-separated list (order kept, duplicates dropped)."""
    seen, out = set(), []
    for part in str(text or "").replace("\n", ",").split(","):
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def identifier(text: str) -> str:
    """A safe, lower-case Delta identifier part (letters, digits, underscores)."""
    import re
    out = re.sub(r"[^a-z0-9_]+", "_", str(text).lower()).strip("_")
    return out or "t"


def render(template: Optional[str], database: str, schema: str, table: str, as_identifier: bool = True):
    """Fill {database} / {schema} / {table} in a template; blank template -> None."""
    if not str(template or "").strip():
        return None
    f = identifier if as_identifier else (lambda v: v)
    return (str(template).replace("{database}", f(database)).replace("{schema}", f(schema))
            .replace("{table}", f(table)).strip())


def recommended_partitioning(size_mb, partition_candidate, threshold_mb: float, partition_size_mb: int):
    """(load_partitioned, partition_col, partition_size_mb) for a discovered table."""
    if partition_candidate and float(size_mb or 0) >= float(threshold_mb):
        return True, partition_candidate, int(partition_size_mb)
    return False, None, None


def review_rows(discovered: List[dict], shared: dict) -> List[dict]:
    """The review grid: one editable row per discovered table, from the recommendations plus the
    shared fields (sink templates, client_id template, partition threshold). Registered tables and
    tables with no readable columns start excluded."""
    rows = []
    for t in discovered:
        db, sch, tbl = t["src_database"], t["src_schema"], t["src_table"]
        part, pcol, psize = recommended_partitioning(t.get("size_mb"), t.get("partition_candidate"),
                                                     shared.get("partition_threshold_mb", DEFAULT_PARTITION_THRESHOLD_MB),
                                                     shared.get("partition_size_mb", DEFAULT_PARTITION_SIZE_MB))
        registered = t.get("registered_id")
        rows.append({
            "include": bool(shared.get("include_default", True)) and not registered and bool(t.get("select_list")),
            "src_database": db, "src_schema": sch, "src_table": tbl,
            "rows": t.get("row_count"), "size_mb": float(t.get("size_mb") or 0),
            "ct_enabled": bool(t.get("ct_enabled")),
            "load_type": t.get("load_type"), "primary_key": t.get("primary_key"),
            "cursor_column": t.get("cursor_column"),
            "load_partitioned": part, "partition_col": pcol, "partition_size_mb": psize,
            "sink_catalog": render(shared.get("sink_catalog"), db, sch, tbl),
            "sink_schema": render(shared.get("sink_schema"), db, sch, tbl),
            "sink_table": render(shared.get("sink_table"), db, sch, tbl),
            "client_id": render(shared.get("client_id"), db, sch, tbl, as_identifier=False),
            "freshness_sla_minutes": shared.get("freshness_sla_minutes") or None,
            "registered_id": registered, "notes": t.get("notes"),
            # carried through, not shown for editing
            "_select_list": t.get("select_list"), "_src_type": t.get("src_type"), "_src_host": t.get("src_host"),
            "_src_port": t.get("src_port"), "_secret_key": t.get("secret_key"),
        })
    return rows


def control_row_from_review(row: dict, task_collection: str) -> dict:
    """A control row (for validate_control_row / control_params) from an edited review row."""
    return {
        "task_collection": task_collection, "src_type": row["_src_type"], "src_host": row["_src_host"],
        "src_port": row["_src_port"], "secret_key": row["_secret_key"], "src_database": row["src_database"],
        "src_schema": row["src_schema"], "src_table": row["src_table"], "select_list": row["_select_list"],
        "primary_key": row.get("primary_key"), "load_type": row.get("load_type"),
        "cursor_column": row.get("cursor_column") if row.get("load_type") == "cursor" else None,
        "sink_catalog": row.get("sink_catalog"), "sink_schema": row.get("sink_schema"),
        "sink_table": row.get("sink_table"), "client_id": row.get("client_id"),
        "load_partitioned": bool(row.get("load_partitioned")),
        "partition_col": row.get("partition_col") if row.get("load_partitioned") else None,
        "partition_size_mb": row.get("partition_size_mb") if row.get("load_partitioned") else None,
        "freshness_sla_minutes": row.get("freshness_sla_minutes"),
    }


def insert_control_if_absent_sql() -> str:
    """Like insert_control_sql, but a row whose sink (or sink + client_id) is already taken is skipped
    (the unique index) instead of failing the whole import."""
    return insert_control_sql().replace(" returning id", " on conflict do nothing returning id")


EDITABLE_REVIEW_FIELDS = ("include", "load_type", "primary_key", "cursor_column", "load_partitioned",
                          "partition_col", "partition_size_mb", "sink_catalog", "sink_schema", "sink_table",
                          "client_id", "freshness_sla_minutes")


def review_key(row: dict) -> tuple:
    return (row["src_database"], row["src_schema"], row["src_table"])


def merge_review_edits(review: List[dict], edits: List[dict]) -> List[dict]:
    """Overlay a reviewer's edits on the server-computed review rows, by source table. Only
    EDITABLE_REVIEW_FIELDS are taken from the client; connection, secret and select_list always come
    from the stored discovery. Tables without an edit keep their computed values."""
    by_key = {review_key(e): e for e in edits}
    out = []
    for r in review:
        e = by_key.get(review_key(r))
        out.append({**r, **{f: e[f] for f in EDITABLE_REVIEW_FIELDS if e is not None and f in e}})
    return out


def public_review_row(row: dict) -> dict:
    """A review row without the server-only fields (connection, secret key, select_list)."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


# ---- editing control rows -----------------------------------------------------------------------
#
# Integrity rules (mirrored by the ingestion planner's identity check, which also catches SQL edits):
# changing where a ct / cursor table reads from, where it writes to, or its merge key makes the
# existing checkpoint meaningless, so the edit flips the table to seeding in the same transaction.
# Edits are refused while either affected collection is running (the run's advisory lock).

CONTROL_EDIT_COLUMNS = (
    "task_collection", "task_enabled", "tier", "priority", "src_type", "src_host", "src_port", "secret_key",
    "src_database", "src_schema", "src_table", "select_list", "primary_key", "load_type", "cursor_column",
    "sink_catalog", "sink_schema", "sink_table", "client_id", "load_partitioned", "partition_col",
    "partition_size_mb", "jdbc_options", "freshness_sla_minutes", "src_connection", "use_remote_query",
    "write_mode", "client_attrs",
)
BULK_EDIT_FIELDS = ("task_collection", "task_enabled", "tier", "priority", "freshness_sla_minutes", "load_partitioned",
                    "partition_col", "partition_size_mb", "jdbc_options", "sink_catalog", "sink_schema",
                    "src_connection", "use_remote_query")
RESEED_FIELDS = {
    "src_type": "source_changed", "src_host": "source_changed", "src_port": "source_changed",
    "src_database": "source_changed", "src_schema": "source_changed", "src_table": "source_changed",
    "sink_catalog": "sink_changed", "sink_schema": "sink_changed", "sink_table": "sink_changed",
    "client_id": "sink_changed", "primary_key": "primary_key_changed",
}
INT_FIELDS = {"src_port", "priority", "partition_size_mb", "freshness_sla_minutes"}
BOOL_FIELDS = {"task_enabled", "load_partitioned", "use_remote_query"}
JSON_FIELDS = {"jdbc_options", "client_attrs"}
DEFAULT_PORTS = {"sqlserver": 1433, "oracle": 1521, "postgresql": 5432, "mysql": 3306}
CASE_INSENSITIVE = {"src_host", "src_database", "src_schema", "src_table", "sink_catalog", "sink_schema",
                    "sink_table", "primary_key", "src_connection", "write_mode"}
SINK_FIELDS = ("sink_catalog", "sink_schema", "sink_table")


def normalize_value(field: str, value):
    """Form / JSON value -> the stored value (blank strings -> NULL, numbers and booleans typed; a blank
    write_mode is merge and a blank use_remote_query false, the columns' defaults; JSON objects as text)."""
    if field == "write_mode":
        return str(value or "").strip().lower() or "merge"
    if field == "use_remote_query":
        return _truthy(value)
    if field in JSON_FIELDS and isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
    if value is None:
        return None
    if field in INT_FIELDS:
        return int(value)
    if field in BOOL_FIELDS:
        return value if isinstance(value, bool) else str(value).lower() in ("true", "1", "yes", "y")
    return value


def _same(field: str, old, new, row: dict) -> bool:
    if field in CASE_INSENSITIVE:
        norm = lambda v: ",".join(p.strip() for p in str(v).lower().split(",")) if v is not None else None
        return norm(old) == norm(new)
    if field == "src_port":
        default = DEFAULT_PORTS.get(str(row.get("src_type") or "sqlserver"))
        return (old or default) == (new or default)
    if field in JSON_FIELDS:            # compare the objects, not their spelling
        try:
            return _json_object(old) == _json_object(new)
        except ValueError:
            return old == new
    if field == "use_remote_query":
        return bool(old) == bool(new)
    return old == new


def plan_edit(rows: List[dict], changes: dict, bulk: bool) -> List[dict]:
    """What an edit will do to each control row: {id, changes: {field: [old, new]}, reseed (reason
    or None), notes, errors}. Pure; the API applies it only when no row has errors."""
    allowed = BULK_EDIT_FIELDS if bulk else CONTROL_EDIT_COLUMNS
    bad = sorted(set(changes) - set(allowed))
    plans = []
    for row in rows:
        p = {"id": row["id"], "table": f"{row['src_database']}.{row['src_schema']}.{row['src_table']}",
             "changes": {}, "reseed": None, "notes": [], "errors": []}
        if bad:
            p["errors"].append(("cannot be changed in bulk: " if bulk else "unknown fields: ") + ", ".join(bad))
            plans.append(p)
            continue
        try:
            new_values = {f: normalize_value(f, v) for f, v in changes.items()}
        except (TypeError, ValueError) as e:
            p["errors"].append(f"invalid value: {e}")
            plans.append(p)
            continue
        merged = dict(row)
        for f, v in new_values.items():
            if not _same(f, row.get(f), v, row):
                p["changes"][f] = [row.get(f), v]
                merged[f] = v
        p["errors"] += validate_control_row(merged)
        lt = merged.get("load_type")
        incremental = lt in ("ct", "cursor")
        reasons = [RESEED_FIELDS[f] for f in p["changes"] if f in RESEED_FIELDS]
        if "src_connection" in p["changes"] and not merged.get("src_host"):
            reasons.append("source_changed")     # the connection is the source's host (state_store.source_identity)
        if "write_mode" in p["changes"] and not any(f in p["changes"] for f in SINK_FIELDS):
            p["errors"].append("write_mode can't change on the same sink (an append sink is a change log, a merge "
                               "sink the current rows): point the row at a new sink in the same edit")
        if incremental and reasons:
            p["reseed"] = reasons[0]
        if "load_type" in p["changes"] or ("cursor_column" in p["changes"] and lt == "cursor"):
            p["notes"].append("the load strategy changes: the next run reseeds the table")
        if "select_list" in p["changes"]:
            old_cols = {c.strip().lower() for c in str(row.get("select_list") or "").split(",") if c.strip()}
            new_cols = {c.strip().lower() for c in str(merged.get("select_list") or "").split(",") if c.strip()}
            if new_cols - old_cols:
                p["notes"].append(f"new columns {', '.join(sorted(new_cols - old_cols))}: the next run reseeds to backfill them")
            if old_cols - new_cols:
                p["notes"].append(f"removed columns {', '.join(sorted(old_cols - new_cols))} stay in the sink and stop updating")
        if any(f in p["changes"] for f in ("sink_catalog", "sink_schema", "sink_table")):
            old_sink = ".".join(str(row.get(k)) for k in ("sink_catalog", "sink_schema", "sink_table"))
            p["notes"].append(f"the old sink {old_sink} is left in place; drop it once the new one is verified")
        if "client_id" in p["changes"]:
            p["notes"].append("the rows already written under the old client_id stay in the sink; delete them separately")
        if "use_remote_query" in p["changes"]:
            p["notes"].append("Spark JDBC and remote_query map source types differently: keep each sink's rows on "
                              "one of the two (a new sink is safest)")
        if "client_attrs" in p["changes"]:
            p["notes"].append("rows already written keep their client_attrs values; columns added now are NULL on them")
        if "task_collection" in p["changes"]:
            p["notes"].append(f"moves to collection {merged['task_collection']}: that collection's runs and schedule apply")
        if p["reseed"]:
            p["notes"].insert(0, f"reseed required ({p['reseed']}): the table is copied again on its next run")
        plans.append(p)
    return plans


def control_rows_for_update_sql() -> str:
    cols = ", ".join(("id",) + CONTROL_EDIT_COLUMNS)
    return f"select {cols} from {SCHEMA}.control where id = any(%s) order by id for update"


def control_row_sql() -> str:
    cols = ", ".join(("id",) + CONTROL_EDIT_COLUMNS)
    return f"select {cols} from {SCHEMA}.control where id = %s"


def update_control_sql(fields: List[str]) -> str:
    """UPDATE of whitelisted columns only (field names come from CONTROL_EDIT_COLUMNS)."""
    assert fields and all(f in CONTROL_EDIT_COLUMNS for f in fields), fields
    return f"update {SCHEMA}.control set " + ", ".join(f"{f} = %s" for f in fields) + " where id = %s"


def collection_lock_sql() -> str:
    """Take a collection's run lock for this transaction (the same key the ingestion job holds for a
    whole run). False = a run of that collection is in progress. Param: lock key."""
    return "select pg_try_advisory_xact_lock(hashtextextended(%s, 0))"


def collection_lock_key(collection: str) -> str:
    return "lakefed_ingest:" + str(collection)   # state_store.LOCK_NAMESPACE + collection


def flip_to_seeding_sql() -> str:
    """Params: detail, control ids."""
    return f"update {SCHEMA}.checkpoint set {_RESEED_SET}, detail = %s where control_id = any(%s)"


def mixed_sinks_for_sql() -> str:
    """Sinks among the given (lower-cased catalog.schema.table) list claimed by a dedicated row and by
    other rows. Param: list of sink names."""
    return (f"select lower(sink_catalog || '.' || sink_schema || '.' || sink_table) as sink from {SCHEMA}.control "
            f"where lower(sink_catalog || '.' || sink_schema || '.' || sink_table) = any(%s) group by 1 "
            f"having count(*) filter (where client_id is null) > 0 and count(*) > 1")


def log_change_sql() -> str:
    """Params: changed_by, action, control_id, task_collection, field, old_value, new_value,
    reseed_reason, batch_id."""
    return (f"insert into {SCHEMA}.control_change (changed_by, action, control_id, task_collection, field, "
            f"old_value, new_value, reseed_reason, batch_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)")


def changes_sql(control_id: Optional[int], limit: int) -> Tuple[str, tuple]:
    where, params = "", ()
    if control_id is not None:
        where, params = "where control_id = %s ", (int(control_id),)
    return (f"select changed_at, changed_by, action, control_id, task_collection, field, old_value, new_value, "
            f"reseed_reason, batch_id from {SCHEMA}.control_change {where}order by changed_at desc, id desc limit %s",
            params + (int(limit),))


def checkpoint_for_sql() -> str:
    """One table's checkpoint. Param: control_id."""
    return (f"select phase, status, position, strategy, detail, last_success_at, updated_at, source_identity, "
            f"sink_identity, merge_key from {SCHEMA}.checkpoint where control_id = %s")


# ---- schedules ----------------------------------------------------------------------------------

SCHEDULE_FIELDS = ("name", "cadence_minutes", "timezone", "parameters", "job_variant", "stagger", "paused",
                   "notify_emails")


def schedules_sql() -> str:
    """Active schedules, by name."""
    return (f"select id, {', '.join(SCHEDULE_FIELDS)}, created_by, created_at, updated_at, deleted_at "
            f"from {SCHEMA}.schedule where deleted_at is null order by lower(name)")


def schedule_sql() -> str:
    """One schedule (deleted or not). Param: id."""
    return (f"select id, {', '.join(SCHEDULE_FIELDS)}, created_by, created_at, updated_at, deleted_at "
            f"from {SCHEMA}.schedule where id = %s")


def schedule_members_sql() -> str:
    """Members of the given schedules, active and removed. ``delete_pending``: removed, and its job not
    deleted yet (no successful sync since the removal). Param: list of schedule ids."""
    return (f"select id, schedule_id, task_collection, offset_minutes, job_id, synced_at, sync_error, added_at, removed_at, "
            f"(removed_at is not null and job_id is not null and (synced_at is null or synced_at < removed_at)) "
            f"as delete_pending "
            f"from {SCHEMA}.schedule_member where schedule_id = any(%s) order by schedule_id, task_collection")


# Syncs of one schedule are serialized: a second one waits, then sees the job ids the first recorded
# (two concurrent syncs would each create a job for an unsynced member). Two int keys, a class of its
# own: separate from the collection run locks (one key) and the job's other two-key classes.
SCHEDULE_LOCK_CLASS = 0x6C667364


def schedule_lock_sql() -> str:
    """Wait for, and take, a schedule's sync lock for this transaction. Params: class, schedule key."""
    return "select pg_advisory_xact_lock(%s::int, %s::int)"


def schedule_lock_key(schedule_id: int) -> int:
    return int(schedule_id) & 0x7FFFFFFF


def insert_schedule_sql() -> str:
    cols = SCHEDULE_FIELDS + ("created_by",)
    vals = ", ".join("%s::jsonb" if c == "parameters" else "%s" for c in cols)
    return f"insert into {SCHEMA}.schedule ({', '.join(cols)}) values ({vals}) returning id"


def update_schedule_sql(fields: List[str]) -> str:
    bad = [f for f in fields if f not in SCHEDULE_FIELDS]
    if bad or not fields:
        raise ValueError(f"not schedule fields: {bad}")
    sets = ", ".join(f"{f} = %s::jsonb" if f == "parameters" else f"{f} = %s" for f in fields)
    return f"update {SCHEMA}.schedule set {sets}, updated_at = now() where id = %s and deleted_at is null"


def delete_schedule_sql() -> str:
    """Soft delete (the app has no DELETE privilege). Param: id."""
    return f"update {SCHEMA}.schedule set deleted_at = now(), updated_at = now() where id = %s and deleted_at is null"


def insert_member_sql() -> str:
    return (f"insert into {SCHEMA}.schedule_member (schedule_id, task_collection, offset_minutes) "
            f"values (%s, %s, %s) returning id")


def update_member_offset_sql() -> str:
    return f"update {SCHEMA}.schedule_member set offset_minutes = %s where id = %s"


def remove_members_sql() -> str:
    """Soft-remove members (their jobs are deleted by the next sync). Param: list of member ids."""
    return f"update {SCHEMA}.schedule_member set removed_at = now() where id = any(%s) and removed_at is null"


def member_synced_sql() -> str:
    """Record a successful sync. Params: job_id, member id."""
    return f"update {SCHEMA}.schedule_member set job_id = %s, sync_error = null, synced_at = now() where id = %s"


def member_sync_failed_sql() -> str:
    """Record a failed sync, keeping synced_at (a removed member's job delete stays pending and is retried).
    Params: sync_error, member id."""
    return f"update {SCHEMA}.schedule_member set sync_error = %s where id = %s"


def collection_run_stats_sql() -> str:
    """Per collection: the latest run, run-time percentiles over 7 days, runs and failures in 24 hours,
    and the strictest freshness SLA among its enabled tables. Param: list of collections (4 times)."""
    return f"""
with r as (
    select task_collection, status, started_at, finished_at,
           extract(epoch from (finished_at - started_at)) as dur_s
    from {SCHEMA}.ingest_run
    where task_collection = any(%s) and started_at > now() - interval '7 days'
),
agg as (
    select task_collection,
           round(percentile_cont(0.95) within group (order by dur_s) filter (where finished_at is not null and status <> 'skipped')::numeric) as p95_s,
           round(percentile_cont(0.5) within group (order by dur_s) filter (where finished_at is not null and status <> 'skipped')::numeric) as p50_s,
           count(*) filter (where started_at > now() - interval '24 hours')                      as runs_24h,
           count(*) filter (where started_at > now() - interval '24 hours' and status = 'failed') as failed_24h,
           count(*) filter (where started_at > now() - interval '24 hours' and status = 'skipped') as lock_skipped_24h
    from r group by task_collection
),
last as (
    select distinct on (task_collection) task_collection, run_id as last_run_id, status as last_status,
           started_at as last_started, finished_at as last_finished,
           round(extract(epoch from (coalesce(finished_at, now()) - started_at)))::int as last_duration_s
    from {SCHEMA}.ingest_run where task_collection = any(%s)
    order by task_collection, started_at desc
),
sla as (
    select task_collection, min(freshness_sla_minutes) as min_sla_minutes, count(*) as enabled_tables
    from {SCHEMA}.control where task_enabled and task_collection = any(%s) group by task_collection
)
select c.task_collection, l.last_run_id, l.last_status, l.last_started, l.last_finished, l.last_duration_s,
       a.p50_s, a.p95_s, coalesce(a.runs_24h, 0) as runs_24h, coalesce(a.failed_24h, 0) as failed_24h,
       coalesce(a.lock_skipped_24h, 0) as lock_skipped_24h, s.min_sla_minutes, coalesce(s.enabled_tables, 0) as enabled_tables
from unnest(%s::text[]) as c(task_collection)
left join last l on l.task_collection = c.task_collection
left join agg a on a.task_collection = c.task_collection
left join sla s on s.task_collection = c.task_collection
order by c.task_collection"""


def timeline_runs_sql() -> str:
    """Runs overlapping a window. Params: window end, window start."""
    return (f"select run_id, task_collection, status, started_at, finished_at, job_run_id, ok, failed, skipped "
            f"from {SCHEMA}.ingest_run "
            f"where started_at < %s and coalesce(finished_at, now()) > %s order by task_collection, started_at")
