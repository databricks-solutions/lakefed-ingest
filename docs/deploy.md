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
      node_type_id: Standard_D8ds_v5          # ingestion workers / the single-node utility cluster
      driver_node_type_id: Standard_D16ds_v5  # the ingestion job is driver-bound: size this first
      num_workers: 2
      # Optional overrides (defaults shown):
      # lakebase_instance: lakefed-ingest
      # lakebase_capacity: CU_1
      # lakebase_database: lakefed_ingest
      # lakebase_catalog: lakefed_ingest_lakebase
      # secret_scope: lakefed_ingest
      # Ingestion job parameter defaults, e.g. for a large estate (see README "Large estates"):
      # append_batch_size: "16"
      # max_seeds_per_run: "10000"
      # max_queries_per_source: "64"         # see README "Concurrency"
      # table_retry_budget: "20"              # retries per run; the default "0" retries no table (README "Table retries")
      # table_retries: "2"                    # attempts per table; used only while table_retry_budget is above 0
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

**Upgrading a preview deployment (Lakebase resources in the `lakefed_ingest` bundle).** Earlier
preview builds deployed the Lakebase instance (`lakefed-ingest-mt`), its catalog
(`lakefed_ingest_mt_pg`), the secret scope (`lakefed_ingest_mt`), and jobs `lakefed_ingest_mt_sweep` /
`lakefed_ingest_mt_setup` from the root `lakefed_ingest` bundle. That bundle name now belongs to
`job_orchestration/`, whose config no longer has those resources, so **a deploy from
`job_orchestration/` would delete them** (including all control/checkpoint data and source secrets).
Move them to this bundle before deploying either version:

```bash
# 1. Release them from the old bundle state (run from job_orchestration/; resource keys from the old config)
cd job_orchestration
databricks bundle deployment unbind mt_lakebase         -t <target>
databricks bundle deployment unbind mt_lakebase_catalog -t <target>
databricks bundle deployment unbind mt_secret_scope     -t <target>
databricks bundle deployment unbind lakefed_ingest_mt_sweep -t <target>
databricks bundle deployment unbind lakefed_ingest_mt_setup -t <target>
databricks bundle plan -t <target>        # must show 0 to delete
cd ..
```

2. In this bundle's target, set the old names, because the defaults point to a *new, empty* instance:
   `lakebase_instance: lakefed-ingest-mt`, `lakebase_database: lakefed_ingest_mt`,
   `lakebase_catalog: lakefed_ingest_mt_pg`, `secret_scope: lakefed_ingest_mt`.
3. Bind them here (`bind lakebase`, `bind lakebase_catalog`, `bind secret_scope`, and optionally the
   two jobs as `lakefed_ingest` / `lakefed_ingest_setup`), run `databricks bundle plan` and confirm it
   deletes nothing, deploy, then run `lakefed_ingest_setup` to migrate the schema in place.

If `unbind` reports that a key isn't in the state, that resource was never deployed by the old
bundle, so skip it.

**One catalog per Lakebase database.** A Lakebase database can be registered by only one UC
catalog. If the database is already registered (for example by a catalog created by hand), bind that
catalog or set `lakebase_catalog` to its name — creating a second catalog for it fails.

## 2. Deploy and apply the schema

```bash
databricks bundle deploy -t dev
databricks bundle run lakefed_ingest_setup -t dev   # idempotent
```

> Deploying creates a **billable** Lakebase instance (unless you bound an existing one).

### Compute for the setup and discovery jobs

The one-time and ad hoc jobs shouldn't wait several minutes for a job cluster to start:

- **Setup** defaults to serverless jobs compute: it only applies DDL to Lakebase. Its libraries
  (`psycopg`, the pinned SDK) come from a job environment. The operations app's grant job defaults to
  serverless too (see [app/README.md](../app/README.md)).
- **Discovery** reads the sources' catalogs, so it needs the source network path. It runs on the
  bundle's `lakefed_ingest utility` cluster: a small single-node all-purpose cluster that stays up
  between runs (onboarding is often several discovery runs in a row) and terminates after an hour
  without use. Deploying creates it, and Databricks starts a new cluster on creation. To use a cluster
  you already have, override the task's `existing_cluster_id` in your target. A source that is paused
  or resuming (Azure SQL serverless) is retried three times, with pauses doubling from 15-30 s.
  With the operations app, Discover on a source runs on the app bundle's small serverless SQL warehouse
  instead (`lakefed_ingest discovery`, 2X-Small, up to 2 clusters, stopping after 5 idle minutes: bundle variables
  `discovery_warehouse_size`, `discovery_warehouse_max_clusters`, `discovery_warehouse_auto_stop_mins`), through the
  Unity Catalog connection, so the test, the database list and the table reads answer in seconds; the warehouse
  needs the same network path to the sources (serverless network connectivity). The discovery job stays for listing
  through Azure and for sources read by host and password. See [app/README.md](../app/README.md), Discovery through
  the SQL warehouse.
- **Ingestion** runs on job clusters with the source network path (scheduled, recurring work).
- **The operations app's helper jobs**, `lakefed_ingest_cluster_copies` and `lakefed_ingest_delete_job`, run on
  serverless as the deployer. The delete job purges an app job's groups: a Delete keeps the job for
  `deleted_job_retention_days` (default 7, the same value as the app's `DELETED_JOB_RETENTION_DAYS`, so set both alike),
  then the daily run purges it, or Delete now purges it at once. The purge deletes the job's groups' rows in
  Lakebase (it owns the state database; the app has no `DELETE` privilege) and, when asked, drops the target tables
  only those groups write, so the deployer needs to be able to drop those tables (`MANAGE` or ownership in Unity
  Catalog). Deploy this bundle before the app, which looks both jobs up by name.
  The same job also removes one table from a live job (the Failing tables list's Remove), with its `control_ids`
  parameter: the table's control row, checkpoint and reseed rows go, its target table stays. The app's Remove needs
  this bundle, so deploy it first. A Remove started before it fails and removes nothing: the older job refuses a
  live job's run, and the app records the failure on the row.
- **The operations app's login scope** (the app bundle's `jdbc_login_scope`, default `lakefed_ingest_jdbc_logins`) holds
  the JDBC logins typed into the app. The app bundle creates it, and its deployer becomes the scope's creator and holds
  `MANAGE`. The app's service principal gets `WRITE` on that one scope and nothing else: not `MANAGE`, which would let
  it change who reads the scope, and not `READ`. The `lakefed_ingest_app_grants` job sets that grant through the Secrets
  API, as the deployer. The bundle declares no permission on the scope, because one naming the app's service principal
  fails at deploy, before the app exists. The scope has `prevent_destroy`, so destroying the app bundle won't delete the
  logins. This assumes the ingestion jobs run as that same deployer, so they can read the logins; deploy the app bundle
  as the user who runs the ingestion jobs, and check the jobs' run-as identity on your deployment. The audit log records
  each write with the value redacted (checked 30 Oct 2026). See the app's README, "JDBC connections entered by hand".
- **The check** (`lakefed_ingest_check`) runs on serverless by default and reads every source database it checks,
  so serverless compute needs a network path to the sources, not only to Lakebase: for example private
  endpoints through a network connectivity configuration (NCC), or source firewall rules that allow the
  serverless egress. A source that only a job cluster can reach is checked on a classic job cluster or an
  existing cluster instead, see [Compute for the check](#compute-for-the-check). Without either, its group
  should use the default variant.

If the workspace doesn't have serverless jobs (or serverless can't reach Lakebase there), run setup
on an all-purpose cluster instead, such as the utility cluster. It's a one-time or ad hoc job, often
run several times during initial setup, so a running cluster beats waiting for a job cluster to start
each time. In your target:

```yaml
targets:
  dev:
    resources:
      jobs:
        lakefed_ingest_setup:
          tasks:
            - task_key: apply_lakebase_schema
              environment_key: ""                 # clears the serverless environment
              existing_cluster_id: ${resources.clusters.lakefed_ingest_utility.id}   # or any all-purpose cluster
              libraries:                          # installed on that cluster
                - pypi:
                    package: "psycopg[binary]"
                - pypi:
                    package: "databricks-sdk==0.145.0"
```

The job keeps its (now unused) environment definition; the task runs on the cluster. The job's run-as
identity needs permission to attach to the cluster, and the cluster must reach Lakebase on port 5432.

### Compute for the check

The check job (`lakefed_ingest_check`) runs on serverless by default. Its one task uses the job
environment `lakebase_client`, which installs `psycopg[binary]` and `databricks-sdk==0.145.0`. The default
needs no setting. A classic job cluster or an existing cluster is set by a target override, below.

**Why a variable can't switch this.** A task takes one compute reference: `environment_key` (serverless),
`job_cluster_key` (an entry in the job's `job_clusters`), or `existing_cluster_id`. A task can't have two.
Each choice sets one field and clears the others, and YAML has no conditionals. A bundle variable fills one
string, and the schema (`databricks bundle schema`) types these fields as plain strings, so a variable
could only stand in for one field, not clear the rest. How the CLI treats an empty `job_cluster_key` or
`existing_cluster_id` is not confirmed, so each alternative is a target override. The setup job's
override under [Compute for the setup and discovery jobs](#compute-for-the-setup-and-discovery-jobs) uses
the same pattern (`environment_key: ""`).

**Classic job cluster.** The check runs on a job cluster sized by the bundle's own compute variables
(`node_type_id`, `driver_node_type_id`, `num_workers`). Put this in your target in `databricks.yml`:

```yaml
targets:
  dev:                                          # your target
    resources:
      jobs:
        lakefed_ingest_check:
          job_clusters:
            - job_cluster_key: check_cluster
              new_cluster:
                spark_version: 18.x-scala2.13   # the ingestion job's runtime; it is hard-coded there, no variable
                data_security_mode: SINGLE_USER
                node_type_id: ${var.node_type_id}
                driver_node_type_id: ${var.driver_node_type_id}
                num_workers: ${var.num_workers}
                custom_tags:
                  project: lakefed_ingest
          tasks:
            - task_key: check
              environment_key: ""               # clears the serverless environment
              job_cluster_key: check_cluster
              libraries:                        # the packages the serverless environment installs
                - pypi:
                    package: "psycopg[binary]"
                - pypi:
                    package: "databricks-sdk==0.145.0"
```

- The cluster needs Databricks Runtime 18.3 or later for `remote_query` (per the
  [remote_query docs](https://learn.microsoft.com/en-us/azure/databricks/query-federation/remote-queries)).
  Check the runtime `18.x` resolves to in your workspace.
- `SINGLE_USER` mode: the identity the job runs as needs `MANAGE` as well as `USE CONNECTION` on each
  connection the check reads through (the same `remote_query` page says single-user clusters need `MANAGE`).
- The cluster reaches Lakebase on port 5432 through the workspace IP access list or Private Link (step 3,
  item 1), and it needs a network path to each source, in place of the serverless NCC or firewall rules.
- The job keeps its environment definition, which this task no longer uses.

**Existing cluster.** An all-purpose cluster you already run. Only the cluster ID is set; no variable is needed:

```yaml
targets:
  dev:                                          # your target
    resources:
      jobs:
        lakefed_ingest_check:
          tasks:
            - task_key: check
              environment_key: ""               # clears the serverless environment
              existing_cluster_id: "<cluster-id>"
              libraries:
                - pypi:
                    package: "psycopg[binary]"
                - pypi:
                    package: "databricks-sdk==0.145.0"
```

- It must be a classic cluster on Databricks Runtime 18.3 or later, in the same network position as the
  classic option above (Lakebase on 5432, a path to each source).
- The job's run-as identity needs permission to attach to the cluster, as for the setup job.

### The app's SQL warehouse

The operations app runs its discovery SQL (the connection test, database lists, and table reads through
`remote_query`) on one SQL warehouse. The app gets its ID as `WAREHOUSE_ID`, from the app resource
`discovery-warehouse` in `app/resources/lakefed_ingest_app.yml`. Only `app/src/warehouse.py` reads it, from the
environment, so no app code holds a warehouse ID.

By default that resource is the bundle's own warehouse, `lakefed_ingest discovery`: Pro, serverless, sized by
`discovery_warehouse_size`, `discovery_warehouse_max_clusters` and `discovery_warehouse_auto_stop_mins` in
`app/databricks.yml`. It auto-stops when idle.

To use a customer's warehouse instead, it must be Pro or serverless, on version 2025.35 or above. The
[remote_query docs](https://learn.microsoft.com/en-us/azure/databricks/query-federation/remote-queries) say
"SQL warehouses must be Pro or Serverless and use version 2025.35 or above" (the AWS page is
[query-federation/remote-queries](https://docs.databricks.com/aws/en/query-federation/remote-queries)).
(The Runtime 18.3 requirement applies to clusters, not to SQL warehouses.)

**Why a variable can't switch this.** The app resource takes one warehouse ID, and the default is the ID of a
warehouse the bundle creates. YAML has no conditionals. The override below changes the `discovery-warehouse`
resource in a target of `app/databricks.yml`. It repeats every app resource, because whether a target replaces a
list or merges it is not confirmed. Run `databricks bundle plan` and confirm no app resource is removed.

```yaml
# app/databricks.yml
targets:
  dev:                                           # your target
    resources:
      apps:
        lakefed_ingest_app:
          resources:
            - name: ingest-job
              description: The lakefed_ingest ingestion job.
              job:
                id: ${var.ingest_job_id}
                permission: CAN_MANAGE
            - name: setup-job
              description: The lakefed_ingest_setup schema job.
              job:
                id: ${var.setup_job_id}
                permission: CAN_MANAGE_RUN
            - name: discover-job
              description: The lakefed_ingest_discover table discovery job (bulk import).
              job:
                id: ${var.discover_job_id}
                permission: CAN_MANAGE
            - name: check-job
              description: The lakefed_ingest_check job, started by schedules of the check variant.
              job:
                id: ${var.check_job_id}
                permission: CAN_MANAGE
            - name: cluster-copies-job
              description: The lakefed_ingest_cluster_copies job, started when an app job's own cluster changes.
              job:
                id: ${var.cluster_copies_job_id}
                permission: CAN_MANAGE_RUN
            - name: delete-job
              description: The lakefed_ingest_delete_job job, started when an app job is deleted.
              job:
                id: ${var.delete_job_id}
                permission: CAN_MANAGE_RUN
            - name: discovery-warehouse
              description: The SQL warehouse discovery runs on.
              sql_warehouse:
                id: "<customer-warehouse-id>"         # Pro or serverless, version 2025.35 or above
                permission: CAN_USE
```

- The bundle's own warehouse is still defined and created. This override doesn't remove it, and it stays idle
  while nothing uses it. Whether a target can drop it is not confirmed.
- The app's service principal gets `CAN_USE` through the resource. What the deploying user needs to bind a
  customer's warehouse is not confirmed; check it at deploy.
- Discovery reads the sources through `remote_query` on whichever warehouse is set, so that warehouse needs a
  network path to each source. For a customer's warehouse, that is its own network setup, not the bundle's
  serverless network connectivity.

## 3. Steps outside the bundle

1. **Network: classic compute → Lakebase (Postgres, port 5432).** Verify this first. Classic
   clusters reach Lakebase over its endpoint, so the workspace IP access list must allow the
   clusters' egress, or use Private Link. A blocked connection fails with
   `External authorization failed ... blocked by Databricks IP ACL`.
2. **Postgres role.** The identity the jobs run as must be a Postgres role on the Lakebase instance
   (the deployer is one; add a role for a service principal if jobs run as one).
3. **Source credentials.** One secret per source database: JSON `{"user": "...", "password": "..."}`.
   The scope may be Azure Key Vault-backed — AKV secret names allow only letters, digits, and dashes.
   Each control row names its secret in `secret_key`. A row can instead read through an existing
   Unity Catalog connection, which holds the credentials: set `src_connection` to its name and
   `use_remote_query` to `true`. Then grant the identity the job runs as `USE CONNECTION` and
   `MANAGE` on the connection (`MANAGE` because the job cluster is dedicated), for example
   `GRANT USE CONNECTION, MANAGE ON CONNECTION <name> TO <principal>`. `remote_query` needs
   Databricks Runtime 18.3 or later (the job runs `18.x`).
4. **Control rows.** Register tables in the Lakebase `control` table (see `../README.md`, "Control
   rows", and `notebooks/load_control_example.ipynb`): `src_type`, `load_type`, source
   host/port/database/table, `secret_key`, `primary_key` (not needed for `full`), explicit
   `select_list`, sink, `task_collection`, and optional partitioned-seed settings. `cursor` rows also
   need `cursor_column` (rowversion recommended). To consolidate many source databases into shared
   sink tables, give their rows the same sink and a distinct `client_id` each (see "Consolidated
   sinks" in the README).
   - `ct` tables need Change Tracking enabled on the database and the table, a primary key, and a
     `CHANGE_RETENTION` longer than the expected seed time. Snapshot isolation is not required (the
     reads don't use it).
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

6. **The operations app (optional), first deploy.** In this order: deploy the ingestion bundle (step 2 above); deploy
   the app bundle and start the app (`app/README.md`, Deploy); run `databricks bundle run lakefed_ingest_app_grants -t
   <target>` from `app/`, which needs the app's service principal to exist; then check the login scope's access list with
   `databricks secrets list-acls lakefed_ingest_jdbc_logins -p <profile>`. It should show the app's service principal
   with `WRITE`, the deployer with `MANAGE`, and no other entry.

## 4. First run and verification

```bash
databricks bundle run lakefed_ingest -t dev --params task_collection=<name>
```

The first run seeds; later runs apply Change Tracking increments (`ct` rows), read rows above the
watermark (`cursor` rows), or reload the table (`full` rows). Check progress and results with the telemetry queries in [monitoring.md](monitoring.md)
(`ingest_run`, `ingest_event`, per-table freshness).

**Upgrading an existing deployment:** pull and deploy. The next ingestion run finds the database
behind its schema version and migrates it first, in one transaction (a failure changes nothing and
fails the run with the cause). To migrate before the next run instead, run `lakefed_ingest_setup`.
Either way the migration needs the schema's owner: the deployer, unless the jobs run as another
identity. From an early deploy, it renames the `ct_checkpoint` / `sweep_run` tables and their
CT-specific columns to the generic `checkpoint` / `ingest_run` names in place (data kept).

**Large telemetry tables:** the schema-14 upgrade builds `ingest_event_control_started_idx` on `ingest_event`
inside the migration transaction, which holds that table's lock for the whole build (see
[lakebase_schema.md](lakebase_schema.md#locks-and-waiting)). On a deployment with a large `ingest_event`, build
it first, as the schema's owner and outside any transaction, so the migration skips it:

```sql
create index concurrently if not exists ingest_event_control_started_idx
    on lakefed_ingest.ingest_event (control_id, started_at);
```

Check that it is valid: `select indisvalid from pg_index where indexrelid = 'lakefed_ingest.ingest_event_control_started_idx'::regclass;`
must return `true`. A concurrent build that fails leaves an invalid index under the same name, which the migration
would skip, so drop it (`drop index concurrently lakefed_ingest.ingest_event_control_started_idx;`) and build it again.

**Scheduling:** the ingestion job's schedule (paused by default) can't pass parameters, so it only ingests
the default `task_collection`. For per-collection cadence, use the operations app's schedules (a wrapper
job per collection), or the check job, `lakefed_ingest_check`: it checks a group of collections on
serverless and starts the ingestion job only for those with work. Its own schedule (paused) checks every
collection; a schedule of the app's `check` variant starts it with that schedule's members. It reads the
sources like the ingest does, so serverless needs the same network path to them (a UC connection, or
serverless network connectivity to the servers; see [Compute](#compute-for-the-setup-and-discovery-jobs)).

## Schema changes

Writes use schema evolution (`MERGE WITH SCHEMA EVOLUTION`, `INSERT WITH SCHEMA EVOLUTION OVERWRITE
... BY NAME`): adding a column to a control row's `select_list` adds it to the sink, and the job
reseeds that table so historical rows are backfilled. New sinks are created with type widening on;
for sinks created earlier run
`ALTER TABLE <sink> SET TBLPROPERTIES ('delta.enableTypeWidening' = 'true')`.
