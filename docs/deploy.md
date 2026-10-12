# Deploying lakefed_ingest

Everything the default version needs is a resource in its bundle (`lakefed_ingest_v2`, at the repo
root): the Lakebase instance and its UC-registered database, the secret scope, and the ingestion and
setup jobs on job clusters (`resources/lakefed_ingest*.yml`). `databricks bundle deploy` is
idempotent for resources the bundle owns. This page covers the steps the bundle can't do. Run all
commands from the repo root. (The job orchestration version is a separate bundle; see
`../job_orchestration/README.md`.)

## 1. Add a target

In `databricks.yml`, add a target for your workspace (placeholders shown):

```yaml
targets:
  dev:
    mode: development
    workspace:
      host: https://<your-workspace-host>/
    variables:
      node_type_id: Standard_D8ds_v5          # ingestion workers / setup single node
      driver_node_type_id: Standard_D16ds_v5  # the ingestion job is driver-bound: size this first
      num_workers: 2
      # Optional overrides (defaults shown):
      # lakebase_instance: lakefed-ingest
      # lakebase_database: lakefed_ingest
      # lakebase_catalog: lakefed_ingest_lakebase
      # secret_scope: lakefed_ingest
```

The default compute needs ~32 cores; a subscription with a small vCPU quota must use smaller nodes
or raise the quota first.

**Already have a Lakebase instance or secret scope?** Adopt it instead of creating a new one —
`bind` works for the instance, its database catalog, and the secret scope:

```bash
databricks bundle deployment bind lakebase <instance-name>        -t dev
databricks bundle deployment bind lakebase_catalog <catalog-name> -t dev
databricks bundle deployment bind secret_scope <scope-name>       -t dev
```

Before binding, set the matching bundle variables (`lakebase_instance`, `lakebase_database`,
`lakebase_catalog`, `secret_scope`) in your target; after binding, the bundle manages them. Existing
jobs can be adopted the same way (`bind lakefed_ingest <job-id>`, `bind lakefed_ingest_setup
<job-id>`). Run `databricks bundle plan -t dev` before the first deploy and confirm it deletes nothing.

**One catalog per Lakebase database.** A Lakebase database can be registered by only one UC
catalog. If the database is already registered (for example by a catalog created by hand), bind that
catalog or set `lakebase_catalog` to its name — creating a second catalog for it fails.

## 2. Deploy and apply the schema

```bash
databricks bundle deploy -t dev
databricks bundle run lakefed_ingest_setup -t dev   # idempotent; re-run after upgrades
```

> Deploying creates a **billable** Lakebase instance (unless you bound an existing one).

## 3. Steps outside the bundle

1. **Network: classic compute → Lakebase (Postgres, port 5432).** Verify this first. Classic
   clusters reach Lakebase over its endpoint, so the workspace IP access list must allow the
   clusters' egress, or use Private Link. A blocked connection fails with
   `External authorization failed ... blocked by Databricks IP ACL`.
2. **Postgres role.** The identity the jobs run as must be a Postgres role on the Lakebase instance
   (the deployer is one; add a role for a service principal if jobs run as one).
3. **Source credentials.** One secret per source database: JSON `{"user": "...", "password": "..."}`.
   The scope may be Azure Key Vault-backed — AKV secret names allow only letters, digits, and dashes.
   Each control row names its secret in `secret_key`.
4. **Control rows.** Register tables in the Lakebase `control` table (see `../README.md`, "Control
   rows", and `notebooks/load_control_example.ipynb`): `src_type`, `load_type`, source
   host/port/database/table, `secret_key`, `primary_key` (not needed for `full`), explicit
   `select_list`, sink, `task_collection`, and optional partitioned-seed settings.
5. **Oracle / MySQL sources only: JDBC driver.** Databricks Runtime bundles the SQL Server and
   PostgreSQL drivers but not Oracle's or MySQL's. Uncomment the matching Maven library in
   `resources/lakefed_ingest.yml` (pin an approved version) and redeploy. Connection notes:
   - Oracle: `src_database` = service name, `src_schema` = owner (use the stored upper-case names).
     The partitioned seed sizes tables from `ALL_TABLES` statistics (needs only `SELECT` on the
     table; gather statistics for accurate partition counts). TLS (TCPS) needs a wallet/truststore
     and a custom URL — not configured by the adapter yet.
   - PostgreSQL: TLS is always on — `sslmode=verify-full` when `trust_server_certificate` is
     `false`, `require` otherwise.
   - MySQL: `src_schema` = the database; `sslMode=VERIFY_IDENTITY` / `REQUIRED` likewise.

## 4. First run and verification

```bash
databricks bundle run lakefed_ingest -t dev --params task_collection=<name>
```

The first run seeds; later runs apply Change Tracking increments (`ct` rows) or reload the table
(`full` rows). Check progress and results with the telemetry queries in [monitoring.md](monitoring.md)
(`ingest_run`, `ingest_event`, per-table freshness).

**Upgrading an existing deployment:** re-run `lakefed_ingest_setup` after deploying. It renames the
earlier `ct_checkpoint` / `sweep_run` tables and their CT-specific columns to the generic
`checkpoint` / `ingest_run` names in place (data kept); re-running it is a no-op.

**Scheduling:** the ingestion job's schedule (paused by default) can't pass parameters, so it only ingests
the default `task_collection`. For per-collection cadence, deploy a job per collection or trigger runs
per collection; a launcher job is planned.

## Schema changes

Writes use schema evolution (`MERGE WITH SCHEMA EVOLUTION`, `INSERT WITH SCHEMA EVOLUTION OVERWRITE
... BY NAME`): adding a column to a control row's `select_list` adds it to the sink, and the job
reseeds that table so historical rows are backfilled. New sinks are created with type widening on;
for sinks created earlier run
`ALTER TABLE <sink> SET TBLPROPERTIES ('delta.enableTypeWidening' = 'true')`.
