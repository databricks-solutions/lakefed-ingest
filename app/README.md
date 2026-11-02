# lakefed_ingest operations app (optional)

A Databricks App for operating the default version of lakefed_ingest. It's a separate, optional
bundle: deploy it only if your workspace allows Databricks Apps. The ingestion bundle at the repo
root doesn't depend on it, and everything the app does can also be done with SQL and the Jobs UI
(see [../docs/monitoring.md](../docs/monitoring.md) and [../docs/lakebase_schema.md](../docs/lakebase_schema.md)).

| Page | What it does |
|---|---|
| **Overview** | Ingestion jobs (a schedule and its ingestion groups) with their tables by state, late databases (a database with a late table; its database page lists the tables), last job run and Status (running, failing, late, paused); groups in no job; job runs going now (progress over all the job's tables, speed, expected end); jobs needing attention, with their failures grouped by cause |
| **Ingestion jobs** | Every ingestion job, filtered (running, failing, late, paused) and searched; one job's details with its job runs (one per trigger: outcome, took, speed, parallelism, clusters) and its groups; one job run with its group runs, databases and failures |
| **Runs** | Job runs of every ingestion job, running first; group runs of groups in no job. A group run's page shows its outcome, speed, time left, tables in flight, databases and table events |
| **Sources** | Each source instance: how it is read (UC connection or JDBC host), its ingestion jobs, its tables by state, and tables discovered and not registered yet; UC connections with nothing registered |
| **Timeline** | Runs per collection over a window (6 hours to 7 days): planned triggers, runs colored by outcome, and triggers Databricks skipped because the previous run was still going |
| **Schedules** | Run collections at a cadence (see [Schedules](#schedules)): create, edit, pause, resume and delete schedules; each member's next run, wrapper job, last run, p95 run time, skipped triggers, and warnings for overruns and SLAs a schedule can't meet |
| **Tables** | Browse control rows with their checkpoint (phase, status, freshness lag, SLA). Filter by collection, text, or failing / SLA breached / no SLA / seeding / disabled. Select one row to **edit** it (every field, plus its change history), or several to **bulk edit** (e.g. move to another collection, or switch to reads through a UC connection), **enable**, **disable** or **force a reseed**. Changing `write_mode` needs a new sink in the same edit (an append sink and a merge sink can't share a table) |
| **Databricks jobs** | Under Schedules and jobs: the Databricks ingestion job's active and recent runs (with the ingestion job that started each); **cancel** a run |
| **Manage** | Only for those with Can Manage on the app itself (see [Security model](#security-model)): the app's version (the commit it was deployed from, and its active deployment) and the state database's schema version against the app's (see Schema upgrades, under Deploy); **Export as CSV** of one, several or every ingestion job's enabled tables, in the columns Create from one CSV reads, with an `ingestion_job` column naming each line's job (streamed a page at a time, so a 200,000-table export works; one job's lines import as a new ingestion job, and an import refuses a file of several jobs); and the Databricks jobs the app runs, each linked |
| **Changes** | The change log: every add, register, edit, enable / disable and reseed made in the app, with who, what and when |
| **Add table** | A form that adds one control row, checked before insert (the database constraints are the backstop): a JDBC source (host and secret) or a UC connection (`use_remote_query`), `write_mode`, `client_attrs` and `jdbc_options` |
| **Bulk import** | Discover a server's tables (the `lakefed_ingest_discover` job, catalog metadata only), review the recommended settings in an editable grid, set shared fields once (collection, sink catalog / schema / table templates, `client_id` template, freshness SLA), and register the selection. Tables at or above a size threshold (default 1 GB) get a partitioned seed; tables already registered start unticked; duplicates are skipped |
| **Tables CSV** (New ingestion job and Add tables) | Discover on a source stays the first choice; the CSV is the fallback. For a CSV, choose the UC connection first, then upload a file with no connection column: `src_database` (required), `src_schema` and `src_table` (both, or neither), then optional `select_list`, `primary_key`, `cursor_column`, `client_id` and `load_type`. A line with a database alone takes every table of that database: its tables are found by name through the connection when the job is created (the task shows discovering, then writing), and a table line in that database is taken whether or not it is picked, and wins over what discovery finds for it. A `src_connection` column is accepted only when every row names the chosen connection; otherwise the file is refused with the lines named. The preview counts the databases, the tables and the lines that can't be taken, with why; a database that can't be opened is named and blocks Create. The page offers a template. The file is kept in Lakebase once uploaded, and there is no separate databases CSV |
| **Import CSV** | Import a CSV of tables into one ingestion group: each line says where a table is (`src_connection`, or `src_host` with `secret_key`; `src_database`, `src_schema`, `src_table`; `select_list`, `primary_key`, `cursor_column`; `client_id` and `attr:<column>` for a fan-in), and the group's settings say how to ingest it (source type, load type, write mode, fan-in, sink name templates, freshness target, read options). A group's settings are the values its rows share, shown on its page with an Export CSV of the same form; a new group, or a setting its rows differ on, is set at import. Every line is previewed and checked as Add table checks a row; Apply writes only when none is invalid, skips tables already registered, and logs each row as an import. At most 5,000 lines per import |

Every write asks for confirmation. The app itself never deletes rows (deleted schedules are marked deleted); it
deletes only the wrapper jobs it created for schedules, and a job's Delete hands its tables to the delete job (below).
To remove tables for good or prune old history,
see [Removing tables and old history](../docs/lakebase_schema.md#removing-tables-and-old-history).

## Schedules

A Databricks job schedule can't pass parameters, so the Databricks ingestion job's own schedule can only run its
default collection. Schedules in the app fill that gap; the app shows each schedule as an ingestion job:

- **A schedule** has a cadence (every 5 minutes to daily; each cadence divides a day), a time zone,
  Databricks job parameters (any of the job's tuning parameters, such as `parallelism`, `table_retries` or
  `max_queries_per_source`; the rest keep the job's defaults), optional failure e-mails, and one or more member
  collections. A collection can be in more than one schedule.
- **One wrapper job per member.** The app creates a small Databricks job for each (schedule,
  collection): a cron schedule and one `run_job` task that starts the Databricks ingestion job with
  `task_collection` set. Wrapper jobs need no compute of their own, and every run still happens in the
  one bundle-deployed Databricks ingestion job, so a redeploy upgrades every schedule. They're named
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
entitlement, and `run_job` tasks need no compute) and already has `CAN_MANAGE` on the ingestion job.
The group in `SCHEDULE_JOB_VIEW_GROUP` (default `users`; blank for owner and admins only) gets
`CAN_VIEW` on them. For more ingestion jobs (e.g. a larger cluster), add an app `job` resource and an
`INGEST_JOB_ID_<NAME>` environment variable; schedules can then pick that variant.

Limits: each running collection counts as one task (2,000 per workspace) plus one parent task while its
wrapper runs (750 per workspace), all collections are runs of the one ingestion job (its
`max_concurrent_runs`, default 100, up to 1,000), and each member is a saved job (12,000 per workspace).

**Check first** (the `check` variant, offered when `CHECK_JOB_ID` is set): one wrapper job for the whole
schedule instead of one per member. At the cadence it starts the check job, `lakefed_ingest_check`, with the
members as `task_collections`; the check asks each member's source databases whether anything changed,
stamps the unchanged ones, and starts the ingestion job only for members with work, each for its databases
with work, with run-now. A quiet cycle starts no ingest cluster. Members are not staggered (one check takes
them all), the schedule is one saved job and one parent task while the check runs, and switching a schedule
to or from `check` replaces its wrapper jobs.

**Who and when.** Each ingestion job shows who created it and when (`created_by`, `created_at`), and who last changed
it and when: an edit, pause or resume of the job, or any change to its groups' tables (`options.updated_by`,
`updated_at`). The job page shows them under its title; the Ingestion jobs list has them as Created and Last updated,
sortable by time. A job never changed since it was created shows its creator as Last updated by, as the Jobs list does; jobs last changed before the app recorded it show "unknown" for who.

**Its own cluster.** The job Edit view's Compute section sets the job's ingestion cluster, like a
Databricks job cluster: driver type, worker type and 0 to 8 workers, defaulting to the ingestion job's own. A run of
the ingestion job can't change its cluster, so a job with its own cluster runs its own copy of the ingestion job:
- on Save, the app starts `lakefed_ingest_cluster_copies`, which runs as the deployer and makes or updates the copy
  (the same tasks and grants, only those cluster settings changed), and records its id in the schedule's options;
- the app then syncs the job's wrappers to start the copy (on `check`, the check job is given the copy as
  `ingest_job_id`);
- back at the defaults, the job runs the ingestion job again and the copy is deleted;
- Sync jobs copies the ingestion job again, so a redeploy reaches the copies.

Changing the cluster needs Can Manage on the job's Databricks job, like any change.

**Deleting a job.** A group always belongs to a job, so a job's Delete leaves no groups behind. The app has no
`DELETE` privilege on the Lakebase tables, so the rows are removed by the `lakefed_ingest_delete_job` job, which
runs as the deployer. A Delete takes two steps: the app marks the job deleted, and the purge comes later.
- The dialog (from `GET /api/ingestion-jobs/{id}/delete-preview`) says how many groups and tables go, the groups
  another job also runs (they stay), and has the tick box **Also delete the target tables**, with how many target
  tables only this job writes and the names of those another job's rows also write (kept).
- Delete is refused while a run of the job is going, and when the groups have tables but no delete job is configured
  (`DELETE_JOB_ID`). Otherwise the app marks the job deleted (`deleted_at`), removes its members (its wrapper jobs and
  any cluster copy go in the sync) and records the tick box. Nothing is purged yet, and no delete job starts.
- The job is **kept** under **Deleted jobs** for `DELETED_JOB_RETENTION_DAYS` (default 7; the bundle variable
  `deleted_job_retention_days`, which the engine bundle's purge reads too, so both must match), with the days left.
- **Restore** brings it back paused, with the members its Delete removed (members removed earlier stay removed). It is
  refused (409) when its name is taken, when one of its groups is now in another live job, when a live job reads or
  writes the same database or target table, or when its purge has started or finished. Its tables and state are as
  they were, since nothing was purged.
- **Delete now** (`POST /api/schedules/{id}/purge`) starts the delete job at once. The daily run (03:00 UTC) purges
  every deleted job whose retention has run out. Each purge deletes the groups' control, checkpoint, reseed_queue,
  database_check, check_work and rollup rows, and the job's job_selection rows, in one transaction under their run
  locks, then marks the job `delete_finished`. It keeps run history and the change log. Ticked, it then drops the
  target tables only those groups write (`DROP TABLE IF EXISTS`, never a schema).
- Until its purge finishes the job reads **Deleting** while the delete job runs, or **Delete failed** with the reason;
  **Delete now** runs it again. Once purged, the job leaves the list.
- While a kept job still reads a database, a new job or new tables in that database are refused (409) until the job
  is restored or purged.
- Deploy order: the engine bundle (root `databricks.yml`) first, so the daily purge exists, then the app.

The same job purges New job's drafts (below) with `purge_drafts`: the app's poller starts it at most once an hour,
and only when a draft is due.

**Run now and Cancel run.** Neither shows an end state before Databricks confirms it. **Run now** reads **Starting…**
until the poller records a run going. **Cancel run** reads **Cancelling…** until Databricks ends the run, then
**Cancelled**: the poller closes the group run as cancelled at Databricks' end time, and closes its running or waiting
tables as cancelled too, so a table that fails while the cancel is going is recorded as cancelled, not failed. If
Databricks has not ended the run two minutes after the cancel, the poller closes it as cancelled anyway, without an
end time from Databricks. Both read the `databricks_run` table the poller keeps, with no timer on the page. After 15
minutes without a start, or 10 minutes without a cancel being confirmed, the page says Databricks has not confirmed it
and keeps Run now off.

**The other actions wait the same way.** Add databases, Add tables and Create show done only once their tables are written and the
job's Databricks jobs are synced; a wrapper job that did not sync is named. Edit's Save shows saved only once the
wrapper jobs are synced and any cluster copies run has ended; a copies run that failed says the cluster was not applied.

## Discovery through the SQL warehouse

Discover on a source runs on a small serverless SQL warehouse the app bundle makes (`lakefed_ingest discovery`), not on
the discovery job's cluster, so each step answers in seconds rather than waiting minutes for a cluster. Every statement
is plain SQL sent to SQL Server through the Unity Catalog connection (`remote_query`); the app can't open the
connection itself (Unity Catalog lends its login only to Databricks compute), so the warehouse does it, as the app's
service principal.

| Step | What runs on the warehouse |
|---|---|
| The connection test (Next on the connection step) | one statement in master listing the server's online user databases (`source_catalog.DATABASES_SQL`); the filter works on the next step. A failed test shows its codes beside Next (Databricks' error class, SQL Server's error number, the SQLSTATE: `source_catalog.error_codes`), the source's message and what to do; Next moves on only once it passes |
| The database list | one statement per database (`warehouse_reads.ACCESS_SQL`): opening it is the check, and it counts its tables. Only the page shown is checked (another page or filter checks its own), so a server of 22,000 databases is never checked as a whole. A database the login can't open is greyed out, can't be ticked, and says why on hover; ticking the whole server leaves out the ones found so and says how many, and one on a page not looked at fails in the read with the server's reason, named before anything is added |
| Reading the chosen tables | one statement per database (`source_catalog.TABLE_SUMMARY_SQL`, one row per user table), saved to `discovered_table` with the same recommendations as the discovery job |

At most 16 statements of one discovery are going at once (`warehouse_reads.IN_FLIGHT`). Each request the page makes
does one step: it reads the statements going, saves the ones that ended and starts more. Where each database stands
is kept on its `discovery` row (`listing`: its statement, its claim, and a check's answer), and a request claims the
databases it starts, so two pages or app instances never start the same one twice.

The step's top line counts the choice: databases chosen of those listed and their tables (counting while some aren't
checked yet); the same numbers end the step's last line and the job's Chosen summary.

**Keys and identity columns of a new fan-in job.** Each row's `__client_id__` is the server and database joined by an
underscore (`{server}_{database}`): the UC connection name, or host and port, in lower case, then the database name.
Each row also gets `source_server` and `source_database`, which the New job form shows and lets you remove. Add tables
and Add databases on a job made before keep its `{database}` key and add no columns. Create refuses a key the rules
don't allow and names the server and database.

**New job reads the tables as they are chosen (drafts).** Ticking a whole database, or the whole server under its
filter, starts the read of its tables at once, while the rest of the job is filled in. The read is a draft: its rows
are the read's own `discovery` and `discovered_table` rows, marked `draft` in `listing`, and nothing of the job
(control, checkpoint, schedule, rollups) is written before Create.
- **Add the tables chosen** doesn't wait: the reads go on from New job's page, whose preview counts what is read so
  far and says how far the read is (asked every 2 seconds while a database is left).
- **Create** is enabled throughout. Pressed early, it waits for the rest of the read in a background task (its
  progress shows "reading the chosen databases' tables first"), then writes. A database that couldn't be read stops it
  by name before anything is written; one the whole server's read couldn't open is left out and counted.
- Create's transaction takes the draft: the mark comes off and the job is named on its rows.
- **Cancel**, leaving the page, or choosing the tables again before Create discards the draft
  (`POST /api/discoveries/{id}/discard`, by `sendBeacon` as the page closes): nothing more is read and its statements
  still going are cancelled. A draft nobody has asked about for two hours (`DRAFT_TTL_S`) counts as discarded.
- The app has no DELETE, so the delete job purges discarded drafts (`purge_drafts`; see Deleting a job). Nothing is
  kept for a job that wasn't created, once the next purge has run.
- Edit's Add tables reads the same way but still waits at Add, as before.

**The warehouse.** Serverless, PRO, set by three bundle variables: `discovery_warehouse_size` (default `2X-Small`),
`discovery_warehouse_max_clusters` (default 2: it scales out by clusters when statements queue, not by size) and
`discovery_warehouse_auto_stop_mins` (default 5). A serverless warehouse starts in a few seconds. 2X-Small is 4 DBUs an
hour per cluster while it runs: at a list price of about 0.70 USD per serverless SQL DBU, about 2.80 USD an hour, about
5.60 USD with both clusters; it stops after 5 idle minutes, so it costs only while someone chooses tables. Your
contract's price may differ. The app gets `CAN_USE` on it as an app resource (`WAREHOUSE_ID`).

**At a whole server's size** (estimates): a check takes about a second, and only the page shown (100 databases) is
checked, about 7 seconds at 16 at once whatever the server's size. A read of 1,000 tables is about 2 to 5 seconds per
database, about 1 to 2 hours for 22,000 at 16 at once; in New job it starts at the choice, so Create waits only for
what is left when it is pressed. Without a warehouse set (`WAREHOUSE_ID` empty), Discover runs the discovery job as before.

**What stays on the discovery job:** listing an Azure SQL server's databases through Azure (it takes a service
credential only Databricks compute holds; the tables are then read through the warehouse), and sources read by host
and password (no connection to send through). The job stays in the bundle for those.

## JDBC connections entered by hand

Discover's **Create a connection** offers two ways. **Through Unity Catalog** is as before. **JDBC connection entered by
hand** takes a name, a source type (SQL Server, Oracle, PostgreSQL or MySQL), a host, a port (blank for the type's
default), a secret scope, a secret key, and the server certificate choice: verify it (the default) or trust it.

Before a connection is made, an admin creates the work outside the app: the secret scope, and in it a JSON secret
`{"user": ..., "password": ...}` under the key. A key holding `{database}` (say `creds-{database}`) takes one secret per
database, named by each database's name. The app never reads a secret value. It stores the names only, in
`lakefed_ingest.jdbc_connection`, which comes with schema version 16: the schema upgrade (Manage, and the setup job it
starts) adds it to a database that is behind. Its one write to a secret is a login typed into the form, described under
Logins below, and that goes only into the dedicated login scope.

- **Who makes one:** any signed-in user, in the dedicated login scope, once they have typed a login under the
  connection's name for a key it makes (see Logins below). Only an app manager may make one that names any other scope,
  since that scope holds secrets an admin wrote. Making one writes no secret. Anyone may list them, by name, with their
  scope and key names; none shows a secret.
- **Checked at each read:** a discovery, a new job, Add tables, Add databases, a re-discovery and every other write of
  control rows through a connection is checked again when it is made, so a connection made before these rules is held
  to them too. Its maker must still be allowed its scope. In the login scope, each key it reads (one per database it
  names) must hold the newest login typed under this connection's name, so a login typed for one connection never goes
  to another connection's host. A whole server can't be read through a login-scope connection with `{database}` in its
  key, since the databases it lists are named by the source and can't be checked first: name the databases. The
  engine holds the same line on its own: the discover, check and ingestion jobs read the typed logins' records at the
  start of a run, and a login typed for a connection is sent only to that connection's host, port and source type,
  whatever a control row or a discovery spec names (a run started outside the app included).
- **A job's tables keep the connection's source:** every table written into a job that reads through a connection
  takes its host, port, source type and a key its key template makes, and an edit of `secret_key`, `src_host`,
  `src_port`, `src_type`, `use_remote_query` or `src_connection` on such a table is refused (Tables, Import and Edit).
- **Key names:** letters, digits and dashes, as Azure Key Vault names must be, so a key works on a Key Vault-backed scope
  too; `{database}` may appear once for each database's name. Filled in, the key follows Databricks' secret key rule
  (letters, digits, dashes, underscores and dots, up to 128 characters), so a login typed for `Sales_DB` or `sales.eu`
  goes under `creds-Sales_DB` or `creds-sales.eu`, the key discovery reads. A database whose name has a space or any
  other character can't have a login of its own: the form refuses it, and a read of it through the login scope is
  refused as having no login.
- **Discovery:** a discovery through a connection takes the host, port, source type and key from the connection, and the
  secret scope and certificate choice as job parameters (`secret_scope`, `trust_server_certificate`). The request names
  only the connection and the databases. The discover job reads the source over Spark JDBC (`src_host`, `src_port`,
  `secret_key`, `use_remote_query` off), so the warehouse is not used for it.
- **Jobs:** a job made from such a discovery runs with the connection's scope and certificate choice; a request's own
  values for them are replaced. Its re-discovery reads through the same connection.
- **One source per job:** a selection can't mix a JDBC connection with another source, and tables from a JDBC connection
  can't be added to an existing job or registered into one. Make a new ingestion job from them.
- **Shown:** Discover, the job's tree (on its page and in Edit) show the connection's type, host, port, scope and key
  names and its certificate choice. No secret value is shown anywhere.
- **Logins:** under a connection's secret key the form has a user name and a password field. The fields show only when
  the connection's secret scope is the dedicated login scope, since that is where the login is written, and a key with
  `{database}` in it asks which database the login is for. Save the login writes
  `{"user": ..., "password": ...}` as one JSON secret into the dedicated login scope (`JDBC_LOGIN_SCOPE`, by default
  `lakefed_ingest_jdbc_logins`, set by the app bundle's `jdbc_login_scope` variable). The write goes through the Secrets
  API as the app's service principal, so it shows under the app's identity and never the user's token. The grant job
  (under Deploy) gives that principal `WRITE` on this one scope and nothing else: not `MANAGE`, which would let it change
  who reads the scope, and not `READ`, which a write doesn't need. The scope has `prevent_destroy`, so destroying the
  bundle can't delete the logins.
  - **Who may save one:** whoever may change the ingestion groups that read the key through the login scope (their
    job's `secret_scope`), the same check the Edit path makes; a job reading the same key name from another scope
    doesn't count. A key no such group reads but that already holds a login is saved again only by someone who typed
    a login under it, or who made a connection one was typed for. A key with no login yet is open to any signed-in
    user, as a new job is.
  - **Replacing:** "Replace the login" saves a new one under the same key. It is recorded as `rotated`. A key used by a
    job needs `CAN_MANAGE` on that job to rotate it. A key whose newest login was typed for another connection takes
    one under a new connection only from someone who may change that connection's jobs (or, while it has none, who
    typed that login or made the connection).
  - **One save at a time:** a save takes the key's lock, records the login, writes the secret and then commits, in one
    transaction, so a refused write leaves no record and two saves of one key take turns. The newest record always
    names the connection the key's login was typed for, which the reads above rely on.
  - **Recorded:** `lakefed_ingest.jdbc_login_write` gets one row per save: who entered it, when, the connection, the scope
    and key, and whether it was `saved` or `rotated`. There is no password column.
  - **Never kept:** the password is not stored in Lakebase, not logged, and not returned. The form clears it after a save and
    never prefills it. Error text and the 422 for this route keep the password out.
  - **Who reads it:** the ingestion jobs read the login as the identity they run as, normally the deployer, which must be
    the person who deployed the app bundle, since that person is the scope's creator. See the grant bullet below.
  - **Audit log:** checked on 30 Oct 2026 by writing a throwaway secret with a marker value: the audit log's
    `putSecret` record (`system.access.audit`, service `secrets`) shows `string_value` as `REDACTED`, and the marker
    appears nowhere in the record. Its `user_identity` and `identity_metadata` name the identity that wrote it.

## Re-discovery

A job made from a discovery can add new databases and tables by itself. On its Edit page, under When it runs,
**Add new databases and tables** is off by default, or daily at an hour in UTC. It's stored as the schedule option
`rediscover_hour`, and the job page shows the hour and the last run.

Once a day at that hour, the scheduler in the app process (a thread; `REDISCOVERY_TICK_S`, default 300 seconds, 0
turns it off) reads the job's selection again and adds what the rules allow. A server reached through a connection is
read through the discovery warehouse, as Add tables reads it (its databases listed again for a whole-server part,
then each database's tables); one listed through Azure, or read by host and password, runs the discovery job:

| The job was chosen as | Re-discovery adds |
|---|---|
| a whole server, with its filter | each new database matching the filter, with all its tables. New tables of a database the job already has are not added |
| a whole database | its new tables |
| specific tables | nothing |

What it adds goes through the same path as Add tables: the run locks, the checks, the job's own settings and the
usual group sizes. The change log records each added table under the user `re-discovery`. Tables the source no
longer has are kept and listed as vanished. A database that can't be opened fails alone and is listed under errors.

Other rules: a paused job is skipped; a run that meets a group already running is deferred to the next day; a job
made from an uploaded CSV has nothing to read again. Each run claims its job in SQL first, so two app instances
don't run the same job twice. The scheduler runs only while the app runs: a day missed while it was down runs on
the first tick after the hour.

**Run it now** on the job page starts the same run (it is refused while one is going). While it runs the page reads
pending from the discovery run in the poller, then **adding tables**, then done with what was found and added, or failed
with the reason. Run it now stays off until the run has ended; a discovery run past its wait is not confirmed, not done.

A whole-server part reads its servers again from `discovery.listing` (how the discovery listed databases: the LIKE
pattern, the Azure resource and the host secret template, never a secret). A discovery made before schema version
12 has no listing, so discover that server again from Add tables. A host whose databases take different secrets, with
no template recorded, can't have a whole-server part read again (discover it from Add tables); its whole-database
parts are read once per secret.

The job page's block shows the last run: its status (running, done, deferred or failed), when it started and
finished, what it found and added, what was skipped, the new databases, the vanished tables, and any errors.

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
| Run poller | `src/run_poller.py` | One poller (every 15 seconds, one instance at a time through an advisory lock) writes the `databricks_run` table; the pages read that table, and it closes the ingest runs whose Databricks run has ended. `RUN_POLLER=0` turns it off on an instance. Until the table exists, the app reads the Jobs API as before |
| SQL and rules | `src/queries.py` | Every statement (parameterized), form validation, bulk-import review logic |
| Frontend | `src/frontend/` (React, TypeScript, Vite) | Pages under `src/frontend/src/pages/`; React Router, TanStack Query, AG Grid Community for the grids |

Bulk registration is validated and merged on the server: the browser sends only the editable fields
(include, load type, keys, partitioning, sink, client_id). Connection details, secret keys and select
lists always come from the stored discovery.

Databricks Apps builds the frontend at deploy time (`npm install`, then `npm run build` into
`src/static/`), so build output isn't committed. `package-lock.json` pins every dependency.

## Security model

The app runs as its own service principal (created with the app):

- **Jobs:** `CAN_MANAGE` on the ingestion, check and discovery jobs, through app resources, so it can read their
  access lists. This is a trade-off for the per-user checks below: starting runs needs only `CAN_MANAGE_RUN`, but
  the Permissions API returns a job's access list only to those who manage it, so the service principal could also
  edit or delete these jobs. The app never edits or deletes them. The setup, cluster copies and delete jobs need
  only `CAN_MANAGE_RUN`: the app starts them and reads their runs, and each run executes as the job's run-as
  identity, the deployer. No user starts the setup job through the app, so its access list is never read.
- **Lakebase:** granted by the `lakefed_ingest_app_grants` job:
  - `CAN_USE` on the instance (to mint its own short-lived credential);
  - a Postgres role;
  - `SELECT`, `INSERT` and `UPDATE` on the `lakefed_ingest` tables, with no `DELETE`; except the two JDBC record tables,
    `jdbc_login_write` and `jdbc_connection`, which get `SELECT` and `INSERT` only (the app never updates a login or
    connection row, and the grant job revokes `UPDATE` on them right after the blanket grant).
- **JDBC login scope:** `WRITE` on the dedicated login scope and nothing else, set through the Secrets API by the same
  job (the bundle declares no permission on the scope). See [JDBC connections](#jdbc-connections-entered-by-hand).
- **Source connections:** `USE CONNECTION` on each Unity Catalog connection named in the bundle's
  `source_connections` variable (comma-separated), granted by the same job as whoever runs it, who needs to own
  or manage those connections. The app lists connections as its own identity, so Discover shows only those it
  holds `USE CONNECTION` on; the connections aren't the bundle's, so they are named, not created. A connection
  the job can't grant on is reported in its output and the others are still granted; Discover's screen names
  the grant for any connection it can't see.
- **Discovery warehouse:** `CAN_USE` on the bundle's `lakefed_ingest discovery` SQL warehouse, through an app
  resource. The statements run as the service principal, which holds `USE CONNECTION` on the connections above.
- **Azure SQL listing (optional):** for a login that can't open master, Discover's connection test can have the
  discovery job list an Azure SQL server's (or Managed Instance's) databases through Azure Resource Manager instead.
  It takes a Unity Catalog service credential (Catalog Explorer, External data, Credentials) on an access connector
  whose managed identity has Reader on the server, `ACCESS` on that credential for whoever runs the discovery job,
  and the server's resource id, both typed under Azure SQL on the connection step. The app never calls Azure;
  each listed database is then read through the connection, and one the login can't open fails on its own.
- **JDBC secret scopes:** `READ` on each scope a JDBC connection names, for the identity its jobs run as (normally the
  deployer). The app never holds it and its grant job doesn't give it, so a job reading a scope it can't read fails on
  its first run. The app bundle creates the dedicated login scope with no permission on it, and the grant job gives the
  app `WRITE` there and nothing else (see Lakebase above). The app makes no other scope and writes no other secret.
  See [JDBC connections](#jdbc-connections-entered-by-hand).
- **Who can act:** every action on an existing ingestion job follows the signed-in user's own Databricks
  permission on the job its runs execute as (the check job on a check schedule, else the ingestion job), as the
  Jobs UI does:
  - running it (Run now), cancelling its run (Cancel run), or a new job that isn't paused: `CAN_MANAGE_RUN`;
  - changing it (Edit and Save, Pause, Resume, Sync jobs), its groups' tables (edit, enable, disable, reseed,
    Add table, Import CSV, Bulk import's register), or deleting it: `CAN_MANAGE` or owner;
  - a change to tables needs `CAN_MANAGE` on every ingestion job that runs one of their groups, since a group
    can be in more than one job and its tables change for all of them;
  - a read by host and password outside a JDBC connection is an app manager's: a discovery by host, a New job, Add
    table, Import or Add tables with tables read by host, an edit that points a table at a host or a key, a job's
    `secret_scope` set or changed, and a re-discovery by host of a job a manager didn't make. Such a read takes its
    login from a scope an admin wrote (the job's `secret_scope`, by default the bundle's scope) and sends it to the
    host the request names. Anyone else reads through a Unity Catalog connection, or a JDBC connection in the login
    scope;
  - workspace admins may do all of these.

  Who the user is comes from the `X-Forwarded-Email` header the Databricks Apps proxy adds. Without it the app
  refuses every write and every action (403), so an unknown caller never passes a check. A user's groups are
  read from SCIM (their direct groups), and the groups those are in are followed up to 5 levels and 200 groups,
  so a grant to `data-eng` counts for a member of `team-x` inside it. A lookup that fails or passes those caps
  keeps the groups found so far: it can refuse something Databricks would allow, never the reverse.

  Manage and its endpoints (`/api/manage/...`) are for the app's managers: workspace admins, and the users or groups
  in the bundle's `app_managers` (by default the user who deploys it). Anyone else is asked of the app's access list
  (`CAN_MANAGE` on the app itself), which the service principal can read only with Can Manage on its own app; it isn't
  given that, so without a place in `app_managers` a user is refused, and the refusal says how to get one.

  Running the discovery job, or cancelling a run, needs `CAN_MANAGE_RUN` on that job. Groups in no
  ingestion job, and new paused jobs, are open to everyone with `CAN_USE` on the app. The server checks every
  call, and the pages show a refused button disabled with the reason. Permissions are read again after 20
  seconds.

The app has no Lakebase `database` resource. Some workspaces refuse to attach one even for the
instance's manager ("User needs MANAGE permission on the resource"), and the grant job works
everywhere.

## Deploy

Prerequisites: the ingestion bundle is deployed and its setup job has run (Lakebase schema exists).

1. Add a target to `app/databricks.yml` for your workspace (the committed `dev` / `prod` hosts are
   placeholders). Set the variables that differ from the defaults:
   - `lakebase_instance`, `lakebase_database`: the ingestion bundle's values.
   - `ingest_job_id`, `setup_job_id`, `discover_job_id`, `check_job_id`, `cluster_copies_job_id`, `delete_job_id`:
     looked up by job name, so deploy the ingestion bundle first. In development mode the ingestion
     bundle prefixes job names (`[dev your_name] lakefed_ingest`), so set the ids or the prefixed
     names for dev targets.
   - `app_name`: 30 characters max.
   - `app_managers`: who sees Manage besides workspace admins, users or groups, comma separated; by default the user
     who deploys.
   - `app_version`: what Manage shows as the version, by default the git commit deployed from. It is blank when the
     bundle is deployed from a folder that isn't a git checkout: pass `--var app_version=<commit>` there.
2. Deploy and start the app, then run the grant job. Run these from `app/`, as the user who manages the Lakebase
   instance and owns the tables (whoever ran the ingestion setup). On a first deploy the order matters: the grant job
   needs the app's service principal, which exists only once the app is deployed.

   ```bash
   cd app
   databricks bundle deploy -t <target>
   databricks bundle run lakefed_ingest_app -t <target>          # deploys the code and starts the app
   databricks bundle run lakefed_ingest_app_grants -t <target>   # Lakebase access, USE CONNECTION on source_connections, WRITE on the login scope
   ```
3. Check the login scope's access list. The grant job sets the app's `WRITE` there (the bundle declares no permission on
   the scope), so the list should show the app's service principal with `WRITE`, the deployer with `MANAGE`, and nothing
   else: `databricks secrets list-acls lakefed_ingest_jdbc_logins -p <profile>`.
4. Grant `CAN_USE` on the app to your operators, then open the URL printed by `bundle run`.

Re-run the grant job if the app is recreated (new service principal). It also sets the new principal's `WRITE` on the
login scope; the old principal's entry stays until you remove it (`databricks secrets delete-acl
lakefed_ingest_jdbc_logins <old service principal>`). Default privileges cover tables the same owner adds later, so
schema upgrades don't need it.

**Schema upgrades.** When the app starts and finds the state database behind the schema version its code expects
(`schema_upgrade.SCHEMA_VERSION`), it starts the setup job once, under an advisory lock and only if no setup run is
going, and logs it in the change log (`schema_upgrade`). Manage shows the database's version against the app's, and
**Upgrading the state database** with the run's link while it runs. The next ingestion or check run also migrates a
database behind, as before. A first install, with no schema at all, is left to the deploy steps: the grant job and
the app's own Postgres role need the schema to exist first. `SCHEMA_UPGRADE=0` turns the check off.

The grant job defaults to serverless jobs compute (it only calls the workspace APIs and Lakebase). If
the workspace doesn't have serverless jobs, run it on an existing all-purpose cluster in your target
(it's a one-time job, so a running cluster beats waiting for a job cluster to start):

```yaml
targets:
  <target>:
    resources:
      jobs:
        lakefed_ingest_app_grants:
          tasks:
            - task_key: grant_app_access
              environment_key: ""                 # clears the serverless environment
              existing_cluster_id: <cluster-id>   # an all-purpose cluster you can attach to
              libraries:                          # installed on that cluster
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

- Per-user authorization: everyone with `CAN_USE` acts as the app's service principal.
- Discovery for sources other than SQL Server: add their control rows with Add table or SQL.
- Logins for a JDBC connection in a scope other than the dedicated login scope: an admin creates that scope and its
  secret beforehand. The form writes only into the dedicated scope.

## License

See [Help and License](../README.md#help) in the repo README, including the licenses of the
third-party libraries this app uses.
