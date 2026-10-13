-- Lakebase (OLTP Postgres) schema for lakefed_ingest.
-- Per ADR-0004, Lakebase holds the hot per-(db,table) checkpoint, phase/status, and reseed
-- queue (concurrent point writes + low-latency point reads from many workers, plus a future
-- workbench-app backend). Per the consolidation decision, the cold task config/placement is
-- co-located here too (the `control` table) so there is a single metadata store — no Delta
-- control table. Config reads and all state writes run on the job cluster via the Postgres
-- client. Warehouse-free: everything runs on the cluster.
--
-- Naming: `control` is the framework config table. `control.src_type` selects the SOURCE ADAPTER
-- (source_adapters.py) and `control.load_type` the LOAD STRATEGY (load_strategies.py). `checkpoint`
-- is generic: `strategy` names the strategy that wrote it and `position` is that strategy's opaque
-- resume position (CT: the database CT version as text; cursor: JSON {col, type, value}). `reseed_queue`
-- is used by the CT and cursor strategies.
--
-- Idempotent: re-applying this file migrates an existing instance in place
-- (add-column-if-not-exists / drop-and-re-add constraints / non-destructive backfills). It is applied
-- in one transaction by state_store.StateStore.migrate: from the setup job, and from an ingestion run
-- that finds the database behind state_store.SCHEMA_VERSION (bump it with every change here).
-- Table / column / index RENAMES (ct_checkpoint -> checkpoint, sweep_run -> ingest_run, ...) have no
-- IF EXISTS form in Postgres, so the migration runs them first, guarded, from state_store.RENAMES.
-- Statements are split on ';', so no DO $$ blocks here, and every statement must be valid in a
-- transaction (no CREATE INDEX CONCURRENTLY). Every upgrade re-applies this whole file from any earlier
-- version: keep each statement idempotent and additive (docs/lakebase_schema.md, "Changing the schema";
-- checked by tests/state_store_test.py).

create schema if not exists lakefed_ingest;

-- Schema versions applied (state_store.SCHEMA_VERSION), one row each. An ingestion run whose code
-- needs a newer version than the newest here applies this file itself before it runs.
create table if not exists lakefed_ingest.schema_version (
    version     integer     primary key,
    applied_at  timestamptz not null default now(),
    applied_by  text        not null default current_user
);

-- Cold task config / placement map. One row per (source db, table) ingestion task.
-- Array-typed columns (primary_key, sink_cluster_cols) are stored as comma-separated text.
create table if not exists lakefed_ingest.control (
    id                    bigserial primary key,
    job_name              text,                               -- optional label (unused by this version)
    task_collection       text        not null,
    src_type              text        not null default 'sqlserver',  -- source adapter
    src_connection        text,                               -- UC connection: read through it (use_remote_query), else legacy host lookup
    src_host              text,                               -- source host (JDBC; no UC connection)
    src_port              int,                                -- NULL -> the adapter's default port
    secret_key            text,                               -- per-DB creds secret key (JSON {user,password})
    src_database          text,                               -- database (Oracle: service name)
    src_catalog           text,                               -- unused by the JDBC sweep
    src_schema            text        not null,
    src_table             text        not null,
    sink_catalog          text        not null,
    sink_schema           text        not null,
    sink_table            text        not null,
    client_id             text,                               -- set: consolidated sink shared by client_id
    enable_iceberg_reads  boolean     not null default false,
    primary_key           text,                               -- comma-separated; required for ct/cursor/cdc
    sink_cluster_cols     text,                               -- comma-separated or NULL
    load_type             text        not null default 'ct',  -- load strategy: ct | cdc | cursor | full
    load_partitioned      boolean     not null default false,
    partition_col         text,                               -- partitioned seed: numeric/date/datetime col
    partition_size_mb     int,                                -- partitioned seed: target MB per partition
    select_list           text        not null,
    use_remote_query      boolean     not null default false,  -- read through src_connection (remote_query), not JDBC
    ct_enabled            boolean     not null default true,
    cursor_column         text,                               -- load_type cursor: high-watermark column
    cdc_capture_instance  text,                               -- load_type cdc: capture instance / slot
    jdbc_options          text,                               -- extra Spark JDBC read options (JSON object)
    source_instance_group text,                               -- placement
    tier                  text,                               -- placement: cadence/SLA tier
    priority              int,                                -- placement: sweep priority
    warehouse_id          text,                               -- unused (this version is warehouse-free)
    task_enabled          boolean     not null default true
);

-- Migrate an existing control table: JDBC host/port + per-DB secret key.
alter table lakefed_ingest.control add column if not exists src_host text;
alter table lakefed_ingest.control add column if not exists src_port int;
alter table lakefed_ingest.control add column if not exists secret_key text;
alter table lakefed_ingest.control alter column use_remote_query set default false;
-- The port default is the adapter's (1433 / 1521 / 5432 / 3306), not a column default.
alter table lakefed_ingest.control alter column src_port drop default;
-- Size-bounded partitioned seed settings.
alter table lakefed_ingest.control add column if not exists partition_col text;
alter table lakefed_ingest.control add column if not exists partition_size_mb int;
-- Load-strategy settings. A full load needs no primary key.
alter table lakefed_ingest.control add column if not exists cursor_column text;
alter table lakefed_ingest.control add column if not exists cdc_capture_instance text;
alter table lakefed_ingest.control add column if not exists jdbc_options text;
alter table lakefed_ingest.control alter column primary_key drop not null;
-- job_name is an optional label in this version (the job orchestration version uses it).
alter table lakefed_ingest.control alter column job_name drop not null;
-- Consolidated sinks (optional): rows with client_id set share one sink table, each writing only its
-- own client's rows (MERGE on client_id + PK, REPLACE WHERE client_id for seeds / full reloads).
alter table lakefed_ingest.control add column if not exists client_id text;
-- Write mode. 'merge' (default) keeps the sink in step with the source: PK MERGE for changes,
-- client-scoped replace for seeds and full reloads. 'append' lands every change, and every seed or
-- full snapshot, as new rows tagged __change_op / __change_version / __extracted_at / __run_id: a
-- change log that many source databases can fan in to (with client_id) for downstream processing.
alter table lakefed_ingest.control add column if not exists write_mode text not null default 'merge';
-- Identity columns written on every row next to __client_id__, as a JSON object {column: value}
-- (for example the tenant name or the source server).
alter table lakefed_ingest.control add column if not exists client_attrs text;

-- Constraints are (re-)declared here so re-applying the file updates existing tables. Older
-- deploys had an inline load_type check (auto-named) and `mt_`-prefixed names; drop those too.
alter table lakefed_ingest.control drop constraint if exists control_load_type_check;
alter table lakefed_ingest.control drop constraint if exists valid_load_type;
alter table lakefed_ingest.control add constraint valid_load_type check (
    load_type in ('ct', 'cdc', 'cursor', 'full')
);
alter table lakefed_ingest.control drop constraint if exists valid_src_type;
alter table lakefed_ingest.control add constraint valid_src_type check (
    src_type in ('sqlserver', 'oracle', 'postgresql', 'mysql')
);
-- CT tasks need a PK, a SQL Server source, and a source to read: src_host, or a UC connection.
alter table lakefed_ingest.control drop constraint if exists mt_valid_ct_metadata;
alter table lakefed_ingest.control drop constraint if exists valid_ct_metadata;
alter table lakefed_ingest.control add constraint valid_ct_metadata check (
    load_type <> 'ct'
    or (length(coalesce(primary_key, '')) > 0 and src_type = 'sqlserver'
        and (src_host is not null or src_connection is not null))
);
-- Cursor tasks need the high-watermark column and a PK (increments are MERGEd by PK).
alter table lakefed_ingest.control drop constraint if exists valid_cursor_metadata;
alter table lakefed_ingest.control add constraint valid_cursor_metadata check (
    load_type <> 'cursor'
    or (length(coalesce(cursor_column, '')) > 0 and length(coalesce(primary_key, '')) > 0)
);
-- CDC tasks need a PK (I/U/D changes are MERGEd by PK).
alter table lakefed_ingest.control drop constraint if exists valid_cdc_metadata;
alter table lakefed_ingest.control add constraint valid_cdc_metadata check (
    load_type <> 'cdc' or length(coalesce(primary_key, '')) > 0
);
-- Partitioned seed needs a partition column and a positive partition size.
alter table lakefed_ingest.control drop constraint if exists mt_valid_partitioning;
alter table lakefed_ingest.control drop constraint if exists valid_partitioning;
alter table lakefed_ingest.control add constraint valid_partitioning check (
    not load_partitioned or (partition_col is not null and coalesce(partition_size_mb, 0) > 0)
);
-- A read through a Unity Catalog connection (remote_query) needs the connection; no host or secret.
alter table lakefed_ingest.control drop constraint if exists valid_remote_query;
alter table lakefed_ingest.control add constraint valid_remote_query check (
    not use_remote_query or src_connection is not null
);
-- jdbc_options, when set, must be a JSON object.
alter table lakefed_ingest.control drop constraint if exists valid_jdbc_options;
alter table lakefed_ingest.control add constraint valid_jdbc_options check (
    jdbc_options is null or jsonb_typeof(jdbc_options::jsonb) = 'object'
);

-- Append is implemented for change tracking and full reloads.
alter table lakefed_ingest.control drop constraint if exists valid_write_mode;
alter table lakefed_ingest.control add constraint valid_write_mode check (
    write_mode in ('merge', 'append') and (write_mode = 'merge' or load_type in ('ct', 'full'))
);
-- client_attrs, when set, must be a JSON object.
alter table lakefed_ingest.control drop constraint if exists valid_client_attrs;
alter table lakefed_ingest.control add constraint valid_client_attrs check (
    client_attrs is null or jsonb_typeof(client_attrs::jsonb) = 'object'
);

-- client_id becomes a Delta predicate literal: a restricted character set (same rule as ct_engine).
alter table lakefed_ingest.control drop constraint if exists valid_client_id;
alter table lakefed_ingest.control add constraint valid_client_id check (
    client_id is null or client_id ~ '^[A-Za-z0-9_.:-]{1,128}$'
);
-- One writer per sink slice: a dedicated sink (client_id NULL) has exactly one control row, and a
-- consolidated sink at most one row per client. (A sink mixing a dedicated row with consolidated
-- rows is rejected at plan time, per table.) Fails to apply if existing rows already collide.
create unique index if not exists control_sink_client_uq
    on lakefed_ingest.control (lower(sink_catalog), lower(sink_schema), lower(sink_table),
                               coalesce(client_id, ''));

create index if not exists control_collection_idx
    on lakefed_ingest.control (task_collection)
    where task_enabled = true;
-- The dedicated rows, by sink: each sweep's mixed-sink check starts here (state_store.mixed_sinks_sql).
create index if not exists control_dedicated_sink_idx
    on lakefed_ingest.control (lower(sink_catalog), lower(sink_schema), lower(sink_table))
    where client_id is null;

-- Generic checkpoint, one row per control task (keyed by control.id). Keying by the control id
-- (not db/schema/table) keeps same-named databases on different servers from colliding; the src_*
-- columns are kept for readability. `position` is opaque text owned by `strategy`.
create table if not exists lakefed_ingest.checkpoint (
    control_id     bigint      not null,
    src_database   text        not null,
    src_schema     text        not null,
    src_table      text        not null,
    strategy       text        not null default 'ct',  -- load strategy that wrote the position
    position       text,                              -- resume position (CT: CT version as text)
    phase          text        not null default 'seeding',  -- seeding | incremental (ct, cursor) | loaded (full)
    status         text        not null default 'ok', -- ok | running | failed | quarantined
    detail         text,                              -- last error / note
    updated_at     timestamptz not null default now(),
    constraint checkpoint_control_pk primary key (control_id)
);

-- Migrate an existing (db,schema,table)-keyed checkpoint to control_id keying, non-destructively:
-- add the column, backfill from control ONLY where exactly one control row matches the source
-- table (same-named DBs on different servers are ambiguous — guessing could resume CDC from another
-- server's CT version), drop the old composite PK, and add a unique index so ON CONFLICT
-- (control_id) works. Unmatched/ambiguous rows stay NULL (orphaned, harmless — that table re-seeds).
alter table lakefed_ingest.checkpoint add column if not exists control_id bigint;
update lakefed_ingest.checkpoint cc set control_id = m.id
    from (select src_database, src_schema, src_table, min(id) as id
          from lakefed_ingest.control group by src_database, src_schema, src_table
          having count(*) = 1) m
    where cc.control_id is null and cc.src_database = m.src_database
      and cc.src_schema = m.src_schema and cc.src_table = m.src_table;
alter table lakefed_ingest.checkpoint drop constraint if exists ct_checkpoint_pkey;
create unique index if not exists checkpoint_control_uq
    on lakefed_ingest.checkpoint (control_id);

-- Migrate the CT-specific checkpoint to the generic one: the CT version (bigint, renamed to
-- `position` by the applier) becomes text, and every pre-existing row was written by CT.
alter table lakefed_ingest.checkpoint add column if not exists strategy text not null default 'ct';
alter table lakefed_ingest.checkpoint alter column position drop default;
alter table lakefed_ingest.checkpoint alter column position drop not null;
alter table lakefed_ingest.checkpoint alter column position type text using position::text;
alter table lakefed_ingest.checkpoint drop constraint if exists ct_checkpoint_phase_check;
alter table lakefed_ingest.checkpoint drop constraint if exists valid_phase;
-- The steady-state phase was called 'cdc' before; it is 'incremental' (shared by ct and cursor, and
-- distinct from the 'cdc' load type). Rename in place before re-adding the check (ingest_event
-- history is renamed after that table's DDL, below).
update lakefed_ingest.checkpoint set phase = 'incremental' where phase = 'cdc';
alter table lakefed_ingest.checkpoint add constraint valid_phase check (
    phase in ('seeding', 'incremental', 'loaded')
);

-- Freshness/lag: when did this table last ingest successfully.
alter table lakefed_ingest.checkpoint add column if not exists last_success_at timestamptz;
-- CT begin_version recorded at seed time; a different current value means CT was re-enabled on the
-- table since (changes made while it was off are invisible to CHANGETABLE), so the table re-seeds.
alter table lakefed_ingest.checkpoint add column if not exists ct_begin_version bigint;

-- The select_list the sink was last written with. The sweep compares it with control.select_list:
-- a column ADDED to the control row means the table is reseeded (CT returns changed rows only, so
-- history needs a backfill). NULL = not recorded yet (legacy rows: the sweep checks the sink once).
alter table lakefed_ingest.checkpoint add column if not exists select_list text;

-- Append writes (control.write_mode 'append') are not idempotent the way a MERGE is, so before
-- appending, the run records the position the append brings the sink to and its own run_id. The
-- checkpoint advance clears both. If a run stops in between, the next run looks in the sink for rows
-- with that __run_id: found, the append committed and the checkpoint advances; not found, it reads again.
alter table lakefed_ingest.checkpoint add column if not exists pending_position text;
alter table lakefed_ingest.checkpoint add column if not exists pending_run_id bigint;

-- Tables awaiting a full reseed (CT retention lapsed, schema change, or CT versions reset).
-- Processed by the sweep's reseed handling (phase flips back to seeding).
create table if not exists lakefed_ingest.reseed_queue (
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

alter table lakefed_ingest.reseed_queue add column if not exists control_id bigint;

create index if not exists reseed_queue_unprocessed_idx
    on lakefed_ingest.reseed_queue (enqueued_at)
    where processed = false;

-- ---------------------------------------------------------------------------------------
-- Observability / telemetry. App-ready: a future Databricks App reads these for live status.
-- One ingestion run processes many tables inside ONE job task, so per-table progress is not
-- visible in the Jobs UI. These tables re-create that visibility via concurrent point writes
-- (one ingest_event row per (run, table): INSERT at start, UPDATE at finish), queryable live
-- through the Lakebase UC catalog (see docs/monitoring.md).
-- ---------------------------------------------------------------------------------------

-- One row per ingestion run (one lakefed_ingest run over a task_collection).
create table if not exists lakefed_ingest.ingest_run (
    run_id          bigserial   primary key,
    task_collection text        not null,
    job_run_id      text,
    cluster_id      text,
    parallelism     int,
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    status          text        not null default 'running',   -- running | completed | failed | skipped
    total           int,
    ok              int,
    failed          int,
    skipped         int,
    reseeded        int,
    detail          text
);

create index if not exists ingest_run_collection_idx
    on lakefed_ingest.ingest_run (task_collection, started_at);

-- One row per (run, table): per-table telemetry for a run.
create table if not exists lakefed_ingest.ingest_event (
    id              bigserial   primary key,
    run_id          bigint,                                   -- -> ingest_run.run_id
    task_collection text,
    control_id      bigint,                                   -- -> control.id
    src_database    text,
    src_schema      text,
    src_table       text,
    sink_fqn        text,
    action          text,                                     -- seed | increment | reseed | full
    phase           text,
    stage           text,                                     -- plan | detect | ensure_sink | read | seed | merge | load | checkpoint
    status          text        not null default 'running',   -- running | ok | failed | skipped | retried (an attempt tried again)
    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    duration_ms     bigint,
    rows_read       bigint,
    rows_merged     bigint,
    position_from   text,                                     -- checkpoint position before / after
    position_to     text,
    pool            text,
    error           text,
    retry_count     int         not null default 0          -- earlier attempts of this table in the run (table_retries)
);

-- CT versions were bigint before the generic checkpoint; positions are text.
alter table lakefed_ingest.ingest_event alter column position_from type text using position_from::text;
alter table lakefed_ingest.ingest_event alter column position_to type text using position_to::text;
-- Phase 'cdc' was renamed 'incremental' (see checkpoint above); keep history consistent.
update lakefed_ingest.ingest_event set phase = 'incremental' where phase = 'cdc';

create index if not exists ingest_event_run_idx
    on lakefed_ingest.ingest_event (run_id);
create index if not exists ingest_event_failed_idx
    on lakefed_ingest.ingest_event (run_id)
    where status = 'failed';
create index if not exists ingest_event_collection_idx
    on lakefed_ingest.ingest_event (task_collection, finished_at);

-- NOTE: the two-level controller/batching was removed — one run ingests a whole task_collection in
-- one task (see ingest.ipynb), so there is no `task_batch` table. A `task_batch` left over from an
-- earlier deploy is harmless/orphaned; we don't drop it here.

-- ---------------------------------------------------------------------------------------
-- Table discovery (bulk onboarding). The lakefed_ingest_discover job lists a source database's
-- tables with recommended control-row settings (discovery.py); the operations app shows them for
-- review and creates control rows from the selection. One `discovery` row per (discovery, database)
-- reports status / errors; `discovered_table` holds one row per table found.
-- ---------------------------------------------------------------------------------------
create table if not exists lakefed_ingest.discovery (
    discovery_id  text        not null,
    src_type      text,
    src_host      text,
    src_port      int,
    src_database  text        not null,
    secret_key    text,
    status        text        not null default 'running',  -- running | ok | failed
    error         text,
    tables        int,
    requested_by  text,
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    constraint discovery_pk primary key (discovery_id, src_database)
);

create table if not exists lakefed_ingest.discovered_table (
    discovery_id          text        not null,
    src_database          text        not null,
    src_schema            text        not null,
    src_table             text        not null,
    row_count             bigint,
    size_mb               numeric,
    ct_enabled            boolean,
    primary_key           text,
    select_list           text,
    excluded_columns      text,
    rowversion_column     text,
    identity_column       text,
    load_type             text,                    -- recommended: ct | cursor | full
    cursor_column         text,
    partition_candidate   text,                    -- column a partitioned seed could split on
    notes                 text,
    discovered_at         timestamptz not null default now(),
    constraint discovered_table_pk primary key (discovery_id, src_database, src_schema, src_table)
);
