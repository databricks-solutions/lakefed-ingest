# Deploying multi-tenant ingestion

Everything the multi-tenant jobs need is a bundle resource (`resources/lakefed_ingest_mt_*.yml`):
the Lakebase instance + its UC-registered database, the secret scope, and the sweep/setup jobs on
job clusters. `databricks bundle deploy` is **idempotent** for resources the bundle owns — redeploying
updates or no-ops. This page covers the few steps DABs can't do.

## 1. Add a target

In `databricks.yml`, add a target for your workspace (placeholders shown):

```yaml
targets:
  mt_dev:
    mode: development
    workspace:
      host: https://<your-workspace-host>/
    variables:
      warehouse_id:                  # used only by the original (non-MT) lakefed_ingest jobs
        lookup:
          warehouse: "<warehouse name>"
      concurrency: 16
      mt_node_type_id: Standard_D8ds_v5          # sweep workers / setup single node
      mt_driver_node_type_id: Standard_D16ds_v5  # the sweep is driver-bound: size this first
      mt_num_workers: 2
```

The default compute needs ~32 cores; a subscription with a small vCPU quota must use smaller nodes
or raise the quota first.

**Already have a Lakebase instance or secret scope?** Adopt it instead of creating a new one —
`bind` works for the instance, its database catalog, and the secret scope:

```bash
databricks bundle deployment bind mt_lakebase <instance-name>        -t mt_dev
databricks bundle deployment bind mt_lakebase_catalog <catalog-name> -t mt_dev
databricks bundle deployment bind mt_secret_scope <scope-name>       -t mt_dev
```

Set the resource names in `lakefed_ingest_mt_setup.yml` to match before binding; after binding,
the bundle manages them.

## 2. Deploy and apply the schema

```bash
databricks bundle deploy -t mt_dev
databricks bundle run lakefed_ingest_mt_setup -t mt_dev   # idempotent; re-run after upgrades
```

> Deploying creates a **billable** Lakebase instance (unless you bound an existing one).

## 3. Steps outside the bundle

1. **Network: classic compute → Lakebase (Postgres, port 5432).** Verify this first. Classic
   clusters reach Lakebase over its endpoint, so the workspace IP access list must allow the
   clusters' egress, or use Private Link. A blocked connection fails with
   `External authorization failed ... blocked by Databricks IP ACL`.
2. **Postgres role.** The identity the jobs run as must be a Postgres role on the Lakebase instance
   (the deployer is one; add a role for a service principal if jobs run as one).
3. **Source credentials.** One secret per tenant database: JSON `{"user": "...", "password": "..."}`.
   The scope may be Azure Key Vault-backed — AKV secret names allow only letters, digits, and dashes.
   Each control row names its secret in `secret_key`.
4. **Control rows.** Register tables in the Lakebase `control` table (see `../README.md`, "Control
   rows", and `notebooks/load_control_example.ipynb`): source host/port/database/table, `secret_key`,
   `primary_key`, explicit `select_list`, sink, `task_collection`, and optional partitioned-seed
   settings.

## 4. First sweep and verification

```bash
databricks bundle run lakefed_ingest_mt_sweep -t mt_dev --params task_collection=<name>
```

The first run seeds; later runs apply Change Tracking increments. Check progress and results with the
telemetry queries in [monitoring.md](monitoring.md) (`sweep_run`, `ingest_event`, per-table
freshness).

**Scheduling:** the sweep job's schedule (paused by default) can't pass parameters, so it only sweeps
the default `task_collection`. For per-collection cadence, deploy a job per collection or trigger runs
per collection; a launcher job is planned.

## Schema changes

Writes use schema evolution (`MERGE WITH SCHEMA EVOLUTION`, `INSERT WITH SCHEMA EVOLUTION OVERWRITE
... BY NAME`): adding a column to a control row's `select_list` adds it to the sink, and the sweep
reseeds that table so historical rows are backfilled. New sinks are created with type widening on;
for sinks created earlier run
`ALTER TABLE <sink> SET TBLPROPERTIES ('delta.enableTypeWidening' = 'true')`.
