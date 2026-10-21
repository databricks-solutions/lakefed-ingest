# lakefed_ingest operations app (optional)

A Databricks App for operating the default version of lakefed_ingest. It's a separate, optional
bundle: deploy it only if your workspace allows Databricks Apps. The ingestion bundle at the repo
root doesn't depend on it, and everything the app does can also be done with SQL and the Jobs UI
(see [../docs/monitoring.md](../docs/monitoring.md) and [../docs/lakebase_schema.md](../docs/lakebase_schema.md)).

| Page | What it does |
|---|---|
| **Overview** | Per-collection health: enabled, failing, seeding and SLA-breached tables, and a warning for enabled tables with no freshness SLA; the last run; runs in progress (elapsed against the collection's typical run time, tables done and failed); failed tables in the last 24 hours |
| **Runs** | Recent runs (filter by collection); select one to see its per-table events and errors |
| **Timeline** | Runs per collection over a window (6 hours to 7 days): planned triggers, runs colored by outcome, and triggers Databricks skipped because the previous run was still going |
| **Schedules** | Run collections at a cadence (see [Schedules](#schedules)): create, edit, pause, resume and delete schedules; each member's next run, wrapper job, last run, p95 run time, skipped triggers, and warnings for overruns and SLAs a schedule can't meet |
| **Tables** | Browse control rows with their checkpoint (phase, status, freshness lag, SLA). Filter by collection, text, or failing / SLA breached / no SLA / seeding / disabled. Select one row to **edit** it (every field, plus its change history), or several to **bulk edit** (e.g. move to another collection, or switch to reads through a UC connection), **enable**, **disable** or **force a reseed**. Changing `write_mode` needs a new sink in the same edit (an append sink and a merge sink can't share a table) |
| **Jobs** | The ingestion job's schedule; active and recent runs (with the schedule that started each); **start a run** for a collection; **cancel** a run; run the schema setup job |
| **Changes** | The change log: every add, register, edit, enable / disable and reseed made in the app, with who, what and when |
| **Add table** | A form that adds one control row, checked before insert (the database constraints are the backstop): a JDBC source (host and secret) or a UC connection (`use_remote_query`), `write_mode`, `client_attrs` and `jdbc_options` |
| **Bulk import** | Discover a server's tables (the `lakefed_ingest_discover` job, catalog metadata only), review the recommended settings in an editable grid, set shared fields once (collection, sink catalog / schema / table templates, `client_id` template, freshness SLA), and register the selection. Tables at or above a size threshold (default 1 GB) get a partitioned seed; tables already registered start unticked; duplicates are skipped |

Every write asks for confirmation. The app never deletes rows (deleted schedules are marked deleted); it
deletes only the wrapper jobs it created for schedules.

## Schedules

A Databricks job schedule can't pass parameters, so the ingestion job's own schedule can only run its
default collection. Schedules in the app fill that gap:

- **A schedule** has a cadence (every 5 minutes to daily; each cadence divides a day), a time zone,
  ingestion job parameters (any of the job's tuning parameters, such as `parallelism`, `table_retries` or
  `max_queries_per_source`; the rest keep the job's defaults), optional failure e-mails, and one or more member
  collections. A collection can be in more than one schedule.
- **One wrapper job per member.** The app creates a small Databricks job for each (schedule,
  collection): a cron schedule and one `run_job` task that starts the ingestion job with
  `task_collection` set. Wrapper jobs need no compute of their own, and every run still happens in the
  one bundle-deployed ingestion job, so a redeploy upgrades every schedule. They're named
  `lakefed_ingest schedule: <schedule> / <collection>` and tagged `lakefed_ingest_schedule`,
  `lakefed_ingest_collection` and `lakefed_ingest_app` (the app's Lakebase `<instance>/<database>`, so
  apps of other deployments in the workspace leave them alone). Skipped triggers send no failure e-mail.
- **Overlap.** Wrapper jobs allow one run at a time and don't queue: if a collection's run is still going
  at its next trigger, Databricks skips that trigger (the Timeline marks it), and the next run catches
  up. Separate jobs per collection mean one slow collection never delays the others. Manual and
  app-started runs are still guarded by the collection's run lock.
- **Staggering** (on by default) spreads members' start times over the cadence (e.g. :00 and :07 for two
  collections every 15 minutes), so cluster starts and source queries don't all land at once.
- **Sync and drift.** Every create, edit, pause, resume and delete syncs the wrapper jobs right away and is
  logged on the Changes page. "Check jobs" reports jobs edited or deleted outside the app; "Sync jobs"
  recreates missing ones and undoes outside edits. A sync reconciles by tag: it adopts a member's job
  whose id was never recorded and deletes the schedule's jobs no member owns; a failed delete is retried
  by the next sync. Syncs of one schedule take turns (a Lakebase advisory lock).
- **Warnings** per member: p95 run time (7 days) close to or over the cadence (triggers will be skipped),
  skipped triggers in the last 24 hours, and "can't meet SLA" when the cadence plus the p95 run time is
  longer than the strictest `freshness_sla_minutes` among the collection's tables.

Permissions: the app's service principal owns the wrapper jobs it creates (creating jobs needs no extra
entitlement, and `run_job` tasks need no compute) and already has `CAN_MANAGE_RUN` on the ingestion job.
The group in `SCHEDULE_JOB_VIEW_GROUP` (default `users`; blank for owner and admins only) gets
`CAN_VIEW` on them. For more ingestion jobs (e.g. a larger cluster), add an app `job` resource and an
`INGEST_JOB_ID_<NAME>` environment variable; schedules can then pick that variant.

Limits: each running collection counts as one task (2,000 per workspace) plus one parent task while its
wrapper runs (750 per workspace), all collections are runs of the one ingestion job (its
`max_concurrent_runs`, default 100, up to 1,000), and each member is a saved job (12,000 per workspace).

## Guard rails for edits

Data integrity comes first, so edits go through the server's rules, not the browser's:

- **Preview first.** An edit shows, per table, what changes, whether the table reseeds and why,
  warnings, and validation errors. Apply is only offered for a clean preview of exactly that edit.
- **All or nothing.** An edit is one transaction: if any table fails validation, or a sink would
  collide with another control row, nothing changes.
- **Reseed when the meaning changes.** For `ct` and `cursor` tables, changing the source (host,
  port, database, schema, table, engine), the sink (catalog, schema, table, `client_id`) or the
  primary key flips the table to seeding in the same transaction. Without that, the next run would
  merge only new changes into the new place or match on the wrong key. Cosmetic changes (case, an
  explicit default port) don't reseed. Moving a table to another collection, credentials, priority
  and partition settings don't reseed.
- **Warnings for what an edit leaves behind.** For example, the old sink table after a sink change,
  rows of the old `client_id` in a shared sink, and removed `select_list` columns that stay in the sink.
- **Not while running.** Edits, enable / disable and reseeds are refused while an affected collection
  is running (they take that collection's run lock), so they can't race the run's own checkpoint writes.
- **Bulk edits are limited** to fields that are safe across many tables: collection, enabled, tier,
  priority, freshness SLA, partition settings, `jdbc_options`, sink catalog and schema. Source, key and sink table
  name changes are made one table at a time.
- **Change log.** Every change is recorded in `lakefed_ingest.control_change` with the signed-in
  user, in the same transaction.

The ingestion job backs this up for changes made outside the app (for example with SQL): each
checkpoint records the source, sink and key it was written for, and a table that now points
elsewhere, or whose sink table no longer exists, is reseeded instead of silently leaving a gap.

## How it's built

| Part | Where | What |
|---|---|---|
| Backend | `src/main.py` (FastAPI) | JSON API under `/api` (OpenAPI docs at `/api/docs`); serves the built frontend for every other path |
| Data access | `src/lakebase.py`, `src/jobs.py` | Lakebase (psycopg, SDK-minted credential) and the Jobs API, as the app's service principal |
| SQL and rules | `src/queries.py` | Every statement (parameterized), form validation, bulk-import review logic |
| Frontend | `src/frontend/` (React, TypeScript, Vite) | Pages under `src/frontend/src/pages/`; React Router, TanStack Query, AG Grid Community for the grids |

Bulk registration is validated and merged on the server: the browser sends only the editable fields
(include, load type, keys, partitioning, sink, client_id). Connection details, secret keys and select
lists always come from the stored discovery.

Databricks Apps builds the frontend at deploy time (`npm install`, then `npm run build` into
`src/static/`), so build output isn't committed. `package-lock.json` pins every dependency.

## Security model

The app runs as its own service principal (created with the app):

- **Jobs:** `CAN_MANAGE_RUN` on the ingestion, setup and discovery jobs, through app resources: view,
  run and cancel, but not edit or delete.
- **Lakebase:** granted by the `lakefed_ingest_app_grants` job:
  - `CAN_USE` on the instance (to mint its own short-lived credential);
  - a Postgres role;
  - `SELECT`, `INSERT` and `UPDATE` on the `lakefed_ingest` tables, with no `DELETE`.
- **Who can act:** everyone with `CAN_USE` on the app acts with these privileges. Grant `CAN_USE`
  only to operators (app permissions in the workspace UI, or a `permissions` block on the app
  resource).

The app has no Lakebase `database` resource. Some workspaces refuse to attach one even for the
instance's manager ("User needs MANAGE permission on the resource"), and the grant job works
everywhere.

## Deploy

Prerequisites: the ingestion bundle is deployed and its setup job has run (Lakebase schema exists).

1. Add a target to `app/databricks.yml` for your workspace (the committed `dev` / `prod` hosts are
   placeholders). Set the variables that differ from the defaults:
   - `lakebase_instance`, `lakebase_database`: the ingestion bundle's values.
   - `ingest_job_id`, `setup_job_id`, `discover_job_id`: looked up by job name. In development mode the ingestion
     bundle prefixes job names (`[dev your_name] lakefed_ingest`), so set the ids or the prefixed
     names for dev targets.
   - `app_name`: 30 characters max.
2. Deploy and start the app, then grant it Lakebase access. Run these from `app/`, as the user who
   manages the Lakebase instance and owns the tables (whoever ran the ingestion setup):

   ```bash
   cd app
   databricks bundle deploy -t <target>
   databricks bundle run lakefed_ingest_app -t <target>          # deploys the code and starts the app
   databricks bundle run lakefed_ingest_app_grants -t <target>   # Lakebase access for the app
   ```
3. Grant `CAN_USE` on the app to your operators, then open the URL printed by `bundle run`.

Re-run the grant job if the app is recreated (new service principal). Default privileges cover
tables the same owner adds later, so schema upgrades don't need it.

The grant job defaults to serverless jobs compute (it only calls the workspace APIs and Lakebase). If
the workspace doesn't have serverless jobs, fall back to a classic single-node cluster in your target
(set `node_type_id` for your cloud):

```yaml
targets:
  <target>:
    resources:
      jobs:
        lakefed_ingest_app_grants:
          job_clusters:
            - job_cluster_key: grants_cluster
              new_cluster:
                spark_version: 18.x-scala2.13
                data_security_mode: SINGLE_USER
                node_type_id: Standard_D4ds_v5
                num_workers: 0
                spark_conf:
                  spark.databricks.cluster.profile: singleNode
                  spark.master: "local[*]"
                custom_tags:
                  ResourceClass: SingleNode
          tasks:
            - task_key: grant_app_access
              environment_key: ""             # clears the serverless environment
              job_cluster_key: grants_cluster
              libraries:
                - pypi:
                    package: "psycopg[binary]"
                - pypi:
                    package: "databricks-sdk==0.145.0"
```

## Develop locally

```bash
cd app/src
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest httpx
npm install
export DATABRICKS_CONFIG_PROFILE=<profile> LAKEBASE_INSTANCE=<instance> LAKEBASE_DATABASE=<db> \
       INGEST_JOB_ID=<id> SETUP_JOB_ID=<id> DISCOVER_JOB_ID=<id>
.venv/bin/python main.py          # API on :8000 (serves the built frontend from static/)
npm run dev                       # frontend with hot reload on :5173, proxying /api to :8000
npm run check                     # type-check + production build
```

Locally the app connects as you. Tests:
- `pytest app/tests` covers the SQL and review logic, and runs as part of the repo's `pytest`;
- the API tests (`tests/api_test.py`) also run when FastAPI and httpx are installed, as in the venv above.

## Not in this version

- Editing an existing control row: disable it and add a corrected one, or use SQL.
- Per-user authorization: everyone with `CAN_USE` acts as the app's service principal.
- Bulk import of control rows: use `notebooks/load_control_example.ipynb`.

## License

See [Help and License](../README.md#help) in the repo README, including the licenses of the
third-party libraries this app uses.
