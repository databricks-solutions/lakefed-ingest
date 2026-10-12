# Observability

The ingestion job ingests a whole task_collection inside **one** job task (thread pool + FAIR scheduler
pools), so there is no per-table task in the Jobs UI to watch. Visibility is re-created by writing
per-table telemetry to **Lakebase**. The Lakebase database is already
registered as a Unity Catalog catalog (`lakefed_ingest_lakebase`, via the `database_catalogs` resource
in `lakefed_ingest_setup.yml`), so you query the telemetry **directly from DBSQL** — no sync job,
no copy. Writes go to Lakebase (point writes from the ingestion run); reads go through UC federation.

## Tables (Lakebase, UC path `lakefed_ingest_lakebase.lakefed_ingest`)

If your target overrides the `lakebase_catalog` bundle variable, substitute that catalog name in the
queries below.

| Table | Grain | Written by |
| --- | --- | --- |
| `ingest_run` | one row per ingestion run | `ingest` (`start_run` at launch, `finish_run` at end) |
| `ingest_event` | one row per (run, table) | `ingest` worker (`record_event_start` → `record_event_finish`) |
| `checkpoint.last_success_at` | per source table | stamped on each successful event (freshness/lag) |

`ingest_event` carries `action` (seed/increment/reseed/full), `stage` (plan/detect/ensure_sink/read/
seed/merge/load/checkpoint — pinpoints *where* a failure happened; `plan` = the row's
`src_type`/`load_type` pair isn't supported), `status`, `duration_ms`, `rows_read`/`rows_merged`,
`position_from`/`position_to` (checkpoint positions as text — the CT version for `ct`; NULL for
`full`), `pool`, and `error`. One row is INSERTed at start
(status `running`) and UPDATEd at finish — concurrent workers touch distinct rows, so there is no
write contention.

## Canonical queries (run in DBSQL against the Lakebase UC catalog)

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

Per-table freshness / lag (SLA tracking):
```sql
SELECT src_database, src_schema, src_table, strategy, last_success_at,
       timestampdiff(MINUTE, last_success_at, current_timestamp()) AS lag_minutes
FROM lakefed_ingest_lakebase.lakefed_ingest.checkpoint
ORDER BY last_success_at NULLS FIRST;
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

## Optional: durable history beyond the OLTP store

Lakebase is the live store; if you want run history retained in Delta (e.g. for long-term
analysis or to offload the OLTP instance), run **one scheduled statement** — no notebook or job
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
needs. This is optional — the UC-federated reads above work without it.

## Future: a Databricks App

Both Lakebase tables are app-ready — a lightweight Databricks App can read them (via the same UC
catalog) for a live operations dashboard (in-flight tables, failures-by-stage, per-table freshness,
sweep throughput) without any change to the ingestion code. Out of scope for now; noted as a
follow-on.
