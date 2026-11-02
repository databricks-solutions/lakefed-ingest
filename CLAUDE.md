# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

The repo holds **two versions** of a metadata-driven framework that ingests external databases into
Databricks Delta tables. Each is its own Databricks Asset Bundle:

| Version | Location | Bundle | Orchestration / compute / state |
|---|---|---|---|
| **Default** | repo root (`databricks.yml`, `resources/`, `src/lakefed_ingest/`, `tests/`) | `lakefed_ingest_v2` | One job task per `task_collection`, in-process threads + FAIR pools on a job cluster; Lakebase (Postgres) state; Spark JDBC; source adapters (SQL Server, Oracle, PostgreSQL, MySQL) + load strategies (SQL Server Change Tracking and watermark cursor, full reload) |
| **Job orchestration** | `job_orchestration/` | `lakefed_ingest` | Jobs `for_each` tasks; SQL warehouse + notebooks; Delta control table; Lakehouse Federation / `remote_query` |

An optional operations app (Databricks App: FastAPI backend + React/TypeScript frontend) for the
default version is its own bundle in `app/` (`lakefed_ingest_app`); see `app/README.md`. SQL lives in
`app/src/queries.py` (unit-tested); Databricks Apps builds the frontend at deploy time. Schedules
(`app/src/schedules.py`): one wrapper job per (schedule, collection), a cron + `run_job` task that starts the
ingestion job with `task_collection`; `max_concurrent_runs` 1, no queue (overruns skip triggers). A schedule of
the `check` variant has one wrapper that starts `lakefed_ingest_check` with its members (`task_collections`).

Never run bundle commands for one version from the other's directory. Local bundle state
(`.databricks/bundle/<target>/`) is keyed by target only, so each version keeps its own cache in its
own directory.

## Commands

### Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pytest            # unit tests need nothing else; tests/validation_test.py also needs pytds
```

### Run Tests
```bash
pytest                                   # both versions (pytest.ini testpaths)
pytest tests                             # default version
pytest tests/planner_test.py::test_name  # single test
pytest job_orchestration/tests           # job orchestration version
```

### Deploy
```bash
# Default version (repo root)
databricks bundle deploy -t <target> -p <profile>
databricks bundle run lakefed_ingest_setup -t <target> -p <profile>   # create the Lakebase schema (upgrades migrate on the next ingest run)
databricks bundle run lakefed_ingest -t <target> -p <profile> --params task_collection=<name>

# Job orchestration version
cd job_orchestration
databricks bundle deploy --target prod --profile PROD
databricks bundle run --target prod --profile PROD
```

## Default version (repo root)

Warehouse-free: everything runs on cluster compute. Source reads use Spark JDBC with per-database
credentials (JSON `{user,password}`) from a secret scope named by `control.secret_key`; or, for a
row with `use_remote_query`, `remote_query` through the UC connection in `src_connection` (no
secret). See `README.md`, `docs/deploy.md`, `docs/monitoring.md`, `docs/lakebase_schema.md`.

### Lakebase state (`src/lakefed_ingest/lakebase_schema.sql`, schema `lakefed_ingest`)
- `control` — one row per (source database, table): `task_collection`, `src_type` (source adapter:
  `sqlserver` | `oracle` | `postgresql` | `mysql`), `src_host`/`src_port` and `secret_key` (JDBC) or
  `src_connection` + `use_remote_query` (UC connection), `src_database`, `src_schema`,
  `src_table`, `primary_key`, `select_list` (explicit columns), sink, `load_type` (load
  strategy: `ct` | `full` | `cursor` (SQL Server; placeholder elsewhere); `cdc` is a placeholder), partitioned-seed settings,
  `cursor_column`, `cdc_capture_instance`, `jdbc_options` (JSON), `client_id` (optional: consolidated
  sink shared by many source DBs; written to sink column `__client_id__`), `client_attrs` (JSON
  {column: value}: identity columns written as STRING on every row), `write_mode` (`merge` default |
  `append` for ct/full: change-log sink with `__change_op`/`__change_version`/`__extracted_at`/`__run_id`), `freshness_sla_minutes`
  (optional; monitoring only, the app's "SLA breached" rule `queries.SLA_BREACHED`), `created_at`.
  CHECK constraints enforce valid combos; a unique index allows one row per (sink, client_id).
- `checkpoint` — generic: `strategy` + `position` (text; CT version for `ct`, JSON {col,type,value} for
  `cursor`), phase (`seeding → incremental` for ct and cursor, `loaded` for full), status, freshness,
  `ct_begin_version`, `pending_position`/`pending_run_id` (an append in flight). `reseed_queue` (ct, cursor).
- `database_check`: one stamp per (collection, source database) found unchanged as a whole (CT version,
  time), in place of each table's `last_success_at`; `planner.stamp_vouches` says which tables it covers.
- `check_work`: the check job's work list: the databases with work per (work_id, collection).
- `ingest_run`, `ingest_event` — telemetry (`run_id`, `position_from/to`). The DB is registered in UC
  (default catalog `lakefed_ingest_lakebase`) for DBSQL monitoring.
- `schedule`, `schedule_member` — written by the operations app (cadence, members, wrapper `job_id`);
  soft deletes (`deleted_at`, `removed_at`): the app has no DELETE privilege.
- `app_task` (a large write the app runs in a background thread: progress, heartbeat, a unique `request_id`),
  `table_upload` / `table_upload_line` (a CSV posted once, a row per line) and `job_selection` (what a job was chosen
  from: a whole server with its filter, a whole database, or tables), written by the operations app (version 9).
- `rollup_database`, `rollup_group`: the monitoring rollups: one row per database (per server, connection, host and
  instance label) and one per ingestion group, rewritten in the same transaction as the rows they count (the engine
  after each database's work, the app after its own edits). The pages read them, so counts are never scanned from
  control; a grain with no control row left is zeroed (`control_rows = 0`), not deleted. See `docs/lakebase_schema.md`.
- DDL is applied by `StateStore.migrate` (from `apply_lakebase_schema.ipynb`, and from `ingest.ipynb`
  when `schema_version` is behind `state_store.SCHEMA_VERSION`, so upgrades are deploy-only): one
  transaction, split on `;`, no `DO $$` blocks, no `CREATE INDEX CONCURRENTLY`. Migrations must be
  idempotent (`if not exists`, `drop constraint if exists`). Renames (no IF EXISTS form in Postgres) go
  in `state_store.RENAMES`; only the pending ones run, before the DDL. Every DDL or RENAMES change
  bumps `SCHEMA_VERSION` (`tests/state_store_test.py` pins its fingerprint). An upgrade re-reads the whole
  file from any earlier version and skips statements the catalog shows are already in place (`pg_catalog`;
  backfills use `UPDATE_PROBES`); constraint re-declarations always run. `ddl_problems` in the same test
  checks the forms, and statements that change existing columns or rows need a `REVIEWED_NON_ADDITIVE` entry
  (docs/lakebase_schema.md, "Changing the schema"). Each attempt waits 3 s for locks (`lock_timeout`) and
  retries up to 10 times; a run whose migration can't get the locks exits with `migration_waiting` and
  ingests nothing.

### Jobs (`resources/`)
- `lakefed_ingest` → task `ingest` → `ingest.ipynb`: one run ingests a `task_collection`. Parameters:
  `task_collection`, `work_id` (set by the check job: only the databases it listed in `check_work`),
  `parallelism` (single budget of concurrent query units), `secret_scope`,
  `detection_chunk`, `trust_server_certificate`, `snapshot_isolation` (change reads under SNAPSHOT),
  `reseed_on_new_columns` (false: added select_list columns don't reseed), `append_batch_size` (>1:
  batched appends, see `batch_append.py`), `max_seeds_per_run` (seed budget, 0 = none),
  `freshness_interval_s` (skip no-op bulk-advance writes newer than this), `max_queries_per_source`
  (cap per source across runs, 0 = none: each run takes cap / runs reading the source, counted from
  shared advisory locks; `parallel.SourceLimiter`), `table_retries` (extra attempts per table after a
  transient source error, 0 = off; used only above a budget of 0; `table_retry.py`), `table_retry_budget` (retries per
  run, default 0: no table is retried, transient or unknown; the app's Advanced configuration raises it per job),
  `fail_on_table_errors` (accepted for older jobs and not read: a run with failed tables always ends as failed),
  `db_instance`, `db_name`.
  The tuning parameters' defaults are bundle variables of the same name (`databricks.yml`).
- `lakefed_ingest_check` → `check_launch.ipynb` (serverless): checks a group of collections
  (`task_collections`, blank = every collection with enabled rows except those only a deleted job holds; or `schedule_id`, an app job's groups read
  from Lakebase; `initial_check.run_check`, each collection's errors apart; its source queries count under
  `max_queries_per_source`, `initial_check.SourceCap`), stamps
  unchanged databases, records each collection's databases with work in `check_work`, starts
  `lakefed_ingest` once per collection with work by run-now (`work_id`; `launcher.py`), watches the
  runs until they end (`wait`, default true; `timeout_s` 21600, then the runs still going are cancelled and the check
  fails; a run that failed or was skipped fails the check, so the wrapper's failure email fires). Locks are let go
  when each run starts, so the wait holds none. No For each. Its own schedule
  (paused) is the fallback cadence; an app schedule in group mode starts it with its members.
  With `runs_by` `changes` (`group`, the default until its lab proof, keeps one run per collection with work)
  the cycle is shared out by its changes (`initial_check.plan_cycle`, `launcher.cycle_requests`): each database's
  tables with work (`check_collection`'s `weights`), as many runs as those tables over the job's group size (its tables over its
  groups, `group_size_of`), each database whole on one run, a collection with a first load on a run of its own.
  The check then holds each collection shared and each database it checks exclusively (`share_locks`), leaving out
  the databases a run still holds; a database a run going was given waits for it (`deferred`). A run spanning
  collections gets `task_collection` blank and its `work_id`: `ingest.ipynb` takes its collections in turn on its
  cluster, each as a run of its own (`dbutils.notebook.run`, `cycle_part`: a part that is locked or has nothing
  left is skipped, not failed), with its own locks and `ingest_run` record.
- `lakefed_ingest_cluster_copies` → `cluster_copies.ipynb` (serverless, runs as the deployer): one copy of
  `lakefed_ingest` per app schedule with its own cluster (`options.cluster`), only the cluster's types and workers
  changed (`cluster_copies.py`); records the copy in `options.ingest_copy_job_id`. The check job takes the job to
  start as its `ingest_job_id` job parameter.
- `lakefed_ingest_delete_job` → `delete_job.ipynb` (serverless, runs as the deployer, queued, one at a time):
  purges an app job's groups (`delete_job.py`). The app marks a job deleted and keeps it for
  `deleted_job_retention_days` (Restore brings it back); Delete now starts this job with `schedule_id` and
  `drop_targets`, and the daily run (03:00, its defaults: `purge_expired` with `retention_days`) purges every deleted job whose retention has run out
  (`expired_jobs_sql`, `purge_expired`, each job in its own transaction). The purge sets `options.delete_finished`,
  which the app reads as purged. In one transaction, under each group's run lock (a run of any group refuses the lot), it deletes
  the groups no other live job holds: their control, checkpoint, reseed_queue, database_check, check_work and rollup
  rows, and the job's job_selection rows (what it was chosen from; skipped below schema version 9).
  Run history (ingest_run, ingest_event) and control_change stay. With `drop_targets`, after the commit, it drops
  (`DROP TABLE IF EXISTS`) only the target tables no other group's rows write, never a schema. With `purge_drafts`
  (no `schedule_id`) it purges instead the app's drafts no job took (`delete_job.purge_drafts`: discovery rows marked
  `draft` in `listing`, discarded or not asked about for `draft_ttl_s`, and their discovered_table rows); the app's
  run poller starts it at most hourly, only when one is due.
- `lakefed_ingest_discover` → `discover_tables.ipynb`: table discovery for bulk onboarding (params
  `discovery_id`, `spec` JSON, `discover_workers` (databases read at once, default 32, capped by
  `max_queries_per_source`)); writes `discovery` / `discovered_table` in one transaction per database. Runs on the
  bundle's single-node all-purpose cluster `lakefed_ingest utility` (`resources.clusters.lakefed_ingest_utility`,
  auto-terminates after 60 min) so repeated runs skip cluster start; transient connection errors
  (paused sources) retry with backoff (`discovery.CONNECT_RETRIES`).
- Setup and the app's grant job run on serverless (job `environments`); ingestion on job clusters. The check job
  also runs on serverless and reads the sources, so serverless needs a network path to them (NCC private
  endpoints or firewall rules); a source only a job cluster reaches belongs in a default-variant schedule.
- `lakefed_ingest_setup` → `apply_lakebase_schema.ipynb`; plus bundle resources for the Lakebase
  instance, its UC catalog, and the secret scope (`database_instances.lakebase`,
  `database_catalogs.lakebase_catalog`, `secret_scopes.secret_scope`).

### Run flow (`ingest.ipynb`)
1. Per-collection Lakebase advisory lock (duplicate run exits `skipped`; a duplicate with a `work_id` fails,
   since its databases would not be ingested). A run with a `work_id` holds it shared plus an exclusive lock per
   database of its list (`state_store.database_lock_key`), so runs of one collection on different databases go at
   once; a database another run holds is skipped (left to it, never stamped), and a list wholly held elsewhere ends
   `skipped`. A run without one, the check, the app's edits and Delete need it exclusively and are refused meanwhile.
   A shared holder closes abandoned runs only when no other session holds the lock (`alone_on_collection`); the
   group rollup recount takes a transaction lock first (`rollup_group_lock_sql`), so two runs can't leave it stale.
2. Plan (`planner.py`, pure): unsupported (`src_type`, `load_type`) pair → fail per table (stage
   `plan`); `full` → reload; `cursor` → `cursor_seed` / schema_reseed / `cursor` (per-table high
   watermark, `cursor_engine`); CT tables → seed / schema_reseed / detection candidate; group by
   (source database, load_type); one query per DB captures the CT current version + per-table CT state
   (`ct_engine.database_ct_state`) → fast exit if all checkpoints current,
   else batched detection (`detection_chunk` tables per query); failed chunk → per-table fallback;
   checkpoint ahead of current version → reseed; CT disabled on the DB → fail loudly. Per-table CT
   status (`sys.change_tracking_tables`, read with the version, before the fast exit): CT off on a table,
   retention lapsed, or `begin_version` ≠ `checkpoint.ct_begin_version` (CT re-enabled) → reseed.
   Identity backstop (ct / cursor): control row's source / sink / primary key ≠ the checkpoint's recorded
   `source_identity` / `sink_identity` / `merge_key`, or the sink table is missing (one
   `information_schema` query per sink catalog) → reseed. Never merge into a new, empty sink.
3. Unchanged tables: one bulk Lakebase checkpoint advance; a whole unchanged database gets one
   `database_check` stamp instead (`planner.unchanged_writes`).
4. Per-table worker: full reload (`load_strategies.FullStrategy` → `ct_engine.seed`), seed (capture V0
   before seed; partitioned seed via staging + atomic swap),
   reseed enqueue, or CHANGETABLE read + idempotent PK MERGE (schema evolution) then checkpoint
   advance. The change read carries a sessionInitStatement retention guard (through a UC
   connection: the query's TOP count, `ct_read_query_guarded`) and is bounded at the
   captured version. Append mode (`write_mode` append) appends instead (seed/full: op 'S'). A ct append
   records the pending append first; a `full` append doesn't (it adds one snapshot per run, so there
   is no position to protect). A `pending` route (run stopped before the advance) checks the sink
   for that run's rows (`append_committed`) before anything else, unless the row now points at
   another source, sink or key, or its sink is gone: then it reseeds (`enqueue_reseed` clears the pending append).
   With `table_retries` > 0 (default 2) and `table_retry_budget` > 0 (default 0: off), a transient source failure is
   retried in the run (`table_retry.with_retries`;
   the next attempt from `planner.retry_plan` on the fresh checkpoint). An append-mode retry goes to
   `pending`, or carries `verify_run_id` (stage `verify`): rows of this run in the sink → finish the
   checkpoint (`recovered`), never append again.

### Modules
| File | Purpose |
|---|---|
| `src/lakefed_ingest/ct_engine.py` | Source ops (Spark JDBC, or `remote_query` through a UC connection): CT versions, detection, read + MERGE / append, seed, sink DDL, batched appends (`append_batch`) |
| `src/lakefed_ingest/table_retry.py` | Table retries: classify transient, permanent or unknown errors (deny rules win; unknown gets one retry, then fails; a budget of 0 retries nothing), equal-jitter backoff, per-run budget and per-source stop (`RetryPolicy`), the worker wrapper (`with_retries`); the next attempt comes from `planner.retry_plan` |
| `src/lakefed_ingest/batch_append.py` | Batched appends (append mode, consolidated sinks): plan batches by (sink, kind), write with bisect on failure, bulk checkpoints + telemetry; per-table fallback |
| `src/lakefed_ingest/source_adapters.py` | Per-engine dialect (`src_type`): JDBC URL/driver/options, quoting, literals, select/size/bounds SQL |
| `src/lakefed_ingest/load_strategies.py` | Per-`load_type` strategy, position encoding, `supported()` matrix (+ placeholder pairs); cdc placeholder |
| `src/lakefed_ingest/cursor_engine.py` | Cursor (watermark) strategy, SQL Server: type lookup, high watermark (rowversion capped below MIN_ACTIVE_ROWVERSION), typed literals, (lo, hi] read + upsert MERGE |
| `src/lakefed_ingest/planner.py` | Strategy routing + skip-no-change planning, database check stamps (no Spark/DB; unit-tested with fakes) |
| `src/lakefed_ingest/initial_check.py` | The check: which databases of a group of collections need an ingest, and each collection's work list (pure; `run_check` takes the Lakebase calls) |
| `src/lakefed_ingest/launcher.py` | Starts one `lakefed_ingest` run per collection with work by run-now, confirms they started (skipped or unconfirmed = failure), never more than the job has room for (no limit when its queue is on) |
| `src/lakefed_ingest/parallel.py` | Thread pool, FAIR pools, `WorkSlots` budget, `SourceLimiter` (per-source permits) |
| `src/lakefed_ingest/partitions.py` | Partitioned-seed planning |
| `src/lakefed_ingest/discovery.py` | Table discovery for bulk onboarding (catalog queries + recommended control-row settings); run by `discover_tables.ipynb` / job `lakefed_ingest_discover` |
| `src/lakefed_ingest/delete_job.py` | A deleted app job's groups: which go, the run locks, the row deletes, which target tables are dropped and which kept (pure; run by `delete_job.ipynb`) |
| `src/lakefed_ingest/state_store.py` | Lakebase accessor (psycopg, SDK-minted OAuth token) + pure helpers |
| `tests/conftest.py` | Puts `src/lakefed_ingest` on `sys.path` |
| `tests/synthetic/` | Synthetic SQL Server DB generator, change generator, reconcile |
| `tools/source_profile.sql` | Read-only SQL Server profiling script for sizing |

### Engineering rules
- No `.cache()` / `.persist()`; read each source once per cycle (Delta MERGE materializes the JDBC
  source itself).
- No SQL warehouse dependency in the default version.
- Checkpoints advance only after the Delta commit (at-least-once + idempotent MERGE). An append is
  not idempotent: record the pending append (`store.record_pending`) before it; the advance clears it.
- App writes to `control` / `checkpoint` run in one transaction that takes the affected collections'
  run locks (`pg_try_advisory_xact_lock`, same key as the job), flips tables to seeding when source /
  sink / key change (`queries.plan_edit`), and logs to `control_change`.
- Every sink write must be client-scoped when `client_id` is set: tag rows (`ct_engine._tag`, with
  `append_meta(cfg, ...)` for append writes), write `write_columns(cfg)`, MERGE on `merge_keys(cfg)`,
  overwrite via `insert_overwrite_sql(..., cfg)` (REPLACE WHERE client_id) or, in append mode, append
  (`_write_snapshot` / `_insert_into`), stage via `staging_table_name(sink, cfg)`. Wrap writes in
  `with_write_retry` (Delta conflict retry, exponential backoff).

## Job orchestration version (`job_orchestration/`)

All paths in this section are relative to `job_orchestration/`.

### Control Table

The control table (default: `lakefed_ingest.default.control`) is the source of truth for every ingestion task. Each row defines:
- **Source**: `src_type`, `src_connection`, `src_database`, `src_catalog`, `src_schema`, `src_table`
- **Sink**: `sink_catalog`, `sink_schema`, `sink_table`, `sink_cluster_cols`, `enable_iceberg_reads`
- **Load strategy**: `load_type` (`full` | `incremental`), `load_partitioned`, watermark columns, partition column, `use_remote_query`
- **Grouping**: `task_collection`, `task_enabled`

Create the control table with `notebooks/_create_control_table.ipynb`. Load metadata using the example notebooks in `notebooks/`.

### Job Hierarchy

```
lakefed_ingest_controller
├── [SQL] get_task_ids (load_partitioned=false)  → foreach: lakefed_ingest_copy (concurrent)
└── [SQL] get_task_ids (load_partitioned=true)   → foreach: lakefed_ingest_copy_partitioned_lvl1 (sequential)
```

**`lakefed_ingest_copy`** (non-partitioned, full & incremental):
1. `get_task` — fetches control table row for the `task_id`
2. `get_src_tbl_metadata` — creates a `WHERE 1=0` temp view, runs `DESCRIBE EXTENDED ... AS JSON` to infer schema
3. `create_sink_table` — creates Delta table with column mapping, optionally enables Liquid Clustering and Iceberg UniForm
4. Branch on `load_type`:
   - **full**: `copy_data.ipynb` — `INSERT OVERWRITE` with `fetchSize=100000`
   - **incremental**: `get_new_watermark` → `copy_data_incremental.ipynb` → `update_watermark`

**`lakefed_ingest_copy_partitioned_lvl1`** (partitioned full loads):
1–3. Same `get_task` / `get_src_tbl_metadata` / `create_sink_table` as above (also truncates sink)
4. `create_partition_udtf` — registers `generate_partition_list` UDTF in the control catalog
5. `generate_partitions` — queries source for MIN/MAX bounds and table size, computes `num_partitions = table_size_mb / partition_size_mb`, writes `<sink_table>_partitions` table, returns `batch_id_list`
6. `foreach_batch` (sequential) → `lakefed_ingest_copy_partitioned_lvl2` (concurrent per partition)

**`lakefed_ingest_copy_partitioned_lvl2`**: reads the `_partitions` table and runs `copy_data_partitioned.ipynb` concurrently across partitions.

### Partition Logic

`src/lakefed_ingest/create_partition_udtf.sql` embeds a Python class (`GeneratePartitionList`) as a Databricks UDTF using `LANGUAGE PYTHON`. The UDTF implements Spark's JDBC partition algorithm:
- `stride = int(upper / N - lower / N)`
- Yields N `WHERE` clause strings covering all rows including NULLs (first partition appends `OR col IS NULL`)
- Supports INT/BIGINT/FLOAT, DATE, TIMESTAMP, TIMESTAMP_NTZ

This same Python class is tested locally via `tests/udtf_test.py`, which extracts the class from the SQL file using a regex and runs it in-process — no Databricks cluster needed.

### `remote_query` vs. Lakehouse Federation

When `use_remote_query = true` in the control table, all copy notebooks (`copy_data.ipynb`, `copy_data_incremental.ipynb`, `copy_data_partitioned.ipynb`) and `get_new_watermark.sql` push queries to the source database via `remote_query()` (native SQL passthrough for performance) instead of using Lakehouse Federation. This is the recommended mode for DB2 and any source where a Unity Catalog foreign catalog is not configured.

`generate_partitions.sql` always uses `remote_query()` for MIN/MAX bounds and table size queries for SQL Server, Oracle, PostgreSQL, Redshift, and DB2. Synapse falls back to Lakehouse Federation throughout because `remote_query` is not supported for Synapse — the `valid_remote_query` constraint in the control table prevents setting `use_remote_query = true` for Synapse tasks.

### Key Files

| File | Purpose |
|------|---------|
| `databricks.yml` | Bundle config; `dev` target is default |
| `resources/*.yml` | Workflow definitions (jobs, tasks, parameters) |
| `src/lakefed_ingest/get_task.sql` | Reads one control table row by `task_id` |
| `src/lakefed_ingest/get_src_tbl_metadata.sql` | Infers source schema via `DESCRIBE EXTENDED ... AS JSON` |
| `src/lakefed_ingest/create_sink_table.ipynb` | DDL for Delta sink: column mapping, Liquid Clustering, Iceberg UniForm, optional truncate |
| `src/lakefed_ingest/create_partition_udtf.sql` | Registers `generate_partition_list` UDTF (Python class embedded in SQL) |
| `src/lakefed_ingest/generate_partitions.sql` | Computes partitions and writes `_partitions` table |
| `tests/udtf_test.py` | Tests `GeneratePartitionList` locally by extracting Python from the SQL file |

### Supported Sources

SQL Server, Oracle, PostgreSQL, Redshift, Synapse, IBM DB2 LUW. Oracle requires `sys.dba_segments` read permission for table size queries in partitioned loads. DB2 requires `use_remote_query = true` — only a Unity Catalog connection (`src_connection`) is needed; no foreign catalog (`src_catalog`) is required and it can be left NULL.
