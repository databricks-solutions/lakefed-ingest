# Multi-tenant ingestion

An evolution of the accelerator for **multi-tenant ISV** sources (many isolated SQL Server
tenant databases, per-tenant credentials). The framework is **strategy-agnostic**: it fans out
ingestion across thousands of tenant databases and keeps hot per-(db,table) state in Lakebase,
and it can host multiple incremental strategies over time. **SQL Server Change Tracking (CT)**
is the **first** strategy — the incremental primitive for ISV sources that lack a reliable
monotonic watermark column.

Background: the field note *"Multi-Tenant CT Ingestion — EIA Enhancements"* and `docs/adr/0001–0004`.

**v1 scope:** the governed UC path — **UC Connections + `remote_query`** for the CT reads (reuses
the production federation/DBSQL engine), **all metadata consolidated in Lakebase (OLTP Postgres)**
— cold config/placement *and* hot per-(db,table) checkpoints in one store — and a **seed → CT
handoff**. The Spark-JDBC + Key Vault transport is **deferred** to a future >1,000-instance
expansion (ADR-0001).

Ships as **its own jobs and its own metadata store**; the production `lakefed_ingest_*` jobs,
control table, and `src/lakefed_ingest/` are untouched (ADR-0003 non-negotiable).

## Naming convention (strategy-agnostic framework, strategy-specific modules)

CT is the first strategy, not the whole framework — so names are generic at the framework level
and only carry a strategy suffix where the logic is strategy-specific:

| Scope | Convention | Example |
|---|---|---|
| Folder / jobs / bundle resources | generic | `multitenant/`, `lakefed_ingest_mt_sweep`, `_ingest`, `_setup` |
| Task selection (framework-level) | generic | `list_task_configs`, `task_collection` |
| Shared config table | generic + a `load_type` discriminator | `control` (`load_type in ('full','incremental','ct')`) |
| Strategy-specific state & modules | strategy suffix is fine | `ct_checkpoint`, `reseed_queue`, `copy_data_ct`, `ct_current_version`, `reseed_check` |

## Layout

```
multitenant/
  src/multitenant/
    copy_data_sweep.ipynb     # cluster: ingest a WHOLE task_collection in one task (threads + FAIR pools) [scale path]
    parallel.py               # in-process thread-pool + FAIR-pool engine for the sweep — unit-tested
    state_store.py            # Lakebase accessor (config + hot state) + pure helpers — unit-tested
    lakebase_schema.sql       # Postgres DDL: control, ct_checkpoint, reseed_queue
    apply_lakebase_schema.ipynb  # cluster: apply lakebase_schema.sql
    get_task.ipynb            # cluster: read one config row from Lakebase -> task values (single-table path)
    get_checkpoint.ipynb      # cluster: read checkpoint -> task values (single-table path)
    checkpoint_seed.ipynb     # cluster: seeding -> cdc, checkpoint V0 (single-table path)
    advance_checkpoint.ipynb  # cluster: advance version AFTER bronze commit (single-table path)
    mark_reseed.ipynb         # cluster: enqueue reseed + phase->seeding (single-table path)
    create_sqlserver_connection.sql  # helper: CREATE CONNECTION per SQL Server instance
    ct_engine.py              # [STUB — Alex, A/B/F] source/CT ops the sweep calls (current/min_valid version, read+merge, seed, ensure_sink)
    ct_current_version.sql    # [STUB — Alex, workstream B] CHANGE_TRACKING_CURRENT_VERSION() via remote_query
    reseed_check.sql          # [STUB — Alex, workstream F] CHANGE_TRACKING_MIN_VALID_VERSION() via remote_query
    copy_data_ct.ipynb        # [STUB — Alex, workstream A] CHANGETABLE(CHANGES,@v) via remote_query + PK MERGE
  notebooks/
    load_control_example.ipynb   # cluster: upsert an example cohort into Lakebase control
  tests/                      # unit tests (state_store builders + decide_action; parallel engine)
../resources/
  lakefed_ingest_mt_sweep.yml        # [scale path] ingest a whole task_collection in ONE task (threads + FAIR pools)
  lakefed_ingest_mt_ingest.yml       # [debug] single-table: seed->CT->merge->advance (one task per step)
  lakefed_ingest_mt_setup.yml        # prerequisites: Lakebase instance/catalog/secret scope + schema job
```

The job YAMLs live in the repo-level `resources/` (auto-included by the existing bundle)
so they can reuse the production `src/lakefed_ingest/*` files by relative path. Single bundle,
separate jobs — isolation is enforced by distinct jobs + a distinct metadata store, not by a
separate bundle root (which would break file reuse across DAB sync roots).

## Collaboration

Two contributors build this together on the `multitenant-ingestion` branch. Work is split by
file ownership so the two halves don't collide — the full plan (contracts, work division,
definition of done, dev workflow) lives in the **Contributor Plan** doc.

- **Control plane (this scaffold):** Lakebase schema + `state_store`, the sweep orchestrator
  (`copy_data_sweep` + `parallel.py`), the single-table ingest + setup jobs, and prerequisite
  automation.
- **CT capture engine (stubs here, owned by Alex):** for the consolidated sweep — `ct_engine.py`
  (`current_version` / `min_valid_version` / `read_and_merge_ct` / `seed` / `ensure_sink_table`);
  for the single-table debug path — `copy_data_ct.ipynb` (A), `ct_current_version.sql` (B),
  `reseed_check.sql` (F). Plus the test SQL Server + change-generator validation harness and
  `tests/ct_builders_test.py`. The stubs carry implementation notes/placeholders so the jobs wire
  up and deploy before the engine is written.

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
work the sweep calls lives in `ct_engine.py` (Alex, A/B/F); per-table failures are isolated and
written to the Lakebase checkpoint `status`.

**Scaling & future orchestration.** Shard by `task_collection` — size each collection to what one
cluster can sweep within the cadence window, and run multiple collections in parallel (one sweep
run each). Heavier orchestration can be layered on later without changing the sweep: e.g. a thin
launcher that fans out over collections (a small `for_each`, far under the limits), or a
partitioned backfill job for large one-time seeds.

**Single-table debug path.** `lakefed_ingest_mt_ingest` keeps the explicit one-task-per-step state
machine (one run per table), handy for developing and debugging A/B/F on a single table. Retained,
but not the default entry point.

## Single metadata store (Lakebase)

All metadata lives in Lakebase (`lakebase_schema.sql`): `control` (cold config/placement) and
`ct_checkpoint` + `reseed_queue` (CT hot state). There is **no Delta control table**. Because a
DBSQL warehouse cannot write to Lakebase — and we need the cluster for state writes anyway —
config **reads** also run on the cluster via the Postgres client: the sweep (`copy_data_sweep`)
loads a whole collection's configs with `list_task_configs` and drives the per-table state machine
in-process; the single-table debug path uses the `get_task` notebook (publishing a row as job
**task values** for the downstream warehouse SQL tasks, `{{tasks.get_task.values.*}}`).

## State machine (per tenant db/table, CT strategy)

Phase lives in Lakebase (`ct_checkpoint.phase`): `seeding → cdc`.

- **seeding:** capture `V0 = CHANGE_TRACKING_CURRENT_VERSION()` **before** the seed →
  full seed copy (reused `copy_data.ipynb`) → checkpoint `{V0, cdc}`.
- **cdc:** reseed check (`min_valid_version > checkpoint` → `mark_reseed`) → capture upper-bound
  version → `CHANGETABLE(CHANGES, checkpoint)` read + idempotent PK MERGE into bronze (deletes
  applied) → advance checkpoint to the captured version **after** the merge commits.

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
| **Schema** (`control`, `ct_checkpoint`, `reseed_queue`) | `lakefed_ingest_mt_setup` job → `apply_lakebase_schema.ipynb` |
| Lakebase **auth** | SDK-minted OAuth token at runtime — no secret to set; the run principal must be a Postgres role on the instance |
| **Secret scope** `lakefed_ingest_mt` (SQL Server password) | `secret_scopes` resource; set the value: `databricks secrets put-secret lakefed_ingest_mt password` |
| **UC Connection** per SQL Server instance | `create_sqlserver_connection.sql` helper (not a DAB resource; needs SQL Server creds) |
| `remote_query` enabled; SQL Server tables **CT-enabled + PK**, `ALLOW_SNAPSHOT_ISOLATION ON`, `CHANGE_RETENTION` > seed time | workspace/account + source-side config (external) |
| **DBSQL warehouse** (`warehouse_id` var) for governed reads | existing workspace warehouse |

Lakebase connection details are not configured by hand: the cluster notebooks resolve the
instance DNS and mint a short-lived token via the Databricks SDK, driven by the job parameters
`db_instance` (default `lakefed-ingest-mt`) and `db_name` (default `lakefed_ingest_mt`).

> Deploying `lakefed_ingest_mt_setup.yml` **creates a billable Lakebase instance.**

## Deploy & run

```bash
# From repo root (single bundle; deploys prod + mt jobs + mt setup/infra to the chosen target).
databricks bundle deploy -t <target> -p <profile> --var warehouse_id=<id> --var mt_cluster_id=<cluster_id>

# One-time setup:
#  1. Apply the schema:          databricks bundle run lakefed_ingest_mt_setup
#     (Lakebase auth is an SDK-minted token — nothing to put for Lakebase itself.)
#  2. Create UC connection(s):   put the SQL Server password, then run
#       databricks secrets put-secret lakefed_ingest_mt password
#     and run src/multitenant/create_sqlserver_connection.sql per instance
#     (params: connection_name, host, port, user, sqlserver_scope).
#  3. Register the cohort:       edit + run notebooks/load_control_example.ipynb

# Run one sweep per task_collection (shard via task_collection for horizontal scale):
databricks bundle run lakefed_ingest_mt_sweep --params task_collection=<collection>
#   run twice per collection: cycle 1 seeds, cycle 2 does the first CT increment
```

> The CT capture engine (`copy_data_ct`, `ct_current_version`, `reseed_check`) ships as stubs;
> an end-to-end run only works once those workstreams land.

## Tests

```bash
pytest multitenant/tests      # state_store builders + decide_action; parallel.run_parallel (pools + failure isolation)
```

## Reuse tally (workstream H → closes ADR-0003)

Log each production module as reused-unchanged / adapted-pattern / net-new. Update as the work lands.

| Production asset | Disposition in v1 |
|---|---|
| `src/lakefed_ingest/get_src_tbl_metadata.sql` | **reused unchanged** |
| `src/lakefed_ingest/create_sink_table.ipynb` | **reused unchanged** (bronze DDL) |
| `src/lakefed_ingest/copy_data.ipynb` | **reused unchanged** (full seed) |
| `remote_query` string-builder idiom | **adapted** into `ct_current_version.sql`, `reseed_check.sql`, `copy_data_ct.ipynb` |
| `copy_data_incremental.ipynb` join-clause helper | **adapted** into the CT MERGE join/merge builders |
| `get_task.sql` / `get_task_ids.sql` | **retired** for MT — replaced by cluster notebooks reading Lakebase (config consolidation) |
| Partitioned seed (`generate_partitions.sql`, `copy_data_partitioned.ipynb`) | available for reuse (v1 uses full seed; partitioned seed = extension) |
| `databricks.yml`, existing `resources/*.yml`, prod control table | **untouched** |

Net-new is confined to `multitenant/` + the additive `resources/lakefed_ingest_mt_*.yml`, with
**zero invasive edits to shared/core code** (the only `databricks.yml` change is an additive
`mt_cluster_id` variable). Provisional read: additive + isolated ⇒ **module + separate jobs**
(ADR-0003 option b), not a fork. Confirm after an end-to-end cohort run.

## Non-goals (v1)

Spark JDBC + Key Vault (expansion), full 10k scale, the workbench app, deep schema-drift
handling, prod hardening/alerting, pursuing the connection-cap increase.
