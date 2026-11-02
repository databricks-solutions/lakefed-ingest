# Observability

The ingestion job ingests a whole task_collection inside **one** job task (thread pool + FAIR scheduler
pools), so there is no per-table task in the Jobs UI to watch. Visibility is re-created by writing
per-table telemetry to **Lakebase**. The Lakebase database is already
registered as a Unity Catalog catalog (`lakefed_ingest_lakebase`, via the `database_catalogs` resource
in `lakefed_ingest_setup.yml`), so you query the telemetry **directly from DBSQL**, with no sync job,
no copy. Writes go to Lakebase (point writes from the ingestion run); reads go through UC federation.

## Tables (Lakebase, UC path `lakefed_ingest_lakebase.lakefed_ingest`)

If your target overrides the `lakebase_catalog` bundle variable, substitute that catalog name in the
queries below.

| Table | Grain | Written by |
| --- | --- | --- |
| `ingest_run` | one row per ingestion run, and one per collection the check job checked (`work_id` `check-<job run>`; leave these out when counting runs) | `ingest` (`start_run` at launch, `finish_run` at end); the check job |
| `ingest_event` | one row per (run, table) | `ingest` worker (`record_event_start` → `record_event_finish`) |
| `checkpoint.last_success_at` | per source table | stamped on each successful event (freshness/lag) |
| `database_check` | per (collection, source database) | stamped when the whole database is unchanged, in place of each table's `last_success_at` |

`ingest_event` carries `action` (seed/increment/reseed/full), `stage` (plan/detect/ensure_sink/read/
seed/merge/load/checkpoint). It pinpoints *where* a failure happened; `plan` means the row's
`src_type`/`load_type` pair isn't supported; `status`, `duration_ms`, `rows_read`/`rows_merged`,
`position_from`/`position_to` (checkpoint positions as text, the CT version for `ct`; NULL for
`full`), `pool`, and `error`. One row is INSERTed at start
(status `running`) and UPDATEd at finish; concurrent workers touch distinct rows, so there is no
write contention. Tables in a batched append (`append_batch_size` > 1) are the exception: their rows
are inserted, finished, when the batch ends, with `pool` = `batch<size>` and no row counts, so they
don't appear among the in-flight tables. A `verify` stage is a run checking the sink for an append
an earlier run left pending, or a retry checking for the rows of the attempt that failed.

With `table_retries` set and the run's `table_retry_budget` above 0 (its default is 0, so nothing is retried:
README "Table retries"), an attempt that fails with a transient source error and is tried again finishes as `retried`, with its stage and error, and the table gets another row for
its next attempt; `retry_count` numbers them (0, 1, ...). Only a final failure is `failed`, so the
failure query below lists tables that are still failing. A table stopped by a Cancel is `cancelled`
instead, so it is not in that list.

A run that ends without finishing (cancelled, or its cluster went away) can't close its own rows. The
next run of the same collection holds the collection's lock, so it closes them at launch: the dead
run's `ingest_run` row and its `running` events become `failed`, or `cancelled` when its Databricks run
ended cancelled or was asked to stop, with the run's `detail` naming the run that closed it (`closed_by_run`).
So does the last `retried` event of a table that was waiting for
its retry, keeping its error with the reason added. Until then the dead run still reads `running`,
unless the operations app is running: its run poller, every 15 seconds, asks the Jobs API about each
one's Databricks run (`job_run_id`) and closes those that have ended (terminated, skipped or an internal error;
one the API no longer has, once it began a day ago) the same way, with `closed_by_app` and the reason in
`detail`. A run still going in Databricks, or one whose state can't be read, is left as it is, and the app never
cancels anything on its own. Cancel run on an ingestion job's page cancels the job's wrapper, check and group
runs, and closes the records of those it cancelled. The page shows Cancelling… until every one of those runs has ended
in Databricks (as the poller records it), and Run now stays off until then.

Table and column reference: [lakebase_schema.md](lakebase_schema.md).

## Canonical queries (run in DBSQL against the Lakebase UC catalog)

Run these on a SQL warehouse (or serverless). The Lakebase catalog is a database catalog, and
queries from a classic all-purpose or job cluster fail with `PERMISSION_DENIED` (foreign
credentials). The ingestion job itself doesn't use it; it connects to Lakebase directly.

These are Spark-SQL dialect against the federated catalog `lakefed_ingest_lakebase`. `:collection` is a
DBSQL query parameter.

In-flight tables for the latest run of a collection:
```sql
SELECT e.src_database, e.src_table, e.stage, e.pool, e.started_at
FROM lakefed_ingest_lakebase.lakefed_ingest.ingest_event e
WHERE e.run_id = (
        SELECT max(run_id) FROM lakefed_ingest_lakebase.lakefed_ingest.ingest_run
        WHERE task_collection = :collection)
  AND e.status = 'running'
ORDER BY e.started_at;
```

Failures in the latest run, with the stage + error (the "find the failing db/table/layer" view):
```sql
SELECT src_database, src_schema, src_table, stage, left(error, 200) AS error, finished_at
FROM lakefed_ingest_lakebase.lakefed_ingest.ingest_event
WHERE run_id = (
        SELECT max(run_id) FROM lakefed_ingest_lakebase.lakefed_ingest.ingest_run
        WHERE task_collection = :collection)
  AND status = 'failed'
ORDER BY finished_at DESC;
```

The tables a Cancel stopped in the latest run are the same query with `status = 'cancelled'`.

Per-table freshness / lag. A table's last success is its own stamp or its database's
check stamp, whichever is later, when that stamp vouches for it (see `database_check` in
lakebase_schema.md):
```sql
WITH t AS (
  SELECT cp.src_database, cp.src_schema, cp.src_table, cp.strategy,
         greatest(cp.last_success_at,
                  CASE WHEN cp.strategy = 'ct' AND cp.phase = 'incremental'
                        AND CAST(cp.position AS BIGINT) >= dc.checked_version THEN dc.checked_at END) AS last_success_at
  FROM lakefed_ingest_lakebase.lakefed_ingest.checkpoint cp
  JOIN lakefed_ingest_lakebase.lakefed_ingest.control c ON c.id = cp.control_id
  LEFT JOIN lakefed_ingest_lakebase.lakefed_ingest.database_check dc
    ON dc.task_collection = c.task_collection AND dc.src_database = c.src_database
   AND dc.source = concat_ws('|', lower(coalesce(nullif(c.src_host, ''), nullif(c.src_connection, ''), '')),
                             coalesce(nullif(c.src_port, 0),
                                      CASE WHEN nullif(c.src_host, '') IS NULL
                                                AND nullif(c.src_connection, '') IS NOT NULL THEN 0
                                           ELSE CASE coalesce(nullif(lower(trim(c.src_type)), ''), 'sqlserver')
                                                  WHEN 'sqlserver' THEN 1433 WHEN 'oracle' THEN 1521
                                                  WHEN 'postgresql' THEN 5432 WHEN 'mysql' THEN 3306
                                                  ELSE 0 END END),
                             coalesce(nullif(c.secret_key, ''), nullif(c.src_connection, ''), '')))
SELECT *, timestampdiff(MINUTE, last_success_at, current_timestamp()) AS lag_minutes
FROM t ORDER BY last_success_at NULLS FIRST;
```

Freshness SLA breaches. The SLA is per table, in `control.freshness_sla_minutes` (NULL = no SLA). An
enabled table breaches it when its last success, or its creation if it has never succeeded, is older
than the SLA. Its last success counts its database's check stamp, as in the query above: a quiet
database checked and found unchanged stamps the database, not each table. The operations app applies
the same rule (`queries.SLA_BREACHED`), so an alert on this query (trigger when it returns rows) matches
what the app shows:
```sql
WITH t AS (
  SELECT c.task_collection, c.src_database, c.src_schema, c.src_table, c.freshness_sla_minutes, c.created_at,
         greatest(cp.last_success_at,
                  CASE WHEN cp.strategy = 'ct' AND cp.phase = 'incremental'
                        AND CAST(cp.position AS BIGINT) >= dc.checked_version THEN dc.checked_at END) AS last_success_at
  FROM lakefed_ingest_lakebase.lakefed_ingest.control c
  LEFT JOIN lakefed_ingest_lakebase.lakefed_ingest.checkpoint cp ON cp.control_id = c.id
  LEFT JOIN lakefed_ingest_lakebase.lakefed_ingest.database_check dc
    ON dc.task_collection = c.task_collection AND dc.src_database = c.src_database
   AND dc.source = concat_ws('|', lower(coalesce(nullif(c.src_host, ''), nullif(c.src_connection, ''), '')),
                             coalesce(nullif(c.src_port, 0),
                                      CASE WHEN nullif(c.src_host, '') IS NULL
                                                AND nullif(c.src_connection, '') IS NOT NULL THEN 0
                                           ELSE CASE coalesce(nullif(lower(trim(c.src_type)), ''), 'sqlserver')
                                                  WHEN 'sqlserver' THEN 1433 WHEN 'oracle' THEN 1521
                                                  WHEN 'postgresql' THEN 5432 WHEN 'mysql' THEN 3306
                                                  ELSE 0 END END),
                             coalesce(nullif(c.secret_key, ''), nullif(c.src_connection, ''), '')))
  WHERE c.task_enabled AND c.freshness_sla_minutes IS NOT NULL)
SELECT *, timestampdiff(MINUTE, coalesce(last_success_at, created_at), current_timestamp()) AS lag_minutes
FROM t
WHERE timestampdiff(MINUTE, coalesce(last_success_at, created_at), current_timestamp()) > freshness_sla_minutes
ORDER BY lag_minutes DESC;
```

Per-run throughput and outcome (`skipped` counts unchanged tables; `detail.detection` has the skip-no-change stats):
```sql
SELECT run_id, task_collection, total, ok, failed, skipped, reseeded,
       timestampdiff(SECOND, started_at, finished_at) AS seconds,
       round(total / nullif(timestampdiff(SECOND, started_at, finished_at), 0), 2) AS tables_per_sec
FROM lakefed_ingest_lakebase.lakefed_ingest.ingest_run
ORDER BY started_at DESC
LIMIT 20;
```

Batched appends (`append_batch_size` > 1) record no `rows_merged` on their tables' events; the run's
`detail` carries their rows instead: `batch_rows` (rows the batches committed, from each commit's
metrics) and `batch_rows_unknown` (tables whose rows landed in a commit that reported an error; not
counted in `batch_rows`).

Table retries: the run's `detail.table_retries` holds `retries` (granted), `recovered` (tables that
succeeded on a retry), `failed_after_retry`, `budget` and `budget_left`, `budget_exhausted`,
`sources_stopped` and `by_class` (retries per error class). At a budget of 0 nothing is retried, and
`budget_exhausted` stays `false`, since no budget was spent. The retried attempts of a run:
```sql
SELECT src_database, src_schema, src_table, retry_count, stage, left(error, 200) AS error, finished_at
FROM lakefed_ingest_lakebase.lakefed_ingest.ingest_event
WHERE run_id = :run_id AND status = 'retried'
ORDER BY finished_at;
```

### Scheduled collections

Alert-ready queries over the schedule tables (written by the operations app). Collections whose
latest run failed:
```sql
SELECT m.task_collection, s.name AS schedule, r.run_id, r.started_at, r.failed
FROM lakefed_ingest_lakebase.lakefed_ingest.schedule_member m
JOIN lakefed_ingest_lakebase.lakefed_ingest.schedule s ON s.id = m.schedule_id
JOIN (SELECT *, row_number() OVER (PARTITION BY task_collection ORDER BY started_at DESC) AS rn
      FROM lakefed_ingest_lakebase.lakefed_ingest.ingest_run) r
  ON r.task_collection = m.task_collection AND r.rn = 1
WHERE m.removed_at IS NULL AND s.deleted_at IS NULL AND NOT s.paused AND r.status = 'failed';
```

A run that was cancelled is `cancelled` and does not match, so a Cancel does not alert.

Runs longer than their schedule's cadence in the last 24 hours (the next trigger was skipped):
```sql
SELECT r.task_collection, s.name AS schedule, s.cadence_minutes, r.run_id, r.started_at,
       round(timestampdiff(SECOND, r.started_at, coalesce(r.finished_at, current_timestamp())) / 60, 1) AS minutes
FROM lakefed_ingest_lakebase.lakefed_ingest.ingest_run r
JOIN lakefed_ingest_lakebase.lakefed_ingest.schedule_member m ON m.task_collection = r.task_collection AND m.removed_at IS NULL
JOIN lakefed_ingest_lakebase.lakefed_ingest.schedule s ON s.id = m.schedule_id AND s.deleted_at IS NULL
WHERE r.started_at > current_timestamp() - INTERVAL 24 HOURS
  AND timestampdiff(SECOND, r.started_at, coalesce(r.finished_at, current_timestamp())) > s.cadence_minutes * 60
ORDER BY r.started_at DESC;
```

Scheduled collections with no run started for two cadences (missed or skipped triggers, or a wrapper job
that is missing or paused outside the app):
```sql
SELECT m.task_collection, s.name AS schedule, s.cadence_minutes, max(r.started_at) AS last_started
FROM lakefed_ingest_lakebase.lakefed_ingest.schedule_member m
JOIN lakefed_ingest_lakebase.lakefed_ingest.schedule s ON s.id = m.schedule_id
LEFT JOIN lakefed_ingest_lakebase.lakefed_ingest.ingest_run r ON r.task_collection = m.task_collection
WHERE m.removed_at IS NULL AND s.deleted_at IS NULL AND NOT s.paused
GROUP BY m.task_collection, s.name, s.cadence_minutes
HAVING coalesce(max(r.started_at), TIMESTAMP '1970-01-01') < current_timestamp() - make_interval(0, 0, 0, 0, 0, s.cadence_minutes * 2, 0);
```

## Optional: durable history beyond the OLTP store

Lakebase is the live store; if you want run history retained in Delta (e.g. for long-term
analysis or to offload the OLTP instance), run **one scheduled statement**, with no notebook or job
needed. Simplest is a periodic snapshot (parameterize `<catalog>.<schema>`):

```sql
CREATE OR REPLACE TABLE <catalog>.<schema>.ingest_event_history AS
SELECT * FROM lakefed_ingest_lakebase.lakefed_ingest.ingest_event;
```

For append-only growth, an incremental MERGE on the primary key instead of a full snapshot:
```sql
MERGE INTO <catalog>.<schema>.ingest_event_history t
USING lakefed_ingest_lakebase.lakefed_ingest.ingest_event s
ON t.id = s.id
WHEN NOT MATCHED THEN INSERT *;
```

Schedule either as a DBSQL query (or a one-statement task) at whatever cadence durable history
needs. This is optional: the UC-federated reads above work without it.

## The operations app

The optional operations app (`app/`) reads the same tables for a live view: collection health, runs in
progress, a timeline of runs and skipped triggers, schedules with overrun and SLA warnings, and failed
tables. See `app/README.md`.
