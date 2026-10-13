# lakefed_ingest operations app (optional)

A Databricks App for operating the default version of lakefed_ingest. It's a separate, optional
bundle: deploy it only if your workspace allows Databricks Apps. The ingestion bundle at the repo
root doesn't depend on it, and everything the app does can also be done with SQL and the Jobs UI
(see [../docs/monitoring.md](../docs/monitoring.md) and [../docs/lakebase_schema.md](../docs/lakebase_schema.md)).

| Page | What it does |
|---|---|
| **Overview** | Per-collection health: enabled, failing, seeding and stale tables; the last run; runs in progress; failed tables in the last 24 hours |
| **Runs** | Recent runs (filter by collection); select one to see its per-table events and errors |
| **Tables** | Browse control rows with their checkpoint (phase, status, freshness lag). Filter by collection, text, or failing / stale / seeding / disabled. Select rows to **enable**, **disable** or **force a reseed** |
| **Jobs** | The ingestion job's schedule; active and recent runs; **start a run** for a collection; **cancel** a run; run the schema setup job |
| **Add table** | A form that adds one control row, checked before insert (the database constraints are the backstop) |

Every write asks for confirmation. The app never deletes rows. It's built with Streamlit; the SQL is
in `src/queries.py` (parameterized, unit-tested in `tests/`).

## Security model

The app runs as its own service principal (created with the app):

- **Jobs:** `CAN_MANAGE_RUN` on the ingestion and setup jobs, through app resources: view, run and
  cancel, but not edit or delete.
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
   - `ingest_job_id`, `setup_job_id`: looked up by job name. In development mode the ingestion
     bundle prefixes job names (`[dev your_name] lakefed_ingest`), so set the ids or the prefixed
     names for dev targets.
   - `app_name`: 30 characters max.
   - `node_type_id`: for the single-node grant job (default is an Azure type).
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

## Develop locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r app/src/requirements.txt
cd app/src
DATABRICKS_CONFIG_PROFILE=<profile> LAKEBASE_INSTANCE=<instance> LAKEBASE_DATABASE=<db> \
INGEST_JOB_ID=<id> SETUP_JOB_ID=<id> ../../.venv/bin/streamlit run app.py
```

Locally the app connects as you. Unit tests: `pytest app/tests` (run by the repo's `pytest`).

## Not in this version

- Editing an existing control row: disable it and add a corrected one, or use SQL.
- Changing job schedules: needs `CAN_MANAGE` on the job; use the Jobs UI.
- Per-user authorization: everyone with `CAN_USE` acts as the app's service principal.
- Bulk import of control rows: use `notebooks/load_control_example.ipynb`.
