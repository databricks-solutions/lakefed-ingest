-- Lakebase (OLTP Postgres) schema for multi-tenant ingestion.
-- Per ADR-0004, Lakebase holds the hot per-(db,table) checkpoint, phase/status, and reseed
-- queue (concurrent point writes + low-latency point reads from many workers, plus a future
-- workbench-app backend). Per the consolidation decision, the cold task config/placement is
-- co-located here too (the `control` table) so there is a single metadata store — no Delta
-- control table. Config reads and all state writes run on the job cluster via the Postgres
-- client. The multi-tenant path is warehouse-free: everything runs on the cluster.
--
-- Naming: `control` is the strategy-agnostic framework config table; `ct_checkpoint` and
-- `reseed_queue` are specific to the Change-Tracking (CT) strategy (the first strategy).
--
-- Idempotent: re-applying this file (lakefed_ingest_mt_setup) migrates an existing instance in
-- place (add-column-if-not-exists / drop-and-re-add constraints / non-destructive backfills).
-- Statements are split on ';' by apply_lakebase_schema.ipynb, so no DO $$ blocks here.

create schema if not exists lakefed_ingest_mt;

-- Cold task config / placement map. One row per (tenant db, table) ingestion task.
-- Mirrors the production control-table columns the reused prelude needs. Array-typed columns
-- (primary_key, sink_cluster_cols) are stored as comma-separated text to match what the
-- reused get_task output produced (downstream splits on ',').
create table if not exists lakefed_ingest_mt.control (
    id                    bigserial primary key,
    job_name              text        not null,
    task_collection       text        not null,
    src_type              text        not null default 'sqlserver',
    src_connection        text,                               -- legacy: UC connection (host lookup only)
    src_host              text,                               -- SQL Server host (JDBC; no UC connection)
    src_port              int         default 1433,
    secret_key            text,                               -- per-DB creds secret key (JSON {user,password})
    src_database          text,                               -- tenant database on the instance
    src_catalog           text,                               -- NULL for the CT/remote_query path
    src_schema            text        not null,
    src_table             text        not null,
    sink_catalog          text        not null,
    sink_schema           text        not null,
    sink_table            text        not null,
    enable_iceberg_reads  boolean     not null default false,
    primary_key           text        not null,               -- comma-separated; required for CT
    sink_cluster_cols     text,                               -- comma-separated or NULL
    load_type             text        not null default 'ct'
                          check (load_type in ('full', 'incremental', 'ct')),
    load_partitioned      boolean     not null default false,
    partition_col         text,                               -- partitioned seed: numeric/date/datetime col
    partition_size_mb     int,                                -- partitioned seed: target MB per partition
    select_list           text        not null,
    use_remote_query      boolean     not null default false,  -- unused by the JDBC sweep
    ct_enabled            boolean     not null default true,
    source_instance_group text,                               -- placement
    tier                  text,                               -- placement: cadence/SLA tier
    priority              int,                                -- placement: sweep priority
    warehouse_id          text,                               -- unused (multi-tenant path is warehouse-free)
    task_enabled          boolean     not null default true,
    -- CT tasks need a PK, a SQL Server source, and a resolvable host (src_host, or a legacy
    -- UC connection). Re-declared below so re-applying the file updates existing tables.
    constraint mt_valid_ct_metadata check (
        load_type <> 'ct'
        or (length(coalesce(primary_key, '')) > 0 and src_type = 'sqlserver'
            and (src_host is not null or src_connection is not null))
    ),
    -- Partitioned seed needs a partition column and a positive partition size.
    constraint mt_valid_partitioning check (
        not load_partitioned or (partition_col is not null and coalesce(partition_size_mb, 0) > 0)
    )
);

-- Migrate an existing control table: JDBC host/port + per-DB secret key; relax the CT constraint
-- (the governed remote_query transport is no longer required).
alter table lakefed_ingest_mt.control add column if not exists src_host text;
alter table lakefed_ingest_mt.control add column if not exists src_port int default 1433;
alter table lakefed_ingest_mt.control add column if not exists secret_key text;
alter table lakefed_ingest_mt.control alter column use_remote_query set default false;
alter table lakefed_ingest_mt.control drop constraint if exists mt_valid_ct_metadata;
alter table lakefed_ingest_mt.control add constraint mt_valid_ct_metadata check (
    load_type <> 'ct'
    or (length(coalesce(primary_key, '')) > 0 and src_type = 'sqlserver'
        and (src_host is not null or src_connection is not null))
);

-- Migrate an existing control table: size-bounded partitioned seed settings.
alter table lakefed_ingest_mt.control add column if not exists partition_col text;
alter table lakefed_ingest_mt.control add column if not exists partition_size_mb int;
alter table lakefed_ingest_mt.control drop constraint if exists mt_valid_partitioning;
alter table lakefed_ingest_mt.control add constraint mt_valid_partitioning check (
    not load_partitioned or (partition_col is not null and coalesce(partition_size_mb, 0) > 0)
);

create index if not exists control_collection_idx
    on lakefed_ingest_mt.control (task_collection)
    where task_enabled = true;

-- Change-Tracking checkpoint, one row per control task (keyed by control.id). Keying by the
-- control id (not db/schema/table) keeps same-named databases on different servers from
-- colliding; the src_* columns are kept for readability.
create table if not exists lakefed_ingest_mt.ct_checkpoint (
    control_id     bigint      not null,
    src_database   text        not null,
    src_schema     text        not null,
    src_table      text        not null,
    ct_version     bigint      not null default 0,   -- last successfully merged CT version
    phase          text        not null default 'seeding'
                   check (phase in ('seeding', 'cdc')),
    status         text        not null default 'ok', -- ok | running | failed | quarantined
    detail         text,                              -- last error / note
    updated_at     timestamptz not null default now(),
    constraint ct_checkpoint_control_pk primary key (control_id)
);

-- Migrate an existing (db,schema,table)-keyed ct_checkpoint to control_id keying, non-destructively:
-- add the column, backfill from control ONLY where exactly one control row matches the source
-- table (same-named DBs on different servers are ambiguous — guessing could resume CDC from another
-- server's CT version), drop the old composite PK, and add a unique index so ON CONFLICT
-- (control_id) works. Unmatched/ambiguous rows stay NULL (orphaned, harmless — that table re-seeds).
alter table lakefed_ingest_mt.ct_checkpoint add column if not exists control_id bigint;
update lakefed_ingest_mt.ct_checkpoint cc set control_id = m.id
    from (select src_database, src_schema, src_table, min(id) as id
          from lakefed_ingest_mt.control group by src_database, src_schema, src_table
          having count(*) = 1) m
    where cc.control_id is null and cc.src_database = m.src_database
      and cc.src_schema = m.src_schema and cc.src_table = m.src_table;
alter table lakefed_ingest_mt.ct_checkpoint drop constraint if exists ct_checkpoint_pkey;
create unique index if not exists ct_checkpoint_control_uq
    on lakefed_ingest_mt.ct_checkpoint (control_id);

-- Tables awaiting a full reseed (CT retention lapsed, or first-time enablement).
-- Processed by the sweep's reseed handling (phase flips back to seeding).
create table if not exists lakefed_ingest_mt.reseed_queue (
    id           bigserial   primary key,
    control_id   bigint,                               -- -> control.id
    src_database text        not null,
    src_schema   text        not null,
    src_table    text        not null,
    reason       text,                                 -- e.g. 'retention_miss'
    enqueued_at  timestamptz not null default now(),
    processed    boolean     not null default false,
    processed_at timestamptz
);

alter table lakefed_ingest_mt.reseed_queue add column if not exists control_id bigint;

create index if not exists reseed_queue_unprocessed_idx
    on lakefed_ingest_mt.reseed_queue (enqueued_at)
    where processed = false;

-- Freshness/lag: when did this table last ingest successfully. ADD COLUMN IF NOT EXISTS so
-- re-applying the schema backfills the column onto an already-created ct_checkpoint.
alter table lakefed_ingest_mt.ct_checkpoint add column if not exists last_success_at timestamptz;

-- ---------------------------------------------------------------------------------------
-- Observability / telemetry. App-ready: a future Databricks App reads these for live status.
-- The consolidated sweep ingests many tables inside ONE job task, so per-table progress is not
-- visible in the Jobs UI. These tables re-create that visibility via concurrent point writes
-- (one ingest_event row per (sweep, table): INSERT at start, UPDATE at finish), queryable live
-- through the Lakebase UC catalog (see docs/monitoring.md).
-- ---------------------------------------------------------------------------------------

-- One row per sweep run (one lakefed_ingest_mt_sweep run over a task_collection).
create table if not exists lakefed_ingest_mt.sweep_run (
    sweep_id        bigserial   primary key,
    task_collection text        not null,
    job_run_id      text,
    cluster_id      text,
    parallelism     int,
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    status          text        not null default 'running',   -- running | completed | failed
    total           int,
    ok              int,
    failed          int,
    skipped         int,
    reseeded        int,
    detail          text
);

create index if not exists sweep_run_collection_idx
    on lakefed_ingest_mt.sweep_run (task_collection, started_at);

-- One row per (sweep, table): per-table telemetry for a sweep.
create table if not exists lakefed_ingest_mt.ingest_event (
    id              bigserial   primary key,
    sweep_id        bigint,                                   -- -> sweep_run.sweep_id
    task_collection text,
    control_id      bigint,                                   -- -> control.id
    src_database    text,
    src_schema      text,
    src_table       text,
    sink_fqn        text,
    action          text,                                     -- seed | increment | reseed | skip
    phase           text,
    stage           text,                                     -- ensure_sink | read | merge | checkpoint | seed
    status          text        not null default 'running',   -- running | ok | failed | skipped
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    duration_ms     bigint,
    rows_read       bigint,
    rows_merged     bigint,
    ct_version_from bigint,
    ct_version_to   bigint,
    pool            text,
    error           text,
    retry_count     int         not null default 0
);

create index if not exists ingest_event_sweep_idx
    on lakefed_ingest_mt.ingest_event (sweep_id);
create index if not exists ingest_event_failed_idx
    on lakefed_ingest_mt.ingest_event (sweep_id)
    where status = 'failed';
create index if not exists ingest_event_collection_idx
    on lakefed_ingest_mt.ingest_event (task_collection, finished_at);

-- NOTE: the two-level controller/batching was removed — the consolidated sweep ingests a whole
-- task_collection in one task (see copy_data_sweep.ipynb), so there is no `task_batch` table.
-- A `task_batch` left over from an earlier deploy is harmless/orphaned; we don't drop it here.
