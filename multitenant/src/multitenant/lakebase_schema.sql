-- Lakebase (OLTP Postgres) schema for multi-tenant ingestion.
-- Per ADR-0004, Lakebase holds the hot per-(db,table) checkpoint, phase/status, and reseed
-- queue (concurrent point writes + low-latency point reads from many workers, plus a future
-- workbench-app backend). Per the consolidation decision, the cold task config/placement is
-- co-located here too (the `control` table) so there is a single metadata store — no Delta
-- control table. Config reads and all state writes run on the job cluster via the Postgres
-- client; a DBSQL warehouse cannot write to Lakebase.
--
-- Naming: `control` is the strategy-agnostic framework config table; `ct_checkpoint` and
-- `reseed_queue` are specific to the Change-Tracking (CT) strategy (the first strategy).
--
-- Run this once against the target Lakebase database (Postgres client / psql).

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
    src_connection        text,                               -- UC Connection (governed remote_query)
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
    select_list           text        not null,
    use_remote_query      boolean     not null default true,
    ct_enabled            boolean     not null default true,
    source_instance_group text,                               -- placement
    tier                  text,                               -- placement: cadence/SLA tier
    priority              int,                                -- placement: sweep priority
    warehouse_id          text,                               -- placement: optional read routing
    task_enabled          boolean     not null default true,
    -- CT tasks need a PK, the governed remote_query transport, and a SQL Server source.
    constraint mt_valid_ct_metadata check (
        load_type <> 'ct'
        or (length(coalesce(primary_key, '')) > 0 and use_remote_query = true and src_type = 'sqlserver')
    )
);

create index if not exists control_collection_idx
    on lakefed_ingest_mt.control (task_collection)
    where task_enabled = true;

-- Per-(db,schema,table) Change-Tracking checkpoint. One row per source table.
create table if not exists lakefed_ingest_mt.ct_checkpoint (
    src_database   text        not null,
    src_schema     text        not null,
    src_table      text        not null,
    ct_version     bigint      not null default 0,   -- last successfully merged CT version
    phase          text        not null default 'seeding'
                   check (phase in ('seeding', 'cdc')),
    status         text        not null default 'ok', -- ok | running | failed | quarantined
    detail         text,                              -- last error / note
    updated_at     timestamptz not null default now(),
    primary key (src_database, src_schema, src_table)
);

-- Tables awaiting a full reseed (CT retention lapsed, or first-time enablement).
-- Processed by the controller's reseed handling with concurrency caps.
create table if not exists lakefed_ingest_mt.reseed_queue (
    id           bigserial   primary key,
    src_database text        not null,
    src_schema   text        not null,
    src_table    text        not null,
    reason       text,                                 -- e.g. 'retention_miss'
    enqueued_at  timestamptz not null default now(),
    processed    boolean     not null default false,
    processed_at timestamptz
);

create index if not exists reseed_queue_unprocessed_idx
    on lakefed_ingest_mt.reseed_queue (enqueued_at)
    where processed = false;

-- NOTE: the two-level controller/batching was removed — the consolidated sweep ingests a whole
-- task_collection in one task (see copy_data_sweep.ipynb), so there is no `task_batch` table.
-- A `task_batch` left over from an earlier deploy is harmless/orphaned; we don't drop it here.
