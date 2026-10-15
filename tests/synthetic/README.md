# Synthetic Change Tracking test database

Tools for standing up a SQL Server source with Change Tracking (CT) for development and testing,
when no copy of a real database is available. They build a database, apply changes to it, and check
that the ingested Delta tables match the source.

| File | What it does |
|---|---|
| `build_synth_db.py` (notebook) | Creates 1,800 tables, loads deterministic data, then enables CT on the database and every table |
| `change_generator.py` (notebook) | Inserts, updates and deletes rows in a seeded sample of the tables; writes a manifest of what it changed |
| `reconcile.py` (notebook) | Compares source and sink for the manifest's tables (or all tables) and reports mismatches |
| `synth_catalog.py` | The table catalog: names, columns, primary keys and row counts (pure Python) |
| `synth_sql.py`, `synth_changes.py` | T-SQL builders used by the notebooks (pure Python; unit-tested in `tests/synthetic_test.py`) |

## The database

The catalog models a property-management suite: 18 modules of 100 tables each, such as `prop_*`,
`ar_*` and `gl_*`, all in schema `dbo`. About 15% are empty, 50% are lookup or config tables, 25%
master/detail, 9% transaction/history, and 1% are very large. Tables have 10–60 columns (a few wide
ones have more than 100), and about 15% have composite primary keys.

The data is generated inside SQL Server with set-based `INSERT ... SELECT`, not pushed from Spark.
It's a deterministic function of `seed`, so a rebuild produces the same data. CT is enabled after
the load, so the initial rows aren't tracked as changes.

`scale_factor` sets the size:

| `scale_factor` | Rows | Approximate size | Largest table |
|---|---|---|---|
| 0.01 | 4.6 M | 0.8 GB | 56 MB |
| 0.1 (default) | 46 M | 8.4 GB | 560 MB |
| 1.0 | 455 M | 84 GB | 5.6 GB |

Use `scale_factor` 0.01 for a fast dev loop. Use 0.1 or more to exercise partitioned seeds.

## Prerequisites

- A cluster with Databricks Runtime (the bundled SQL Server JDBC driver is used) that can reach the
  SQL Server.
- An empty database on Azure SQL Database, SQL Server on a VM, or on-premises SQL Server. Compatibility
  level 160 or later uses `GENERATE_SERIES`; earlier levels fall back to a cross join of `sys.all_columns`.
- A login that can create tables and enable Change Tracking (`db_owner` on the database is simplest).
- A secret holding JSON `{"user": "...", "password": "..."}`. It has the same format as the ingestion
  job's per-database secrets, so one secret can serve both.
- For `load_type` `ct`, the ingestion prerequisites also apply (see the main README).

## Run

Import this folder into the workspace, keeping the `.py` modules next to the notebooks. Then run the
notebooks with these widgets (all three share the connection widgets):

| Widget | Default | Meaning |
|---|---|---|
| `host` | (required) | SQL Server host name |
| `port` | `1433` | SQL Server port |
| `trust_server_certificate` | `false` | `true` skips certificate validation, for dev servers with a self-signed certificate |
| `database` | (required) | Database name |
| `secret_scope`, `secret_key` | (required) | The credential secret |
| `seed` | `42` | Catalog and data seed; keep it the same across all three notebooks |
| `scale_factor` | `0.1` | Size (see above); keep it the same across build and changes |

1. **Build:** run `build_synth_db`. Its other widgets are `threads` (12), `batch_rows` (1,000,000) and
   `ct_retention_days` (3). It's safe to rerun: a table that already has the expected row count is skipped, and a partial one
   is rebuilt. For several tenant databases, run it once per database.
2. **Register and seed:** add control rows for the tables with `notebooks/load_control_example.ipynb`.
   `synth_catalog.build_catalog(seed)` gives each table's columns and primary key. Then run the
   `lakefed_ingest` job to seed them.
3. **Change:** run `change_generator`. It needs a `manifest_table` (a Delta table, created on first
   use). `fraction` is the share of non-empty tables to change (default 0.05); `inserts`, `updates` and
   `deletes` are per table; `run_label` names the run (default: a timestamp).
4. **Ingest:** run the `lakefed_ingest` job again.
5. **Reconcile:** run `reconcile` with `bronze_schema` (the sinks' `catalog.schema`; sink tables must
   have the source table names), `manifest_table` and the `run_label` from step 3. Leave `run_label`
   blank to check every table. It compares row count, the sum of the leading primary-key column, and an
   aggregate of the column the generator updates. The summary lists `tables_checked`, `matched`, and up to 50 `mismatched` tables with their differences.

The build and change notebooks write to the database; reconcile only reads it.
