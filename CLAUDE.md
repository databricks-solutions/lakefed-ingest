# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

### Run Tests
```bash
pytest                        # all tests
pytest tests/udtf_test.py     # single file
pytest tests/udtf_test.py::test_eval  # single test
```

### Deploy
```bash
databricks bundle deploy                        # dev (default target)
databricks bundle deploy --target prod --profile PROD
databricks bundle run --target prod --profile PROD
```

## Architecture

This is a **metadata-driven ingestion framework** that moves data from external databases into Databricks Delta tables using Lakehouse Federation. All execution happens in Databricks Workflows (SQL Warehouse + notebooks) — there is no Python application server.

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
