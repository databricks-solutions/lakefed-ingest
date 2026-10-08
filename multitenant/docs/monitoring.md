# Observability

The consolidated sweep ingests many tables inside **one** job task (thread pool + FAIR scheduler
pools), so there is no per-table task in the Jobs UI to watch. Visibility is re-created by writing
per-table telemetry to **Lakebase** (concurrent point writes). The Lakebase database is already
registered as a Unity Catalog catalog (`lakefed_ingest_mt_pg`, via the `database_catalogs` resource
in `lakefed_ingest_mt_setup.yml`), so you query the telemetry **directly from DBSQL** — no sync job,
no copy. Writes go to Lakebase (point writes from the sweep); reads go through UC federation.

## Tables (Lakebase, UC path `lakefed_ingest_mt_pg.lakefed_ingest_mt`)

| Table | Grain | Written by |
| --- | --- | --- |
| `sweep_run` | one row per sweep run | `copy_data_sweep` (`start_sweep` at launch, `finish_sweep` at end) |
| `ingest_event` | one row per (sweep, table) | `copy_data_sweep` worker (`record_event_start` → `record_event_finish`) |
| `ct_checkpoint.last_success_at` | per source table | stamped on each successful event (freshness/lag) |

`ingest_event` carries `action` (seed/increment/reseed/skip), `stage` (ensure_sink/read/merge/
checkpoint/seed — pinpoints *where* a failure happened), `status`, `duration_ms`, `rows_read`/
`rows_merged`, `ct_version_from`/`ct_version_to`, `pool`, and `error`. One row is INSERTed at start
(status `running`) and UPDATEd at finish — concurrent workers touch distinct rows, so there is no
write contention.

## Canonical queries (run in DBSQL against the Lakebase UC catalog)

These are Spark-SQL dialect against the federated catalog `lakefed_ingest_mt_pg`. `:collection` is a
DBSQL query parameter.

In-flight tables for the latest sweep of a collection:
```sql
SELECT e.src_database, e.src_table, e.stage, e.pool, e.started_at
FROM lakefed_ingest_mt_pg.lakefed_ingest_mt.ingest_event e
WHERE e.sweep_id = (
        SELECT max(sweep_id) FROM lakefed_ingest_mt_pg.lakefed_ingest_mt.sweep_run
        WHERE task_collection = :collection)
  AND e.status = 'running'
ORDER BY e.started_at;
```

Failures in the latest sweep, with the stage + error (the "find the failing db/table/layer" view):
```sql
SELECT src_database, src_schema, src_table, stage, left(error, 200) AS error, finished_at
FROM lakefed_ingest_mt_pg.lakefed_ingest_mt.ingest_event
WHERE sweep_id = (
        SELECT max(sweep_id) FROM lakefed_ingest_mt_pg.lakefed_ingest_mt.sweep_run
        WHERE task_collection = :collection)
  AND status = 'failed'
ORDER BY finished_at DESC;
```

Per-table freshness / lag (SLA tracking):
```sql
SELECT src_database, src_schema, src_table, last_success_at,
       timestampdiff(MINUTE, last_success_at, current_timestamp()) AS lag_minutes
FROM lakefed_ingest_mt_pg.lakefed_ingest_mt.ct_checkpoint
ORDER BY last_success_at NULLS FIRST;
```

Per-sweep throughput and outcome:
```sql
SELECT sweep_id, task_collection, total, ok, failed, skipped, reseeded,
       timestampdiff(SECOND, started_at, finished_at) AS seconds,
       round(total / nullif(timestampdiff(SECOND, started_at, finished_at), 0), 2) AS tables_per_sec
FROM lakefed_ingest_mt_pg.lakefed_ingest_mt.sweep_run
ORDER BY started_at DESC
LIMIT 20;
```

## Optional: durable history beyond the OLTP store

Lakebase is the live store; if you want sweep history retained in Delta (e.g. for long-term
analysis or to offload the OLTP instance), run **one scheduled statement** — no notebook or job
needed. Simplest is a periodic snapshot (parameterize `<catalog>.<schema>`):

```sql
CREATE OR REPLACE TABLE <catalog>.<schema>.mt_ingest_event_history AS
SELECT * FROM lakefed_ingest_mt_pg.lakefed_ingest_mt.ingest_event;
```

For append-only growth, an incremental MERGE on the primary key instead of a full snapshot:
```sql
MERGE INTO <catalog>.<schema>.mt_ingest_event_history t
USING lakefed_ingest_mt_pg.lakefed_ingest_mt.ingest_event s
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
