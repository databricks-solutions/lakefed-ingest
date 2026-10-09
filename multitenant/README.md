# Multi-tenant ingestion

An evolution of the accelerator for **multi-tenant ISV** sources (many isolated SQL Server
tenant databases, per-tenant credentials). The framework is **strategy-agnostic**: it fans out
ingestion across thousands of tenant databases and keeps hot per-(db,table) state in Lakebase,
and it can host multiple incremental strategies over time. **SQL Server Change Tracking (CT)**
is the **first** strategy — the incremental primitive for ISV sources that lack a reliable
monotonic watermark column.

Background: the field note *"Multi-Tenant CT Ingestion — EIA Enhancements"* and `docs/adr/0001–0004`.

**Scope:** source reads via **Spark JDBC on the cluster** with **per-database credentials from a
secret scope** (Databricks- or Azure Key Vault-backed) — no UC connections (avoids the
1,000-connections-per-metastore cap) and **no SQL warehouse**: the multi-tenant path runs entirely on
cluster compute. **All metadata lives in Lakebase (OLTP Postgres)** — cold config/placement, hot
per-table checkpoints, and run telemetry — with a **seed → CT handoff**.

Ships as **its own jobs and its own metadata store**; the production `lakefed_ingest_*` jobs,
control table, and `src/lakefed_ingest/` are untouched (ADR-0003 non-negotiable).

## Naming convention (strategy-agnostic framework, strategy-specific modules)

CT is the first strategy, not the whole framework — so names are generic at the framework level
and only carry a strategy suffix where the logic is strategy-specific:

| Scope | Convention | Example |
|---|---|---|
| Folder / jobs / bundle resources | generic | `multitenant/`, `lakefed_ingest_mt_sweep`, `_setup` |
| Task selection (framework-level) | generic | `list_task_configs`, `task_collection` |
| Shared config table | generic + a `load_type` discriminator | `control` (`load_type in ('full','incremental','ct')`) |
| Strategy-specific state & modules | strategy suffix is fine | `ct_checkpoint`, `reseed_queue`, `ct_engine.py` |

## Layout

```
multitenant/
  src/multitenant/
    copy_data_sweep.ipynb     # cluster: ingest a WHOLE task_collection in one task (threads + FAIR pools)
    ct_engine.py              # CT/source ops via Spark JDBC: current/min-valid version, read+MERGE, seed, ensure_sink
    parallel.py               # in-process thread-pool + FAIR-pool engine for the sweep — unit-tested
    state_store.py            # Lakebase accessor (config, checkpoints, telemetry) + pure helpers — unit-tested
    lakebase_schema.sql       # Postgres DDL + idempotent migrations: control, ct_checkpoint, reseed_queue, sweep_run, ingest_event
    apply_lakebase_schema.ipynb  # cluster: apply / migrate lakebase_schema.sql
  notebooks/
    load_control_example.ipynb   # cluster: insert an example cohort into Lakebase control
  docs/monitoring.md          # telemetry tables + canonical monitoring queries
  tests/                      # unit tests (no cluster) + validation harness
../resources/
  lakefed_ingest_mt_sweep.yml        # the ingestion job: one run sweeps a task_collection
  lakefed_ingest_mt_setup.yml        # prerequisites: Lakebase instance/catalog/secret scope + schema job
```

The job YAMLs live in the repo-level `resources/` (auto-included by the existing bundle). Single
bundle, separate jobs — isolation is enforced by distinct jobs + a distinct metadata store.

## Collaboration

Built on the `multitenant-ingestion` branch. The plan (contracts, work division, definition of done,
dev workflow) lives in the **Contributor Plan** doc. Control plane + orchestration + telemetry:
Chris. CT engine (`ct_engine.py`, workstreams A/B/F) + validation harness: Alex.

## Job hierarchy (consolidated sweep — scales past the workspace task-run limit)

```
lakefed_ingest_mt_sweep   ONE task per task_collection: reads all enabled tasks from `control`
                          and threads over them in-process (FAIR scheduler pools) ->
                          per table: seed / reseed / CT+MERGE / advance checkpoint
```

**Why a sweep instead of a run per table.** A workspace allows at most **2,000 concurrent task
runs** and **10,000 run-submits/hour**, so a task-per-table design (one job run per table) stalls
at full multi-tenant scale (10^5–10^6 tables at a 15–20 min cadence). The sweep collapses a whole
`task_collection` into **one** job task that iterates its tables with a thread pool + Spark **FAIR
scheduler pools** (`parallel.py`, adapted from the precursor accelerator's parallel-notebook
engine). Throughput is then bounded by **compute** (cluster cores), the intended cost model, not by
job-run count. The cluster must run the FAIR scheduler (`spark.scheduler.mode=FAIR`). All source/CT
work the sweep calls lives in `ct_engine.py`; per-table failures are isolated and written to the
Lakebase checkpoint `status` and `ingest_event` telemetry.

**Scaling & future orchestration.** Shard by `task_collection` — size each collection to what one
cluster can sweep within the cadence window, and run multiple collections in parallel (one sweep
run each). Heavier orchestration can be layered on later without changing the sweep: e.g. a thin
launcher that fans out over collections (a small `for_each`, far under the limits), or a
partitioned backfill job for large one-time seeds.

**Compute.** The sweep runs on a job cluster (DBR 18 LTS, single-user, `spark.scheduler.mode=FAIR`)
sized by the `mt_node_type_id` / `mt_driver_node_type_id` / `mt_num_workers` bundle variables. It
is **driver-bound** — each per-table MERGE costs driver-side planning plus a Delta commit — so size
the driver first. The job ships with a paused 15-minute schedule. Job schedules can't pass parameters, so that schedule
only sweeps the job's default `task_collection`; per-collection cadence needs a job per collection
(or a target-level override of the default) or a launcher over collections (planned). Ad-hoc runs
of any collection use `run-now` with `job_parameters`.

## Single metadata store (Lakebase)

All metadata lives in Lakebase (`lakebase_schema.sql`): `control` (cold config/placement),
`ct_checkpoint` + `reseed_queue` (CT hot state, keyed by the control task id so same-named databases
on different servers never collide), and `sweep_run` + `ingest_event` (telemetry). There is **no
Delta control table**. The sweep loads a collection's configs with `list_task_configs` and drives
the per-table state machine in-process; all state writes go through the Postgres client.

## State machine (per tenant db/table, CT strategy)

Phase lives in Lakebase (`ct_checkpoint.phase`): `seeding → cdc`.

- **seeding:** capture `V0 = CHANGE_TRACKING_CURRENT_VERSION()` **before** the seed →
  full seed copy (one JDBC read streamed into a Delta overwrite) → checkpoint `{V0, cdc}`.
- **cdc:** reseed check (`min_valid_version > checkpoint` → enqueue reseed, phase back to seeding) →
  capture upper-bound version → `CHANGETABLE(CHANGES, checkpoint)` read fed straight into an
  idempotent PK MERGE into bronze (deletes applied) → advance checkpoint to the captured version
  **after** the merge commits.

Each table's source is read **once** per cycle and nothing is `.cache()`d: row counts come from Delta
commit metrics, and Delta MERGE's own source materialization (`materializeSource=auto`) guarantees
the JDBC change set is evaluated once.

The seed→CT reconcile completes on the first cdc cycle after seeding: because `V0` is captured
before the seed, `CHANGETABLE(CHANGES, V0)` replays every insert/update/delete that occurred
during the seed. At-least-once (checkpoint advances only post-commit) + idempotent MERGE ⇒ no
gap, no dupe.

## Prerequisites

Automated by the bundle (`resources/lakefed_ingest_mt_setup.yml`), vs. the few that can't be:

| Prerequisite | How |
|---|---|
| **Lakebase OLTP instance** (`lakefed-ingest-mt`) | `database_instances` resource — created on deploy |
| **Lakebase database** `lakefed_ingest_mt` (UC-registered) | `database_catalogs` resource (`create_database_if_not_exists`) |
| **Schema + migrations** | `lakefed_ingest_mt_setup` job → `apply_lakebase_schema.ipynb` (idempotent; re-run after upgrades) |
| Lakebase **auth** | SDK-minted OAuth token at runtime — the run principal must be a Postgres role on the instance |
| Cluster → Lakebase **network path** | Classic compute reaches Lakebase on port 5432 subject to the workspace IP access list — allow the cluster egress or use Private Link. Verify this first in a new workspace. |
| **Per-database SQL Server credentials** | One JSON secret `{"user","password"}` per tenant database, named by `control.secret_key`, in the scope passed as the sweep's `secret_scope` parameter (default `lakefed_ingest_mt`; may be an **Azure Key Vault-backed** scope — AKV names allow only alphanumerics and dashes) |
| **Network path to each SQL Server** (port 1433) from the cluster | customer networking (VNet injection / hub-and-spoke) |
| SQL Server tables **CT-enabled + PK**, `ALLOW_SNAPSHOT_ISOLATION ON`, `CHANGE_RETENTION` > seed time | source-side config (external) |

No UC connections, `remote_query`, or SQL warehouse are needed for the multi-tenant path.

> Deploying `lakefed_ingest_mt_setup.yml` **creates a billable Lakebase instance.**

## Control rows

One row per (tenant database, table). Key columns: `task_collection`, `src_host` / `src_port`,
`src_database`, `src_schema`, `src_table`, `secret_key`, `primary_key` (comma-separated),
`select_list` (explicit comma-separated columns — `*` is not supported with Change Tracking),
`sink_catalog` / `sink_schema` / `sink_table`, `load_type='ct'`. See
`notebooks/load_control_example.ipynb`.

**Partitioned seed (large tables).** Set `load_partitioned = true`, `partition_col` (numeric, date or
datetime — usually an integer PK or the leading integer PK column) and `partition_size_mb`. The seed
sizes the table from the SQL Server catalog, computes `num_partitions = max(size_mb /
partition_size_mb, 2)` (same formula as the production partitioned load), and builds Spark-JDBC-style
range predicates (NULLs in the first partition). Partition count is bounded by size, not cluster
cores: a big table becomes many small range reads. Each range is read once and appended to a
**staging table** (`<sink>__seed_staging`); only when every partition succeeds is the target replaced
from staging in **one atomic Delta commit** (same table, so grants / row filters / lineage are kept)
and staging dropped. If any partition fails, the not-yet-started ones are cancelled, the target is
untouched (readers keep the previous complete data) and the table stays in `seeding`; the next
attempt rebuilds staging from scratch (pure planning logic in `partitions.py`).

**Concurrency — one knob.** The sweep's `parallelism` job parameter is the maximum number of *units
of query work* running at once in a run, whatever their type: a CT read+MERGE, a non-partitioned
seed, **one partition** of a partitioned seed, a small source query (CT version, min-valid version,
table size, bounds), or the staging→target swap. Each unit holds one slot of a shared
`parallel.WorkSlots(parallelism)` for its duration; Lakebase checkpoint/telemetry writes don't take a
slot. Threads may outnumber slots (table workers + partition workers), but a thread only runs Spark/
JDBC work while holding a slot and never holds one while waiting — e.g. a table waiting on its
partitions — so nested fan-out can't deadlock. Each slot maps to its own FAIR scheduler pool. The
budget is per sweep run: concurrent sweeps of different collections (separate job clusters) each have
their own. Peak units are reported in the sweep summary and `sweep_run.detail`.

## Deploy & run

Step-by-step setup for a new workspace (target template, adopting existing resources with
`bundle deployment bind`, network/Postgres-role/secret steps): [docs/deploy.md](docs/deploy.md).

**Schema changes:** writes use schema evolution — adding a column to `select_list` adds it to the
sink and the sweep reseeds that table (CT alone would leave historical rows NULL). New sinks get
`delta.enableTypeWidening`; for existing sinks run
`ALTER TABLE <sink> SET TBLPROPERTIES ('delta.enableTypeWidening' = 'true')`.

```bash
# From repo root (single bundle). Compute vars default to Azure node types; override per target.
databricks bundle deploy -t <target> -p <profile> --var warehouse_id=<id>   # warehouse_id is for the original (non-MT) jobs

# One-time / after upgrades:
databricks bundle run lakefed_ingest_mt_setup -t <target> -p <profile>     # apply/migrate the Lakebase schema
databricks secrets put-secret <scope> <secret_key> --string-value '{"user":"..","password":".."}'  # per tenant DB
#   (or create an Azure Key Vault-backed scope and pass it as secret_scope)
# Register the cohort: edit + run notebooks/load_control_example.ipynb

# Sweep a task_collection (cycle 1 seeds, later cycles apply CT increments):
databricks bundle run lakefed_ingest_mt_sweep -t <target> -p <profile> \
  --params task_collection=<collection>,parallelism=32,secret_scope=<scope>
```

Monitor progress live in `sweep_run` / `ingest_event` (see `docs/monitoring.md`).

## Tests

```bash
pytest multitenant/tests      # state_store builders + DDL checks; ct_engine builders + connection helpers; parallel engine
```

## Reuse tally (closes ADR-0003)

| Production asset | Disposition |
|---|---|
| `copy_data_incremental.ipynb` join-clause helper | **adapted** into the CT MERGE builders (`ct_engine.py`) |
| `get_task.sql` / `get_task_ids.sql`, warehouse SQL tasks | **not used** — the multi-tenant path is warehouse-free and reads config from Lakebase |
| Partitioned seed (`generate_partitions.sql` / original Python `get_partition_list`) | **adapted**: same size-bounded partition math in `partitions.py`, run in-process via `parallel.py` |
| Existing `resources/*.yml`, `src/lakefed_ingest/`, prod control table | **untouched** |

Net-new is confined to `multitenant/` + the additive `resources/lakefed_ingest_mt_*.yml` and bundle
variables ⇒ **module + separate jobs** (ADR-0003 option b), not a fork.

## Non-goals (v1)

Full 10k-database scale, the workbench app, deep schema-drift handling, CDC / cursor load modes,
consolidated (multi-tenant) bronze tables, and production alerting — tracked as follow-ups.
