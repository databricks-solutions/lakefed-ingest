# lakefed_ingest

Metadata-driven ingestion from external databases into Delta tables on Databricks. This repo holds
two versions:

| | **Default version** (repo root) | **Job orchestration version** ([`job_orchestration/`](job_orchestration/README.md)) |
|---|---|---|
| Orchestration | One job task ingests a whole `task_collection` in-process (thread pool + Spark FAIR scheduler pools) | Jobs `for_each` tasks: one task run per table / partition |
| Compute | Job cluster only — no SQL warehouse | SQL warehouse + notebooks |
| Metadata / state | Lakebase (Postgres): control, checkpoints, reseed queue, telemetry | Delta control table |
| Source access | Spark JDBC with per-database credentials from a secret scope | Lakehouse Federation / `remote_query` via UC connections |
| Load types | SQL Server Change Tracking (`ct`) and watermark `cursor`, each with full / partitioned seed; `full` reload (partitioned or not) for every source. `cdc` and non-SQL Server `cursor` are placeholders | Full, watermark incremental, partitioned full |
| Sources | SQL Server (`ct`, `cursor`, `full`); Oracle, PostgreSQL, MySQL (`full`) | SQL Server, Oracle, PostgreSQL, Redshift, Synapse, IBM Db2 LUW |
| Bundle | `lakefed_ingest_v2` | `lakefed_ingest` |

The job orchestration version remains supported. Use it when you want warehouse-only compute,
watermark-based incremental loads from a source other than SQL Server, or a source the default
version doesn't support yet. Use the default version when you have many source databases or tables
(thousands to hundreds of thousands), per-database credentials, want many source databases
consolidated into shared tables, or need Change Tracking (incremental loads without a reliable
watermark column, deletes included).

The rest of this README covers the default version.

## Why one task per collection

A workspace allows at most **2,000 concurrent task runs** and **10,000 run-submits per hour**, so a
task-per-table design stalls at 10^5–10^6 tables on a 15–20 minute cadence. The default version
collapses a whole `task_collection` into **one** job task that iterates its tables with a thread
pool and Spark FAIR scheduler pools. Throughput is then bounded by cluster compute, not by job-run
count. Per-table failures are isolated and recorded in Lakebase.

**Scaling.** Shard tables into `task_collection`s sized to what one cluster can ingest within the
cadence window, and run collections concurrently (one run per collection, each on its own job
cluster). A per-collection Lakebase advisory lock makes a duplicate run of the same collection exit
as `skipped`.

## Layout

```
databricks.yml                     # bundle lakefed_ingest_v2
resources/
  lakefed_ingest.yml               # ingestion job: one run ingests a task_collection
  lakefed_ingest_setup.yml         # Lakebase instance + UC catalog, secret scope, schema job
src/lakefed_ingest/
  ingest.ipynb                     # the ingestion notebook: plan, then fan out per table
  planner.py                       # strategy routing + skip-no-change planning (pure, unit-tested)
  source_adapters.py               # per-engine dialect: JDBC URL/driver, quoting, size/bounds/select SQL
  load_strategies.py               # per-load_type strategy + the (src_type, load_type) support matrix
  ct_engine.py                     # source ops via Spark JDBC: CT versions, detection, read + MERGE, seed
  parallel.py                      # thread pool + FAIR pools + the shared WorkSlots budget
  partitions.py                    # partitioned-seed planning
  state_store.py                   # Lakebase accessor: config, checkpoints, telemetry, locks
  lakebase_schema.sql              # Postgres DDL + idempotent migrations
  apply_lakebase_schema.ipynb      # applies / migrates lakebase_schema.sql
notebooks/load_control_example.ipynb   # register tables in the Lakebase control table
tools/source_profile.sql           # read-only SQL Server profiling script for sizing
docs/deploy.md, docs/monitoring.md
tests/                             # unit tests (no cluster), synthetic DB generator, validation harness
job_orchestration/                 # the job orchestration version (its own bundle)
```

## How a run works

Each run of the `lakefed_ingest` job ingests one `task_collection`:

1. **Plan.** Load the collection's control rows and checkpoints from Lakebase (one query each) and
   route every table by its load strategy: `full` tables reload; `ct` tables seed, reseed (schema
   change), or become change-detection candidates. A row whose (`src_type`, `load_type`) pair isn't
   supported fails on its own at stage `plan` with a clear message.
2. **Skip unchanged tables** (see below). Unchanged tables get their checkpoint advanced in one bulk
   Lakebase update — no source reads, no MERGEs, no empty Delta commits.
3. **Fan out.** Seeds, reseeds, and changed tables run in parallel. Each change set is read once
   from `CHANGETABLE(CHANGES ...)` and applied with an idempotent primary-key MERGE (deletes
   included); the checkpoint advances only after the MERGE commits.
4. **Record.** Per-table events and a run summary are written to Lakebase telemetry.

### Sources and load strategies

`control.src_type` selects a **source adapter** (`source_adapters.py`: JDBC URL and driver, quoting,
literals, and the SELECT / size / bounds queries) and `control.load_type` a **load strategy**
(`load_strategies.py`). Supported today:

| `src_type` | `ct` | `full` | `cursor` | `cdc` |
|---|---|---|---|---|
| `sqlserver` | yes | yes | yes | placeholder |
| `oracle` | — | yes | placeholder | placeholder |
| `postgresql` | — | yes | placeholder | placeholder |
| `mysql` | — | yes | placeholder | placeholder |

- **`cursor`** (SQL Server) reads rows above a high watermark — see
  [Cursor (watermark) tables](#cursor-watermark-tables).
- **`full`** reloads the whole table every run: one JDBC read into a data-only `INSERT OVERWRITE`
  (the table id, grants, clustering and properties are kept), or the staged partitioned swap when
  `load_partitioned`. No primary key needed. The checkpoint records `strategy = 'full'`, phase
  `loaded`, and no position.
- **Drivers.** The SQL Server and PostgreSQL JDBC drivers ship with Databricks Runtime. Oracle and
  MySQL need a cluster library — see the commented `libraries` entries in
  `resources/lakefed_ingest.yml`.
- **Per-engine notes.** Oracle: `src_database` is the service name, `src_schema` the owner, and
  identifiers are quoted, so use the stored (usually upper-case) names; the partitioned seed's size
  comes from `ALL_TABLES` statistics (no extra privileges). MySQL: `src_schema` is the database.
- **`jdbc_options`** (JSON object, optional) adds Spark JDBC read options per row, e.g.
  `{"fetchsize": "50000"}`. Connection, credential, driver and query options can't be overridden.

Checkpoints are generic: `checkpoint.strategy` names the strategy that wrote the row and
`checkpoint.position` (text) is its resume position — for `ct`, the database CT version; for
`cursor`, JSON `{"col", "type", "value"}` (the watermark column, its SQL Server type, and the value).

### Per-table state (Change Tracking)

`checkpoint.phase` moves `seeding → cdc`.

- **seeding:** capture `V0 = CHANGE_TRACKING_CURRENT_VERSION()` *before* the seed, copy the table,
  then checkpoint `{V0, cdc}`. The first CDC cycle replays everything that changed during the seed,
  so there is no gap.
- **cdc:** read changes since the checkpoint, MERGE, advance. If the checkpoint falls below Change
  Tracking's minimum valid version (retention lapsed), the table is queued for a reseed.

Delivery is at-least-once (checkpoint advances post-commit) and the MERGE is idempotent, so there
are no gaps and no duplicates.

### Cursor (watermark) tables

For tables without Change Tracking, `load_type = 'cursor'` follows a monotonically increasing
column, `cursor_column` (a PK is required — increments are MERGEd by it):

- **Seed:** capture the column's high watermark `hi` *before* the full copy (partitioned when
  `load_partitioned`), then checkpoint `hi`. Rows written during the seed are re-read next run.
- **Each run:** one small query reads the current `hi`. Equal to the checkpoint → unchanged (no
  read, no MERGE). Otherwise read `cursor_column > checkpoint AND cursor_column <= hi` once, upsert
  it by PK, then advance the checkpoint to `hi`.
- **Column types:** `rowversion` (recommended), integer / `decimal` / `numeric` (identity keys), and
  `date` / `datetime` / `datetime2` / `smalldatetime` / `datetimeoffset` (an `updated_at` the
  application maintains). Float, real and string columns are rejected.
- **Limits — choose the column accordingly:**
  - Deletes are never captured. Use `ct` where deletes matter, or a periodic `full` reload.
  - Updates are captured only if they move the column: `rowversion` always does, an identity never
    does (inserts only), and an `updated_at` only if every writer sets it.
  - A transaction that commits a value *below* an already-read watermark after the read is missed.
    `rowversion` avoids this: the watermark is capped below `MIN_ACTIVE_ROWVERSION()`, so rows from
    still-open transactions wait for the next run.
- Changing `cursor_column` re-seeds the table; adding columns to `select_list` reseeds it, as for
  `ct`. Nothing is `.cache()`d; Delta MERGE materializes the JDBC source
once (`materializeSource=auto`).

### Skip-no-change

Most tables in a large estate don't change between runs. Change Tracking versions are per
database, so detection is grouped by source database:

- **Per-database fast exit.** Capture the database's current CT version once. If every
  checkpoint is already at that version, the whole database is unchanged after one small query.
- **Batched detection.** Otherwise one `EXISTS`-style query per chunk of tables
  (`detection_chunk`, default 200) classifies each table as changed, unchanged, or needing reseed.
  The version is captured *before* detection, so changes after it are picked up next run.
- **Chunk fallback.** If a detection chunk fails, its tables take the per-table path instead of
  being guessed at. A database that fails entirely (unreachable, auth) has its tables recorded as
  failed at stage `detect`; other databases continue.
- **Read-time retention guard.** Retention can lapse between planning and a table's read in a long
  run. The change read carries a session guard that makes SQL Server raise if the checkpoint is no
  longer valid; the table is queued for a reseed instead of silently missing changes.
- **CT disabled / versions reset.** If Change Tracking is disabled on a database, its tables fail
  with a clear error (never treated as unchanged). Per-table CT status is read once per database
  per run (one metadata query, before the fast exit), so it is checked on quiet databases too:
  - A table with CT turned off is queued for a reseed. The seed then refuses to run (stage `read`,
    clear error) every run until CT is enabled on the table again, and then reseeds.
  - A table whose CT `begin_version` differs from the one recorded at seed time (CT turned off and
    back on between runs) is queued for a reseed (reason `ct_reenabled`).
  - A checkpoint ahead of the database's current version (restore) routes the table to a reseed.
  - **Limitation:** if CT is turned off and back on while the database's CT version still equals
    the table's original `begin_version` (no tracked commit anywhere in the database since CT was
    first enabled), the toggle leaves no trace in SQL Server metadata. Checkpoints created before
    `ct_begin_version` existed record it the first time they're seen, so a toggle before that
    upgrade isn't detected either. After CT maintenance like this, force a reseed:
    `update lakefed_ingest.checkpoint set phase = 'seeding' where control_id in (...);`

### Partitioned seed (large tables)

Set `load_partitioned = true`, `partition_col` (numeric, date, or datetime — usually an integer PK)
and `partition_size_mb`. The seed computes `num_partitions = max(size_mb / partition_size_mb, 2)`
and reads range predicates in parallel (NULLs in the first partition) into a staging table. Only
when every partition succeeds is the target replaced from staging in one atomic Delta commit, so
grants, row filters, and lineage are kept and readers never see a partial table. A failed seed
leaves the target untouched; the next attempt rebuilds staging.

### Consolidated sinks (optional)

By default each control row owns its sink table — typically one schema per source database. To
avoid one table per client per source table, set `control.client_id` and point the rows of many
source databases at the **same** sink (e.g. every client's `dbo.orders` → `main.bronze.orders`):

- Every row written carries the row's `client_id` value in the column `__client_id__` (a
  reserved-looking name, so it can't clash with a real source column such as `client_id`); the sink
  is created with liquid clustering on it. To query it as `client_id`, use a view
  (`SELECT __client_id__ AS client_id, ...`).
- MERGEs match on (`__client_id__`, PK), so clients with the same key values never touch each other's
  rows, and a CT delete removes only that client's row.
- Seeds and full reloads replace only that client's rows
  (`INSERT WITH SCHEMA EVOLUTION INTO <sink> BY NAME REPLACE WHERE __client_id__ = '<id>' ...`). A
  partitioned seed stages in a per-client table, so clients can seed the same sink at once.
- Schema drift between clients is absorbed by schema evolution: a column one client has and
  another lacks is added to the sink and is NULL for the others.
- Checkpoints stay per control row, so each client's CT version / watermark is independent.
- `client_id` is per row, so consolidated and dedicated sinks can coexist (for different sink
  tables). It must match `^[A-Za-z0-9_.:-]{1,128}$`, and `__client_id__` must not be in `select_list`.

**Guards.** A unique index allows one row per (sink, `client_id`) — one row per dedicated sink.
A sink claimed by a dedicated row *and* consolidated rows fails those rows at stage `plan`, and a
write into an existing sink whose mode doesn't match (a dedicated row into a table with a
`__client_id__` column, or the reverse) fails that table — so no row can overwrite another's data.

**Write conflicts.** Clients (or jobs) writing the same sink at once can hit Delta concurrency
conflicts. A conflict retries the whole unit (re-read + write) with exponential backoff and full
jitter: 6 attempts, 1 s base, 60 s cap (env `LAKEFED_WRITE_RETRY_ATTEMPTS`, `_BASE_S`, `_MAX_S`). No
slot is held while waiting. Retries are logged (`[retry]`); if they become frequent, stagger the
collections writing the same sinks.

### Concurrency — one knob

The job's `parallelism` parameter is the maximum number of units of query work running at once in
a run: a change read + MERGE, a seed, one partition of a partitioned seed, a small source query, or
the staging swap. Each unit holds one slot of a shared budget and its own FAIR pool. A thread never
holds a slot while waiting on other work, so nested fan-out can't deadlock. The budget is per run;
concurrent runs of different collections each have their own.

**Compute.** The ingestion job runs on a job cluster (DBR 18, single user,
`spark.scheduler.mode=FAIR`) sized by the `node_type_id`, `driver_node_type_id`, and `num_workers`
bundle variables. It is driver-bound (per-table MERGE planning and Delta commits), so size the
driver first.

## Metadata (Lakebase)

All metadata lives in Lakebase (`src/lakefed_ingest/lakebase_schema.sql`), schema `lakefed_ingest`:

| Table | Holds |
|---|---|
| `control` | One row per (source database, table): source, credentials key, keys, sink, `task_collection` |
| `checkpoint` | Hot per-table state: strategy, position (CT version for `ct`, watermark for `cursor`), phase, status, freshness |
| `reseed_queue` | Tables queued for a reseed and why |
| `ingest_run`, `ingest_event` | Run- and table-level telemetry |

Column-level reference, a diagram, and the checkpoint lifecycle: [docs/lakebase_schema.md](docs/lakebase_schema.md).

Upgrading from an earlier deploy: re-run `lakefed_ingest_setup`. It renames `ct_checkpoint` →
`checkpoint` (`ct_version` → `position`), `sweep_run` → `ingest_run` (`sweep_id` → `run_id`), and
`ingest_event.ct_version_from/to` → `position_from/to` in place; data is kept. This only reaches the
existing data if the bundle points to the existing instance and database. Preview deployments used
different names and a different bundle, so follow
[Upgrading a preview deployment](docs/deploy.md#1-add-a-target) first; the defaults would create a new,
empty instance.

The setup also creates the one-row-per-sink unique index. If existing control rows already write
the same sink, it fails with `could not create unique index "control_sink_client_uq"`; those rows
were overwriting each other's data. Find them, point each at its own sink (or set `client_id` to
consolidate), and re-run the setup:

```sql
select lower(sink_catalog), lower(sink_schema), lower(sink_table), array_agg(id order by id)
from lakefed_ingest.control group by 1, 2, 3
having count(*) filter (where client_id is null) > 1;
```

The Lakebase database is registered in Unity Catalog (default catalog `lakefed_ingest_lakebase`),
so control and telemetry tables can be queried from DBSQL. See [docs/monitoring.md](docs/monitoring.md).

### Control rows

Key columns: `task_collection`, `src_type`, `src_host` / `src_port` (NULL port = the engine
default), `src_database`, `src_schema`, `src_table`, `secret_key`, `primary_key` (comma-separated;
required for `ct`, `cursor` and `cdc`), `select_list` (explicit columns — `*` isn't supported),
`sink_catalog` / `sink_schema` / `sink_table`, `load_type` (`ct` | `full` | `cursor` | `cdc`), the
optional partitioned-seed settings, `cursor_column` (required for `cursor`), `client_id`
(consolidated sinks), and `jdbc_options`.
`cdc_capture_instance` is reserved for the placeholder `cdc` strategy. Check constraints reject invalid combinations (e.g.
`ct` on a non-SQL Server source). See `notebooks/load_control_example.ipynb`.

**Schema changes.** Writes use schema evolution. Adding a column to `select_list` adds it to the
sink and reseeds that table so historical rows are backfilled. New sinks get
`delta.enableTypeWidening`; for older sinks run
`ALTER TABLE <sink> SET TBLPROPERTIES ('delta.enableTypeWidening' = 'true')`.

## Prerequisites

| Prerequisite | How |
|---|---|
| Lakebase instance, database, UC catalog | Bundle resources in `resources/lakefed_ingest_setup.yml` (created on deploy, or adopt existing ones with `bundle deployment bind`) |
| Schema + migrations | `lakefed_ingest_setup` job (idempotent; re-run after upgrades) |
| Lakebase auth | SDK-minted OAuth token at runtime; the run identity must be a Postgres role on the instance |
| Cluster → Lakebase network path (port 5432) | Workspace IP access list must allow cluster egress, or use Private Link. Verify first. |
| Per-database source credentials | One JSON secret `{"user","password"}` per source database, named by `control.secret_key`, in the `secret_scope` (may be Azure Key Vault-backed) |
| Cluster → source network path (SQL Server 1433, Oracle 1521, PostgreSQL 5432, MySQL 3306) | Customer networking |
| `ct` source tables: CT enabled + primary key, `ALLOW_SNAPSHOT_ISOLATION ON`, `CHANGE_RETENTION` longer than the seed time | Source-side configuration |
| Oracle / MySQL sources: JDBC driver library on the job cluster | Uncomment it in `resources/lakefed_ingest.yml` |

No UC connections, `remote_query`, or SQL warehouse are needed.

> Deploying `lakefed_ingest_setup.yml` **creates a billable Lakebase instance** unless you bind an
> existing one.

## Deploy and run

Step-by-step setup for a new workspace: [docs/deploy.md](docs/deploy.md).

```bash
# From the repo root (bundle lakefed_ingest_v2). Compute variables default to Azure node types.
databricks bundle deploy -t <target> -p <profile>

# One-time and after upgrades: apply / migrate the Lakebase schema.
databricks bundle run lakefed_ingest_setup -t <target> -p <profile>

# Load one secret per source database (or use an Azure Key Vault-backed scope).
databricks secrets put-secret <scope> <secret_key> --string-value '{"user":"..","password":".."}'

# Register tables: edit and run notebooks/load_control_example.ipynb.

# Ingest a task_collection (the first run seeds; later runs apply Change Tracking increments).
databricks bundle run lakefed_ingest -t <target> -p <profile> \
  --params task_collection=<collection>,parallelism=32
```

The job ships with a paused 15-minute schedule. Job schedules can't pass parameters, so the
schedule only ingests the job's default `task_collection`; run other collections with `run-now`
and `job_parameters` (a launcher over collections is planned).

**Monitoring:** run and per-table telemetry, failures, and freshness queries are in
[docs/monitoring.md](docs/monitoring.md).

**Sizing a source estate:** [`tools/source_profile.sql`](tools/source_profile.sql) is a read-only
script to run once per SQL Server database. It reports table sizes, CT settings, and how many
tables and rows change per time bucket — the inputs for choosing collections, `parallelism`, and
cluster size.

## Tests

```bash
pytest                              # both versions (testpaths in pytest.ini)
pytest tests                        # default version only
pytest job_orchestration/tests      # job orchestration version only
```

Unit tests need no cluster. `tests/validation_test.py` runs against a live SQL Server when the
`LAKEFED_TEST_SQLSERVER_*` environment variables are set and is skipped otherwise.

## Not yet supported

The `cdc` load strategy (placeholder with a documented contract in `load_strategies.py`),
`cursor` and `cdc` for sources other than SQL Server (placeholders), and production alerting.
