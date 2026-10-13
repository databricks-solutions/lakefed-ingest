"""SQL for the operations app — pure builders over the Lakebase schema (docs/lakebase_schema.md).

Every statement is parameterized (psycopg %s); nothing user-supplied is formatted into SQL. Kept
free of web-framework and database imports so it is unit-tested in app/tests/queries_test.py.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

SCHEMA = "lakefed_ingest"


# ---- overview -----------------------------------------------------------------------------------

def collections_overview_sql() -> str:
    """One row per task_collection: table counts, the latest run, and current health.
    Param: stale threshold in minutes."""
    return f"""
with tables as (
    select t.task_collection,
           count(*) filter (where t.task_enabled)                                   as enabled_tables,
           count(*)                                                                 as all_tables,
           count(*) filter (where t.task_enabled and cp.status = 'failed')          as failing_tables,
           count(*) filter (where t.task_enabled and (cp.control_id is null or cp.phase = 'seeding')) as seeding_tables,
           count(*) filter (where t.task_enabled and cp.last_success_at < now() - make_interval(mins => %s)) as stale_tables,
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
       tb.stale_tables, tb.last_success_at,
       r.run_id as last_run_id, r.status as last_run_status, r.started_at as last_run_started,
       r.finished_at as last_run_finished, r.ok as last_run_ok, r.failed as last_run_failed
from tables tb
left join last_run r on r.task_collection = tb.task_collection
order by tb.task_collection"""


def running_runs_sql() -> str:
    """Runs still marked running (newest first)."""
    return (f"select run_id, task_collection, job_run_id, started_at, parallelism "
            f"from {SCHEMA}.ingest_run where status = 'running' order by started_at desc")


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

TABLE_FILTERS = ("all", "failing", "stale", "seeding", "disabled")


def tables_sql(collection: Optional[str], search: Optional[str], status_filter: str,
               stale_minutes: int, limit: int) -> Tuple[str, tuple]:
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
    elif status_filter == "stale":
        conds.append("t.task_enabled and cp.last_success_at < now() - make_interval(mins => %s)")
        params.append(int(stale_minutes))
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
           f"cp.position, cp.detail "
           f"from {SCHEMA}.control t left join {SCHEMA}.checkpoint cp on cp.control_id = t.id "
           f"{where}order by t.task_collection, t.src_database, t.src_schema, t.src_table limit %s")
    return sql, tuple(params) + (int(limit),)


def collections_sql() -> str:
    return f"select distinct task_collection from {SCHEMA}.control order by 1"


def set_enabled_sql() -> str:
    """Params: enabled (bool), control ids (list)."""
    return f"update {SCHEMA}.control set task_enabled = %s where id = any(%s)"


def force_reseed_sql() -> str:
    """Flip checkpoints back to seeding (the next run re-copies those tables). Param: control ids."""
    return (f"update {SCHEMA}.checkpoint set phase = 'seeding', detail = 'reseed requested from the app', "
            f"updated_at = now() where control_id = any(%s)")


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
)


def validate_control_row(row: dict) -> List[str]:
    """Form-level checks with clear messages (the database CHECK constraints are the backstop)."""
    errs = []
    for col in ("task_collection", "src_host", "secret_key", "src_database", "src_schema", "src_table",
                "select_list", "sink_catalog", "sink_schema", "sink_table"):
        if not str(row.get(col) or "").strip():
            errs.append(f"{col} is required")
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
    return errs


def insert_control_sql() -> str:
    cols = ", ".join(CONTROL_INSERT_COLUMNS)
    marks = ", ".join(["%s"] * len(CONTROL_INSERT_COLUMNS))
    return f"insert into {SCHEMA}.control ({cols}) values ({marks}) returning id"


def control_params(row: dict) -> tuple:
    """Insert params in CONTROL_INSERT_COLUMNS order; blank strings become NULL."""
    def clean(v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v
    return tuple(clean(row.get(c)) for c in CONTROL_INSERT_COLUMNS)


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
    }


def insert_control_if_absent_sql() -> str:
    """Like insert_control_sql, but a row whose sink (or sink + client_id) is already taken is skipped
    (the unique index) instead of failing the whole import."""
    return insert_control_sql().replace(" returning id", " on conflict do nothing returning id")


EDITABLE_REVIEW_FIELDS = ("include", "load_type", "primary_key", "cursor_column", "load_partitioned",
                          "partition_col", "partition_size_mb", "sink_catalog", "sink_schema", "sink_table",
                          "client_id")


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
