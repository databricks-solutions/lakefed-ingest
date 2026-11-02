"""SQL for the operations app — pure builders over the Lakebase schema (docs/lakebase_schema.md).

Every statement is parameterized (psycopg %s); nothing user-supplied is formatted into SQL. Kept
free of web-framework and database imports so it is unit-tested in app/tests/queries_test.py.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

SCHEMA = "lakefed_ingest"
# The default port of each source type (the engine's source_adapters); a test keeps them equal.
DEFAULT_PORTS = {"sqlserver": 1433, "oracle": 1521, "postgresql": 5432, "mysql": 3306}


# ---- freshness SLA ------------------------------------------------------------------------------

def database_source_sql(alias: str) -> str:
    """The server, port and credential a control row reads its database through: the key of its
    database's check stamp (the engine's ``state_store.database_source_sql``; a test keeps them equal).
    A blank port reads the source type's default port, as ``DEFAULT_PORTS`` lists it, except on a
    connection-backed row (no host), whose port is the connection's and stays 0."""
    whens = " ".join(f"when '{src_type}' then {port}" for src_type, port in DEFAULT_PORTS.items())
    default_port = f"(case coalesce(nullif(lower(trim({alias}.src_type)), ''), 'sqlserver') {whens} else 0 end)"
    connection_backed = f"(nullif({alias}.src_host, '') is null and nullif({alias}.src_connection, '') is not null)"
    return (f"lower(coalesce(nullif({alias}.src_host, ''), nullif({alias}.src_connection, ''), '')) || '|' || "
            f"coalesce(nullif({alias}.src_port, 0), "
            f"case when {connection_backed} then 0 else {default_port} end) || '|' || "
            f"coalesce(nullif({alias}.secret_key, ''), nullif({alias}.src_connection, ''), '')")


# A wholly unchanged database gets one check stamp (database_check) instead of a last_success_at on each
# table. The stamp vouches for a ct table in the incremental phase at or past its version; for those, the
# last success is the later of the two (the engine's planner.stamp_vouches). Aliases: t = control,
# cp = checkpoint, dc = database_check (DATABASE_CHECK_JOIN).
DATABASE_CHECK_JOIN = (f"left join {SCHEMA}.database_check dc on dc.task_collection = t.task_collection "
                       f"and dc.src_database = t.src_database and dc.source = {database_source_sql('t')}")
LAST_SUCCESS = ("greatest(cp.last_success_at, case when cp.strategy = 'ct' and cp.phase = 'incremental' "
                "and cp.position ~ '^[0-9]+$' then case when cp.position::bigint >= dc.checked_version "
                "then dc.checked_at end end)")

# An enabled table with control.freshness_sla_minutes breaches its SLA when its last success (or, if
# it has never succeeded, its control row's creation) is older than the SLA. Tables without an SLA
# are never flagged. Aliases as above.
SLA_BREACHED = ("(t.task_enabled and t.freshness_sla_minutes is not null "
                f"and coalesce({LAST_SUCCESS}, t.created_at) < now() - make_interval(mins => t.freshness_sla_minutes))")


# The server a table's row reaches, as the engine's per-source query cap counts it (ct_engine.source_key): its UC
# connection when it reads through one (or names one and no host), else its host and port (the source type's default
# port when none is set, or 0, as the engine reads it). The instance-group label does not decide it: two labels on one
# server are one server. Alias t = control.


def endpoint_sql(alias: str) -> str:
    """SQL: a row's host and effective port, "host:port" (rollup.endpoint in Python), for a table with src_host,
    src_port and src_type under `alias`."""
    default = (f"case lower(coalesce({alias}.src_type, 'sqlserver')) when 'oracle' then '1521' "
               f"when 'postgresql' then '5432' when 'mysql' then '3306' else '1433' end")
    return f"lower({alias}.src_host) || ':' || coalesce(nullif({alias}.src_port, 0)::text, {default})"


SERVER = (f"(case when t.src_connection is not null and (t.use_remote_query or t.src_host is null) "
          f"then lower(t.src_connection) when t.src_host is not null then {endpoint_sql('t')} else '' end)")
# One source database: its server (SERVER) and its name, so two servers' databases of the same name stay
# two databases. Alias t = control.
DATABASE = f"({SERVER}, coalesce(t.src_database, ''))"

# Each enabled table in exactly one state, the first that applies: failing, waiting for a first load,
# late (SLA breached), else current. Aliases: t = control, cp = checkpoint.
_FAILING = "cp.status = 'failed'"
_FIRST_LOAD = "(cp.control_id is null or cp.phase = 'seeding')"
STATE_COUNTS = (
    f"count(*) filter (where t.task_enabled and {_FAILING}) as state_failing, "
    f"count(*) filter (where t.task_enabled and not coalesce({_FAILING}, false) "
    f"and {_FIRST_LOAD}) as state_first_load, "
    f"count(*) filter (where t.task_enabled and not coalesce({_FAILING}, false) and not {_FIRST_LOAD} "
    f"and {SLA_BREACHED}) as state_late, "
    f"count(*) filter (where t.task_enabled and not coalesce({_FAILING}, false) and not {_FIRST_LOAD} "
    f"and not {SLA_BREACHED}) as state_current")


# ---- overview -----------------------------------------------------------------------------------

def database_states_sql(only_groups: bool = False) -> str:
    """One row per (server, connection, host, ingestion group, database, instance-group label): enabled tables by
    state (STATE_COUNTS), tables without a freshness target, the oldest and newest success of its enabled tables,
    and the newest of any table (what a UC connection's databases show). A scan of the control table, so the
    monitoring views read it only for a group with no rollup row yet (only_groups: param task_collection list).
    The label rides along so Sources can say where it differs from the server."""
    where = "where t.task_collection = any(%s)" if only_groups else ""
    return f"""
select {SERVER} as server, t.src_connection, lower(t.src_host) as src_host, t.task_collection,
       coalesce(t.src_database, '') as src_database, t.source_instance_group as instance_label,
       count(*) filter (where t.task_enabled) as tables,
       count(*) filter (where t.task_enabled and t.freshness_sla_minutes is null) as no_sla_tables,
       {STATE_COUNTS},
       min({LAST_SUCCESS}) filter (where t.task_enabled) as oldest_success_at,
       max({LAST_SUCCESS}) filter (where t.task_enabled) as newest_success_at,
       max({LAST_SUCCESS}) as newest_any_success_at
from {SCHEMA}.control t
left join {SCHEMA}.checkpoint cp on cp.control_id = t.id
{DATABASE_CHECK_JOIN}
{where}
group by 1, 2, 3, 4, 5, 6"""


# ---- monitoring rollups ---------------------------------------------------------------------------
# The engine rewrites these rows (state_store.rollup_database_rewrite_sql, rollup_group_rewrite_sql) after each
# database's work, and the app rewrites the same rows after its own edits. The two statements are the engine's
# text, copied here; app/tests/rollup_sql_test.py keeps them equal. Parameters: the collection and the database
# name of each database to rewrite (a list each), then the collection of each group (a list).

ROLLUP_CHUNK = 200
_ROLLUP_DATABASE_REWRITE = """
with keys as (
    select k.task_collection, k.src_database
    from unnest(%s::text[], %s::text[]) as k(task_collection, src_database)),
states as (
    select t.task_collection, coalesce(t.src_database, '') as src_database, (case when t.src_connection is not null and (t.use_remote_query or t.src_host is null) then lower(t.src_connection) when t.src_host is not null then lower(t.src_host) || ':' || coalesce(nullif(t.src_port, 0)::text, case lower(coalesce(t.src_type, 'sqlserver')) when 'oracle' then '1521' when 'postgresql' then '5432' when 'mysql' then '3306' else '1433' end) else '' end) as server,
           coalesce(t.src_connection, '') as src_connection, coalesce(lower(t.src_host), '') as src_host,
           coalesce(t.source_instance_group, '') as instance_label, t.task_enabled, t.freshness_sla_minutes,
           t.created_at, coalesce(cp.status = 'failed', false) as failing,
           (cp.control_id is null or cp.phase = 'seeding') as first_load,
           greatest(cp.last_success_at, case when cp.strategy = 'ct' and cp.phase = 'incremental'
                    and cp.position ~ '^[0-9]+$' then case when cp.position::bigint >= dc.checked_version
                    then dc.checked_at end end) as last_success_at
    from keys k
    join lakefed_ingest.control t on t.task_collection = k.task_collection and coalesce(t.src_database, '') = k.src_database
    left join lakefed_ingest.checkpoint cp on cp.control_id = t.id
    left join lakefed_ingest.database_check dc on dc.task_collection = t.task_collection
        and dc.src_database = t.src_database and dc.source = lower(coalesce(nullif(t.src_host, ''), nullif(t.src_connection, ''), '')) || '|' || coalesce(nullif(t.src_port, 0), case when (nullif(t.src_host, '') is null and nullif(t.src_connection, '') is not null) then 0 else (case coalesce(nullif(lower(trim(t.src_type)), ''), 'sqlserver') when 'sqlserver' then 1433 when 'oracle' then 1521 when 'postgresql' then 5432 when 'mysql' then 3306 else 0 end) end) || '|' || coalesce(nullif(t.secret_key, ''), nullif(t.src_connection, ''), '')),
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
    insert into lakefed_ingest.rollup_database (task_collection, src_database, server, src_connection, src_host,
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
    update lakefed_ingest.rollup_database r set tables = 0, no_sla_tables = 0, failing = 0, first_load = 0, loaded = 0,
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
_ROLLUP_GROUP_REWRITE = """
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
    left join lakefed_ingest.rollup_database r on r.task_collection = g.task_collection
    group by g.task_collection)
insert into lakefed_ingest.rollup_group (task_collection, tables, no_sla_tables, failing, first_load, loaded, databases,
    servers, oldest_success_at, newest_success_at, updated_at)
select task_collection, tables, no_sla_tables, failing, first_load, loaded, databases, servers,
       oldest_success_at, newest_success_at, now()
from agg
on conflict (task_collection) do update set
    tables = excluded.tables, no_sla_tables = excluded.no_sla_tables, failing = excluded.failing,
    first_load = excluded.first_load, loaded = excluded.loaded, databases = excluded.databases,
    servers = excluded.servers, oldest_success_at = excluded.oldest_success_at,
    newest_success_at = excluded.newest_success_at, updated_at = now()"""


def rollup_database_rewrite_sql() -> str:
    """Recompute the rollup rows of some databases of their collections, in one statement."""
    return _ROLLUP_DATABASE_REWRITE


def rollup_group_rewrite_sql() -> str:
    """Recompute the rollup rows of some ingestion groups from their database rows, in one statement."""
    return _ROLLUP_GROUP_REWRITE


def rollup_states_sql() -> str:
    """Every database's rollup row, one per grain (server, connection, host, instance label), as the monitoring views
    read them in place of a scan of the control table. late_database is whether an enabled table with a freshness
    target is past its due time now: the rewrite keeps the earliest due time, so this is live without a scan.
    Rows with no control row left (a table moved to another grain) are not read."""
    return f"""
select r.server, nullif(r.src_connection, '') as src_connection, nullif(r.src_host, '') as src_host,
       r.task_collection, r.src_database, nullif(r.instance_label, '') as instance_label,
       r.tables, r.no_sla_tables, r.failing as state_failing, r.first_load as state_first_load,
       r.loaded as state_loaded, coalesce(r.earliest_due_at < now(), false) as late_database,
       r.oldest_success_at, r.newest_success_at, r.newest_any_success_at
from {SCHEMA}.rollup_database r
where r.control_rows > 0"""


def rollup_group_names_sql() -> str:
    """The ingestion groups that have a rollup row (a group written by a rewrite, a check or an edit)."""
    return f"select task_collection from {SCHEMA}.rollup_group"


def control_group_names_sql() -> str:
    """Every ingestion group with a control row. A scan of the control index: read at most once per few minutes."""
    return f"select distinct task_collection from {SCHEMA}.control"


def database_states_for_groups_sql() -> str:
    """database_states_sql for some groups only (param: task_collection list), for a group with no rollup row yet."""
    return database_states_sql(only_groups=True)


def control_databases_sql(by: str) -> str:
    """The (collection, database name) of the control rows of some ids (by="ids") or some groups (by="groups").
    Param: the list of ids or of group names."""
    where = "t.id = any(%s)" if by == "ids" else "t.task_collection = any(%s)"
    return (f"select distinct t.task_collection, coalesce(t.src_database, '') from {SCHEMA}.control t where {where}")


def database_counts_sql() -> str:
    """One database's enabled tables by state, read live (the database page): the same counts as STATE_COUNTS over its
    control rows. Params: group, server, database. control_rows is 0 when the database has no row."""
    return f"""
select count(*) as control_rows,
       count(*) filter (where t.task_enabled) as tables,
       count(*) filter (where t.task_enabled and t.freshness_sla_minutes is null) as no_sla_tables,
       {STATE_COUNTS}
from {SCHEMA}.control t
left join {SCHEMA}.checkpoint cp on cp.control_id = t.id
{DATABASE_CHECK_JOIN}
where t.task_collection = %s and {SERVER} = %s and coalesce(t.src_database, '') = %s"""


def not_a_check(alias: str = "") -> str:
    """SQL: the ingest_run row is a group run, not the check job's own record of a check (schedules.is_check):
    work_id "check-<job run>", or a detail holding the check's summary for records written before that mark."""
    a = f"{alias}." if alias else ""
    return (f"left(coalesce({a}work_id, ''), 6) <> 'check-' "
            f"and left(coalesce({a}detail, ''), 9) <> '{{\"check\":'")


def last_runs_sql() -> str:
    """Each ingestion group's latest run: the groups with tables or in a job, each read newest first through
    ingest_run_collection_idx, so it costs one index step per group rather than a scan of every run ever made."""
    return f"""
select r.* from (select task_collection from {SCHEMA}.control
                 union select task_collection from {SCHEMA}.schedule_member) g
cross join lateral (
    select task_collection, run_id, status, started_at, finished_at, total, ok, failed, skipped, detail
    from {SCHEMA}.ingest_run
    where task_collection = g.task_collection and {not_a_check()}
    order by started_at desc limit 1
) r"""


def running_progress_sql() -> str:
    """Every running group run's progress in one pass: its group's enabled tables (a work-list run's
    planned tables: its total, recorded at the start) and how many it has checked (stamped since it
    started, on the database's clock like started_at, a database check stamp included), its table events so far
    (unchanged tables are stamped in bulk without events), the loads that finished in the last 5 minutes
    (live speed and time left), and its group's 7-day median and p95 run time. A run another run closed
    never recorded its totals (its finish is the closing time), so it is left out of the median and p95.
    Skipped events are reseeds queued: done for this run, though not stamped as checked."""
    return f"""
with r as (
    select run_id, task_collection, job_run_id, cluster_id, parallelism, started_at, total
    from {SCHEMA}.ingest_run where status = 'running' and {not_a_check()}
),
tables as (
    select r.run_id,
           count(*) filter (where t.task_enabled) as enabled_tables,
           count(*) filter (where t.task_enabled and {LAST_SUCCESS} >= r.started_at) as tables_checked
    from r join {SCHEMA}.control t on t.task_collection = r.task_collection
    left join {SCHEMA}.checkpoint cp on cp.control_id = t.id
    {DATABASE_CHECK_JOIN}
    group by r.run_id
),
events as (
    select e.run_id,
           count(*) filter (where e.status = 'ok')      as tables_ok,
           count(*) filter (where e.status = 'failed')  as tables_failed,
           count(*) filter (where e.status = 'running') as tables_running,
           count(*) filter (where e.status = 'skipped') as tables_skipped,
           min(e.started_at)                            as work_started_at,
           count(*) filter (where e.status = 'ok' and e.finished_at > now() - interval '5 minutes') as ok_last_5m,
           sum(e.rows_merged) filter (where e.status = 'ok' and e.finished_at > now() - interval '5 minutes') as rows_last_5m
    from {SCHEMA}.ingest_event e join r on r.run_id = e.run_id
    group by e.run_id
)
select r.run_id, r.task_collection, r.job_run_id, r.cluster_id, r.parallelism, r.started_at,
       round(extract(epoch from (now() - r.started_at)))::int as elapsed_s,
       coalesce(r.total, tb.enabled_tables, 0) as enabled_tables, coalesce(tb.tables_checked, 0) as tables_checked,
       coalesce(ev.tables_ok, 0) as tables_ok, coalesce(ev.tables_failed, 0) as tables_failed,
       coalesce(ev.tables_running, 0) as tables_running, coalesce(ev.tables_skipped, 0) as tables_skipped,
       ev.work_started_at,
       coalesce(ev.ok_last_5m, 0) as ok_last_5m, ev.rows_last_5m, p.p50_s, p.p95_s
from r
left join tables tb on tb.run_id = r.run_id
left join events ev on ev.run_id = r.run_id
left join lateral (
    select round(percentile_cont(0.5) within group (order by extract(epoch from (x.finished_at - x.started_at)))::numeric) as p50_s,
           round(percentile_cont(0.95) within group (order by extract(epoch from (x.finished_at - x.started_at)))::numeric) as p95_s
    from {SCHEMA}.ingest_run x
    where x.task_collection = r.task_collection and x.finished_at is not null and x.status not in ('skipped', 'cancelled')
      and {not_a_check("x")}
      and x.total is not null and x.started_at > now() - interval '7 days') p on true
order by r.started_at desc"""


# Loads that recorded no row count and that no batched append's total covers: a batch's members record none, since
# the batch's commit reports one total for them all (on the run's detail, batch_rows), and their pool is batch<size>.
UNCOUNTED_OUTSIDE_BATCHES = ("count(*) filter (where e.status = 'ok' and e.rows_merged is null "
                             "and coalesce(e.pool, '') not like 'batch%%')")


def run_work_sql() -> str:
    """Per group run: its work span (first table start to last table finish), tables loaded and failed,
    and rows written (uncounted: loads with no count outside a batched append). Param: array of run ids."""
    return (f"select e.run_id, min(e.started_at) as work_started_at, max(e.finished_at) as work_finished_at, "
            f"count(*) filter (where e.status = 'ok') as loaded, "
            f"count(*) filter (where e.status = 'failed') as failed_tables, "
            f"sum(e.rows_merged) as rows_written, "
            f"{UNCOUNTED_OUTSIDE_BATCHES} as uncounted "
            f"from {SCHEMA}.ingest_event e where e.run_id = any(%s) group by e.run_id")


def collections_overview_sql() -> str:
    """One row per task_collection: table counts, the latest run, and current health. The state_* counts
    put each enabled table in exactly one state (STATE_COUNTS) for the state bar; rows_24h is the rows
    its table loads wrote in the last 24 hours (batched appends record theirs on the run, so they are
    added from run_batch_rows_sql)."""
    return f"""
with tables as (
    select t.task_collection,
           count(*) filter (where t.task_enabled)                                   as enabled_tables,
           count(*)                                                                 as all_tables,
           count(*) filter (where t.task_enabled and cp.status = 'failed')          as failing_tables,
           count(*) filter (where t.task_enabled and (cp.control_id is null or cp.phase = 'seeding')) as seeding_tables,
           count(*) filter (where t.task_enabled and t.freshness_sla_minutes is null) as no_sla_tables,
           count(*) filter (where {SLA_BREACHED})                                   as sla_breached_tables,
           max({LAST_SUCCESS})                                                      as last_success_at,
           count(distinct {DATABASE}) filter (where t.task_enabled)               as databases,
           count(distinct {SERVER}) filter (where t.task_enabled)                  as servers,
           min({SERVER}) filter (where t.task_enabled)                             as server,
           {STATE_COUNTS}
    from {SCHEMA}.control t
    left join {SCHEMA}.checkpoint cp on cp.control_id = t.id
    {DATABASE_CHECK_JOIN}
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
       r.finished_at as last_run_finished, r.ok as last_run_ok, r.failed as last_run_failed,
       tb.databases, tb.servers, tb.server, tb.state_current, tb.state_late, tb.state_first_load, tb.state_failing,
       w.rows_24h
from tables tb
left join last_run r on r.task_collection = tb.task_collection
left join lateral (select sum(e.rows_merged) as rows_24h from {SCHEMA}.ingest_event e
                   where e.task_collection = tb.task_collection
                     and e.finished_at > now() - interval '24 hours') w on true
order by tb.task_collection"""


def running_runs_sql() -> str:
    """Runs still marked running (newest first), with progress: tables finished so far (unchanged tables
    are bulk-advanced without events, so these count tables that needed work) and elapsed time against
    the collection's typical run (median of its last 20 finished runs).

    tables_checked out of enabled_tables is the run's progress: the engine stamps
    checkpoint.last_success_at on every unchanged table once change detection ends (or one database_check
    stamp for a wholly unchanged database, LAST_SUCCESS), and on every loaded table as it finishes, both on
    the database's clock like started_at. enabled_tables is the tables the run planned (ingest_run.total
    while it runs; fewer than the collection's for a run on a check's work list), else the collection's
    enabled tables. last_activity_at is when a table of the run last started or finished, the sign that the
    run is alive."""
    return f"""
select r.run_id, r.task_collection, r.job_run_id, r.started_at, r.parallelism,
       round(extract(epoch from (now() - r.started_at)))::int as elapsed_s,
       (select count(*) from {SCHEMA}.ingest_event e where e.run_id = r.run_id and e.status = 'ok')      as tables_ok,
       (select count(*) from {SCHEMA}.ingest_event e where e.run_id = r.run_id and e.status = 'failed')  as tables_failed,
       (select count(*) from {SCHEMA}.ingest_event e where e.run_id = r.run_id and e.status = 'running') as tables_running,
       (select round(percentile_cont(0.5) within group (order by extract(epoch from (x.finished_at - x.started_at)))::numeric)
          from (select started_at, finished_at from {SCHEMA}.ingest_run x
                where x.task_collection = r.task_collection and x.finished_at is not null and x.status not in ('skipped', 'cancelled')
                order by x.started_at desc limit 20) x)                                                as typical_s,
       coalesce(r.total, (select count(*) from {SCHEMA}.control t
         where t.task_collection = r.task_collection and t.task_enabled))                     as enabled_tables,
       (select count(*) from {SCHEMA}.control t join {SCHEMA}.checkpoint cp on cp.control_id = t.id
         {DATABASE_CHECK_JOIN}
         where t.task_collection = r.task_collection and t.task_enabled
           and {LAST_SUCCESS} >= r.started_at)                                               as tables_checked,
       (select max(coalesce(e.finished_at, e.started_at)) from {SCHEMA}.ingest_event e
         where e.run_id = r.run_id)                                                          as last_activity_at,
       (select min({SERVER}) from {SCHEMA}.control t where t.task_collection = r.task_collection)      as server
from {SCHEMA}.ingest_run r
where r.status = 'running'
order by r.started_at desc"""


# ---- runs / events ------------------------------------------------------------------------------

_RUN_COLUMNS = ("run_id, task_collection, status, started_at, finished_at, "
                "round(extract(epoch from (coalesce(finished_at, now()) - started_at))::numeric, 0) as duration_s, "
                "total, ok, failed, skipped, reseeded, job_run_id, cluster_id, parallelism, detail, work_id")


def runs_sql(collection: Optional[str], limit: int = 100) -> Tuple[str, tuple]:
    """Recent group runs, optionally for one collection. Returns (sql, params)."""
    where, params = f"where {not_a_check()} ", ()
    if collection:
        where, params = f"where task_collection = %s and {not_a_check()} ", (collection,)
    sql = f"select {_RUN_COLUMNS} from {SCHEMA}.ingest_run {where}order by started_at desc limit %s"
    return sql, params + (int(limit),)


def run_sql() -> str:
    """One run, with the same columns as runs_sql. Param: run_id."""
    return f"select {_RUN_COLUMNS} from {SCHEMA}.ingest_run where run_id = %s"


# The most table events the group run page lists.
RUN_EVENTS_LIMIT = 500


def run_events_sql() -> str:
    """Per-table events of one run, failures first, at most RUN_EVENTS_LIMIT. Param: run_id."""
    return (f"select control_id, src_database, src_schema, src_table, sink_fqn, action, stage, status, "
            f"duration_ms, rows_merged, position_from, position_to, error "
            f"from {SCHEMA}.ingest_event where run_id = %s "
            f"order by (status = 'failed') desc, duration_ms desc nulls last limit {RUN_EVENTS_LIMIT}")


def in_flight_sql() -> str:
    """The tables a group run is loading now, longest first. Params: run_id, limit."""
    return (f"select e.src_database, e.src_schema, e.src_table, e.action, e.stage, e.started_at, "
            f"round(extract(epoch from (now() - e.started_at)))::int as running_s, e.retry_count "
            f"from {SCHEMA}.ingest_event e where e.run_id = %s and e.status = 'running' "
            f"order by e.started_at limit %s")


def in_flight_count_sql() -> str:
    """How many tables a group run is loading now. Param: run_id."""
    return f"select count(*) as tables from {SCHEMA}.ingest_event where run_id = %s and status = 'running'"


def recent_errors_sql() -> str:
    """Failed table events in the last N hours, newest first. Params: hours, limit."""
    return (f"select e.finished_at, e.task_collection, e.run_id, e.control_id, e.src_database, "
            f"e.src_schema, e.src_table, e.stage, e.error "
            f"from {SCHEMA}.ingest_event e "
            f"where e.status = 'failed' and e.started_at > now() - make_interval(hours => %s) "
            f"order by e.started_at desc limit %s")


def servers_sql() -> str:
    """One row per source server (SERVER) with its databases' enabled tables by state, how many
    collections it spans and how many of them are running now."""
    return f"""
select {SERVER} as server,
       count(distinct t.src_database) filter (where t.task_enabled) as databases,
       count(*) filter (where t.task_enabled)                       as tables,
       count(distinct t.task_collection)                            as collections,
       {STATE_COUNTS},
       max({LAST_SUCCESS}) filter (where t.task_enabled)            as newest_success_at,
       min({LAST_SUCCESS}) filter (where t.task_enabled)            as oldest_success_at,
       count(distinct t.task_collection) filter (where exists (
           select 1 from {SCHEMA}.ingest_run r where r.task_collection = t.task_collection
           and r.status = 'running'))                               as running_collections
from {SCHEMA}.control t
left join {SCHEMA}.checkpoint cp on cp.control_id = t.id
{DATABASE_CHECK_JOIN}
group by 1
order by 1"""


def collection_databases_sql() -> str:
    """One row per source database of a collection: its enabled tables by state, its oldest and newest
    success, and, while a run of the collection is going, how many of its tables that run has checked
    (stamped since it started). Bounded to one collection, so it needs no roll-up table.
    Params: task_collection, task_collection."""
    return f"""
with run as (
    select started_at from {SCHEMA}.ingest_run
    where task_collection = %s and status = 'running' order by started_at desc limit 1
)
select {SERVER} as server,
       coalesce(t.src_database, '') as src_database,
       count(*) filter (where t.task_enabled) as tables,
       {STATE_COUNTS},
       count(*) filter (where t.task_enabled
                        and {LAST_SUCCESS} >= (select started_at from run)) as checked_this_run,
       min({LAST_SUCCESS}) filter (where t.task_enabled) as oldest_success_at,
       max({LAST_SUCCESS}) filter (where t.task_enabled) as newest_success_at
from {SCHEMA}.control t
left join {SCHEMA}.checkpoint cp on cp.control_id = t.id
{DATABASE_CHECK_JOIN}
where t.task_collection = %s
group by 1, 2
order by 2, 1"""


def collection_database_rows_sql() -> str:
    """Rows written per source database (server and name) of a collection: by first loads (seed, reseed
    or full), each table's latest successful one only, which is the source's row count at that load, so
    a reseed or a full reload replaces a table's count instead of adding to it; and by changes in the
    last 24 hours. With the first loads that recorded no count (batched appends record one total on the
    run), and the running run's tables loaded, in flight and failed. Events of a deleted control row
    count under a blank server. Params: task_collection."""
    return f"""
with ev as (
    select {SERVER} as server,
           coalesce(e.src_database, '') as src_database,
           e.id, e.run_id, e.control_id, e.action, e.status, e.finished_at, e.rows_merged
    from {SCHEMA}.ingest_event e
    left join {SCHEMA}.control t on t.id = e.control_id
    where e.task_collection = %s
      -- Only the events read below, not the group's whole history: first loads (any age, through
      -- ingest_event_first_load_idx), the last 24 hours, and the running run's.
      and ((e.status = 'ok' and e.action in ('seed', 'reseed', 'full'))
           or e.finished_at > now() - interval '24 hours' or e.status = 'running'
           or exists (select 1 from {SCHEMA}.ingest_run r where r.run_id = e.run_id and r.status = 'running'))
),
latest_first as (
    select distinct on (control_id) server, src_database, rows_merged
    from ev where status = 'ok' and action in ('seed', 'reseed', 'full')
    order by control_id, finished_at desc nulls last, id desc
),
first_loads as (
    select server, src_database, sum(rows_merged) as rows_first_loads, count(*) as first_loads,
           count(*) filter (where rows_merged is null) as first_loads_uncounted
    from latest_first group by 1, 2
),
activity as (
    select ev.server, ev.src_database,
           sum(ev.rows_merged) filter (where ev.status = 'ok' and ev.action = 'increment'
                                       and ev.finished_at > now() - interval '24 hours') as rows_changes_24h,
           count(*) filter (where ev.status = 'ok' and r.status = 'running')             as loaded_this_run,
           count(*) filter (where ev.status = 'running' and r.status = 'running')        as in_flight,
           count(*) filter (where ev.status = 'failed' and r.status = 'running')         as failed_this_run
    from ev join {SCHEMA}.ingest_run r on r.run_id = ev.run_id
    group by 1, 2
)
select a.server, a.src_database, f.rows_first_loads, coalesce(f.first_loads, 0) as first_loads,
       coalesce(f.first_loads_uncounted, 0) as first_loads_uncounted, a.rows_changes_24h,
       a.loaded_this_run, a.in_flight, a.failed_this_run
from activity a
left join first_loads f on f.server = a.server and f.src_database = a.src_database"""


def run_databases_sql() -> str:
    """One row per source database (server and name) of a run: tables loaded, in flight and failed, and
    rows written (uncounted: loads with no per-table count, such as members of a batched append). Events
    of a deleted control row count under a blank server. Param: run_id."""
    return f"""
select {SERVER} as server,
       coalesce(e.src_database, '') as src_database,
       count(*) filter (where e.status = 'ok')      as loaded,
       count(*) filter (where e.status = 'running') as in_flight,
       count(*) filter (where e.status = 'failed')  as failed,
       sum(e.rows_merged)                           as rows_written,
       count(*) filter (where e.status = 'ok' and e.rows_merged is null) as uncounted
from {SCHEMA}.ingest_event e
left join {SCHEMA}.control t on t.id = e.control_id
where e.run_id = %s
group by 1, 2
order by 2, 1"""


def group_runs_sql() -> str:
    """The runs of some ingestion groups that started in a window, oldest first: the group runs placed into
    job runs (schedules.job_runs). Params: groups, window start, window end."""
    return (f"select {_RUN_COLUMNS} from {SCHEMA}.ingest_run "
            f"where task_collection = any(%s) and started_at >= %s and started_at < %s order by started_at")


def first_started_sql() -> str:
    """The first start per ingestion group among the records of one check job run ("check-<job run>") or one
    launch's group runs ("<launch_id>:<collection>"): on a check schedule they all belong to the job run its
    members' first one started in. Params: key, key + ":%"."""
    return (f"select task_collection, min(started_at) as started_at from {SCHEMA}.ingest_run "
            f"where work_id = %s::text or work_id like %s::text group by task_collection")


def last_ran_group_runs_sql() -> str:
    """Per membership of each job, its group's newest finished group run (not skipped, not running) while it was
    the member, with its schedule_id: where a job's last job run is found when none of its recent triggers ran.
    Within one member, start order is slot order, so the newest slot among these is the job's newest that ran.
    Each member's is one step down ingest_run_collection_idx, not a pass over its whole history. Param: schedule ids."""
    return f"""
select m.schedule_id, r.*
from {SCHEMA}.schedule_member m
cross join lateral (
    select {_RUN_COLUMNS} from {SCHEMA}.ingest_run
    where task_collection = m.task_collection and status not in ('skipped', 'running')
      and (m.added_at is null or started_at >= m.added_at) and (m.removed_at is null or started_at < m.removed_at)
    order by started_at desc limit 1
) r
where m.schedule_id = any(%s)"""


def group_runs_in_windows_sql() -> str:
    """The runs of each (ingestion group, window start, window end) given, in one read: the job runs of several
    jobs, each around its own slot. Params: groups, starts, ends (arrays of one length)."""
    return (f"select {_RUN_COLUMNS} from {SCHEMA}.ingest_run r where exists ("
            f"select 1 from unnest(%s::text[], %s::timestamptz[], %s::timestamptz[]) as w(task_collection, starts, ends) "
            f"where w.task_collection = r.task_collection and r.started_at >= w.starts and r.started_at < w.ends) "
            f"order by started_at")    # exists: a run in two windows (overlapping, or a group in two jobs) comes once


def group_runs_before_sql() -> str:
    """The newest group runs of some ingestion groups that started before a time, newest first. Params: groups,
    before, limit."""
    return (f"select {_RUN_COLUMNS} from {SCHEMA}.ingest_run "
            f"where task_collection = any(%s) and started_at < %s and {not_a_check()} "
            f"order by started_at desc limit %s")


def job_run_databases_sql() -> str:
    """run_databases_sql over the group runs of one job run, with each database's ingestion group: failing
    first, then in flight, then by name. Param: run ids."""
    return f"""
select e.task_collection, {SERVER} as server, coalesce(e.src_database, '') as src_database,
       count(*) filter (where e.status = 'ok')      as loaded,
       count(*) filter (where e.status = 'running') as in_flight,
       count(*) filter (where e.status = 'failed')  as failed,
       sum(e.rows_merged)                           as rows_written,
       count(*) filter (where e.status = 'ok' and e.rows_merged is null) as uncounted
from {SCHEMA}.ingest_event e
left join {SCHEMA}.control t on t.id = e.control_id
where e.run_id = any(%s)
group by 1, 2, 3
order by (count(*) filter (where e.status = 'failed')) desc, (count(*) filter (where e.status = 'running')) desc, 3, 2, 1"""


def run_failures_sql() -> str:
    """The failed table events of some group runs, newest first, with each table's server: what
    group_errors groups by cause. Param: run ids."""
    return (f"select e.finished_at, e.task_collection, e.run_id, e.control_id, {SERVER} as server, e.src_database, "
            f"e.src_schema, e.src_table, e.stage, e.error from {SCHEMA}.ingest_event e "
            f"left join {SCHEMA}.control t on t.id = e.control_id "
            f"where e.run_id = any(%s) and e.status = 'failed' order by e.finished_at desc")


def not_ingested_sql() -> str:
    """Per discovered (host and port, database), from its latest successful discovery: its endpoint ("host:port",
    as SERVER spells a JDBC server), tables found, how many have no control row yet (matched as
    discovered_tables_sql's registered_id is, in any group), and the largest of those."""
    return f"""
with latest as (
    select distinct on ({endpoint_sql('d')}, lower(d.src_database)) d.discovery_id, d.src_host, d.src_database,
           d.finished_at, {endpoint_sql('d')} as endpoint
    from {SCHEMA}.discovery d where d.status = 'ok'
    order by {endpoint_sql('d')}, lower(d.src_database), d.finished_at desc
),
found as (
    select l.endpoint, l.src_host, l.src_database, l.finished_at, dt.src_schema, dt.src_table, dt.size_mb,
           exists (select 1 from {SCHEMA}.control c where c.src_host is not null and {endpoint_sql('c')} = l.endpoint
                   and lower(c.src_database) = lower(dt.src_database) and lower(c.src_schema) = lower(dt.src_schema)
                   and lower(c.src_table) = lower(dt.src_table)) as registered
    from latest l
    join {SCHEMA}.discovered_table dt on dt.discovery_id = l.discovery_id and dt.src_database = l.src_database
)
select endpoint, src_host, src_database, finished_at as discovered_at, count(*) as discovered,
       count(*) filter (where not registered) as not_registered,
       (array_agg(src_schema || '.' || src_table order by size_mb desc nulls last)
          filter (where not registered))[1] as largest_not_registered
from found
group by 1, 2, 3, 4
order by 1, 3"""


def _database_rows() -> str:
    return (f"select t.* from {SCHEMA}.control t where t.task_collection = %s and {SERVER} = %s "
            f"and coalesce(t.src_database, '') = %s")


def database_tables_sql() -> str:
    """One database's tables in one ingestion group, with their state now and their part in a group run
    (its latest event for the table, and whether it is still to go): failing first, then in flight, then to
    go, then by name. A table's last success counts its database's check stamp (LAST_SUCCESS). Params: group,
    server, database, run_id (or None), schema.table pattern, limit, offset."""
    return f"""
with t as ({_database_rows()}),
r as (select run_id, started_at from {SCHEMA}.ingest_run where run_id = %s)
select t.id, t.src_schema, t.src_table, t.load_type, t.task_enabled, cp.phase, cp.status, {LAST_SUCCESS} as last_success_at,
       round(extract(epoch from (now() - {LAST_SUCCESS})) / 60)::int as lag_min,
       t.freshness_sla_minutes as sla_min, {SLA_BREACHED} as sla_breached,
       case when not t.task_enabled then 'disabled' when {_FAILING} then 'failing' when {_FIRST_LOAD} then 'first load'
            when {SLA_BREACHED} then 'late' else 'current' end as state,
       e.status as run_status, e.stage as run_stage, e.started_at as run_started_at, e.rows_merged as run_rows,
       round(extract(epoch from (now() - e.started_at)))::int as run_running_s,
       (r.run_id is not null and e.status is null and t.task_enabled
        and ({LAST_SUCCESS} is null or {LAST_SUCCESS} < r.started_at)) as run_to_go
from t
left join {SCHEMA}.checkpoint cp on cp.control_id = t.id
{DATABASE_CHECK_JOIN}
left join r on true
left join lateral (select ev.status, ev.stage, ev.started_at, ev.rows_merged from {SCHEMA}.ingest_event ev
                   where ev.run_id = r.run_id and ev.control_id = t.id order by ev.started_at desc limit 1) e on true
where (t.src_schema || '.' || t.src_table) ilike %s
order by coalesce({_FAILING}, false) desc, (e.status = 'running') is true desc,
         (r.run_id is not null and e.status is null and ({LAST_SUCCESS} is null or {LAST_SUCCESS} < r.started_at)) desc,
         t.src_schema, t.src_table
limit %s offset %s"""


def database_tables_count_sql() -> str:
    """How many tables database_tables_sql pages through. Params: group, server, database, schema.table pattern."""
    return (f"with t as ({_database_rows()}) select count(*) as tables from t "
            f"where (t.src_schema || '.' || t.src_table) ilike %s")


def rows_written_since_sql() -> str:
    """The rows these groups' table loads wrote in the last N hours, and how many loads recorded no count outside a
    batched append (batched appends record theirs on the run: run_batch_rows_sql). Params: groups, hours."""
    return (f"select coalesce(sum(e.rows_merged), 0) as rows_written, "
            f"{UNCOUNTED_OUTSIDE_BATCHES} as uncounted "
            f"from {SCHEMA}.ingest_event e where e.task_collection = any(%s) "
            f"and e.finished_at > now() - make_interval(hours => %s)")


def run_batch_rows_sql() -> str:
    """Runs with a detail that started in the last N hours, for their batched appends' row totals.
    Param: hours."""
    return (f"select run_id, task_collection, detail from {SCHEMA}.ingest_run "
            f"where detail is not null and started_at > now() - make_interval(hours => %s)")


def server_database_key(server, database) -> str:
    """A source database (server and name) as one text, as SERVER_DATABASE_KEY spells it in SQL."""
    return f"{server or ''}\x1f{database or ''}"


# server_database_key in SQL. Alias t = control, e = ingest_event.
SERVER_DATABASE_KEY = f"{SERVER} || chr(31) || coalesce(e.src_database, '')"


def collection_batch_runs_sql() -> str:
    """A collection's runs with a detail whose loads with no count were all their tables' latest first
    loads (seed, reseed or full), and the databases of those loads: such a run's batched append total is
    rows in first loads of those databases. A run that also batched changes is left out, because its one
    total can't be split between first loads and changes, and so is an older batch a later first load
    replaced. ``databases`` names them; ``keys`` are their server_database_key (server and name), for
    matching. Params: task_collection, task_collection."""
    return (f"select r.run_id, r.detail, array_remove(array_agg(distinct e.src_database), null) as databases, "
            f"array_agg(distinct {SERVER_DATABASE_KEY}) as keys "
            f"from {SCHEMA}.ingest_run r join {SCHEMA}.ingest_event e on e.run_id = r.run_id "
            f"and e.status = 'ok' and e.rows_merged is null "
            f"left join {SCHEMA}.control t on t.id = e.control_id "
            f"left join (select distinct on (control_id) control_id, id from {SCHEMA}.ingest_event "
            f"           where task_collection = %s and status = 'ok' and action in ('seed', 'reseed', 'full') "
            f"           order by control_id, finished_at desc nulls last, id desc) latest on latest.control_id = e.control_id "
            f"where r.task_collection = %s and r.detail is not null group by r.run_id, r.detail "
            f"having bool_and(e.action in ('seed', 'reseed', 'full') and e.id = latest.id)")


def batch_rows(detail) -> tuple:
    """(rows, rows of loads with no count) from a run's detail: batched appends record one total per
    batch commit (batch_rows) and how many members had none (batch_rows_unknown). (0, 0) without."""
    try:
        d = json.loads(detail) if isinstance(detail, str) else (detail or {})
    except ValueError:
        return 0, 0
    if not isinstance(d, dict):
        return 0, 0
    return int(d.get("batch_rows") or 0), int(d.get("batch_rows_unknown") or 0)


def _count(row: dict, key: str) -> int:
    return int(row.get(key) or 0)


def _detail(run: dict) -> dict:
    """A run's detail as a dict: it is text that may not be JSON, so parse it here rather than cast it in SQL."""
    detail = run.get("detail")
    try:
        d = json.loads(detail) if isinstance(detail, str) else (detail or {})
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def open_runs_sql() -> str:
    """Every run still recorded as running, group runs and checks, with the Databricks run that ran it: what the app
    reconciles with Databricks (main._close_ended_runs)."""
    return f"select run_id, task_collection, job_run_id, started_at from {SCHEMA}.ingest_run where status = 'running'"


def close_ended_run_sql() -> str:
    """Close one run still 'running' whose Databricks run has ended, and its events still running (or waiting for a
    retry that never came), as failed: what the engine's close_abandoned_runs does when the group's next run starts,
    done by the app when it finds the Databricks run over, so a run cancelled in Databricks doesn't stay running.
    Params: detail, run_id, error (appended to a retried attempt's), error. Returns (runs closed, events closed)."""
    return (
        f"with dead as (update {SCHEMA}.ingest_run set status = 'failed', finished_at = now(), detail = %s "
        "where run_id = %s and status = 'running' returning run_id), "
        f"events as (update {SCHEMA}.ingest_event e set status = 'failed', finished_at = coalesce(e.finished_at, now()), "
        "duration_ms = coalesce(e.duration_ms, (extract(epoch from (now() - e.started_at)) * 1000)::bigint), "
        "error = case when e.status = 'retried' then concat(e.error, ' | ', %s::text) else %s end "
        "from dead where e.run_id = dead.run_id and (e.status = 'running' or ("
        f"e.status = 'retried' and not exists (select 1 from {SCHEMA}.ingest_event later "
        "where later.run_id = e.run_id and later.control_id = e.control_id and later.id > e.id))) "
        "returning e.id) "
        "select (select count(*) from dead), (select count(*) from events)"
    )


def close_cancelled_run_sql() -> str:
    """Close one run still 'running' whose Databricks run was cancelled, as cancelled, with Databricks' end time (or now
    when it gave none). Its events still running, or waiting for a retry that never came, close as cancelled too, so
    the run has no failed or running event left; a late failure the cluster writes after the cancel is not counted.
    Params: finished_at (a timestamp or null), detail, run_id, reason (the error of an event that has none).
    Returns (runs closed, events closed)."""
    return (
        f"with dead as (update {SCHEMA}.ingest_run set status = 'cancelled', "
        "finished_at = coalesce(%s::timestamptz, now()), detail = %s "
        "where run_id = %s and status = 'running' returning run_id, finished_at), "
        f"events as (update {SCHEMA}.ingest_event e set status = 'cancelled', "
        "finished_at = coalesce(e.finished_at, dead.finished_at), "
        "duration_ms = coalesce(e.duration_ms, "
        "greatest(0, (extract(epoch from (dead.finished_at - e.started_at)) * 1000)::bigint)), "
        "error = coalesce(e.error, %s::text) "
        "from dead where e.run_id = dead.run_id and (e.status = 'running' or ("
        f"e.status = 'retried' and not exists (select 1 from {SCHEMA}.ingest_event later "
        "where later.run_id = e.run_id and later.control_id = e.control_id and later.id > e.id))) "
        "returning e.id) "
        "select (select count(*) from dead), (select count(*) from events)"
    )


def run_outcome(run: dict, progress: Optional[dict] = None) -> dict:
    """What a group run's engine record means, as the pages show it: a label, a tone and an optional note.
    The engine records a run that loaded with some table failures as failed, and a run another run closed
    (it stopped without finishing) as failed with closed_by_run in its detail. A run cancelled, in Databricks or from
    the app, is cancelled whatever its events say. A running run is detecting changes until its first table is checked
    or loaded, then loading changed tables."""
    status, failed = run.get("status"), _count(run, "failed")
    if run.get("cancelling") and status in ("running", "cancelled"):
        return {"label": "Cancelling", "tone": "run", "note": None}
    if status == "completed" and not failed:
        return {"label": "Succeeded", "tone": "ok", "note": None}
    if status == "skipped":
        return {"label": "Skipped: already running", "tone": "neutral", "note": None}
    if status == "running":
        if progress is None:
            return {"label": "Running", "tone": "run", "note": None}
        started = any(_count(progress, k) for k in ("tables_ok", "tables_failed", "tables_running", "tables_checked"))
        return {"label": "Loading changed tables" if started else "Detecting changes", "tone": "run", "note": None}
    if status == "cancelled":
        return {"label": "Cancelled", "tone": "neutral", "note": _detail(run).get("reason")}
    closed_by = _detail(run).get("closed_by_run")
    if closed_by is not None:
        return {"label": "Ended without finishing", "tone": "bad", "note": f"closed by run {closed_by}"}
    if _detail(run).get("closed_by_app"):
        return {"label": "Ended without finishing", "tone": "bad", "note": _detail(run).get("reason")}
    if status in ("failed", "completed") and failed:
        return {"label": "Succeeded with failures", "tone": "warn", "note": f"{failed:,} {'table' if failed == 1 else 'tables'} failed"}
    return {"label": "Failed", "tone": "bad", "note": None}


def with_outcome(run: dict, progress: Optional[dict] = None) -> dict:
    """The run with its outcome (run_outcome); a run another run closed has no duration or speed, since
    its finish (and its last events') is the closing time, not when its work stopped."""
    run["outcome"] = run_outcome(run, progress)
    run["ended_without_finishing"] = run["outcome"]["label"] == "Ended without finishing"
    if run["ended_without_finishing"]:
        run["duration_s"] = None
    return run


def _when(value):
    """A timestamp as a datetime: lakebase.query sends them as ISO text."""
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def loaded_tables(run: dict) -> int:
    """Tables a group run loaded: a finished run's ok less its unchanged tables and reseeds queued (both
    count as ok), else its ok events."""
    if run.get("ok") is not None:
        return max(_count(run, "ok") - _count(run, "skipped") - _count(run, "reseeded"), 0)
    return _count(run, "loaded")


def work_speed(run: dict) -> Optional[float]:
    """Tables loaded a minute over the run's work span (first table start to last finish), which leaves
    out planning and change detection. A finished run's ok counts its unchanged tables (cursor tables whose
    watermark didn't move record ok events too) and its reseeds queued, so loaded is ok less both; a run
    with no totals yet counts its ok events. None before any table loaded."""
    start, end, loaded = _when(run.get("work_started_at")), _when(run.get("work_finished_at")), loaded_tables(run)
    if not (start and end and loaded):
        return None
    minutes = max((end - start).total_seconds(), 1) / 60
    return round(loaded / minutes, 1)


# A finished job run's outcome is the worst of its group runs', in this order.
JOB_RUN_ORDER = ("Ended without finishing", "Failed", "Succeeded with failures", "Cancelled", "Succeeded")
RUNNING_OUTCOMES = ("Running", "Cancelling")


def job_run_outcome(members: List[dict]) -> dict:
    """A job run's outcome from its members (schedules.job_runs): running while any group run (extra runs
    included) is running or any member is waiting for or starting its trigger, or being checked; otherwise the worst
    of its group runs' outcomes, extra runs included, with a member its check found unchanged as completed and one
    whose check failed as failed, or "Not run" when none ran. The note counts the members that did not run, as
    "17 of 18 group runs, 1 skipped". A group run that a Cancel asked Databricks to stop, and that is not closed
    yet, makes the job run cancelling."""
    runs = [m["run"] for m in members if m.get("run")]
    ran = [r for r in runs if r.get("status") != "skipped"]
    extras = [r for m in members for r in m.get("extra") or [] if r.get("status") != "skipped"]
    skipped = sum(1 for m in members if m["state"] == "skipped") + (len(runs) - len(ran))
    no_run = sum(1 for m in members if m["state"] == "no run")
    starting = sum(1 for m in members if m["state"] == "starting")
    unchanged = sum(1 for m in members if m["state"] == "unchanged")
    check_failed = sum(1 for m in members if m["state"] == "check failed")
    note = None
    if len(ran) < len(members):
        note = ", ".join([f"{len(ran)} of {len(members)} group runs"] + ([f"{skipped} skipped"] if skipped else [])
                         + ([f"{starting} starting"] if starting else [])
                         + ([f"{unchanged} unchanged"] if unchanged else [])
                         + ([f"{check_failed} check failed"] if check_failed else [])
                         + ([f"{no_run} not run"] if no_run else []))
    if any(r.get("cancelling") and r.get("status") == "running" for r in runs + extras):
        return {"label": "Cancelling", "tone": "run", "note": note}
    if (any(m["state"] in ("waiting", "starting", "checking") for m in members)
            or any(r.get("status") == "running" for r in runs + extras)):
        return {"label": "Running", "tone": "run", "note": note}
    if not ran and not extras and not unchanged and not check_failed:
        return {"label": "Not run", "tone": "neutral", "note": note}
    # A member its check found unchanged completed with nothing to load; one whose check failed failed.
    labels = [run_outcome(r)["label"] for r in ran + extras] + ["Succeeded"] * unchanged + ["Failed"] * check_failed
    worst = min(labels, key=lambda label: JOB_RUN_ORDER.index(label) if label in JOB_RUN_ORDER else len(JOB_RUN_ORDER))
    tone = {"Succeeded": "ok", "Succeeded with failures": "warn", "Cancelled": "neutral"}.get(worst, "bad")
    return {"label": worst, "tone": tone, "note": note}


def job_run_expected_end(members: List[dict], progress_by_run: dict, p50_by_group: dict, now):
    """When a running job run should end: the latest of each running group run's time left (time_left), each
    waiting member's start plus its group's typical run, and for a member starting, its typical run from now.
    None when there is nothing to go on."""
    ends = []
    for m in members:
        for extra in m.get("extra") or []:
            if extra.get("status") == "running":
                left = time_left(progress_by_run.get(extra.get("run_id")) or {}, now)
                if left and not (left["basis"] == "typical" and left["seconds"] == 0):
                    ends.append(now + timedelta(seconds=left["seconds"]))
        run = m.get("run")
        if run and run.get("status") == "running":
            left = time_left(progress_by_run.get(run.get("run_id")) or {}, now)
            if left and not (left["basis"] == "typical" and left["seconds"] == 0):     # past its typical run: no estimate
                ends.append(now + timedelta(seconds=left["seconds"]))
        elif m["state"] == "waiting" and p50_by_group.get(m["task_collection"]) is not None:
            ends.append(_when(m["fire_at"]) + timedelta(seconds=int(p50_by_group[m["task_collection"]])))
        elif m["state"] == "starting" and p50_by_group.get(m["task_collection"]) is not None:
            ends.append(now + timedelta(seconds=int(p50_by_group[m["task_collection"]])))
    return max(ends) if ends else None


# Live speed and a rate-based time left wait for this much loading, so the first few loads don't set the pace.
LIVE_SPEED_AFTER_S = 180
LIVE_WINDOW_S = 300


def _live_window_minutes(progress: dict, now) -> Optional[float]:
    start = _when(progress.get("work_started_at"))
    if not start:
        return None
    loading_s = (now - start).total_seconds()
    if loading_s < LIVE_SPEED_AFTER_S:
        return None
    return min(loading_s, LIVE_WINDOW_S) / 60


def live_speed(progress: dict, now) -> Optional[dict]:
    """A running group run's tables and rows a minute over its last 5 minutes of loading (fewer, when it
    has loaded for less). None in its first 3 minutes of loading."""
    minutes = _live_window_minutes(progress, now)
    if minutes is None:
        return None
    return {"tables_per_min": round(_count(progress, "ok_last_5m") / minutes, 1),
            "rows_per_min": round(_count(progress, "rows_last_5m") / minutes, 1)}


def time_left(progress: dict, now) -> Optional[dict]:
    """About how long a running group run has left. While loading (3 minutes on): the tables to go at the
    last 5 minutes' rate. Before that, or with no recent loads: its group's typical (p50) run time less the
    time so far. None with neither."""
    minutes = _live_window_minutes(progress, now)
    rate = _count(progress, "ok_last_5m") / minutes if minutes else 0
    if rate:
        to_go = max(_count(progress, "enabled_tables") - _count(progress, "tables_checked")
                    - _count(progress, "tables_running") - _count(progress, "tables_failed")
                    - _count(progress, "tables_skipped"), 0)
        return {"seconds": round(to_go / rate * 60), "basis": "rate"}
    if progress.get("p50_s") is None:
        return None
    return {"seconds": max(int(progress["p50_s"]) - _count(progress, "elapsed_s"), 0), "basis": "typical"}


# Failure causes, by the error text, most specific first. Connection and timeout failures usually clear
# on the next run; the others need a change at the source or in config.
ERROR_CLASSES = (
    ("connection", "Could not connect to the source database",
     ("failed_jdbc.connection", "connection reset", "connection refused", "could not connect", "network",
      "login timeout", "socket", "tcp/ip connection to the host")),
    ("timeout", "Timed out", ("timeout", "timed out")),
    ("permission", "Permission denied at the source or sink",
     ("permission", "denied", "not authorized", "login failed")),
    ("change_tracking", "Change Tracking is off at the source", ("change tracking is not enabled",)),
    ("missing", "A table, column or object is missing", ("invalid object name", "does not exist", "not found",
                                                          "invalid column")),
    ("schema", "The schema does not match", ("schema", "mismatch", "cannot cast", "data type")),
)


# Each cause phrase matches only as a whole word or phrase, so a name that contains one (a column called
# network_id or timeout_ms) is not read as that cause.
_ERROR_PATTERNS = tuple((name, label, re.compile("|".join(rf"(?<![a-z0-9_]){re.escape(n)}(?![a-z0-9_])" for n in needles)))
                        for name, label, needles in ERROR_CLASSES)


# Quoted names in an error: anything in single quotes, one word in double quotes, or a bracketed name
# that is not an upper-case error tag.
_QUOTED_NAMES = re.compile(r"'[^'\n]{1,256}'|\"[\w.$#@-]{1,128}\"|\[(?![A-Z0-9_.]+\])[^\]\n]{1,256}\]")


def classify_error(error: Optional[str]) -> Tuple[str, str]:
    """(class, label) of an error message; ('other', 'Other errors') when nothing matches. Names the
    message quotes (a column or table such as 'network', "network" or [network]) are left out, so a name
    is never read as a cause; a double-quoted phrase (a driver's quoted cause) and an upper-case error
    tag such as [FAILED_JDBC.CONNECTION] are kept."""
    text = _QUOTED_NAMES.sub("''", error or "").lower()
    for name, label, pattern in _ERROR_PATTERNS:
        if pattern.search(text):
            return name, label
    return "other", "Other errors"


def error_signature(error: Optional[str]) -> str:
    """The error's first line with numbers, quoted names and hex ids blanked, so the same cause on
    different tables groups together."""
    first = (error or "").strip().splitlines()[0] if (error or "").strip() else ""
    first = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", first)
    first = re.sub(r"0x[0-9a-fA-F]+|\b\d+\b", "#", first)
    return first[:240]


def failed_tables_sql() -> str:
    """The tables that failed in the last N hours, newest failure first, at most `limit` tables: one row
    per table and error message, with how many times the table failed with it, its first and last time,
    and the table's server (SERVER). A table that failed for two causes has a row for each.
    Params: hours, limit (tables)."""
    return f"""
with recent as (
    select control_id, error, task_collection, src_database, src_schema, src_table, started_at, finished_at
    from {SCHEMA}.ingest_event
    where status = 'failed' and started_at > now() - make_interval(hours => %s)
),
tables as (
    select control_id, max(started_at) as table_last_started_at from recent
    group by control_id order by table_last_started_at desc limit %s
),
causes as (
    select r.control_id, r.error, count(*) as failures, min(r.finished_at) as first_at, max(r.finished_at) as last_at,
           (array_agg(r.task_collection order by r.started_at desc))[1] as task_collection,
           (array_agg(r.src_database order by r.started_at desc))[1] as src_database,
           (array_agg(r.src_schema order by r.started_at desc))[1] as src_schema,
           (array_agg(r.src_table order by r.started_at desc))[1] as src_table,
           max(tb.table_last_started_at) as table_last_started_at
    from recent r join tables tb on tb.control_id = r.control_id
    group by r.control_id, r.error
)
select c.control_id, c.error, c.failures, c.first_at, c.last_at, c.last_at as finished_at, c.task_collection,
       c.src_database, c.src_schema, c.src_table, {SERVER} as server
from causes c
left join {SCHEMA}.control t on t.id = c.control_id
order by c.table_last_started_at desc, c.control_id, c.last_at desc"""


def group_errors(rows: List[dict]) -> List[dict]:
    """Failed tables grouped by cause (class and signature), most tables first: tables (each counted
    once per cause, however often it failed), failures, databases (server and name, with collection),
    first and last time, and one raw example message. Takes failed_tables_sql's rows (one per table, with its failures and times) or
    plain failed events (one per failure), and counts a table once either way."""
    groups: dict = {}
    for r in rows:
        name, label = classify_error(r.get("error"))
        g = groups.setdefault((name, error_signature(r.get("error"))), {
            "error_class": name, "label": label, "signature": error_signature(r.get("error")),
            "example": (r.get("error") or "")[:2000], "tables": 0, "failures": 0, "databases": {}, "seen": set(),
            "first_at": None, "last_at": None})
        g["failures"] += int(r.get("failures") or 1)
        table = r.get("control_id") or (r.get("task_collection"), r.get("src_database"), r.get("src_schema"),
                                        r.get("src_table"))
        if table not in g["seen"]:
            g["seen"].add(table)
            g["tables"] += 1
            db = g["databases"].setdefault((r.get("task_collection"), r.get("server"), r.get("src_database")), {
                "task_collection": r.get("task_collection"), "server": r.get("server"),
                "src_database": r.get("src_database"), "tables": []})
            db["tables"].append(f"{r.get('src_schema')}.{r.get('src_table')}")
        for at in (r.get("first_at") or r.get("finished_at"), r.get("last_at") or r.get("finished_at")):
            if at:
                g["first_at"] = min(g["first_at"] or at, at)
                g["last_at"] = max(g["last_at"] or at, at)
    out = []
    for g in sorted(groups.values(), key=lambda g: -g["tables"]):
        del g["seen"]
        g["databases"] = sorted(g["databases"].values(),
                                key=lambda d: (str(d["task_collection"]), str(d["src_database"]), str(d["server"])))
        out.append(g)
    return out


# How far back a failing table's failures are read: how long it has been failing (since its first failure after
# its last success) and how many runs failed it. A table failing for longer shows the start of this window.
FAILING_WINDOW_DAYS = 30


def failing_tables_sql() -> str:
    """The failing tables of a job's groups: enabled tables whose checkpoint is failed, each with its latest failure's
    error, class and kind (blank for failures recorded before schema 14), when it started failing, and how many runs
    and failures it has had since. Param: groups."""
    window = f"now() - make_interval(days => {FAILING_WINDOW_DAYS})"
    return f"""
with failing as (
    select t.id as control_id, t.task_collection, {SERVER} as server, coalesce(t.src_database, '') as src_database,
           t.src_schema, t.src_table, cp.detail as checkpoint_detail
    from {SCHEMA}.control t join {SCHEMA}.checkpoint cp on cp.control_id = t.id
    where t.task_collection = any(%s) and t.task_enabled and {_FAILING}
),
last_ok as (
    select o.control_id, max(o.started_at) as at from {SCHEMA}.ingest_event o
    where o.status = 'ok' and o.started_at > {window} and o.control_id in (select control_id from failing)
    group by o.control_id
),
failures as (
    select e.id, e.control_id, e.run_id, e.started_at, e.error, e.error_kind, e.error_class
    from {SCHEMA}.ingest_event e left join last_ok lo on lo.control_id = e.control_id
    where e.status = 'failed' and e.control_id in (select control_id from failing)
      and e.started_at > coalesce(lo.at, {window})
),
since as (
    select control_id, min(started_at) as failing_since, count(distinct run_id) as runs, count(*) as failures
    from failures group by control_id
),
latest as (
    select distinct on (control_id) control_id, error, error_kind, error_class
    from failures order by control_id, started_at desc, id desc
)
select f.control_id, f.task_collection, f.server, f.src_database, f.src_schema, f.src_table, f.checkpoint_detail,
       s.failing_since, s.runs, s.failures, l.error, l.error_kind, l.error_class
from failing f
left join since s on s.control_id = f.control_id
left join latest l on l.control_id = f.control_id
order by s.failing_since nulls last, f.task_collection, f.server, f.src_database, f.src_schema, f.src_table"""


def failing_table_rows(rows: List[dict]) -> List[dict]:
    """failing_tables_sql's rows as the job page shows them. The class is the engine's error class (a Spark error
    class, or the exception's type) and the app's word for the cause when that is not recorded; the kind is transient,
    permanent or unknown. The message is the latest failure's, else the checkpoint's note."""
    out = []
    for r in rows:
        message = r.get("error") or r.get("checkpoint_detail") or ""
        since = r.get("failing_since")
        out.append({
            "control_id": r["control_id"], "task_collection": r["task_collection"], "server": r["server"],
            "src_database": r["src_database"], "src_schema": r["src_schema"], "src_table": r["src_table"],
            "table": f"{r['src_schema']}.{r['src_table']}",
            "error_class": r.get("error_class") or classify_error(message)[1],
            "error_kind": r.get("error_kind") or None,
            "message": message[:2000],
            "failing_since": since.isoformat() if since else None,
            "runs": int(r.get("runs") or 0),
            "failures": int(r.get("failures") or 0),
        })
    return out


def retry_database_sql() -> str:
    """Record one database's work list for a run started by hand: its enabled rows in one group on one server, under
    work_id, as the check job records a work list (the source is the same key the check uses). Params: work_id,
    group, server, database. Returns the database it recorded, none when it has no enabled rows."""
    return (f"insert into {SCHEMA}.check_work (work_id, task_collection, source, src_database) "
            f"select distinct %s::text, t.task_collection, {database_source_sql('t')}, coalesce(t.src_database, '') "
            f"from {SCHEMA}.control t where t.task_enabled and t.task_collection = %s and {SERVER} = %s "
            f"and coalesce(t.src_database, '') = %s returning src_database")


def job_own_groups_sql() -> str:
    """The groups of a live job that no other live job holds: the only groups a table can be removed from without
    changing another job's tables (the delete job's job_groups_sql). Params: schedule id, schedule id."""
    return (f"select distinct m.task_collection from {SCHEMA}.schedule_member m where m.schedule_id = %s "
            f"and m.removed_at is null and not exists (select 1 from {SCHEMA}.schedule_member o "
            f"join {SCHEMA}.schedule s on s.id = o.schedule_id where o.task_collection = m.task_collection "
            f"and o.removed_at is null and s.deleted_at is null and o.schedule_id <> %s) order by 1")


def table_rows_by_ids_sql() -> str:
    """The control rows named by id, with their server (connection or host), group, database and table names.
    Param: control ids (bigint[])."""
    return (f"select t.id, t.task_collection, coalesce(t.src_connection, t.src_host, '') as server, "
            f"coalesce(t.src_database, '') as src_database, t.src_schema, t.src_table "
            f"from {SCHEMA}.control t where t.id = any(%s) order by t.id")


def set_table_remove_sql() -> str:
    """Merge removal records into a job's table_remove option, keyed by control id (table_remove.requests). Params:
    the records as a JSON object, job id. Written by the app: the app never deletes."""
    return (f"update {SCHEMA}.schedule set options = jsonb_set(coalesce(options, '{{}}'::jsonb), '{{table_remove}}', "
            f"coalesce(options -> 'table_remove', '{{}}'::jsonb) || %s::jsonb) where id = %s")


def add_exclusions_sql() -> str:
    """Merge the tables a removal leaves out into a job's table_exclude option, keyed by table (table_removal.
    exclusion_records): they stay out of a re-discovery and of whole databases until Add tables names them. Params:
    the records as a JSON object, job id."""
    return (f"update {SCHEMA}.schedule set options = jsonb_set(coalesce(options, '{{}}'::jsonb), '{{table_exclude}}', "
            f"coalesce(options -> 'table_exclude', '{{}}'::jsonb) || %s::jsonb) where id = %s")


def clear_exclusions_sql() -> str:
    """Take tables back in from a job's table_exclude option: the ones Add tables named explicitly. Params: their
    exclusion keys (text[]), job id."""
    return (f"update {SCHEMA}.schedule set options = jsonb_set(coalesce(options, '{{}}'::jsonb), '{{table_exclude}}', "
            f"coalesce(options -> 'table_exclude', '{{}}'::jsonb) - %s::text[]) where id = %s")


# ---- UC connections (bulk import) ---------------------------------------------------------------

def connection_databases_sql() -> str:
    """The databases lakefed tracks on one source: by UC connection name or host, each with its
    collections, enabled tables and newest success. Params: connection name, host."""
    return (f"select t.src_database, string_agg(distinct t.task_collection, ', ') as collections, "
            f"count(*) filter (where t.task_enabled) as tables, max({LAST_SUCCESS}) as newest_success_at "
            f"from {SCHEMA}.control t left join {SCHEMA}.checkpoint cp on cp.control_id = t.id "
            f"{DATABASE_CHECK_JOIN} "
            f"where t.src_connection = %s or lower(t.src_host) = lower(%s) "
            f"group by 1 order by 1")


# ---- tables (control + checkpoint) --------------------------------------------------------------

TABLE_FILTERS = ("all", "failing", "sla_breached", "no_sla", "seeding", "disabled")


# The order Tables pages in, and the cursor it pages after: one value per column, the row's id last.
TABLES_ORDER = "t.task_collection, coalesce(t.src_database, ''), t.src_schema, t.src_table, t.id"


def tables_sql(collection: Optional[str], search: Optional[str], status_filter: str,
               limit: int, database: Optional[str] = None, after: Optional[list] = None,
               job: Optional[int] = None) -> Tuple[str, tuple]:
    """Control rows joined with their checkpoint, a page of `limit` in TABLES_ORDER after the cursor `after`
    (the last row's group, database, schema, table and id), each with the ingestion job its group belongs to. `database`
    filters on one database exactly, `job` on one ingestion job's groups (through schedule_member's task_collection
    index; no new index). Groups are named after their job, so TABLES_ORDER also keeps a job's tables together.
    Returns (sql, params)."""
    if status_filter not in TABLE_FILTERS:
        raise ValueError(f"status_filter must be one of {TABLE_FILTERS}")
    conds, params = [], []
    if collection:
        conds.append("t.task_collection = %s")
        params.append(collection)
    if job is not None:
        conds.append(f"t.task_collection in (select m.task_collection from {SCHEMA}.schedule_member m "
                     f"where m.schedule_id = %s and m.removed_at is null)")
        params.append(int(job))
    if database is not None:
        conds.append("coalesce(t.src_database, '') = %s")
        params.append(database)
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
    if after:
        conds.append(f"({TABLES_ORDER}) > (%s, %s, %s, %s, %s)")
        params.extend(after)
    where = ("where " + " and ".join(conds) + " ") if conds else ""
    sql = (f"select t.id, (select s.name from {SCHEMA}.schedule_member m join {SCHEMA}.schedule s on s.id = m.schedule_id "
           f"where m.task_collection = t.task_collection and m.removed_at is null and s.deleted_at is null "
           f"order by s.id limit 1) as ingestion_job, t.task_collection, t.task_enabled, t.src_type, t.load_type, "
           f"t.src_database, t.src_schema, t.src_table, "
           f"t.sink_catalog || '.' || t.sink_schema || '.' || t.sink_table as sink, t.client_id, "
           f"cp.phase, cp.status, {LAST_SUCCESS} as last_success_at, "
           f"round(extract(epoch from (now() - {LAST_SUCCESS})) / 60)::int as lag_min, "
           f"t.freshness_sla_minutes as sla_min, {SLA_BREACHED} as sla_breached, "
           f"cp.position, cp.detail "
           f"from {SCHEMA}.control t left join {SCHEMA}.checkpoint cp on cp.control_id = t.id "
           f"{DATABASE_CHECK_JOIN} "
           f"{where}order by {TABLES_ORDER} limit %s")
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
CLIENT_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"                  # the shared key, as ct_engine accepts it


def _truthy(v) -> bool:
    return v if isinstance(v, bool) else str(v or "").strip().lower() in ("true", "1", "yes", "y")


TRUE_WORDS, FALSE_WORDS = ("true", "1", "yes", "y", "on"), ("false", "0", "no", "n", "off")


def parse_bool(v) -> bool:
    """A yes or no setting as given: a boolean, or one of the words for one ("false" is false). Anything else raises
    ValueError, so a mistyped value is refused rather than read as true."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    word = str(v).strip().lower() if v is not None else ""
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise ValueError(f"{v!r} is not true or false")


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
    if cid not in (None, "") and not re.match(CLIENT_ID_PATTERN, str(cid)):
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


def discovery_totals_sql() -> str:
    """How many databases a discovery has, how many of them match a filter (in their name), how many the login was
    found able and unable to open and the tables of those it can, and the same for the databases the filter keeps
    (matched_closed, matched_unknown: not checked yet, matched_tables). Params: filter, discovery_id."""
    kept = "position(lower(f.filter) in lower(src_database)) > 0"
    return (f"select count(*) as total, count(*) filter (where {kept}) as matched, "
            f"count(*) filter (where (listing ->> 'open')::boolean) as open, "
            f"count(*) filter (where not (listing ->> 'open')::boolean) as closed, "
            f"coalesce(sum(tables) filter (where (listing ->> 'open')::boolean), 0) as tables, "
            f"count(*) filter (where {kept} and not (listing ->> 'open')::boolean) as matched_closed, "
            f"count(*) filter (where {kept} and status = 'listed' and (listing -> 'open') is null) as matched_unknown, "
            f"coalesce(sum(tables) filter (where {kept} and (listing ->> 'open')::boolean), 0) as matched_tables "
            f"from {SCHEMA}.discovery, (select %s::text as filter) f where discovery_id = %s")


def discovery_listing_sql() -> str:
    """One page of a discovery's databases matching a filter (in their name), by name, with each one's status and
    error: the page the Discover step lists, so it never holds all of them. Params: discovery_id, filter, limit, offset."""
    return (f"select src_database, status, error, tables, (listing ->> 'open')::boolean as open, listing ->> 'why' as why "
            f"from {SCHEMA}.discovery "
            f"where discovery_id = %s and position(lower(%s) in lower(src_database)) > 0 "
            f"order by src_database limit %s offset %s")


# A discovery row the app writes itself, for a listing or a read it ran through the SQL warehouse (the discovery job
# writes its own): one per (discovery, database).
DISCOVERY_ROW_COLUMNS = ("discovery_id", "src_type", "src_connection", "src_database", "status", "error", "tables",
                         "requested_by", "listing")


def insert_discovery_rows_sql(count: int) -> str:
    """Insert ``count`` discovery rows (DISCOVERY_ROW_COLUMNS, listing as JSON text), finished now. A row already there
    is kept, so asking again after a statement ended writes nothing twice."""
    one_row = "(" + ", ".join(["%s"] * (len(DISCOVERY_ROW_COLUMNS) - 1)) + ", %s::jsonb, now())"
    return (f"insert into {SCHEMA}.discovery ({', '.join(DISCOVERY_ROW_COLUMNS)}, finished_at) "
            f"values {', '.join([one_row] * count)} on conflict (discovery_id, src_database) do nothing")


# A discovery's checks and reads through the SQL warehouse (warehouse_reads.py) keep their place on the discovery row:
# a check works on the listed databases not checked yet, a read on the databases still running. listing holds the
# statement going (statement_id), when a request claimed the database (claimed_at), and a check's answer (open, why).
WAREHOUSE_WORK = {"check": "status = 'listed' and (listing -> 'open') is null",
                  "read": "status = 'running' and not (coalesce(listing, '{}'::jsonb) ? 'discarded')"}


def warehouse_in_flight_sql(kind: str) -> str:
    """The databases of a discovery with a statement going for them. Param: discovery_id."""
    return (f"select src_database, listing ->> 'statement_id' as statement_id from {SCHEMA}.discovery "
            f"where discovery_id = %s and {WAREHOUSE_WORK[kind]} and listing ? 'statement_id' order by src_database")


def warehouse_claim_sql(kind: str) -> str:
    """Claim up to a number of a discovery's databases with no statement yet, skipping those another request holds or
    claimed less than a while ago. A check claims only the names asked for (the page shown): the login's access is
    checked for what the page lists, never the whole server. A read claims the names asked for first, then by name.
    Params: discovery_id, the age in seconds after which a claim with no statement is free again, the names (text[]),
    limit."""
    names = ("and src_database = any(%s::text[]) order by src_database" if kind == "check"
             else "order by (src_database = any(%s::text[])) desc, src_database")
    return (f"update {SCHEMA}.discovery d set listing = coalesce(d.listing, '{{}}'::jsonb) "
            f"|| jsonb_build_object('claimed_at', now()::text) "
            f"from (select discovery_id, src_database from {SCHEMA}.discovery "
            f"      where discovery_id = %s and {WAREHOUSE_WORK[kind]} and not (coalesce(listing, '{{}}'::jsonb) ? 'statement_id') "
            f"      and (listing ->> 'claimed_at' is null "
            f"           or (listing ->> 'claimed_at')::timestamptz < now() - make_interval(secs => %s)) "
            f"      {names} limit %s for update skip locked) c "
            f"where d.discovery_id = c.discovery_id and d.src_database = c.src_database returning d.src_database")


def warehouse_unchecked_sql() -> str:
    """How many of the names asked for are listed and not checked yet: the page's checks still to come. Params:
    discovery_id, the names (text[])."""
    return (f"select count(*) as unchecked from {SCHEMA}.discovery where discovery_id = %s and {WAREHOUSE_WORK['check']} "
            f"and src_database = any(%s::text[])")


def warehouse_started_sql() -> str:
    """Record the statement started for a database. Params: statement_id, discovery_id, src_database."""
    return (f"update {SCHEMA}.discovery set listing = coalesce(listing, '{{}}'::jsonb) || jsonb_build_object('statement_id', %s::text) "
            f"where discovery_id = %s and src_database = %s")


def warehouse_clear_sql() -> str:
    """Forget a database's statement and claim, so it is started again. Params: discovery_id, src_database."""
    return (f"update {SCHEMA}.discovery set listing = coalesce(listing, '{{}}'::jsonb) - 'statement_id' - 'claimed_at' "
            f"where discovery_id = %s and src_database = %s")


def warehouse_check_done_sql() -> str:
    """A check's answer on a listed database: whether the login can open it, its table count, and why not. Params:
    open (boolean), tables, why, discovery_id, src_database."""
    return (f"update {SCHEMA}.discovery set listing = (coalesce(listing, '{{}}'::jsonb) - 'statement_id' - 'claimed_at') "
            f"|| jsonb_build_object('open', %s::boolean, 'why', %s::text), tables = %s, finished_at = now() "
            f"where discovery_id = %s and src_database = %s and status = 'listed'")


def warehouse_read_done_sql() -> str:
    """A read's end on a database: ok with its table count, or failed with why. Params: status, tables, error,
    discovery_id, src_database. Only a database still running is changed, so a second answer writes nothing."""
    return (f"update {SCHEMA}.discovery set status = %s, tables = %s, error = %s, finished_at = now(), "
            f"listing = coalesce(listing, '{{}}'::jsonb) - 'statement_id' - 'claimed_at' "
            f"where discovery_id = %s and src_database = %s and status = 'running'")


def insert_discovered_tables_sql(columns, count: int) -> str:
    """Insert ``count`` discovered_table rows of the given columns."""
    one_row = "(" + ", ".join(["%s"] * len(columns)) + ")"
    return (f"insert into {SCHEMA}.discovered_table ({', '.join(columns)}) values {', '.join([one_row] * count)} "
            f"on conflict (discovery_id, src_database, src_schema, src_table) do nothing")


def warehouse_progress_sql(kind: str) -> str:
    """Where a discovery's checks or reads stand: its databases, those still to do (pending), and by outcome. A check
    counts the databases open, closed and their tables; a read those read (done), failed, and their tables. Param:
    discovery_id."""
    if kind == "check":
        return (f"select count(*) as databases, count(*) filter (where (listing -> 'open') is null) as pending, "
                f"count(*) filter (where (listing ->> 'open')::boolean) as open, "
                f"count(*) filter (where not (listing ->> 'open')::boolean) as closed, "
                f"coalesce(sum(tables) filter (where (listing ->> 'open')::boolean), 0) as tables "
                f"from {SCHEMA}.discovery where discovery_id = %s and status = 'listed'")
    return (f"select count(*) as databases, count(*) filter (where status = 'running') as pending, "
            f"count(*) filter (where status = 'ok') as done, count(*) filter (where status = 'failed') as failed, "
            f"coalesce(sum(tables) filter (where status = 'ok'), 0) as tables "
            f"from {SCHEMA}.discovery where discovery_id = %s")


def warehouse_read_rows_sql() -> str:
    """Start a read of a listed server's databases: one running row per database of the listing the filter keeps (in
    its name) that the login wasn't found unable to open, for a new discovery. Params: new discovery_id, requested_by,
    the draft's marks (JSON text, {} for none), the listing's discovery_id, filter."""
    return (f"insert into {SCHEMA}.discovery (discovery_id, src_type, src_connection, src_database, status, requested_by, "
            f"listing) select %s, src_type, src_connection, src_database, 'running', %s, "
            f"jsonb_build_object('via', 'warehouse', 'whole_server', true) || %s::jsonb from {SCHEMA}.discovery "
            f"where discovery_id = %s and status = 'listed' and coalesce((listing ->> 'open')::boolean, true) "
            f"and position(lower(%s) in lower(src_database)) > 0 on conflict (discovery_id, src_database) do nothing")


def warehouse_named_rows_sql(count: int) -> str:
    """Start a read of named databases: one running row each. Params per row: discovery_id, src_connection,
    src_database, requested_by, the draft's marks (JSON text, {} for none)."""
    one_row = "(%s, 'sqlserver', %s, %s, 'running', %s, jsonb_build_object('via', 'warehouse') || %s::jsonb)"
    return (f"insert into {SCHEMA}.discovery (discovery_id, src_type, src_connection, src_database, status, requested_by, "
            f"listing) values {', '.join([one_row] * count)} on conflict (discovery_id, src_database) do nothing")


# A draft: a read through the warehouse started as the databases are chosen, before the job exists. Its rows are the
# read's own discovery rows, marked in listing: draft, draft_by and touched_at (the last time a page or a Create asked
# about it, kept on its first row), then discarded when the page is cancelled or left. Create (or Add tables) takes
# the mark off in its own transaction and names the job; the delete job purges discarded drafts and those nobody has
# asked about for run_poller.DRAFT_TTL_S, since the app has no DELETE.


def drafts_to_purge_sql() -> str:
    """Whether any draft is due for the delete job's purge (delete_job.purgeable_drafts_sql's rule): discarded, or not
    asked about for the time limit. Param: seconds."""
    return (f"select discovery_id from {SCHEMA}.discovery where listing ? 'draft' group by discovery_id "
            f"having bool_or(listing ? 'discarded') or coalesce(max((listing ->> 'touched_at')::timestamptz), "
            f"max(started_at)) < now() - make_interval(secs => %s) limit 1")


def draft_of_sql() -> str:
    """A draft's connection, if it is one still open (marked, not discarded). Param: discovery_id."""
    return (f"select src_connection from {SCHEMA}.discovery where discovery_id = %s and listing ? 'draft' "
            f"and not listing ? 'discarded' limit 1")


def draft_touch_sql() -> str:
    """Record that a draft is still wanted, on its first row, at most once a minute. Params: discovery_id,
    discovery_id."""
    return (f"update {SCHEMA}.discovery set listing = listing || jsonb_build_object('touched_at', now()::text) "
            f"where discovery_id = %s and listing ? 'draft' and not listing ? 'discarded' "
            f"and src_database = (select min(src_database) from {SCHEMA}.discovery where discovery_id = %s) "
            f"and coalesce((listing ->> 'touched_at')::timestamptz, 'epoch') < now() - interval '60 seconds'")


def draft_discard_sql() -> str:
    """Discard a draft: every row marked so, nothing read for it any more; the statements still going, to cancel.
    Param: discovery_id."""
    return (f"update {SCHEMA}.discovery set listing = listing || jsonb_build_object('discarded', now()::text) "
            f"where discovery_id = %s and listing ? 'draft' and not listing ? 'discarded' "
            f"returning listing ->> 'statement_id' as statement_id")


def draft_claim_sql() -> str:
    """In a Create's or an Add's transaction: the drafts it was chosen from become the job's, so no purge takes them.
    Params: job id, discovery ids (text[])."""
    return (f"update {SCHEMA}.discovery set listing = (listing - 'draft' - 'draft_by' - 'touched_at') "
            f"|| jsonb_build_object('job_id', %s::bigint) "
            f"where discovery_id = any(%s::text[]) and listing ? 'draft' and not listing ? 'discarded'")


def draft_reads_sql() -> str:
    """Where the reads of a choice's discoveries stand: per discovery, its connection, its databases still to read,
    read, failed, discarded and their tables. Param: discovery ids (text[])."""
    return (f"select discovery_id, min(src_connection) as src_connection, count(*) as databases, "
            f"count(*) filter (where status = 'running' and not listing ? 'discarded') as pending, "
            f"count(*) filter (where status = 'ok') as done, count(*) filter (where status = 'failed') as failed, "
            f"count(*) filter (where listing ? 'discarded') as discarded, "
            f"coalesce(sum(tables) filter (where status = 'ok'), 0) as tables "
            f"from {SCHEMA}.discovery where discovery_id = any(%s::text[]) and status <> 'listed' group by discovery_id")


def draft_failed_sql() -> str:
    """The databases of a choice's discoveries that couldn't be read, with why and whether a whole-server read took
    them (which leaves out one the login can't open, as the check would have). Param: discovery ids (text[])."""
    return (f"select src_database, error, coalesce((listing ->> 'whole_server')::boolean, false) as whole_server "
            f"from {SCHEMA}.discovery where discovery_id = any(%s::text[]) and status = 'failed' order by src_database")


def warehouse_left_out_sql() -> str:
    """How many of a listing's databases the filter keeps that the login can't open: what a whole-server read leaves
    out. Params: discovery_id, filter."""
    return (f"select count(*) as left_out from {SCHEMA}.discovery where discovery_id = %s and status = 'listed' "
            f"and not coalesce((listing ->> 'open')::boolean, true) and position(lower(%s) in lower(src_database)) > 0")


def discovered_tables_sql() -> str:
    """Discovered tables of one discovery, with the source connection and any existing control row
    for the same source table (``registered_id``). Param: discovery_id."""
    return (f"select dt.*, d.src_type, d.src_connection, d.src_host, d.src_port, d.secret_key, "
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


# Characters Unity Catalog refuses in catalog, schema and table names: period, space, forward slash, ASCII
# control characters 00-1F and DELETE (7F). Backticks quote the rest.
# Source: https://docs.databricks.com/en/sql/language-manual/sql-ref-names.html
UNITY_CATALOG_REJECTED = re.compile(r"[. /\x00-\x1f\x7f]")


def unity_catalog_identifier(text) -> str:
    """A lower-case name Unity Catalog accepts: each refused character becomes an underscore, the rest is kept."""
    return UNITY_CATALOG_REJECTED.sub("_", str(text)).lower()


def render(template: Optional[str], database: str, schema: str, table: str, as_identifier: bool = True,
           unity_catalog: bool = False):
    """Fill {database} / {schema} / {table} in a template; blank template -> None. The values become names by the
    older identifier rule, or by Unity Catalog's when unity_catalog is set (a new job's names)."""
    if not str(template or "").strip():
        return None
    f = (unity_catalog_identifier if unity_catalog else identifier) if as_identifier else (lambda v: v)
    return (str(template).replace("{database}", f(database)).replace("{schema}", f(schema))
            .replace("{table}", f(table)).strip())


def recommended_partitioning(size_mb, partition_candidate, threshold_mb: float, partition_size_mb: int):
    """(load_partitioned, partition_col, partition_size_mb) for a discovered table."""
    if partition_candidate and float(size_mb or 0) >= float(threshold_mb):
        return True, partition_candidate, int(partition_size_mb)
    return False, None, None


def review_rows(discovered: List[dict], shared: dict, unity_catalog: bool = False) -> List[dict]:
    """The review grid: one editable row per discovered table, from the recommendations plus the
    shared fields (sink templates, client_id template, partition threshold). Registered tables and
    tables with no readable columns start excluded. unity_catalog: the naming rule of the group the rows go
    into (a job's own rule; see render)."""
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
            "sink_catalog": render(shared.get("sink_catalog"), db, sch, tbl, unity_catalog=unity_catalog),
            "sink_schema": render(shared.get("sink_schema"), db, sch, tbl, unity_catalog=unity_catalog),
            "sink_table": render(shared.get("sink_table"), db, sch, tbl, unity_catalog=unity_catalog),
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


# Control rows written by one statement: 25 columns each, so 1,000 rows is 25,000 parameters, well under Postgres'
# 65,535. A large job's tables go in a few dozen statements instead of one round trip per table.
CONTROL_BATCH = 1000


def insert_controls_sql(n: int) -> str:
    """insert_control_if_absent_sql for n rows in one statement: a row whose sink (or sink + client_id) is taken
    is skipped. It returns each written row's id with its sink key (control_sink_key), so the caller can tell
    which rows were skipped. Params: control_params of each row in turn, flattened."""
    row = "(" + ", ".join(["%s"] * len(CONTROL_INSERT_COLUMNS)) + ")"
    return (f"insert into {SCHEMA}.control ({', '.join(CONTROL_INSERT_COLUMNS)}) values {', '.join([row] * n)} "
            f"on conflict do nothing returning id, lower(sink_catalog), lower(sink_schema), lower(sink_table), "
            f"coalesce(client_id, '')")


def control_sink_key(params: tuple) -> tuple:
    """A row's sink key as the unique index control_sink_client_uq reads it, from its control_params."""
    at = {c: params[CONTROL_INSERT_COLUMNS.index(c)] for c in ("sink_catalog", "sink_schema", "sink_table", "client_id")}
    return (str(at["sink_catalog"] or "").lower(), str(at["sink_schema"] or "").lower(),
            str(at["sink_table"] or "").lower(), at["client_id"] or "")


def log_changes_sql(n: int) -> str:
    """log_change_sql for n change log lines in one statement. Params: each line's 9 values in turn, flattened."""
    one = "(" + ", ".join(["%s"] * 9) + ")"
    return log_change_sql().split(" values ")[0] + " values " + ", ".join([one] * n)


# ---- background writes (app_task) -----------------------------------------------------------------

APP_TASK_FIELDS = ("id", "kind", "request_id", "job_id", "status", "done", "total", "result", "error", "created_by",
                   "created_at", "heartbeat_at", "finished_at", "progress")


def insert_task_sql() -> str:
    """Record a background write, or find the one a request id already made (the same request sent again).
    Params: kind, request_id, job_id, total, result (JSON text), created_by. Returns the task's id and whether this
    call made it."""
    return (f"insert into {SCHEMA}.app_task (kind, request_id, job_id, total, result, created_by) "
            f"values (%s, %s, %s, %s, %s::jsonb, %s) on conflict (request_id) do update set request_id = excluded.request_id "
            f"returning id, (xmax = 0) as made")


def task_sql() -> str:
    """One task. Param: id."""
    return f"select {', '.join(APP_TASK_FIELDS)} from {SCHEMA}.app_task where id = %s"


def task_of_request_sql() -> str:
    """The task a request id made, if any. Param: request_id."""
    return f"select id from {SCHEMA}.app_task where request_id = %s"


def job_tasks_sql() -> str:
    """A job's tasks of the last day, newest first. Param: job_id."""
    return (f"select {', '.join(APP_TASK_FIELDS)} from {SCHEMA}.app_task where job_id = %s "
            f"and created_at > now() - interval '1 day' order by created_at desc limit 10")


def task_progress_sql() -> str:
    """A running task's progress and heartbeat. Params: done, id. The count only goes up: the beat sends the count its
    thread last held, which a by-database write's commits can already have passed."""
    return (f"update {SCHEMA}.app_task set done = greatest(done, %s), heartbeat_at = now() "
            f"where id = %s and status = 'running'")


def finish_task_sql() -> str:
    """A task's end. Params: status ('succeeded' or 'failed'), job_id (or null to keep it), result (JSON text, or null
    to keep it), error, id. Only a running task is finished, so a late finish can't undo a stale one's verdict."""
    return (f"update {SCHEMA}.app_task set status = %s, job_id = coalesce(%s, job_id), "
            f"result = coalesce(%s::jsonb, result), error = %s, finished_at = now() "
            f"where id = %s and status = 'running'")


def task_written_sql() -> str:
    """Inside the write's own transaction, its last statement: what it wrote, so the task's result commits with the
    tables or not at all. Params: result (JSON text), job_id (or null to keep it), done, id."""
    return (f"update {SCHEMA}.app_task set result = %s::jsonb, job_id = coalesce(%s, job_id), done = %s, "
            f"heartbeat_at = now() where id = %s")


def stale_tasks_sql() -> str:
    """End the running tasks whose heartbeat stopped (the app restarted while writing). Every one ends failed, never
    succeeded, so nothing reads as done before what follows it has finished: one with a result had committed its tables
    but missed the Databricks job sync that follows (unsynced is its error); one with progress but no result is a
    by-database write whose committed databases are kept, and the error says to send the request again to finish it
    (partial); the rest were rolled back with the app (rolled_back). Params: unsynced, rolled_back, partial, seconds,
    id (or null), job_id (or null)."""
    return (f"update {SCHEMA}.app_task set status = 'failed', "
            f"error = case when result is not null then %s when progress is null then %s else %s end, finished_at = now() "
            f"where status = 'running' and heartbeat_at < now() - make_interval(secs => %s) "
            f"and (id = %s or job_id = %s)")


def task_progress_json_sql() -> str:
    """A by-database write's progress, durable: written in each database's own transaction, so what the progress says
    is what is committed. Params: progress (JSON text), done (tables written), id."""
    return (f"update {SCHEMA}.app_task set progress = %s::jsonb, done = %s, heartbeat_at = now() "
            f"where id = %s and status = 'running'")


def task_job_sql() -> str:
    """Records the job a write made, in the transaction that made it (so a restart finds the job, not a second one).
    Params: job_id, id."""
    return f"update {SCHEMA}.app_task set job_id = %s, heartbeat_at = now() where id = %s"


def resume_task_sql() -> str:
    """Restarts a by-database write that stopped before its result (failed with progress): the same request id again
    finishes it, not a second job. Returns the task's id when it was restarted. Param: id."""
    return (f"update {SCHEMA}.app_task set status = 'running', error = null, finished_at = null, "
            f"heartbeat_at = now() where id = %s and status = 'failed' and result is null and progress is not null "
            f"returning id")


def resume_synced_task_sql() -> str:
    """Restarts a write that committed but is not done (failed with its result kept: its Databricks sync failed, or the
    app restarted before it). Sent again with its request id, it syncs again and writes nothing. Returns the id when
    it was restarted. Param: id."""
    return (f"update {SCHEMA}.app_task set status = 'running', error = null, finished_at = null, heartbeat_at = now() "
            f"where id = %s and status = 'failed' and result is not null returning id")


def running_task_sql() -> str:
    """Whether a job has a write running (its heartbeat fresh): a Delete waits for it. Params: job_id, seconds."""
    return (f"select id from {SCHEMA}.app_task where job_id = %s and status = 'running' "
            f"and heartbeat_at > now() - make_interval(secs => %s) limit 1")

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
            p["notes"].append(f"moves to ingestion group {merged['task_collection']}: that group's runs and schedule apply")
        if p["reseed"]:
            p["notes"].insert(0, f"reseed required ({p['reseed']}): the table is copied again on its next run")
        plans.append(p)
    return plans


def control_rows_for_update_sql() -> str:
    cols = ", ".join(("id",) + CONTROL_EDIT_COLUMNS)
    return f"select {cols} from {SCHEMA}.control where id = any(%s) order by id for update"


def control_groups_sql() -> str:
    """The groups of some control rows, read without locking them (for the permission check before the locks).
    Param: ids (list)."""
    return f"select distinct task_collection from {SCHEMA}.control where id = any(%s)"


def control_groups_for_secret_key_sql() -> str:
    """The groups of the control rows that read one secret key: who may change them decides who may write that
    key's login (main._refuse_groups). Param: secret_key."""
    return f"select distinct task_collection from {SCHEMA}.control where secret_key = %s"


def jdbc_login_insert_sql() -> str:
    """One row per login the app writes, outcome 'rotated' when an earlier login for the same scope and key exists
    (checked in the same statement, before the row is added). Params: id, connection, secret_scope, secret_key,
    entered_by, then secret_scope and secret_key for that check."""
    return (f"insert into {SCHEMA}.jdbc_login_write (id, connection, secret_scope, secret_key, entered_by, outcome) "
            f"select %s, %s, %s, %s, %s, case when exists (select 1 from {SCHEMA}.jdbc_login_write "
            "where secret_scope = %s and secret_key = %s) then 'rotated' else 'saved' end "
            "returning id, outcome, entered_at")


def jdbc_login_latest_sql() -> str:
    """The newest login written for one scope and secret key (never the password, which the table doesn't hold).
    Params: secret_scope, secret_key."""
    return (f"select id, connection, secret_scope, secret_key, entered_by, entered_at, outcome from {SCHEMA}.jdbc_login_write "
            "where secret_scope = %s and secret_key = %s order by entered_at desc, id desc limit 1")


def jdbc_login_lock_sql() -> str:
    """Wait for one secret key's login lock, held to the end of the transaction: two saves of one key take turns, so
    its newest record always names the login it holds. Param: jdbc_login_lock_key."""
    return "select pg_advisory_xact_lock(hashtextextended(%s, 0))"


def jdbc_login_lock_key(scope: str, secret_key: str) -> str:
    """The lock key of one scope's secret key, apart from every collection's run lock key."""
    return f"{SCHEMA}:jdbc_login:{scope}/{secret_key}"


def jdbc_connection_groups_sql() -> str:
    """The groups of the live jobs made from discoveries through one JDBC connection: whoever may change them may
    save its logins. Param: the connection's name."""
    return (f"select distinct m.task_collection from {SCHEMA}.job_selection s "
            f"join {SCHEMA}.discovery d on d.discovery_id = any(string_to_array(s.source_id, ',')) "
            f"join {SCHEMA}.schedule_member m on m.schedule_id = s.job_id and m.removed_at is null "
            f"join {SCHEMA}.schedule j on j.id = s.job_id and j.deleted_at is null "
            "where s.source_kind = 'discovery' and d.listing ->> 'jdbc_connection' = %s")


def jdbc_login_writers_sql() -> str:
    """Who typed each earlier login under one scope and key, and who made the connection it was typed for (null when
    that connection isn't stored): they may write the key again before a job reads it. Params: secret_scope,
    secret_key."""
    return (f"select distinct w.entered_by, c.created_by from {SCHEMA}.jdbc_login_write w "
            f"left join {SCHEMA}.jdbc_connection c on c.name = w.connection "
            "where w.secret_scope = %s and w.secret_key = %s")


def jdbc_connection_login_sql() -> str:
    """The keys of the logins one user typed in one scope under a connection's name, newest first. The caller keeps the
    keys that fit the connection's key template (jdbc_connections.key_fits): a connection in the login scope is made only
    when its maker typed a login for one of them. Params: secret_scope, connection name, entered_by."""
    return (f"select secret_key from {SCHEMA}.jdbc_login_write where secret_scope = %s and connection = %s "
            "and entered_by = %s order by entered_at desc, id desc")


def jdbc_login_latest_per_key_sql() -> str:
    """The newest login written for each of some secret keys in one scope, one row per key that has any (never the
    password). A JDBC read checks that the login each of its keys holds was typed under the connection that reads it.
    Params: secret_scope, secret_keys (text[])."""
    return (f"select distinct on (secret_key) secret_key, id, connection, entered_by, entered_at, outcome "
            f"from {SCHEMA}.jdbc_login_write where secret_scope = %s and secret_key = any(%s) "
            "order by secret_key, entered_at desc, id desc")


def control_row_sql() -> str:
    cols = ", ".join(("id",) + CONTROL_EDIT_COLUMNS)
    return f"select {cols} from {SCHEMA}.control where id = %s"


def collection_control_rows_sql() -> str:
    """Every control row of one ingestion group, enabled or not, oldest first: what its settings are read from
    (config_import.group_settings) and what its export writes. Param: task_collection."""
    cols = ", ".join(("id",) + CONTROL_EDIT_COLUMNS)
    return f"select {cols} from {SCHEMA}.control where task_collection = %s order by id"


def export_groups_sql() -> str:
    """The groups of the ingestion jobs an export takes: each live job's active members, with its name, the jobs given
    or every live one. Params: the job ids (bigint[]) twice, or null for all."""
    return (f"select s.id, s.name, m.task_collection from {SCHEMA}.schedule s "
            f"join {SCHEMA}.schedule_member m on m.schedule_id = s.id and m.removed_at is null "
            f"where s.deleted_at is null and (%s::bigint[] is null or s.id = any(%s::bigint[])) "
            f"order by lower(s.name), m.task_collection")


def export_attr_names_sql() -> str:
    """The identity column names an export's enabled rows use, so its header can name them before the first line.
    Param: the groups (text[])."""
    return (f"select distinct k.name from {SCHEMA}.control c, jsonb_object_keys(c.client_attrs::jsonb) as k(name) "
            f"where c.task_collection = any(%s) and c.task_enabled and c.client_attrs is not null order by k.name")


def export_reads_jdbc_sql() -> str:
    """Whether any enabled row of an export's groups is read by host and secret (config_import.JDBC_COLUMNS), so its
    header knows whether to carry those columns. Param: the groups (text[])."""
    return (f"select exists (select 1 from {SCHEMA}.control where task_collection = any(%s) and task_enabled "
            f"and (src_host is not null or src_port is not null or secret_key is not null)) as jdbc")


EXPORT_PAGE = 5000


def schema_version_table_sql() -> str:
    """Whether the schema_version table exists (null when it doesn't), for the app's schema upgrade check."""
    return f"select to_regclass('{SCHEMA}.schema_version')::text as to_regclass"


def schema_version_sql() -> str:
    """The newest schema version applied to the database, 0 when none (state_store's own read)."""
    return f"select coalesce(max(version), 0) as version from {SCHEMA}.schema_version"


def try_session_lock_sql() -> str:
    """A session advisory lock on two int keys, taken only if free. Params: the two keys."""
    return "select pg_try_advisory_lock(%s, %s)"


def session_unlock_sql() -> str:
    """Release a session advisory lock taken with try_session_lock_sql. Params: the two keys."""
    return "select pg_advisory_unlock(%s, %s)"


def export_page_sql() -> str:
    """One page of an export: the enabled control rows of some groups after a control id, by id (keyset paging, so a
    200,000-table export never holds more than a page). Params: the groups (text[]), the last id read, the page size."""
    cols = ", ".join(("id", "task_collection", "src_connection", "src_host", "src_port", "secret_key", "src_database",
                      "src_schema", "src_table", "select_list", "primary_key", "cursor_column", "client_id",
                      "load_type", "client_attrs"))
    return (f"select {cols} from {SCHEMA}.control where task_collection = any(%s) and task_enabled and id > %s "
            f"order by id limit %s")


def move_controls_sql() -> str:
    """Move control rows into another ingestion group, as a job's groups are spread again. Each table keeps its
    checkpoint, which is by control id, so nothing reloads. Params: the group, list of control ids."""
    return f"update {SCHEMA}.control set task_collection = %s where id = any(%s)"


def move_database_checks_sql() -> str:
    """Move some databases' check stamps with them into another group: a stamp stays true of its database whatever
    group it is in, so a moved database isn't read as unchecked until the next check. A stamp the new group already
    has for one (left from when the database was there before) is kept, and the old group's stays unused.
    Params: new group, old group, list of database names as the control rows name them, new group."""
    return (f"update {SCHEMA}.database_check d set task_collection = %s where d.task_collection = %s "
            f"and d.src_database = any(%s) and not exists (select 1 from {SCHEMA}.database_check e "
            f"where e.task_collection = %s and e.source = d.source and e.src_database = d.src_database)")


def groups_control_rows_sql(for_update: bool = False) -> str:
    """Every control row of the given ingestion groups, enabled or not, oldest first: an ingestion job's rows, which
    its settings are read from (job_settings.read). Locked for update when an edit applies. Param: list of groups."""
    cols = ", ".join(("id",) + CONTROL_EDIT_COLUMNS)
    return (f"select {cols} from {SCHEMA}.control where task_collection = any(%s) order by id"
            + (" for update" if for_update else ""))


# An ingestion job's tables as a tree: its servers, a server's databases, a database's tables. Each level is
# searched and paged in SQL, so a job of millions of tables never comes back whole. Aliases t, cp as STATE_COUNTS.
_JOB_ROWS = (f"from {SCHEMA}.control t left join {SCHEMA}.checkpoint cp on cp.control_id = t.id {DATABASE_CHECK_JOIN} "
             "where t.task_collection = any(%s)")


def groups_row_count_sql() -> str:
    """How many control rows the given groups have. Param: list of groups."""
    return f"select count(*) as n from {SCHEMA}.control where task_collection = any(%s)"


def job_servers_sql() -> str:
    """Params: groups, search pattern (ilike), limit, offset. Each row carries the level's total."""
    return f"""
select {SERVER} as server, min(t.src_connection) as src_connection, min(lower(t.src_host)) as src_host,
       count(distinct coalesce(t.src_database, '')) as databases, count(*) as tables,
       count(*) filter (where t.task_enabled) as enabled_tables, {STATE_COUNTS}, count(*) over () as total
{_JOB_ROWS} and {SERVER} ilike %s
group by 1 order by 1 limit %s offset %s"""


def job_databases_sql() -> str:
    """Params: groups (for the job's table count), groups, server, search pattern, limit, offset. ``missing`` is
    how many of the job's tables the database lacks."""
    return f"""
with job_tables as (
    select count(distinct (lower(src_schema), lower(src_table))) as n from {SCHEMA}.control where task_collection = any(%s)
)
select coalesce(t.src_database, '') as database, min(t.client_id) as client_id, count(*) as tables,
       count(*) filter (where t.task_enabled) as enabled_tables, {STATE_COUNTS},
       (select n from job_tables) - count(*) as missing, count(*) over () as total
{_JOB_ROWS} and {SERVER} = %s and coalesce(t.src_database, '') ilike %s
group by 1 order by 1 limit %s offset %s"""


def job_tables_sql() -> str:
    """One database's tables of a job, each with its last success counting its database's check stamp
    (LAST_SUCCESS), as the rest of the app shows it. Params: groups, server, database, search pattern, limit, offset."""
    return f"""
select t.id, t.src_schema as schema, t.src_table as "table", t.load_type, t.primary_key, t.select_list,
       t.sink_catalog || '.' || t.sink_schema || '.' || t.sink_table as target, t.task_enabled as enabled,
       t.task_collection, cp.status, cp.phase, max({LAST_SUCCESS}) as last_success_at, {STATE_COUNTS},
       count(*) over () as total
{_JOB_ROWS} and {SERVER} = %s and coalesce(t.src_database, '') = %s
  and (t.src_schema || '.' || t.src_table) ilike %s
group by t.id, cp.control_id order by lower(t.src_schema), lower(t.src_table) limit %s offset %s"""


_TARGET = "t.sink_catalog || '.' || t.sink_schema || '.' || t.sink_table"
# Rows the run records say each table wrote over the last 7 days: its ok events' merged or appended rows.
_WRITTEN_7D = (f"(select e.control_id, sum(e.rows_merged) as rows_written, max(e.finished_at) as last_written "
               f"from {SCHEMA}.ingest_event e where e.task_collection = any(%s) and e.status = 'ok' "
               f"and e.finished_at >= now() - interval '7 days' group by e.control_id)")
# A job's rows (alias t) with their checkpoints, database stamps and rows written ("written"), before the WHERE.
_JOB_ROWS_WRITTEN = (f"from {SCHEMA}.control t left join {SCHEMA}.checkpoint cp on cp.control_id = t.id "
                     f"{DATABASE_CHECK_JOIN} left join written w on w.control_id = t.id")


def job_targets_sql() -> str:
    """A job's target tables, one row each: how many of its source tables write it (and how many outside the job),
    from how many databases, their states now, and the rows written over 7 days by the run records; fan_in_total
    counts the matching targets written from more than one source table. Exact row counts would need a SQL warehouse,
    which the app avoids. Params: groups, groups, groups, search pattern, limit, offset."""
    return f"""
with written as {_WRITTEN_7D},
outside as (
  select o.sink_catalog || '.' || o.sink_schema || '.' || o.sink_table as target, count(*) as n
  from {SCHEMA}.control o where not (o.task_collection = any(%s)) group by 1)
select {_TARGET} as target, t.sink_catalog as catalog, t.sink_schema as schema, t.sink_table as "table",
       count(*) as sources, count(distinct ({SERVER}, coalesce(t.src_database, ''))) as databases,
       count(*) filter (where t.client_id is not null) as with_client_id,
       coalesce(max(x.n), 0) as outside_sources, {STATE_COUNTS},
       sum(w.rows_written) as rows_written_7d, max(w.last_written) as last_written, count(*) over () as total,
       sum(case when count(*) + coalesce(max(x.n), 0) > 1 then 1 else 0 end) over () as fan_in_total
{_JOB_ROWS_WRITTEN}
left join outside x on x.target = {_TARGET}
where t.task_collection = any(%s) and {_TARGET} ilike %s
group by t.sink_catalog, t.sink_schema, t.sink_table
order by lower({_TARGET}) limit %s offset %s"""


def job_target_sources_sql() -> str:
    """The source tables of a job that write one target, with each one's state and its rows written over 7 days.
    Params: groups, groups, catalog, schema, table, limit, offset."""
    return f"""
with written as {_WRITTEN_7D}
select t.id, {SERVER} as server, t.src_database as database, t.src_schema as schema, t.src_table as "table",
       t.client_id, t.task_collection, t.task_enabled as enabled, cp.status, cp.phase,
       max({LAST_SUCCESS}) as last_success_at, {STATE_COUNTS}, max(w.rows_written) as rows_written_7d, max(w.last_written) as last_written,
       count(*) over () as total
{_JOB_ROWS_WRITTEN}
where t.task_collection = any(%s) and t.sink_catalog = %s and t.sink_schema = %s and t.sink_table = %s
group by t.id, cp.control_id
order by lower({SERVER}), lower(coalesce(t.src_database, '')), lower(t.src_schema), lower(t.src_table)
limit %s offset %s"""


def job_row_ids_sql() -> str:
    """The ids of a job's rows on one server, optionally one database and one table (schema and table; blank =
    all). Params: groups, server ('' = every server), database, schema, table."""
    return f"""
select t.id from {SCHEMA}.control t where t.task_collection = any(%s)
  and (%s = '' or {SERVER} = %s) and (%s = '' or coalesce(t.src_database, '') = %s)
  and (%s = '' or lower(t.src_schema) = lower(%s)) and (%s = '' or lower(t.src_table) = lower(%s))
order by t.id"""


def update_control_sql(fields: List[str]) -> str:
    """UPDATE of whitelisted columns only (field names come from CONTROL_EDIT_COLUMNS)."""
    assert fields and all(f in CONTROL_EDIT_COLUMNS for f in fields), fields
    return f"update {SCHEMA}.control set " + ", ".join(f"{f} = %s" for f in fields) + " where id = %s"


# The kept target tables the Delete dialog names (the rest are counted).
DELETE_KEPT_LISTED = 20


def collection_lock_sql() -> str:
    """Take a collection's run lock for this transaction (the same key the ingestion job holds for a
    whole run). False = a run of that collection is in progress. Param: lock key."""
    return "select pg_try_advisory_xact_lock(hashtextextended(%s, 0))"


def collection_lock_key(collection: str) -> str:
    return "lakefed_ingest:" + str(collection)   # state_store.LOCK_NAMESPACE + collection


def collection_locks_sql() -> str:
    """collection_lock_sql for several collections in one statement: a row per lock key with whether this transaction
    took it (False = a run of that collection is in progress). Param: list of lock keys (collection_lock_key)."""
    return "select k, pg_try_advisory_xact_lock(hashtextextended(k, 0)) from unnest(%s::text[]) as k"


def flip_to_seeding_sql() -> str:
    """Params: detail, control ids."""
    return f"update {SCHEMA}.checkpoint set {_RESEED_SET}, detail = %s where control_id = any(%s)"


def mixed_sinks_for_sql() -> str:
    """Sinks among the given ones that a dedicated row and other rows both claim. Each sink is looked up by its
    lower-cased catalog, schema and table, which is the prefix of control_sink_client_uq, so the check costs one
    index probe per sink rather than a scan of the control table. Params: the catalogs, the schemas and the tables
    (three lists, lower case, one entry per sink, in the same order). Returns the sink as catalog.schema.table."""
    return (f"select s.c || '.' || s.sc || '.' || s.t as sink "
            f"from unnest(%s::text[], %s::text[], %s::text[]) as s(c, sc, t) "
            f"cross join lateral (select count(*) as claims, count(*) filter (where r.client_id is null) as dedicated "
            f"from {SCHEMA}.control r where lower(r.sink_catalog) = s.c and lower(r.sink_schema) = s.sc "
            f"and lower(r.sink_table) = s.t) m "
            f"where m.dedicated > 0 and m.claims > 1")


def database_source(row: dict) -> str:
    """``database_source_sql`` of a control-row dict (the engine's ``state_store.database_source``)."""
    host = str(row.get("src_host") or row.get("src_connection") or "").lower()
    credential = row.get("secret_key") or row.get("src_connection") or ""
    port = int(row.get("src_port") or 0)
    if not port and (row.get("src_host") or not row.get("src_connection")):
        port = DEFAULT_PORTS.get((str(row.get("src_type") or "").strip(" ").lower()) or "sqlserver", 0)
    return f"{host}|{port}|{credential}"


def database_key(row: dict) -> str:
    """One source database (its server identity and name) as one text, the key ``split_databases_sql`` takes."""
    return f"{database_source(row)}\x1f{row.get('src_database') or ''}"


def split_databases_sql() -> str:
    """Databases among the given keys (``database_key``) whose control rows are in more than one
    collection. A database's tables share one collection: its run takes the collection's lock and the
    check job reads the database once for the collection. Param: list of keys. Returns key, database,
    collections. The database names (each key's last part) narrow the rows through control_database_idx."""
    key = f"{database_source_sql('t')} || chr(31) || coalesce(t.src_database, '')"
    return (f"with k as (select unnest(%s::text[]) as key) "
            f"select {key} as key, min(t.src_database) as src_database, "
            f"array_agg(distinct t.task_collection order by t.task_collection) as collections "
            f"from {SCHEMA}.control t where t.src_database in (select split_part(key, chr(31), -1) from k) "
            f"and {key} in (select key from k) group by 1 having count(distinct t.task_collection) > 1")


def clash_jobs_sql() -> str:
    """The live ingestion jobs that hold any of the given groups, so a refusal for databases whose tables sit in more
    than one group can name them and link to each. Param: groups (list). Returns id and name, by name, at most 20."""
    return (f"select s.id, s.name from {SCHEMA}.schedule_member m join {SCHEMA}.schedule s on s.id = m.schedule_id "
            f"where m.task_collection = any(%s) and m.removed_at is null and s.deleted_at is null "
            f"group by s.id, s.name order by s.name, s.id limit 20")


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
    """One table's checkpoint, its last success counting its database's check stamp (LAST_SUCCESS).
    Param: control_id."""
    return (f"select cp.phase, cp.status, cp.position, cp.strategy, cp.detail, {LAST_SUCCESS} as last_success_at, "
            f"cp.updated_at, cp.source_identity, cp.sink_identity, cp.merge_key from {SCHEMA}.checkpoint cp "
            f"join {SCHEMA}.control t on t.id = cp.control_id {DATABASE_CHECK_JOIN} where cp.control_id = %s")


# ---- schedules ----------------------------------------------------------------------------------

SCHEDULE_FIELDS = ("name", "cadence_minutes", "timezone", "parameters", "job_variant", "stagger", "paused",
                   "notify_emails", "options")
JSON_SCHEDULE_FIELDS = ("parameters", "options")


# A deleted job whose purge finished: the delete job marks it, in the transaction that removed its groups' rows.
PURGED_OPTION = "(coalesce(options, '{}'::jsonb) ? 'delete_finished')"


def group_job_options_sql() -> str:
    """The options of the job that holds one ingestion group (its member not removed), first by name as schedules_sql
    lists the jobs, a deleted job not purged yet included; no row when no job holds it. Param: task_collection."""
    return (f"select s.id, s.options from {SCHEMA}.schedule s join {SCHEMA}.schedule_member m on m.schedule_id = s.id "
            f"where m.task_collection = %s and m.removed_at is null and (s.deleted_at is null or not {PURGED_OPTION}) "
            "order by lower(s.name), s.id limit 1")


def schedules_sql() -> str:
    """Schedules by name: the active ones, and the deleted ones not yet purged. A deleted job is kept for its retention
    period so a Restore can bring it back (ingestion_jobs.delete_state shows it as Deleted, with the days left); it
    leaves this list once its purge has finished."""
    return (f"select id, {', '.join(SCHEDULE_FIELDS)}, created_by, created_at, updated_at, deleted_at "
            f"from {SCHEMA}.schedule where deleted_at is null or not {PURGED_OPTION} order by lower(name)")


def deleted_jobs_sql() -> str:
    """One page of the deleted jobs kept for a Restore (not yet purged), the newest deletion first. Params: limit,
    offset."""
    return (f"select id, {', '.join(SCHEDULE_FIELDS)}, created_by, created_at, updated_at, deleted_at "
            f"from {SCHEMA}.schedule where deleted_at is not null and not {PURGED_OPTION} "
            f"order by deleted_at desc, id desc limit %s offset %s")


def deleted_jobs_count_sql() -> str:
    """How many deleted jobs are kept for a Restore (not yet purged). Column: total."""
    return f"select count(*) as total from {SCHEMA}.schedule where deleted_at is not null and not {PURGED_OPTION}"


def deleted_tables_left_sql() -> str:
    """Which deleted jobs still have tables a Delete would remove: a group of theirs with control rows that no other
    live job holds (the same groups delete_groups_sql names). Param: schedule ids. Rows: schedule_id."""
    return (f"select distinct m.schedule_id from {SCHEMA}.schedule_member m where m.schedule_id = any(%s) "
            f"and exists (select 1 from {SCHEMA}.control c where c.task_collection = m.task_collection) "
            f"and not exists (select 1 from {SCHEMA}.schedule_member o join {SCHEMA}.schedule s on s.id = o.schedule_id "
            f"where o.task_collection = m.task_collection and o.removed_at is null and s.deleted_at is null "
            f"and o.schedule_id <> m.schedule_id)")


def set_schedule_option_sql() -> str:
    """Set one key of a schedule's options to a number. Params: key (as a one-element text array), value, id."""
    return (f"update {SCHEMA}.schedule set options = jsonb_set(coalesce(options, '{{}}'::jsonb), %s::text[], "
            f"to_jsonb(%s::bigint)) where id = %s")


def set_schedule_text_option_sql() -> str:
    """Set one key of a schedule's options to a text. Params: key (as a one-element text array), value, id."""
    return (f"update {SCHEMA}.schedule set options = jsonb_set(coalesce(options, '{{}}'::jsonb), %s::text[], "
            f"to_jsonb(%s::text)) where id = %s")


def clear_schedule_option_sql() -> str:
    """Remove one key from a schedule's options. Params: key, id."""
    return f"update {SCHEMA}.schedule set options = options - %s where id = %s"


def claim_rediscovery_sql() -> str:
    """Claim a job's re-discovery: its record becomes running, unless a run holds it (a running record whose heartbeat is
    fresh). Returns the id when claimed. Params: the running record as JSON, id, stale seconds."""
    return (f"update {SCHEMA}.schedule set options = jsonb_set(coalesce(options, '{{}}'::jsonb), '{{rediscovery}}', "
            f"%s::jsonb) where id = %s and deleted_at is null and (options->'rediscovery' is null "
            f"or options->'rediscovery'->>'status' is distinct from 'running' "
            f"or coalesce((options->'rediscovery'->>'heartbeat_at')::timestamptz, "
            f"(options->'rediscovery'->>'started_at')::timestamptz, 'epoch'::timestamptz) "
            f"< now() - make_interval(secs => %s)) returning id")


def beat_rediscovery_sql() -> str:
    """Stamp a running re-discovery's heartbeat, so the record shows its thread is alive. Only while the record is
    running, so a beat can't touch one that has ended. Params: the time (ISO text), id."""
    return (f"update {SCHEMA}.schedule set options = jsonb_set(options, '{{rediscovery,heartbeat_at}}', "
            f"to_jsonb(%s::text)) where id = %s and options->'rediscovery'->>'status' = 'running'")


def set_rediscovery_sql() -> str:
    """Write a job's re-discovery record (options.rediscovery). Params: the record as JSON, id."""
    return (f"update {SCHEMA}.schedule set options = jsonb_set(coalesce(options, '{{}}'::jsonb), '{{rediscovery}}', "
            f"%s::jsonb) where id = %s")


def selection_discoveries_sql() -> str:
    """The discovery rows a selection was made from (one per database read): the server's connection or host and port,
    its secrets, its listing and when each was read. Param: list of discovery ids."""
    return (f"select lower(coalesce(src_connection, src_host)) as server, src_type, src_connection, src_host, src_port, "
            f"secret_key, src_database, listing, finished_at from {SCHEMA}.discovery where discovery_id = any(%s)")


def discovery_statuses_sql() -> str:
    """Each database a discovery read, with its status and error (the master row when a server couldn't be listed).
    Param: list of discovery ids."""
    return (f"select lower(coalesce(src_connection, src_host)) as server, src_database, status, error "
            f"from {SCHEMA}.discovery where discovery_id = any(%s)")


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
    vals = ", ".join("%s::jsonb" if c in JSON_SCHEDULE_FIELDS else "%s" for c in cols)
    return f"insert into {SCHEMA}.schedule ({', '.join(cols)}) values ({vals}) returning id"


_STAMP = (f"update {SCHEMA}.schedule set updated_at = now(), "
          f"options = jsonb_set(coalesce(options, '{{}}'::jsonb), '{{updated_by}}', to_jsonb(%s::text)) ")


def touch_jobs_sql() -> str:
    """Record who last changed these ingestion jobs, and when (options.updated_by, updated_at). Params: user, ids."""
    return _STAMP + "where id = any(%s) and deleted_at is null"


def touch_group_jobs_sql() -> str:
    """The same for every ingestion job holding one of these groups, when their tables change. Params: user, groups."""
    return (_STAMP + f"where deleted_at is null and id in (select schedule_id from {SCHEMA}.schedule_member "
            "where task_collection = any(%s) and removed_at is null)")


def update_schedule_sql(fields: List[str]) -> str:
    bad = [f for f in fields if f not in SCHEDULE_FIELDS]
    if bad or not fields:
        raise ValueError(f"not schedule fields: {bad}")
    sets = ", ".join(f"{f} = %s::jsonb" if f in JSON_SCHEDULE_FIELDS else f"{f} = %s" for f in fields)
    return f"update {SCHEMA}.schedule set {sets}, updated_at = now() where id = %s and deleted_at is null"


def delete_schedule_sql() -> str:
    """Soft delete (the app has no DELETE privilege). Param: id."""
    return f"update {SCHEMA}.schedule set deleted_at = now(), updated_at = now() where id = %s and deleted_at is null"


def restore_schedule_sql() -> str:
    """Restore a deleted job not yet purged: live again and paused (nothing runs until it is resumed), its delete
    options cleared. Matches no row once its purge has finished. Param: id."""
    return (f"update {SCHEMA}.schedule set deleted_at = null, paused = true, "
            f"options = coalesce(options, '{{}}'::jsonb) - array['delete_drop_targets', 'delete_run_id', "
            f"'delete_start_error', 'delete_finished'], updated_at = now() "
            f"where id = %s and deleted_at is not null and not {PURGED_OPTION}")


def restore_members_sql() -> str:
    """Put back the members the job's Delete removed: those removed in the same instant the job was deleted (the delete
    sets both with now(), in one transaction), so members removed earlier stay removed. Run before
    restore_schedule_sql, which clears deleted_at. Params: schedule id, schedule id."""
    return (f"update {SCHEMA}.schedule_member m set removed_at = null where m.schedule_id = %s "
            f"and m.removed_at = (select d.deleted_at from {SCHEMA}.schedule d where d.id = %s)")


def live_name_taken_sql() -> str:
    """A live job other than the given one with this name, names compared without case. Params: name, schedule id."""
    return f"select id from {SCHEMA}.schedule where lower(name) = lower(%s) and deleted_at is null and id <> %s"


def groups_held_elsewhere_sql() -> str:
    """The given groups that a live job other than the given one holds. Params: groups (list), schedule id. Columns:
    task_collection, name (of that job)."""
    return (f"select m.task_collection, s.name from {SCHEMA}.schedule_member m "
            f"join {SCHEMA}.schedule s on s.id = m.schedule_id "
            f"where m.task_collection = any(%s) and m.removed_at is null and s.deleted_at is null "
            f"and m.schedule_id <> %s order by 1")


def restore_conflicts_sql() -> str:
    """What the given groups would share with a live job's groups: a source database (the key split_databases_sql
    compares) or a target table (compared without case, as Unity Catalog does) that a live job's group also reads or
    writes. At most 20 rows. Params: groups (list), groups (list). Columns: kind ('database' or 'target'), item,
    name (of
    the live job)."""
    live = (f"join {SCHEMA}.schedule_member om on om.task_collection = o.task_collection and om.removed_at is null "
            f"join {SCHEMA}.schedule s on s.id = om.schedule_id and s.deleted_at is null")
    database_key = lambda alias: f"{database_source_sql(alias)} || chr(31) || coalesce({alias}.src_database, '')"
    return (f"select 'database' as kind, c.src_database as item, s.name as name from {SCHEMA}.control c "
            f"join {SCHEMA}.control o on o.src_database = c.src_database and {database_key('o')} = {database_key('c')} "
            f"and o.task_collection <> c.task_collection {live} where c.task_collection = any(%s) "
            f"union "
            f"select 'target', lower(c.sink_catalog || '.' || c.sink_schema || '.' || c.sink_table), s.name "
            f"from {SCHEMA}.control c join {SCHEMA}.control o on lower(o.sink_catalog) = lower(c.sink_catalog) "
            f"and lower(o.sink_schema) = lower(c.sink_schema) and lower(o.sink_table) = lower(c.sink_table) "
            f"and o.task_collection <> c.task_collection {live} where c.task_collection = any(%s) "
            f"order by 1, 2 limit 20")


def deleted_database_owners_sql() -> str:
    """The deleted jobs kept for a Restore whose groups read the given databases, and that no live job holds: the
    first one, by name, as the database (the name as written), the job's name and id. Param: database keys (text[]),
    as database_key builds them."""
    key = f"{database_source_sql('t')} || chr(31) || coalesce(t.src_database, '')"
    return (f"with k as (select unnest(%s::text[]) as key) "
            f"select min(t.src_database) as database, d.name as job, d.id as schedule_id "
            f"from {SCHEMA}.control t join {SCHEMA}.schedule_member m on m.task_collection = t.task_collection "
            f"join {SCHEMA}.schedule d on d.id = m.schedule_id "
            f"where d.deleted_at is not null and not {PURGED_OPTION} "
            f"and t.src_database in (select split_part(key, chr(31), -1) from k) and {key} in (select key from k) "
            f"and not exists (select 1 from {SCHEMA}.schedule_member o join {SCHEMA}.schedule s "
            f"on s.id = o.schedule_id "
            f"where o.task_collection = t.task_collection and o.removed_at is null and s.deleted_at is null) "
            f"group by d.id, d.name order by d.name, d.id limit 1")


def insert_member_if_absent_sql() -> str:
    """insert_member_sql for a group a job already has a member for (a resumed write): skipped, not refused. It returns
    the new member's id, or nothing when it was skipped. Params: schedule_id, task_collection, offset_minutes."""
    return (f"insert into {SCHEMA}.schedule_member (schedule_id, task_collection, offset_minutes) "
            f"values (%s, %s, %s) on conflict do nothing returning id")


def job_database_sinks_chunk_sql() -> str:
    """job_database_sinks_sql for a chunk of databases at once: one row per (database, sink, client id, group) that
    their control rows already write, so a resumed write skips the rows a committed transaction wrote. The databases
    match exactly (control_database_idx), so pass each one both as the source names it and in lower case: the rows
    an earlier transaction wrote carry one of those. Params: groups (list), databases (list)."""
    return (f"select lower(src_database) as src, "
            f"lower(sink_catalog || '.' || sink_schema || '.' || sink_table) as sink, "
            f"coalesce(client_id, '') as client_id, task_collection from {SCHEMA}.control "
            f"where task_collection = any(%s) and src_database = any(%s)")


def insert_members_if_absent_sql() -> str:
    """insert_member_if_absent_sql for several groups in one statement: a group the job already has a member for is
    skipped. It returns the task collection of each member it added. Params: schedule_id, groups (list),
    offsets (list)."""
    return (f"insert into {SCHEMA}.schedule_member (schedule_id, task_collection, offset_minutes) "
            f"select %s, g.task_collection, g.offset_minutes "
            f"from unnest(%s::text[], %s::int[]) as g(task_collection, offset_minutes) "
            f"on conflict do nothing returning task_collection")


def job_database_sinks_sql() -> str:
    """The sinks (and client ids) the control rows of one database already write in the given groups: what a
    by-database write checks before it writes the database again. Params: groups (list), database (lower case compared)."""
    return (f"select lower(sink_catalog || '.' || sink_schema || '.' || sink_table) as sink, coalesce(client_id, '') as client_id "
            f"from {SCHEMA}.control where task_collection = any(%s) and lower(src_database) = lower(%s)")


def insert_member_sql() -> str:
    return (f"insert into {SCHEMA}.schedule_member (schedule_id, task_collection, offset_minutes) "
            f"values (%s, %s, %s) returning id")


def update_member_offset_sql() -> str:
    return f"update {SCHEMA}.schedule_member set offset_minutes = %s where id = %s"


def remove_members_sql() -> str:
    """Soft-remove members (their jobs are deleted by the next sync). Param: list of member ids."""
    return f"update {SCHEMA}.schedule_member set removed_at = now() where id = any(%s) and removed_at is null"


def set_schedule_flag_sql() -> str:
    """Set one key of a schedule's options to true or false. Params: key (as a one-element text array), value, id."""
    return (f"update {SCHEMA}.schedule set options = jsonb_set(coalesce(options, '{{}}'::jsonb), %s::text[], "
            f"to_jsonb(%s::boolean)) where id = %s")


def delete_groups_sql() -> str:
    """The groups a job's Delete removes: every member it had, removed or not, that no other live job holds (the
    delete job's delete_job.groups_sql, read the same way). Params: schedule id, schedule id."""
    return (f"select distinct m.task_collection from {SCHEMA}.schedule_member m where m.schedule_id = %s "
            f"and not exists (select 1 from {SCHEMA}.schedule_member o join {SCHEMA}.schedule s on s.id = o.schedule_id "
            f"where o.task_collection = m.task_collection and o.removed_at is null and s.deleted_at is null "
            f"and o.schedule_id <> %s) order by 1")


def deleted_groups_sql() -> str:
    """The groups of deleted jobs kept for a Restore (not yet purged) that no live job holds. They belong to the deleted
    job, shown under Deleted jobs, never as "Not in any job"."""
    return (f"select distinct m.task_collection from {SCHEMA}.schedule_member m join {SCHEMA}.schedule d "
            f"on d.id = m.schedule_id where d.deleted_at is not null and not {PURGED_OPTION} "
            f"and not exists (select 1 from {SCHEMA}.schedule_member o join {SCHEMA}.schedule s on s.id = o.schedule_id "
            f"where o.task_collection = m.task_collection and o.removed_at is null and s.deleted_at is null) order by 1")


def delete_targets_sql() -> str:
    """The groups' tables and target tables, for the Delete dialog: how many tables (control rows), how many target
    tables only they write, and the target tables rows of any other group also write (kept; names compared without
    case, as delete_job.targets_sql does), at most ``DELETE_KEPT_LISTED`` of them by name. Params: groups, groups,
    groups."""
    return (f"with mine as (select distinct sink_catalog, sink_schema, sink_table from {SCHEMA}.control "
            f"where task_collection = any(%s)), "
            f"theirs as (select distinct lower(sink_catalog) c, lower(sink_schema) s, lower(sink_table) t "
            f"from {SCHEMA}.control where not (task_collection = any(%s))), "
            f"marked as (select m.*, exists (select 1 from theirs where c = lower(m.sink_catalog) "
            f"and s = lower(m.sink_schema) and t = lower(m.sink_table)) as shared from mine m) "
            f"select (select count(*) from {SCHEMA}.control where task_collection = any(%s)) as tables, "
            f"(select count(*) from marked where not shared) as targets, "
            f"(select count(*) from marked where shared) as kept, "
            f"(select coalesce(json_agg(k order by k), '[]'::json) from (select sink_catalog || '.' || sink_schema "
            f"|| '.' || sink_table as k from marked where shared order by 1 limit {DELETE_KEPT_LISTED}) named) "
            f"as kept_names")


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
    where task_collection = any(%s) and started_at > now() - interval '7 days' and {not_a_check()}
),
agg as (
    select task_collection,
           round(percentile_cont(0.95) within group (order by dur_s) filter (where finished_at is not null and status not in ('skipped', 'cancelled'))::numeric) as p95_s,
           round(percentile_cont(0.5) within group (order by dur_s) filter (where finished_at is not null and status not in ('skipped', 'cancelled'))::numeric) as p50_s,
           count(*) filter (where started_at > now() - interval '24 hours')                      as runs_24h,
           count(*) filter (where started_at > now() - interval '24 hours' and status = 'failed') as failed_24h,
           count(*) filter (where started_at > now() - interval '24 hours' and status = 'skipped') as lock_skipped_24h
    from r group by task_collection
),
last as (
    select distinct on (task_collection) task_collection, run_id as last_run_id, status as last_status,
           started_at as last_started, finished_at as last_finished,
           round(extract(epoch from (coalesce(finished_at, now()) - started_at)))::int as last_duration_s
    from {SCHEMA}.ingest_run where task_collection = any(%s) and {not_a_check()}
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


def check_work_found_sql() -> str:
    """What some checks found to load: per group, the databases with changes each check listed for its run.
    Param: work ids (list)."""
    return (f"select task_collection, count(*) as databases from {SCHEMA}.check_work where work_id = any(%s) "
            f"group by task_collection")


def database_checks_since_sql() -> str:
    """How many databases of some groups a check found unchanged and stamped since a time. Params: groups (list),
    since (timestamptz)."""
    return (f"select count(*) as databases from {SCHEMA}.database_check where task_collection = any(%s) "
            f"and checked_at >= %s")


# ---- uploads and selections by reference ----------------------------------------------------------------------------
# New and Add tables never hold a whole server's tables in the browser: an uploaded CSV is kept in table_upload_line,
# a discovery's tables are in discovered_table, and the pages show their servers and databases, page their tables, and
# name what to take (a selection) instead of sending the tables back.

UPLOAD_LINE_COLUMNS = ("upload_id", "line", "server", "src_database", "src_schema", "src_table", "load_type", "fields",
                       "status", "errors")
JOB_SELECTION_COLUMNS = ("job_id", "scope", "server", "src_database", "tables", "filter", "source_kind", "source_id",
                         "created_by")


def insert_upload_sql() -> str:
    """An upload's header row. Params: upload_id, file_name, uploaded_by, columns, lines, tables, invalid."""
    return (f"insert into {SCHEMA}.table_upload (upload_id, file_name, uploaded_by, columns, lines, tables, invalid) "
            f"values (%s, %s, %s, %s, %s, %s, %s)")


def insert_upload_lines_sql(n: int) -> str:
    """n lines of an upload in one statement. Params: each line's UPLOAD_LINE_COLUMNS in turn (fields as JSON text)."""
    one = "(%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)"
    return f"insert into {SCHEMA}.table_upload_line ({', '.join(UPLOAD_LINE_COLUMNS)}) values {', '.join([one] * n)}"


def upload_sql() -> str:
    """An upload's header row. Param: upload_id."""
    return (f"select upload_id, file_name, uploaded_by, uploaded_at, columns, lines, tables, invalid "
            f"from {SCHEMA}.table_upload where upload_id = %s")


def upload_invalid_sql() -> str:
    """An upload's first invalid lines, for the page to say why. Params: upload_id, limit."""
    return (f"select line, concat_ws('.', src_database, src_schema, src_table) as table, errors "
            f"from {SCHEMA}.table_upload_line where upload_id = %s and status = 'invalid' order by line limit %s")


def upload_database_lines_sql() -> str:
    """An upload's database-only lines (a database alone: every table of it is discovered by name at Create), in file
    order. Param: upload_id."""
    return (f"select line, src_database, fields from {SCHEMA}.table_upload_line "
            f"where upload_id = %s and status = 'database' order by line")


def upload_databases_count_sql() -> str:
    """How many database-only lines an upload has. Param: upload_id."""
    return f"select count(*) as n from {SCHEMA}.table_upload_line where upload_id = %s and status = 'database'"


def upload_head_sql() -> str:
    """An upload's header and its first lines in file order, for the import's preview: one row per line, each carrying
    the header's columns and the line's number and fields. An upload with no line (or none at all) gives a row with no
    line, or no row. Params: limit, upload_id."""
    return (f"select u.columns, l.line, l.fields from {SCHEMA}.table_upload u "
            f"left join lateral (select line, fields from {SCHEMA}.table_upload_line where upload_id = u.upload_id "
            f"order by line limit %s) l on true where u.upload_id = %s")


def source_rows_sql(kind: str) -> str:
    """The tables of an upload (its ok lines; param upload_id) or of discoveries (param: list of discovery ids; a
    table found by more than one, its latest finding), each with its server, database, schema, table, load type, and
    what the CSV a plan reads takes from it: for an upload its line and fields, for a discovery its settings. The
    subquery the views and the selection read from."""
    if kind == "upload":
        return (f"select server, src_database, src_schema, src_table, load_type, null::text as cursor_column, "
                f"fields->>'primary_key' as primary_key, null::bigint as row_count, null::text as notes, line, fields "
                f"from {SCHEMA}.table_upload_line where upload_id = %s and status = 'ok'")
    return (f"select distinct on (lower(coalesce(d.src_connection, d.src_host)), lower(t.src_database), "
            f"lower(t.src_schema), lower(t.src_table)) "
            f"lower(coalesce(d.src_connection, d.src_host)) as server, t.src_database, t.src_schema, t.src_table, "
            f"t.load_type, t.cursor_column, t.primary_key, t.row_count, t.notes, 0 as line, "
            f"jsonb_build_object('src_connection', d.src_connection, "
            f"'src_host', case when d.src_connection is null then d.src_host end, "
            f"'src_port', case when d.src_connection is null then d.src_port::text end, "
            f"'secret_key', case when d.src_connection is null then d.secret_key end, "
            f"'src_database', t.src_database, 'src_schema', t.src_schema, 'src_table', t.src_table, "
            f"'select_list', t.select_list, 'primary_key', t.primary_key, 'cursor_column', t.cursor_column, "
            f"'load_type', t.load_type) as fields "
            f"from {SCHEMA}.discovered_table t join {SCHEMA}.discovery d "
            f"on d.discovery_id = t.discovery_id and d.src_database = t.src_database "
            f"where t.discovery_id = any(%s) "
            f"order by lower(coalesce(d.src_connection, d.src_host)), lower(t.src_database), lower(t.src_schema), "
            f"lower(t.src_table), t.discovered_at desc")


_LOAD_TYPE_COUNTS = ("count(*) filter (where load_type = 'ct') as ct, "
                     "count(*) filter (where load_type = 'cursor') as cursor, "
                     "count(*) filter (where load_type = 'full') as full, "
                     "count(*) filter (where coalesce(load_type, '') not in ('ct', 'cursor', 'full')) as unset")
_MATCHES = "position(lower(%s) in lower(r.src_database)) > 0"


def source_servers_sql(kind: str) -> str:
    """A source's servers: each one's databases, tables and tables by load type, over the databases matching the
    filter. Params: the source's (see source_rows_sql), filter."""
    return (f"select r.server, count(distinct lower(r.src_database)) as databases, count(*) as tables, "
            f"{_LOAD_TYPE_COUNTS} from ({source_rows_sql(kind)}) r where {_MATCHES} group by r.server order by r.server")


def source_databases_sql(kind: str) -> str:
    """One page of a source's databases matching the filter, by server and name: each one's tables and tables by load
    type. Params: the source's, filter, limit, offset."""
    return (f"select r.server, r.src_database, count(*) as tables, {_LOAD_TYPE_COUNTS} "
            f"from ({source_rows_sql(kind)}) r where {_MATCHES} group by r.server, r.src_database "
            f"order by r.server, lower(r.src_database) limit %s offset %s")


def source_tables_sql(kind: str) -> str:
    """One page of a database's tables in a source, by schema and name, those matching the search (in schema.table).
    Params: the source's, server, database, search, limit, offset. Each row carries the page's total as total."""
    return (f"select r.src_schema, r.src_table, r.load_type, r.cursor_column, r.primary_key, r.row_count, r.notes, "
            f"count(*) over () as total from ({source_rows_sql(kind)}) r "
            f"where lower(r.server) = lower(%s) and r.src_database = %s "
            f"and position(lower(%s) in lower(r.src_schema || '.' || r.src_table)) > 0 "
            f"order by lower(r.src_schema), lower(r.src_table) limit %s offset %s")


def selection_key(server: str, database: str, table: str = "") -> str:
    """A database (or a table of it, schema.table) as selected_rows_sql matches it: lower case, chr(31) between."""
    return "\x1f".join(str(part or "").lower() for part in ((server, database, table) if table else (server, database)))


_SELECTED = ("(%s and position(lower(%s) in lower(r.src_database)) > 0) "
             "or lower(r.server) || chr(31) || lower(r.src_database) = any(%s) "
             "or lower(r.server) || chr(31) || lower(r.src_database) || chr(31) || lower(r.src_schema || '.' || r.src_table) "
             "= any(%s)")


def selected_count_sql(kind: str) -> str:
    """How many tables a selection takes from a source. Params: the source's (see source_rows_sql), whole_server,
    filter, database keys, table keys (selection_key)."""
    return f"select count(*) as n from ({source_rows_sql(kind)}) r where {_SELECTED}"


def selected_rows_page_sql(kind: str) -> str:
    """One page of the tables a selection takes, in the order a plan reads them (server, database, line, schema, table),
    after the key of the last row read. Keyset paging: each page starts where the last one ended, with no offset, so a
    selection is read in pages of any size. Each row carries its key's lower-case parts (k_database, k_schema, k_table)
    for the next page. Params: the source's, whole_server, filter, database keys, table keys, after server, after
    k_database, after line, after k_schema, after k_table, limit (the first page's after values: '', '', -1, '', '')."""
    return (f"select r.server, r.src_database, r.src_schema, r.src_table, r.line, r.fields, "
            f"lower(r.src_database) as k_database, lower(r.src_schema) as k_schema, lower(r.src_table) as k_table "
            f"from ({source_rows_sql(kind)}) r where ({_SELECTED}) "
            f"and (r.server, lower(r.src_database), r.line, lower(r.src_schema), lower(r.src_table)) "
            f"> (%s, %s, %s, %s, %s) "
            f"order by r.server, lower(r.src_database), r.line, lower(r.src_schema), lower(r.src_table) limit %s")


def source_databases_after_sql(kind: str) -> str:
    """The next page of a source's databases matching the filter, in key order (server, lower-case name, name), after the
    key of the last one read: keyset paging, so each page starts where the last ended without an offset. Each row
    carries its server, database and tables. Params: the source's, filter, after server, after lower-case name, after
    name, limit (use ('', '', '') for the first page)."""
    return (f"select r.server, r.src_database, count(*) as tables from ({source_rows_sql(kind)}) r "
            f"where {_MATCHES} and (r.server, lower(r.src_database), r.src_database) > (%s, %s, %s) "
            f"group by r.server, r.src_database order by r.server, lower(r.src_database), r.src_database limit %s")


def database_rows_chunk_sql(kind: str) -> str:
    """database_rows_sql for several databases at once, ordered by database and then table. Params: the source's,
    then keys: a list of lower case server + chr(31) + database, one per database. Matches case-insensitively, so it
    reads the whole source; a page of the source listing takes database_rows_exact_sql instead."""
    return (f"select r.server, r.src_database, r.src_schema, r.src_table, r.line, r.fields "
            f"from ({source_rows_sql(kind)}) r "
            f"where lower(r.server) || chr(31) || lower(r.src_database) = any(%s) "
            f"order by lower(r.server), lower(r.src_database), lower(r.src_schema), lower(r.src_table)")


def database_rows_exact_sql() -> str:
    """The rows of several databases of an upload, each database matched exactly on the server and name the upload
    stores (what the source listing returns for it), so each one is an index range on table_upload_line_database_idx
    rather than a pass over the whole upload. Params: the servers (list), the databases (list), in the same order
    (one server and one database per database), then the upload id."""
    return (f"select t.server, t.src_database, t.src_schema, t.src_table, t.line, t.fields "
            f"from unnest(%s::text[], %s::text[]) as k(server, src_database) "
            f"cross join lateral (select server, src_database, src_schema, src_table, line, fields "
            f"from {SCHEMA}.table_upload_line where upload_id = %s and status = 'ok' "
            f"and server = k.server and src_database = k.src_database) t "
            f"order by lower(t.server), lower(t.src_database), lower(t.src_schema), lower(t.src_table)")


def database_rows_sql(kind: str) -> str:
    """Every table of one database of a source, in the order a plan reads them, with the fields its CSV line takes.
    Params: the source's, server, database (both compared lower case)."""
    return (f"select r.server, r.src_database, r.src_schema, r.src_table, r.line, r.fields from ({source_rows_sql(kind)}) r "
            f"where lower(r.server) = lower(%s) and lower(r.src_database) = lower(%s) "
            f"order by lower(r.src_schema), lower(r.src_table)")


def selected_servers_sql(kind: str) -> str:
    """A selection's servers: each one's databases and tables it takes, and its tables by load type (load_unset: a line
    that leaves it to the job), in the order of the servers. One aggregate in Lakebase: only these few rows reach the
    app. Params: the same as selected_rows_sql (the source's, whole_server, filter, database keys, table keys)."""
    return (f"select r.server, count(distinct lower(r.src_database)) as databases, count(*) as tables, "
            f"count(*) filter (where r.load_type = 'ct') as load_ct, "
            f"count(*) filter (where r.load_type = 'cursor') as load_cursor, "
            f"count(*) filter (where r.load_type = 'full') as load_full, "
            f"count(*) filter (where r.load_type is null or r.load_type not in ('ct', 'cursor', 'full')) as load_unset "
            f"from ({source_rows_sql(kind)}) r where {_SELECTED} group by r.server order by r.server")


def selected_databases_sql(kind: str) -> str:
    """A selection's databases with the tables each takes, for spreading them evenly over the job's groups
    (placement.even): one row per database, (server, src_database, tables), in key order. Params: as selected_servers_sql."""
    return (f"select r.server, lower(r.src_database) as src_database, count(*) as tables "
            f"from ({source_rows_sql(kind)}) r where {_SELECTED} group by r.server, lower(r.src_database) "
            f"order by r.server, lower(r.src_database)")


def insert_job_selection_sql(n: int) -> str:
    """n parts of what a job was made from, in one statement. Params: each part's JOB_SELECTION_COLUMNS in turn."""
    one = "(" + ", ".join(["%s"] * len(JOB_SELECTION_COLUMNS)) + ")"
    return f"insert into {SCHEMA}.job_selection ({', '.join(JOB_SELECTION_COLUMNS)}) values {', '.join([one] * n)}"


def job_selection_sql() -> str:
    """What a job was made from, part by part, oldest first. Param: job_id."""
    return (f"select id, {', '.join(JOB_SELECTION_COLUMNS)}, created_at from {SCHEMA}.job_selection "
            f"where job_id = %s order by created_at, id")

# ---- Databricks runs the app tracks (lakefed_ingest.databricks_run, written by run_poller) ----------------------------

DATABRICKS_RUN_COLUMNS = ("run_id, job_id, kind, schedule_id, task_collection, trigger, parameters, life_cycle_state, "
                          "result_state, state_message, run_page_url, started_at, ended_at, seen_at")


def databricks_run_params(row: dict) -> dict:
    """A run's row as the named parameters of databricks_run_upsert_sql."""
    keys = ("run_id", "job_id", "kind", "schedule_id", "task_collection", "trigger", "parameters", "life_cycle_state",
            "result_state", "state_message", "run_page_url", "started_at", "ended_at")
    return {k: row.get(k) for k in keys}


# A run's life cycle states before it starts stopping. Right after a cancel the Jobs API still says one of these, so a
# run the app marked TERMINATING (mark_terminating_sql) keeps that until the API names an ended state.
NOT_YET_STOPPING_STATES = ("QUEUED", "PENDING", "RUNNING", "BLOCKED", "WAITING_FOR_RETRY")


def databricks_run_upsert_sql() -> str:
    """Insert or update one Databricks run as the poller sees it. Params: the named fields of databricks_run_params.
    A run recorded as TERMINATING stays so while the API still lists it as going; any other state replaces it."""
    columns = ("run_id, job_id, kind, schedule_id, task_collection, trigger, parameters, life_cycle_state, "
               "result_state, state_message, run_page_url, started_at, ended_at, seen_at")
    values = ("%(run_id)s, %(job_id)s, %(kind)s, %(schedule_id)s, %(task_collection)s, %(trigger)s, "
              "%(parameters)s::jsonb, %(life_cycle_state)s, %(result_state)s, %(state_message)s, %(run_page_url)s, "
              "%(started_at)s, %(ended_at)s, now()")
    going = ", ".join(f"'{state}'" for state in NOT_YET_STOPPING_STATES)
    return (f"insert into {SCHEMA}.databricks_run as stored ({columns}) values ({values}) "
            "on conflict (run_id) do update set job_id = excluded.job_id, kind = excluded.kind, "
            "schedule_id = excluded.schedule_id, task_collection = excluded.task_collection, trigger = excluded.trigger, "
            "parameters = excluded.parameters, life_cycle_state = case when stored.life_cycle_state = 'TERMINATING' "
            f"and excluded.life_cycle_state in ({going}) then 'TERMINATING' else excluded.life_cycle_state end, "
            "result_state = excluded.result_state, state_message = excluded.state_message, "
            "run_page_url = excluded.run_page_url, started_at = excluded.started_at, ended_at = excluded.ended_at, "
            "seen_at = now()")


def databricks_run_states_sql() -> str:
    """The recorded state of some Databricks runs. Param: list of run ids."""
    return (f"select {DATABRICKS_RUN_COLUMNS}, cancel_requested_at from {SCHEMA}.databricks_run "
            "where run_id = any(%s)")


def active_databricks_runs_sql() -> str:
    """Every recorded Databricks run still in one of the given life cycle states. Param: list of states."""
    return f"select {DATABRICKS_RUN_COLUMNS} from {SCHEMA}.databricks_run where life_cycle_state = any(%s)"


def mark_cancel_requested_sql() -> str:
    """Show runs the app cancelled as terminating, while they are still active, and keep when the cancel was first
    asked for (a second Cancel doesn't restart the clock the poller's fallback close runs on). Params: run ids,
    active states."""
    return (f"update {SCHEMA}.databricks_run set life_cycle_state = 'TERMINATING', "
            "cancel_requested_at = coalesce(cancel_requested_at, now()), seen_at = now() "
            "where run_id = any(%s) and life_cycle_state = any(%s)")


def cancel_requested_runs_sql() -> str:
    """Which of the given Databricks run ids a Cancel asked Databricks to stop, ended or not yet. Param: run ids."""
    return f"select run_id from {SCHEMA}.databricks_run where run_id = any(%s) and cancel_requested_at is not null"


def databricks_runs_of_job_sql() -> str:
    """A job's recorded runs, newest first: its active ones only when active_only is true. Params: job_id,
    active_only, active states, limit."""
    return (f"select {DATABRICKS_RUN_COLUMNS} from {SCHEMA}.databricks_run where job_id = %(job_id)s "
            "and (not %(active_only)s or life_cycle_state = any(%(active_states)s)) "
            "order by started_at desc nulls last, run_id desc limit %(limit)s")


def skipped_databricks_runs_sql() -> str:
    """When a wrapper job's skipped runs started, in a window. Params: job_id, since, until."""
    return (f"select started_at from {SCHEMA}.databricks_run where job_id = %s "
            "and (life_cycle_state = 'SKIPPED' or result_state = 'SKIPPED') "
            "and started_at >= %s and started_at <= %s order by started_at")


def databricks_run_table_sql() -> str:
    """1 when the poller's table exists yet (the app reads the Jobs API until it does)."""
    return (f"select to_regclass('{SCHEMA}.databricks_run') is not null and exists (select 1 from pg_attribute "
            f"where attrelid = to_regclass('{SCHEMA}.databricks_run') and attname = 'cancel_requested_at' "
            "and not attisdropped) as ready")
