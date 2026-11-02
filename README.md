# Lakehouse Federation Ingestion Framework

Metadata-driven ingestion from external databases into Delta tables on Databricks. This repo holds
two versions:

| | **Default version** (repo root) | **Job orchestration version** ([`job_orchestration/`](job_orchestration/README.md)) |
|---|---|---|
| Orchestration | One job task ingests a whole `task_collection` in-process (thread pool + Spark FAIR scheduler pools) | Jobs `for_each` tasks: one task run per table / partition |
| Compute | Job cluster only — no SQL warehouse | SQL warehouse + notebooks |
| Metadata / state | Lakebase (Postgres): control, checkpoints, reseed queue, telemetry | Delta control table |
| Source access | Spark JDBC with per-database credentials from a secret scope, or `remote_query` through a UC connection per row | Lakehouse Federation / `remote_query` via UC connections |
| Load types | SQL Server Change Tracking (`ct`) and watermark `cursor`, each with full / partitioned seed; `full` reload (partitioned or not) for every source. `cdc` and non-SQL Server `cursor` are placeholders | Full, watermark incremental, partitioned full |
| Sources | SQL Server (`ct`, `cursor`, `full`); Oracle, PostgreSQL, MySQL (`full`) | SQL Server, Oracle, PostgreSQL, Redshift, Synapse, IBM Db2 LUW |
| Bundle | `lakefed_ingest_v2` | `lakefed_ingest` |

The job orchestration version remains supported. Use it when you want warehouse-only compute,
watermark-based incremental loads from a source other than SQL Server, or a source the default
version doesn't support yet. Use the default version when you have many source databases or tables
(thousands to hundreds of thousands), per-database credentials, want many source databases
consolidated into shared tables, or need Change Tracking (incremental loads without a reliable
watermark column, deletes included).

An optional **operations app** (a Databricks App) for the default version is in [`app/`](app/README.md):
run and table observability, control-row management, and starting or cancelling runs. It's a
separate bundle, so workspaces that haven't approved Databricks Apps simply don't deploy it.

> **Upgrading from an earlier release (deploy-path change).** The original version moved from the
> repo root to [`job_orchestration/`](job_orchestration/README.md), unchanged and with the same bundle
> name and uuid, so deploying it **from `job_orchestration/`** updates your existing jobs in place.
> The repo root is now the default version, a different bundle (`lakefed_ingest_v2`): a
> `databricks bundle deploy` run from the root — including CI or scripts written for the old layout —
> doesn't touch your existing jobs, but creates the default version's resources, including a
> billable Lakebase instance. Update deploy automation to `cd job_orchestration` first. If you
> deployed a preview of the default version, follow
> [Upgrading a preview deployment](docs/deploy.md) before deploying either version.

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
  batch_append.py                  # batched appends: many tables -> one sink, one job, one commit
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
   included), or appended to an [append sink](#append-sinks-optional); the checkpoint advances only
   after the write commits.
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
- **Reading through a Unity Catalog connection** (optional). A row with `use_remote_query = true`
  reads through the connection named in `src_connection`, with `remote_query` on the same job
  cluster, instead of Spark JDBC. The connection holds the credentials, so the row needs no
  `secret_key`, `src_host` or `src_port`. It needs Databricks Runtime 18.3 or later (the job runs
  `18.x`), and the identity the job runs as needs `USE CONNECTION` on the connection and, because
  the job cluster is dedicated (single user), `MANAGE` too. The read options (adapter defaults and
  `jdbc_options`) become `remote_query` options, such as `fetchsize`; one it doesn't support fails
  the read. A change read's retention guard is part of its query. `snapshot_isolation` can't apply
  without a session statement, so the planner refuses it for such `ct` rows (with
  `READ_COMMITTED_SNAPSHOT ON` on the database, each read is consistent on its own). Spark JDBC and
  `remote_query` map source types separately, so keep each sink's rows on one of the two.

Checkpoints are generic: `checkpoint.strategy` names the strategy that wrote the row and
`checkpoint.position` (text) is its resume position — for `ct`, the database CT version; for
`cursor`, JSON `{"col", "type", "value"}` (the watermark column, its SQL Server type, and the value).

### Per-table state (Change Tracking)

`checkpoint.phase` moves `seeding → incremental`.

- **seeding:** capture `V0 = CHANGE_TRACKING_CURRENT_VERSION()` *before* the seed, copy the table,
  then checkpoint `{V0, incremental}`. The first incremental run replays everything that changed
  during the seed, so there is no gap.
- **incremental:** read changes since the checkpoint, MERGE, advance. If the checkpoint falls below Change
  Tracking's minimum valid version (retention lapsed), the table is queued for a reseed.
- **Change reads** stop at the version captured before detection, which the checkpoint then advances
  to: a change committed later waits for the next run instead of being read twice. Under the default
  READ COMMITTED, the change table and the base table are read at slightly different times; a change
  whose row was deleted in between is skipped (its delete arrives next run). Job parameter
  `snapshot_isolation = "true"` reads both as of one point in time instead (needs
  `ALLOW_SNAPSHOT_ISOLATION ON` on every source database).

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
- The column must be `NOT NULL`: a row with a NULL value is never `> checkpoint`, so increments
  would silently miss it. A nullable `cursor_column` fails the table with a clear error, checked on
  every run (a column altered to NULL after the seed is caught too).
- Changing `cursor_column` re-seeds the table; adding columns to `select_list` reseeds it, as for
  `ct`. Nothing is `.cache()`d; Delta MERGE materializes the JDBC source
once (`materializeSource=auto`).

### Skip-no-change

Most tables in a large estate don't change between runs. Change Tracking versions are per
database, so detection is grouped by source database:

- **Per-database fast exit.** One small query reads the database's current CT version and every
  table's CT state. If every checkpoint is already at that version, the whole database is
  unchanged after that one query.
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
    `update lakefed_ingest.checkpoint set phase = 'seeding', status = 'ok', pending_position = null,
    pending_run_id = null where control_id in (...);` (or Force reseed in the operations app)
- **Edited or missing sinks.** Each checkpoint records the source, sink and primary key it was
  written for. A `ct` / `cursor` table whose control row now points at a different source, sink or
  key, or whose sink table no longer exists, is reseeded rather than merging only new changes into
  the new place (a silent gap). The sink check is one `information_schema` query per sink catalog
  per run; it's skipped with a warning if a catalog can't be listed.

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
`__client_id__` column, or the reverse; a merge row into an append sink, or the reverse) fails that
table, so no row can overwrite another's data.

**Write conflicts.** Clients (or jobs) writing the same sink at once can hit Delta concurrency
conflicts. A conflict retries the whole unit (re-read + write) with exponential backoff and full
jitter: 6 attempts, 1 s base, 60 s cap (env `LAKEFED_WRITE_RETRY_ATTEMPTS`, `_BASE_S`, `_MAX_S`). No
slot is held while waiting. Retries are logged (`[retry]`); if they become frequent, stagger the
collections writing the same sinks.

### Append sinks (optional)

With `control.write_mode = 'append'` (load types `ct` and `full`), the sink is a change log rather
than a copy of the source table: nothing in it is updated or deleted, and every write adds rows
with four extra columns:

| Column | Value |
|---|---|
| `__change_op` | `I`, `U` or `D` for a Change Tracking change; `S` for a snapshot row (a seed, a reseed, or a `full` load) |
| `__change_version` | A change's commit version (`SYS_CHANGE_VERSION`); for a seed, the CT version captured before it; NULL for a `full` load |
| `__extracted_at` | When the rows were read |
| `__run_id` | The `ingest_run` that wrote them |

- A change row has the key from the change table and the other columns from the base table; a
  delete has only the key. Change Tracking returns the net change per key since the checkpoint, so
  a row updated several times between runs appears once, at its latest version.
- A seed or reseed appends the whole table as `S` rows: a complete snapshot of that client's table
  at one version. A reseed follows lapsed retention, CT turned off and on, or a version reset
  (changes that couldn't be read), so downstream, a key missing from a later snapshot was deleted.
  A `full` load appends a snapshot every run, so the sink grows by the table's size each run (fine
  for small tables and views; use merge mode for large ones). Treat each (`__client_id__`,
  `__run_id`) set of `S` rows as one snapshot.
- With `client_id`, many source databases append to one sink, created with liquid clustering on
  `__client_id__`. `client_attrs` adds identity columns: a JSON object such as
  `{"tenant_name": "Acme", "source_server": "sql01"}`, written as STRING columns on every row
  (merge sinks accept them too). Names are letters, digits and `_`, and must not collide with
  `select_list`.
- **No duplicates.** An append isn't idempotent the way a MERGE is. Before each `ct` append (a
  change set or a seed) the run records on the checkpoint the position the append reaches and its
  `run_id`; the checkpoint advance clears them. If a run stops in between, the next run looks in the sink for that client's
  rows with that `__run_id`: found, the append committed and the checkpoint advances (outcome
  `recovered`); not found, the table reads again from its checkpoint. If the row was meanwhile pointed at
  another source, sink or key (or its sink is gone), the table reseeds instead: that append belonged to the old place.
- Downstream (for example SCD2 or a current-state view), order a key's rows by `__change_version`.

**Batched appends.** With many source databases per sink, most of a run is per-table overhead: a
Spark job and a Delta commit for a few changed rows. Set the job parameter `append_batch_size`
(default `1`, off) to batch them: changed tables, seeds and `full` loads of append rows with a
`client_id` (not partitioned) that write the same sink are read together (one JDBC query per source
database, unioned by name, as one Spark job with a task per database) and appended in one Delta
commit. A batch holds one `parallelism` slot per table it reads, so raise `parallelism` with it
(source queries run on the executors, so their cores bound how many run at once). Change batches
run first, seed batches last. If a batch fails, the run first checks the sink for rows the batch
committed anyway, then splits the rest in halves and retries; a table that fails on its own takes
the per-table path, so it fails exactly as it would without batching. A batched table's
`ingest_event` row is written when its batch finishes, with `pool` = `batch<size>` and no row count
(the batch total is in the job output).

### Concurrency

The job's `parallelism` parameter is the maximum number of units of query work running at once in
a run: a change read + MERGE, a seed, one partition of a partitioned seed, a small source query, or
the staging swap. Each unit holds one slot of a shared budget and its own FAIR pool. A thread never
holds a slot while waiting on other work, so nested fan-out can't deadlock. The budget is per run;
concurrent runs of different collections each have their own.

**Defaults.** The bundle's defaults are what a one-database tuning sweep validated on one
Standard_D16ds_v5 driver with one Standard_D8ds_v5 worker: `parallelism` 192, `append_batch_size` 4
(8 loads the same, 16 is slower) and `spark_conf` `{"spark.sql.codegen.wholeStage": "false"}`, which
saves compiling every append's plan (the sinks' clustering on write) for about 26% more tables per second
on one cluster. 192 per run is many queries on one server when several collections run at once: on a
server that hosts other work, set `max_queries_per_source` below.

**Per-source cap.** Concurrent runs whose collections read the same server add up, so a source can
get many runs' `parallelism` at once. Set `max_queries_per_source` (default `0`, off) to cap the
queries on one source across every run that shares the Lakebase database. A source is a UC connection
(rows with `use_remote_query`) or a JDBC host and port. Each run holds a shared Lakebase advisory lock
per source it reads, on its collection-lock connection (released with it), and takes an equal share
of the cap: the cap divided by the runs holding that source's lock, at least 1 (so when more runs
read a source than the cap allows, the total is one query per run). Shares are recounted
every 15 seconds, so the total can pass the cap for up to 15 seconds after a run starts, until the
other runs shrink their shares; a share a run doesn't use isn't lent to the others. A unit that
queries a source takes one of its permits before its slot (a batch takes one per read; a batch
bigger than the share runs alone on that source). Requests for a source are admitted in arrival
order. The job output and `ingest_run.detail` list each source's share and the most permits in use
(`source_caps`). With the cap, `parallelism` can be sized for the compute and the cap for the source.

**Compute.** The ingestion job runs on a job cluster (DBR 18, single user,
`spark.scheduler.mode=FAIR`) sized by the `node_type_id`, `driver_node_type_id`, and `num_workers`
bundle variables. It is driver-bound (per-table MERGE planning and Delta commits), so size the
driver first.

### Table retries

By default no table is retried. The run's retry budget, `table_retry_budget`, defaults to `0`, so a table
that fails ends its run as failed, whatever the error. A job raises the budget in the app's Advanced
configuration (Tuning: Table retry budget), and only then do its tables get retried.

With the budget above `0`, a table that fails with a transient source error is tried again in the same run,
up to `table_retries` more times (job parameter, default `2`; `0` turns it off and the table waits for the next
run): a refused or dropped connection, a timeout, a deadlock, a cancelled query (SQLSTATE 57014), an Oracle
listener error (ORA-12514, ORA-12528), Azure SQL throttling, or Unity Catalog throttling a `remote_query` read
("TEMPORARILY_UNAVAILABLE: Too many requests") (`table_retry.py`). Errors that would fail again (a missing table,
a denied permission, a schema error, bad data (SQLSTATE class 22), a schema change under the write
(`DELTA_SCHEMA_CHANGE`, `INCOMPATIBLE_*`), a failed login when Spark passes on its cause) fail at once, as without
retries. An error it doesn't recognise gets one retry in the run, then fails.

With the budget at `0`, nothing is retried: a transient error fails at once, and so does an unrecognised one,
since its one retry comes out of the budget. `table_retries` only applies when the budget is above `0`.

- **Pauses** double with each attempt, with jitter: 5 to 10 s, then 10 to 20 s, up to 2 minutes
  (environment variables `LAKEFED_TABLE_RETRY_BASE_S`, default `10`, and `LAKEFED_TABLE_RETRY_MAX_S`,
  default `120`). A waiting table holds no `parallelism` slot.
- **Limits:** `table_retry_budget` (default `0`, which retries nothing) retries in the whole run, and a source server stops
  retrying for the rest of the run after 5 failed retries (`LAKEFED_TABLE_RETRY_PER_SOURCE`); retries
  that succeed don't count, so a short blip is retried in full. An outage that fails a server's tables
  all at once gets at most the budget's retries before any of them fails. Either way a source outage
  costs a few extra queries, not one per table. Retries take slots and per-source
  permits like any other work, so `parallelism` and `max_queries_per_source` still hold.
- **No duplicates:** in merge mode every write is idempotent. In append mode the next attempt first
  checks the sink for this run's rows of the table and appends again only when there are none, so an
  append whose commit landed before its statement failed is never written twice.
- **Records:** each attempt is its own `ingest_event` row: `retried` (with the error and the stage),
  then `ok` or `failed`, with `retry_count`. The run's `detail` has the totals (`table_retries`). See
  [monitoring.md](docs/monitoring.md).

A run whose tables failed ends as failed, after recording them, so its job's failure email and any run alerts fire.
A table that recovers on its retry is not a failure. The run's error text names the first five failing tables
with their errors and links the run's page. The `fail_on_table_errors` parameter no longer changes this; it is
still accepted so that older jobs load.

### Large estates

Job parameters for estates of thousands of databases and millions of tables (defaults keep the
behavior described above). Each defaults to the bundle variable of the same name, so a target can
set them:

| Parameter | Default | Effect |
|---|---|---|
| `max_seeds_per_run` | `0` (no limit) | At most this many seeds per run; the rest seed in later runs. Onboarding then takes several bounded runs, and tables already seeded keep getting their increments in each. Seeds whose last attempt failed go last |
| `freshness_interval_s` | `0` | Unchanged tables whose checkpoint is already current are written to Lakebase only when `last_success_at` is older than this. Nearly every table is quiet nearly every run, so this removes most of a run's Lakebase writes; `last_success_at` is then up to this stale |
| `append_batch_size` | `1` (off) | [Batched appends](#append-sinks-optional) for append sinks with `client_id` |
| `detection_chunk` | `200` | Tables per change-detection query. Set it to at least the tables per database, so a changed database costs one detection query |

Each run loads its collection's control rows into the driver (repeated values such as
`select_list` are shared, but each row is still a Python dict), so prefer more, smaller collections
over a few huge ones: for example a few hundred thousand tables each.

## Metadata (Lakebase)

All metadata lives in Lakebase (`src/lakefed_ingest/lakebase_schema.sql`), schema `lakefed_ingest`:

| Table | Holds |
|---|---|
| `control` | One row per (source database, table): source, credentials key, keys, sink, `task_collection` |
| `checkpoint` | Hot per-table state: strategy, position (CT version for `ct`, watermark for `cursor`), phase, status, freshness |
| `reseed_queue` | Tables queued for a reseed and why |
| `ingest_run`, `ingest_event` | Run- and table-level telemetry |

Column-level reference, a diagram, and the checkpoint lifecycle: [docs/lakebase_schema.md](docs/lakebase_schema.md).

Upgrading from an earlier deploy: pull and deploy. The database records its schema version
(`schema_version`); the first ingestion run whose code needs a newer one migrates the schema before it
starts, in one transaction, while runs of other collections wait for it. If the migration fails (for
example, existing rows break a new constraint), nothing changes and the run fails with the cause; fix
it and re-run. Running `lakefed_ingest_setup` after deploying migrates ahead of the first run instead,
and the migration needs the schema's owner (the deployer, unless the jobs run as someone else). From an
early deploy, the migration renames `ct_checkpoint` → `checkpoint` (`ct_version` → `position`),
`sweep_run` → `ingest_run` (`sweep_id` → `run_id`), and `ingest_event.ct_version_from/to` →
`position_from/to` in place; data is kept. It also renames the checkpoint phase `cdc` to `incremental`
(history included); a run still going on the older code then has its checkpoint writes rejected, and
the next run picks those tables up. This only reaches the existing data if the bundle points to the
existing instance and database. Preview deployments used
different names and a different bundle, so follow
[Upgrading a preview deployment](docs/deploy.md#1-add-a-target) first; the defaults would create a new,
empty instance.

The migration also creates the one-row-per-sink unique index. If existing control rows already write
the same sink, it fails with `could not create unique index "control_sink_client_uq"`; those rows
were overwriting each other's data. Find them, point each at its own sink (or set `client_id` to
consolidate), and re-run:

```sql
select lower(sink_catalog), lower(sink_schema), lower(sink_table), array_agg(id order by id)
from lakefed_ingest.control group by 1, 2, 3
having count(*) filter (where client_id is null) > 1;
```

The Lakebase database is registered in Unity Catalog (default catalog `lakefed_ingest_lakebase`),
so control and telemetry tables can be queried from DBSQL. See [docs/monitoring.md](docs/monitoring.md).

### Control rows

Key columns: `task_collection`, `src_type`, `src_host` / `src_port` (NULL port = the engine
default) and `secret_key`, or `src_connection` with `use_remote_query` (see
[Sources and load strategies](#sources-and-load-strategies)), `src_database`, `src_schema`,
`src_table`, `primary_key` (comma-separated;
required for `ct`, `cursor` and `cdc`), `select_list` (explicit columns — `*` isn't supported),
`sink_catalog` / `sink_schema` / `sink_table`, `load_type` (`ct` | `full` | `cursor` | `cdc`), the
optional partitioned-seed settings, `cursor_column` (required for `cursor`), `client_id`
(consolidated sinks), `write_mode` (`merge` | `append`, see [Append sinks](#append-sinks-optional)),
`client_attrs` (identity columns, JSON), `jdbc_options`, and `freshness_sla_minutes` (optional freshness
SLA for monitoring; see `docs/monitoring.md`).
`cdc_capture_instance` is reserved for the placeholder `cdc` strategy.

**Bulk onboarding.** The `lakefed_ingest_discover` job lists a SQL Server database's tables with
recommended settings (load type from Change Tracking / rowversion / primary key, select list, partition
column) into Lakebase. The optional operations app reviews them and creates control rows in bulk;
without the app, query `discovered_table` and insert control rows yourself. Check constraints reject invalid combinations (e.g.
`ct` on a non-SQL Server source). See `notebooks/load_control_example.ipynb`.
A whole server's databases are listed through `master` (the spec's `pattern` is a LIKE filter), or, for an
Azure SQL server whose login can't open `master`, through Azure Resource Manager: the spec's `azure_credential` (a
Unity Catalog service credential whose managed identity has Reader on the server; the job's runner needs `ACCESS` on
it) and `azure_resource_id` (`/subscriptions/<id>/resourceGroups/<group>/providers/Microsoft.Sql/servers/<server>`,
or `managedInstances/<instance>`). The databases are then read through the connection as usual.

**Schema changes.** Writes use schema evolution. Adding a column to `select_list` adds it to the
sink and reseeds that table so historical rows are backfilled. With the job parameter
`reseed_on_new_columns = "false"` the table carries on from its checkpoint instead: the column is
added and is NULL on rows written before (useful for append sinks, where a reseed appends a full
snapshot of every table that gained the column). New sinks get
`delta.enableTypeWidening`; for older sinks run
`ALTER TABLE <sink> SET TBLPROPERTIES ('delta.enableTypeWidening' = 'true')`.

## Prerequisites

| Prerequisite | How |
|---|---|
| Lakebase instance, database, UC catalog | Bundle resources in `resources/lakefed_ingest_setup.yml` (created on deploy, or adopt existing ones with `bundle deployment bind`) |
| Schema + migrations | `lakefed_ingest_setup` job on a new deployment (idempotent); after an upgrade, the first ingestion run migrates the schema itself |
| Serverless jobs compute (default for setup) | The `lakefed_ingest_setup` job (and the operations app's grant job) default to serverless: they only talk to Lakebase, so they don't wait for a cluster. Without serverless, point them at an all-purpose cluster ([docs/deploy.md](docs/deploy.md#compute-for-the-setup-and-discovery-jobs)). Discovery reads the sources, so it runs on the bundle's single-node all-purpose `lakefed_ingest utility` cluster, which stays up between runs (auto-terminates after an hour). Ingestion runs on job clusters |
| Serverless network path to the sources (check variant) | The check job, `lakefed_ingest_check`, reads the source databases from serverless compute, so serverless needs a network path to every source it checks (for example private endpoints through a network connectivity configuration, or source firewall rules), not only to Lakebase. Without one, run the check on a classic job cluster or an existing cluster, or use the default variant for those groups ([docs/deploy.md](docs/deploy.md#compute-for-the-check)) |
| Lakebase auth | SDK-minted OAuth token at runtime; the run identity must be a Postgres role on the instance |
| Cluster → Lakebase network path (port 5432) | Workspace IP access list must allow cluster egress, or use Private Link. Verify first. |
| Per-database source credentials | One JSON secret `{"user","password"}` per source database, named by `control.secret_key`, in the `secret_scope` (may be Azure Key Vault-backed); or, for rows with `use_remote_query`, a UC connection on which the job's identity has `USE CONNECTION` and `MANAGE`. The operations app's JDBC connections name a scope and key the same way ([app/README.md](app/README.md#jdbc-connections-entered-by-hand)) |
| Cluster → source network path (SQL Server 1433, Oracle 1521, PostgreSQL 5432, MySQL 3306) | Customer networking |
| `ct` source tables: CT enabled + primary key, `CHANGE_RETENTION` longer than the seed time (snapshot isolation is not required) | Source-side configuration |
| Oracle / MySQL sources: JDBC driver library on the job cluster | Uncomment it in `resources/lakefed_ingest.yml` |

No SQL warehouse is needed, and UC connections only for rows that read through one.

> Deploying `lakefed_ingest_setup.yml` **creates a billable Lakebase instance** unless you bind an
> existing one.

## Deploy and run

Step-by-step setup for a new workspace: [docs/deploy.md](docs/deploy.md).

```bash
# From the repo root (bundle lakefed_ingest_v2). Compute variables default to Azure node types.
databricks bundle deploy -t <target> -p <profile>

# One-time: create the Lakebase schema. (Upgrades migrate it on the next ingestion run.)
databricks bundle run lakefed_ingest_setup -t <target> -p <profile>

# Load one secret per source database (or use an Azure Key Vault-backed scope). Rows that read
# through a UC connection (use_remote_query) need none.
databricks secrets put-secret <scope> <secret_key> --string-value '{"user":"..","password":".."}'

# Register tables: edit and run notebooks/load_control_example.ipynb.

# Ingest a task_collection (the first run seeds; later runs apply Change Tracking increments).
databricks bundle run lakefed_ingest -t <target> -p <profile> \
  --params task_collection=<collection>,parallelism=32
```

The job ships with a paused 15-minute schedule. Job schedules can't pass parameters, so the
schedule only ingests the job's default `task_collection`; run other collections with `run-now`
and `job_parameters`.

**The check first:** the `lakefed_ingest_check` job runs on serverless over a group of collections
(`task_collections`, blank for every collection with enabled rows), asks each source database
whether anything changed, stamps the unchanged ones, and starts `lakefed_ingest` (run-now) only for
the collections with work, each for its databases with work. A quiet cycle starts no ingest cluster.
The check then waits for the runs it started and fails if one of them failed (so the check's failure email
fires). `timeout_s` (default 21600, six hours; 0 = no limit) bounds that wait: the runs still going are cancelled
and the check fails. While it waits, the check's serverless compute is idle.

```bash
databricks bundle run lakefed_ingest_check -t <target> -p <profile> --params task_collections=<c1>,<c2>
```

Its schedule (paused) checks every collection; an operations-app schedule of the `check` variant
starts it with its members instead.

**Monitoring:** run and per-table telemetry, failures, and freshness queries are in
[docs/monitoring.md](docs/monitoring.md).

**Sizing a source estate:** [`tools/source_profile.sql`](tools/source_profile.sql) is a read-only
script to run once per SQL Server database. It reports table sizes, CT settings, and how many
tables and rows change per time bucket — the inputs for choosing collections, `parallelism`, and
cluster size.

## Compute options

- **The check** (`lakefed_ingest_check`) runs on serverless by default. A classic job cluster or an existing
  cluster can run it instead. The check reads the sources itself, so whichever compute it uses needs a network
  path to each source.
- **The operations app's SQL warehouse** is the bundle's own serverless Pro warehouse by default. A customer's
  Pro or serverless warehouse (version 2025.35 or above) can replace it.
- No bundle variable switches either one: a task has one compute reference, and YAML has no conditionals. Each
  alternative is a ready target override in [docs/deploy.md](docs/deploy.md#compute-for-the-check) and
  [docs/deploy.md](docs/deploy.md#the-apps-sql-warehouse).

## Tests

```bash
pytest                              # both versions (testpaths in pytest.ini)
pytest tests                        # default version only
pytest job_orchestration/tests      # job orchestration version only
```

Run `pytest` from the repo root (`pytest.ini` there sets the test paths for both versions). Unit
tests need no cluster. `tests/validation_test.py` runs against a live SQL Server when the
`LAKEFED_TEST_SQLSERVER_*` environment variables are set and is skipped otherwise.

For a dev or test source, `tests/synthetic/` builds a SQL Server database with 1,800 CT-enabled
tables, applies changes to it, and reconciles the sinks with the source; see
[tests/synthetic/README.md](tests/synthetic/README.md).

## Not yet supported

The `cdc` load strategy (placeholder with a documented contract in `load_strategies.py`),
`cursor` and `cdc` for sources other than SQL Server (placeholders), and production alerting.

## Help

Databricks support does not cover this project. For questions or bugs, open a GitHub issue and the
team will help on a best-effort basis.

## License

&copy; 2026 Databricks, Inc. All rights reserved. The source in this repository is provided subject to the Databricks License [https://databricks.com/db-license-source]. All included or referenced third party libraries are subject to the licenses set forth below.

| library | description | license | source |
|---|---|---|---|
| ag-grid-community | Data grid (operations app) | MIT | https://github.com/ag-grid/ag-grid |
| ag-grid-react | React wrapper for AG Grid (operations app) | MIT | https://github.com/ag-grid/ag-grid |
| certifi | Mozilla CA bundle (live SQL Server validation tests) | MPL-2.0 | https://pypi.org/project/certifi/ |
| @databricks/design-system | The Databricks design system's CSS tokens, copied into the operations app (`app/src/frontend/src/dbui/tokens.css`) | ISC | https://www.npmjs.com/package/@databricks/design-system |
| databricks-sdk | Databricks SDK for Python | Apache-2.0 | https://pypi.org/project/databricks-sdk/ |
| DM Sans | Font of the operations app (`app/src/frontend/src/dbui/DMSans-variable.woff2`, from the design system) | SIL Open Font License 1.1 | https://github.com/googlefonts/dm-fonts |
| fastapi | Web framework for APIs (operations app) | MIT | https://pypi.org/project/fastapi/ |
| httpx | HTTP client (operations app API tests) | BSD-3-Clause | https://pypi.org/project/httpx/ |
| ibm_db | IBM Db2 driver (Db2 test data generator, job orchestration version) | Apache-2.0 | https://pypi.org/project/ibm-db/ |
| Microsoft JDBC Driver for SQL Server | SQL Server JDBC driver (included in Databricks Runtime) | MIT | https://github.com/microsoft/mssql-jdbc |
| MySQL Connector/J | MySQL JDBC driver (optional cluster library) | GPL-2.0 with Universal FOSS Exception | https://github.com/mysql/mysql-connector-j |
| Oracle JDBC driver (ojdbc11) | Oracle JDBC driver (optional cluster library) | Oracle Free Use Terms and Conditions | https://www.oracle.com/downloads/licenses/oracle-free-license.html |
| pandas | Data manipulation and analysis (table profiler notebook) | BSD-3-Clause | https://pypi.org/project/pandas/ |
| PostgreSQL JDBC Driver | PostgreSQL JDBC driver (included in Databricks Runtime) | BSD-2-Clause | https://github.com/pgjdbc/pgjdbc |
| psycopg | PostgreSQL database adapter (v3), for Lakebase | LGPL-3.0 | https://pypi.org/project/psycopg/ |
| pydantic | Data validation using Python type hints (operations app) | MIT | https://pypi.org/project/pydantic/ |
| pyspark | Apache Spark Python API (provided by Databricks Runtime) | Apache-2.0 | https://pypi.org/project/pyspark/ |
| pytest | Testing framework | MIT | https://pypi.org/project/pytest/ |
| python-tds | SQL Server TDS client (live SQL Server validation tests) | MIT | https://pypi.org/project/python-tds/ |
| react | Library for building user interfaces (operations app) | MIT | https://github.com/facebook/react |
| react-dom | React DOM rendering (operations app) | MIT | https://github.com/facebook/react |
| react-router-dom | Routing for React (operations app) | MIT | https://github.com/remix-run/react-router |
| @tanstack/react-query | Data fetching and caching for React (operations app) | MIT | https://github.com/TanStack/query |
| @types/react | TypeScript types for React (operations app, build only) | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped |
| @types/react-dom | TypeScript types for React DOM (operations app, build only) | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped |
| typescript | TypeScript compiler (operations app, build only) | Apache-2.0 | https://github.com/microsoft/TypeScript |
| uvicorn | ASGI web server (operations app) | BSD-3-Clause | https://pypi.org/project/uvicorn/ |
| vite | Frontend build tool (operations app, build only) | MIT | https://github.com/vitejs/vite |
| @vitejs/plugin-react | React support for Vite (operations app, build only) | MIT | https://github.com/vitejs/vite-plugin-react |

Notices for the files the operations app ships with it:
- **@databricks/design-system** (ISC): Copyright (c) Databricks, Inc. Permission to use, copy, modify, and/or
  distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright
  notice and this permission notice appear in all copies. THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS
  ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO
  EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
  WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER
  TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
- **DM Sans** (SIL Open Font License, Version 1.1): Copyright 2014 The DM Sans Project Authors
  (https://github.com/googlefonts/dm-fonts). This Font Software is licensed under the SIL Open Font License,
  Version 1.1, available with a FAQ at https://openfontlicense.org.
