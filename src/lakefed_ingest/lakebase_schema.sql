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
    task_enabled          boolean     not null default true,
    freshness_sla_minutes int,                                -- max minutes since last success; NULL = no SLA
    created_at            timestamptz not null default now()
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
-- Freshness SLA (monitoring only; the ingestion job doesn't read it): an enabled table breaches it
-- when its last success (or, if it never succeeded, its creation) is older than this many minutes.
alter table lakefed_ingest.control add column if not exists freshness_sla_minutes int;
alter table lakefed_ingest.control add column if not exists created_at timestamptz not null default now();

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
alter table lakefed_ingest.control drop constraint if exists valid_freshness_sla;
alter table lakefed_ingest.control add constraint valid_freshness_sla check (
    freshness_sla_minutes is null or freshness_sla_minutes > 0
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
-- A source database's rows: the one-collection check (the app's queries.split_databases_sql, and imports) looks
-- up the databases it writes here instead of reading the whole table.
create index if not exists control_database_idx
    on lakefed_ingest.control (src_database);
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
-- What the checkpoint was written for: source (engine://host:port/db/schema/table), sink (+ #client_id)
-- and merge key. A control row that now points elsewhere (edited) is reseeded instead of merging only
-- new changes into the new place. NULL = not recorded yet (recorded on the next advance).
alter table lakefed_ingest.checkpoint add column if not exists source_identity text;
alter table lakefed_ingest.checkpoint add column if not exists sink_identity text;
alter table lakefed_ingest.checkpoint add column if not exists merge_key text;

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

-- Per-database check stamps. When every table of a source database is unchanged, a run (or the
-- initial check, initial_check.py) writes one row here instead of stamping last_success_at on each
-- of its tables. The row says: at checked_at the database's CT version was checked_version and the
-- collection's tables of this database were checked. A table's last success is then the later of its
-- own last_success_at and checked_at, when it is a CT table in the incremental phase whose position
-- is at or past checked_version (planner.stamp_vouches). `source` is state_store.database_source.
create table if not exists lakefed_ingest.database_check (
    task_collection text        not null,
    source          text        not null,
    src_database    text        not null,
    checked_version bigint      not null,
    tables          int         not null,
    run_id          bigint,                                   -- -> ingest_run.run_id that checked it
    checked_at      timestamptz not null default now(),
    constraint database_check_pk primary key (task_collection, source, src_database)
);

-- The initial check's work list. Before it starts a collection's run, the check job (check_launch.ipynb)
-- writes here the databases of that collection that have work, under one work_id, and starts the run with
-- that work_id; the run then ingests only those databases. A job parameter would not hold them (job
-- parameter values are at most 10,000 characters). The check deletes rows older than a week.
create table if not exists lakefed_ingest.check_work (
    work_id         text        not null,
    task_collection text        not null,
    source          text        not null,
    src_database    text        not null,
    created_at      timestamptz not null default now(),
    constraint check_work_pk primary key (work_id, task_collection, source, src_database)
);

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
    status          text        not null default 'running',   -- running | completed | failed | skipped | cancelled
    total           int,
    ok              int,
    failed          int,
    skipped         int,
    reseeded        int,
    detail          text
);

create index if not exists ingest_run_collection_idx
    on lakefed_ingest.ingest_run (task_collection, started_at);

-- The check job's work list a run was started for ("<launch_id>:<collection>"): the group runs of one
-- launch share its launch id, so the app shows them as one job run. Null for runs started otherwise.
alter table lakefed_ingest.ingest_run add column if not exists work_id text;
create index if not exists ingest_run_work_idx on lakefed_ingest.ingest_run (work_id);
-- Every group's runs by time (the app's Runs page across groups, a window's batched appends).
create index if not exists ingest_run_started_idx on lakefed_ingest.ingest_run (started_at);

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
    status          text        not null default 'running',   -- running | ok | failed | skipped | retried (an attempt tried again) | cancelled
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
-- How a failed attempt was classified (table_retry.classify: permanent | transient | unknown) and the error class it
-- was classified by (its Spark error class, else its exception type). The operations app's failing-tables list reads them.
alter table lakefed_ingest.ingest_event add column if not exists error_kind text;
alter table lakefed_ingest.ingest_event add column if not exists error_class text;

create index if not exists ingest_event_run_idx
    on lakefed_ingest.ingest_event (run_id);
create index if not exists ingest_event_failed_idx
    on lakefed_ingest.ingest_event (run_id)
    where status = 'failed';
create index if not exists ingest_event_collection_idx
    on lakefed_ingest.ingest_event (task_collection, finished_at);
-- Recent failures across every group by time (the app's errors and failed tables).
create index if not exists ingest_event_failed_time_idx
    on lakefed_ingest.ingest_event (started_at)
    where status = 'failed';
-- Each table's latest first load (seed, reseed or full), whatever its age: the rows a database's tables hold,
-- read without passing over the group's other events.
create index if not exists ingest_event_first_load_idx
    on lakefed_ingest.ingest_event (task_collection, control_id, finished_at)
    where status = 'ok' and action in ('seed', 'reseed', 'full');
-- The failing-tables read looks up one table's last ok event and its failures by control_id and started_at. Not partial,
-- so the ok lookup (status = 'ok') and the failures lookup (status = 'failed') both use it.
create index if not exists ingest_event_control_started_idx
    on lakefed_ingest.ingest_event (control_id, started_at);

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
-- A discovery through a Unity Catalog connection (remote_query) names it instead of a host and secret. A discovery
-- of the 'list' kind only lists the server's databases (status 'listed', no tables), as Test connection does.
alter table lakefed_ingest.discovery add column if not exists src_connection text;
-- How the discovery listed its databases (the LIKE pattern, the Azure resource, the host's secret template), so a
-- later re-discovery of a job's selection reads the same way. Null for a discovery made before this column.
alter table lakefed_ingest.discovery add column if not exists listing jsonb;

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

-- ---------------------------------------------------------------------------------------
-- Change log for control rows: who changed what, when. Written by the operations app in the same
-- transaction as each change (add, register, edit, move, enable / disable, reseed). Edits made with
-- SQL aren't logged here; the ingestion planner still reseeds a table whose source, sink or key
-- changed (checkpoint.source_identity / sink_identity / merge_key).
-- ---------------------------------------------------------------------------------------
create table if not exists lakefed_ingest.control_change (
    id              bigserial   primary key,
    changed_at      timestamptz not null default now(),
    changed_by      text,                         -- signed-in user (app) or NULL
    action          text        not null,         -- add | register | edit | enable | disable | reseed | schedule_*
    control_id      bigint,                       -- -> control.id
    task_collection text,                         -- the row's collection after the change
    field           text,                         -- edited column (NULL for whole-row actions)
    old_value       text,
    new_value       text,
    reseed_reason   text,                         -- set when the change flipped the table to seeding
    batch_id        text                          -- groups the rows of one bulk action
);
create index if not exists control_change_control_idx
    on lakefed_ingest.control_change (control_id, changed_at);
create index if not exists control_change_time_idx
    on lakefed_ingest.control_change (changed_at);

-- ---- Schedules (written by the operations app) -------------------------------------------
-- A schedule runs one or more collections at a fixed cadence. The app turns each member
-- (schedule, collection) into a small Databricks job (a cron schedule and one run_job task that
-- starts the ingestion job with that collection). Soft deletes: the app has no DELETE privilege.
create table if not exists lakefed_ingest.schedule (
    id              bigserial   primary key,
    name            text        not null,
    cadence_minutes int         not null,                     -- divides 1440: 5 .. 1440
    timezone        text        not null default 'UTC',
    parameters      jsonb       not null default '{}'::jsonb, -- ingestion job parameters, e.g. parallelism
    job_variant     text        not null default 'default',   -- which ingestion job to start
    stagger         boolean     not null default true,        -- spread members' start times over the cadence
    paused          boolean     not null default false,
    notify_emails   text,                                     -- comma-separated, on failure
    created_by      text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    deleted_at      timestamptz
);
create unique index if not exists schedule_name_uq
    on lakefed_ingest.schedule (lower(name)) where deleted_at is null;
-- Schedule options the app sets, as a JSON object (null = none): start_minute, a set start time for daily
-- and every 2 to 12 hours schedules, in minutes after local midnight.
alter table lakefed_ingest.schedule add column if not exists options jsonb;
alter table lakefed_ingest.schedule drop constraint if exists valid_schedule_cadence;
alter table lakefed_ingest.schedule add constraint valid_schedule_cadence check (
    cadence_minutes in (5, 10, 15, 20, 30, 60, 120, 180, 240, 360, 720, 1440)
);

create table if not exists lakefed_ingest.schedule_member (
    id              bigserial   primary key,
    schedule_id     bigint      not null references lakefed_ingest.schedule (id),
    task_collection text        not null,
    offset_minutes  int         not null default 0,           -- start offset within the cadence
    job_id          bigint,                                   -- the Databricks job the app created
    synced_at       timestamptz,
    sync_error      text,
    added_at        timestamptz not null default now(),
    removed_at      timestamptz
);
alter table lakefed_ingest.schedule_member add column if not exists added_at timestamptz not null default now();
create unique index if not exists schedule_member_uq
    on lakefed_ingest.schedule_member (schedule_id, task_collection) where removed_at is null;
create index if not exists schedule_member_collection_idx
    on lakefed_ingest.schedule_member (task_collection);

-- A long write the operations app runs off the request: a large New ingestion job, Add tables or a databases file.
-- The request records it and answers at once; a background thread in the app writes it in one transaction and moves
-- done and heartbeat_at as it goes; the page reads it. A request sent again with the same request_id finds the same
-- task, so a retry never writes twice. A task whose heartbeat stops (the app restarted) reads as failed, and its
-- transaction was rolled back with it.
create table if not exists lakefed_ingest.app_task (
    id            bigserial   primary key,
    kind          text        not null,                    -- create, tables or databases
    request_id    text        not null,
    job_id        bigint,                                  -- the ingestion job (schedule id), once known
    status        text        not null default 'running',  -- running, succeeded or failed
    done          int         not null default 0,          -- tables written so far
    total         int         not null default 0,
    result        jsonb,                                   -- what the request would have answered
    error         text,
    created_by    text,
    created_at    timestamptz not null default now(),
    heartbeat_at  timestamptz not null default now(),
    finished_at   timestamptz
);
create unique index if not exists app_task_request_uq on lakefed_ingest.app_task (request_id);
create index if not exists app_task_job_idx on lakefed_ingest.app_task (job_id, created_at);
-- A by-database write's progress (stage, databases written of how many, tables written), committed with each database.
alter table lakefed_ingest.app_task add column if not exists progress jsonb;

-- A CSV of tables uploaded to the operations app for New ingestion job or Add tables: the file is posted once and
-- kept here, one row per line, so the pages show its servers and databases from Lakebase and page its tables, and
-- Create names the upload and what of it to take instead of sending the file again. A line's own problems (no
-- database, schema or table, no connection or host) are found on upload; the job's settings are checked on Create.
create table if not exists lakefed_ingest.table_upload (
    upload_id     text        primary key,
    file_name     text,
    uploaded_by   text,
    uploaded_at   timestamptz not null default now(),
    columns       text[]      not null,                    -- the file's header
    lines         int         not null default 0,
    tables        int         not null default 0,          -- lines that can be taken (status ok)
    invalid       int         not null default 0
);
create table if not exists lakefed_ingest.table_upload_line (
    upload_id     text        not null,
    line          int         not null,                    -- the file's line number (the header is line 1)
    server        text,                                    -- its UC connection, or host:port
    src_database  text,
    src_schema    text,
    src_table     text,
    load_type     text,                                    -- the file's own, if it gives one
    fields        jsonb       not null,                    -- the line as read: {column: value}
    status        text        not null,                    -- ok or invalid
    errors        text[],
    constraint table_upload_line_pk primary key (upload_id, line)
);
create index if not exists table_upload_line_database_idx
    on lakefed_ingest.table_upload_line (upload_id, server, src_database);

-- What an ingestion job was made from, part by part, as chosen in New or Add tables: a whole server (with the database
-- filter it was chosen under), a whole database, or some of a database's tables. A later re-discovery reads it: a whole
-- server takes its new databases, a whole database its new tables, chosen tables nothing more.
create table if not exists lakefed_ingest.job_selection (
    id            bigserial   primary key,
    job_id        bigint      not null,                    -- schedule.id
    scope         text        not null,                    -- server, database or tables
    server        text        not null,
    src_database  text,                                    -- null for a whole server
    tables        text[],                                  -- schema.table, for scope tables
    filter        text,                                    -- the database filter a whole server was chosen under
    source_kind   text        not null,                    -- discovery or upload
    source_id     text,
    created_by    text,
    created_at    timestamptz not null default now()
);
create index if not exists job_selection_job_idx on lakefed_ingest.job_selection (job_id);

-- The Databricks runs the operations app tracks (app/src/run_poller.py): the ingestion job and its variants, the
-- check job, the cluster copies, the app's wrapper jobs, and its setup, discovery and delete jobs. One row per run,
-- written by the app's poller only when a run's state changes. The app has no DELETE, so rows are never removed:
-- prune old ones as the owner, like ingest_run.
create table if not exists lakefed_ingest.databricks_run (
    run_id           bigint      primary key,                  -- the Databricks run id
    job_id           bigint      not null,                     -- the Databricks job the run belongs to
    kind             text        not null check (kind in ('wrapper', 'check', 'group', 'setup', 'discover', 'delete', 'copies')),
    schedule_id      bigint,                                   -- wrapper and check runs: their schedule
    task_collection  text,                                     -- group runs: the collection they ingest
    trigger          text,                                     -- PERIODIC, RUN_JOB_TASK, ONE_TIME, ...
    parameters       jsonb       not null default '{}'::jsonb, -- the run's job parameters
    life_cycle_state text        not null,                     -- QUEUED ... TERMINATED, SKIPPED, INTERNAL_ERROR, NOT_FOUND
    result_state     text,
    state_message    text,
    run_page_url     text,
    started_at       timestamptz,
    ended_at         timestamptz,
    seen_at          timestamptz not null default now()        -- when the poller last saw this state change
);
create index if not exists databricks_run_job_started_idx
    on lakefed_ingest.databricks_run (job_id, started_at desc);

-- Cancel (schema 15): when the app's Cancel run asked Databricks to stop this run, and when (null otherwise). The
-- engine and the poller read it to tell a load stopped by a cancel from a failure, and to close the run as cancelled.
alter table lakefed_ingest.databricks_run add column if not exists cancel_requested_at timestamptz;

-- Monitoring rollups (the operations app's pages read these, not a scan of control). One row per database and
-- ingestion group, rewritten from the database's own rows as its work ends (state_store.StateStore.rewrite_rollups)
-- and by the app's edits, in the same transaction. A database with no rows left is zeroed, not deleted, and the
-- delete job removes a deleted group's rows. `earliest_due_at` is when the first loaded table with a freshness target
-- falls due (its last success or creation, plus the target); a database is late once that time has passed.
create table if not exists lakefed_ingest.rollup_database (
    task_collection       text        not null,
    src_database          text        not null,         -- '' when the control row has none
    server                text        not null,         -- the server key (queries.SERVER); '' when none
    src_connection        text        not null,         -- '' when none
    src_host              text        not null,         -- '' when none
    instance_label        text        not null,         -- source_instance_group; '' when none
    tables                int         not null,         -- enabled tables
    no_sla_tables         int         not null,         -- enabled tables without a freshness target
    failing               int         not null,         -- enabled tables whose checkpoint failed
    first_load            int         not null,         -- enabled tables waiting for a first load
    loaded                int         not null,         -- enabled tables neither failing nor waiting
    control_rows          int         not null,         -- control rows in the grain, enabled or not; 0 once none
    oldest_success_at     timestamptz,
    newest_success_at     timestamptz,
    newest_any_success_at timestamptz,                  -- over every row, enabled or not (a connection's databases)
    earliest_due_at       timestamptz,
    updated_at            timestamptz not null default now(),
    constraint rollup_database_pk primary key (task_collection, src_database, server, src_connection, src_host,
                                               instance_label)
);
create index if not exists rollup_database_due_idx
    on lakefed_ingest.rollup_database (task_collection, earliest_due_at);

create table if not exists lakefed_ingest.rollup_group (
    task_collection       text        primary key,
    tables                int         not null,
    no_sla_tables         int         not null,
    failing               int         not null,
    first_load            int         not null,
    loaded                int         not null,
    databases             int         not null,         -- databases with an enabled table
    servers               int         not null,
    oldest_success_at     timestamptz,
    newest_success_at     timestamptz,
    updated_at            timestamptz not null default now()
);

-- A rollup rewrite reads one database's control rows by (collection, database name) instead of the whole collection.
create index if not exists control_collection_database_idx
    on lakefed_ingest.control (task_collection, (coalesce(src_database, '')));

-- A JDBC connection the operations app's users entered by hand (app/src/jdbc_connections.py): a host and port, and the
-- secret scope and key an admin created beforehand, the secret itself never stored here. A discovery and an ingestion
-- job made through it carry the scope and trust as job parameters, so the engine reads the source over Spark JDBC.
-- The app writes and reads this table only (INSERT and SELECT, no UPDATE or DELETE).
create table if not exists lakefed_ingest.jdbc_connection (
    name                     text        primary key,
    src_type                 text        not null,              -- sqlserver, oracle, postgresql or mysql
    src_host                 text        not null,
    src_port                 int         not null,
    secret_scope             text        not null,              -- a Databricks secret scope the admin created
    secret_key               text        not null,              -- the key in that scope; may hold {database}
    trust_server_certificate text        not null default 'false',
    created_by               text,
    created_at               timestamptz not null default now(),
    constraint jdbc_connection_port_range check (src_port between 1 and 65535),
    constraint jdbc_connection_trust_values check (trust_server_certificate in ('true', 'false'))
);

-- One row per time the operations app wrote a JDBC login into the dedicated secret scope (app/src/jdbc_logins.py): who
-- entered it, when, and whether it replaced an earlier login for the same scope and key ('rotated'). The password is
-- never here; it lives only in the secret. The app's role inserts and reads this table, and never deletes from it.
create table if not exists lakefed_ingest.jdbc_login_write (
    id           text        primary key,
    connection   text        not null,
    secret_scope text        not null,
    secret_key   text        not null,
    entered_by   text        not null,
    entered_at   timestamptz not null default now(),
    outcome      text        not null,
    constraint jdbc_login_write_outcome_values check (outcome in ('saved', 'rotated'))
);
create index if not exists jdbc_login_write_key_idx
    on lakefed_ingest.jdbc_login_write (secret_scope, secret_key, entered_at desc);
