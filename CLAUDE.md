# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

The repo holds **two versions** of a metadata-driven framework that ingests external databases into
Databricks Delta tables. Each is its own Databricks Asset Bundle:

| Version | Location | Bundle | Orchestration / compute / state |
|---|---|---|---|
| **Default** | repo root (`databricks.yml`, `resources/`, `src/lakefed_ingest/`, `tests/`) | `lakefed_ingest_v2` | One job task per `task_collection`, in-process threads + FAIR pools on a job cluster; Lakebase (Postgres) state; Spark JDBC; source adapters (SQL Server, Oracle, PostgreSQL, MySQL) + load strategies (SQL Server Change Tracking, full reload) |
| **Job orchestration** | `job_orchestration/` | `lakefed_ingest` | Jobs `for_each` tasks; SQL warehouse + notebooks; Delta control table; Lakehouse Federation / `remote_query` |

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
databricks bundle run lakefed_ingest_setup -t <target> -p <profile>   # apply/migrate Lakebase schema
databricks bundle run lakefed_ingest -t <target> -p <profile> --params task_collection=<name>

# Job orchestration version
cd job_orchestration
databricks bundle deploy --target prod --profile PROD
databricks bundle run --target prod --profile PROD
```

## Default version (repo root)

Warehouse-free: everything runs on cluster compute. No UC connections — source reads use Spark
JDBC with per-database credentials (JSON `{user,password}`) from a secret scope named by
`control.secret_key`. See `README.md`, `docs/deploy.md`, `docs/monitoring.md`.

### Lakebase state (`src/lakefed_ingest/lakebase_schema.sql`, schema `lakefed_ingest`)
- `control` — one row per (source database, table): `task_collection`, `src_type` (source adapter:
  `sqlserver` | `oracle` | `postgresql` | `mysql`), `src_host`/`src_port`, `src_database`, `src_schema`,
  `src_table`, `secret_key`, `primary_key`, `select_list` (explicit columns), sink, `load_type` (load
  strategy: `ct` | `full`; `cursor` | `cdc` are placeholders), partitioned-seed settings,
  `cursor_column`, `cdc_capture_instance`, `jdbc_options` (JSON). CHECK constraints enforce valid combos.
- `checkpoint` — generic: `strategy` + `position` (text; CT version for `ct`), phase (`seeding → cdc`
  for ct, `loaded` for full), status, freshness. `reseed_queue` (ct).
- `ingest_run`, `ingest_event` — telemetry (`run_id`, `position_from/to`). The DB is registered in UC
  (default catalog `lakefed_ingest_lakebase`) for DBSQL monitoring.
- DDL is applied by `apply_lakebase_schema.ipynb`, which splits on `;` — no `DO $$` blocks. Migrations
  must be idempotent (`if not exists`, `drop constraint if exists`). Renames (no IF EXISTS form in
  Postgres) go in `state_store.RENAMES`; the applier runs only the pending ones before the DDL.

### Jobs (`resources/`)
- `lakefed_ingest` → task `ingest` → `ingest.ipynb`: one run ingests a `task_collection`. Parameters:
  `task_collection`, `parallelism` (single budget of concurrent query units), `secret_scope`,
  `detection_chunk`, `trust_server_certificate`, `db_instance`, `db_name`.
- `lakefed_ingest_setup` → `apply_lakebase_schema.ipynb`; plus bundle resources for the Lakebase
  instance, its UC catalog, and the secret scope (`database_instances.lakebase`,
  `database_catalogs.lakebase_catalog`, `secret_scopes.secret_scope`).

### Run flow (`ingest.ipynb`)
1. Per-collection Lakebase advisory lock (duplicate run exits `skipped`).
2. Plan (`planner.py`, pure): unsupported (`src_type`, `load_type`) pair → fail per table (stage
   `plan`); `full` → reload; CT tables → seed / schema_reseed / detection candidate; group by
   (source database, load_type); capture CT current version once per DB → fast exit if all checkpoints current,
   else batched detection (`detection_chunk` tables per query); failed chunk → per-table fallback;
   checkpoint ahead of current version → reseed; CT disabled on the DB → fail loudly. Per-table CT
   status (one `sys.change_tracking_tables` query per DB, before the fast exit): CT off on a table,
   retention lapsed, or `begin_version` ≠ `checkpoint.ct_begin_version` (CT re-enabled) → reseed.
3. Unchanged tables: one bulk Lakebase checkpoint advance.
4. Per-table worker: full reload (`load_strategies.FullStrategy` → `ct_engine.seed`), seed (capture V0
   before seed; partitioned seed via staging + atomic swap),
   reseed enqueue, or CHANGETABLE read + idempotent PK MERGE (schema evolution) then checkpoint
   advance. The change read carries a sessionInitStatement retention guard.

### Modules
| File | Purpose |
|---|---|
| `src/lakefed_ingest/ct_engine.py` | Spark JDBC source ops: CT versions, detection, read + MERGE, seed, sink DDL |
| `src/lakefed_ingest/source_adapters.py` | Per-engine dialect (`src_type`): JDBC URL/driver/options, quoting, literals, select/size/bounds SQL |
| `src/lakefed_ingest/load_strategies.py` | Per-`load_type` strategy, position encoding, `supported()` matrix; cursor/cdc placeholders |
| `src/lakefed_ingest/planner.py` | Strategy routing + skip-no-change planning (no Spark/DB; unit-tested with fakes) |
| `src/lakefed_ingest/parallel.py` | Thread pool, FAIR pools, `WorkSlots` budget |
| `src/lakefed_ingest/partitions.py` | Partitioned-seed planning |
| `src/lakefed_ingest/state_store.py` | Lakebase accessor (psycopg, SDK-minted OAuth token) + pure helpers |
| `tests/conftest.py` | Puts `src/lakefed_ingest` on `sys.path` |
| `tests/synthetic/` | Synthetic SQL Server DB generator, change generator, reconcile |
| `tools/source_profile.sql` | Read-only SQL Server profiling script for sizing |

### Engineering rules
- No `.cache()` / `.persist()`; read each source once per cycle (Delta MERGE materializes the JDBC
  source itself).
- No SQL warehouse dependency in the default version.
- Checkpoints advance only after the Delta commit (at-least-once + idempotent MERGE).

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
