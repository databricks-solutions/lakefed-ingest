# Scale simulation

The scale harness (`tests/scale/`) runs the discover job, the operations app's create-by-reference and upload
paths, and the monitoring pages' queries against a simulated 22,000-database SQL Server catalog
(`tests/synthetic/server_catalog.py`, 240 tables each, 5,280,000 tables). Nothing connects to a database or a workspace:
discovery reads go to a simulated server at the discovery boundary, and Lakebase statements go to recording fakes.

This is a plain Python run on a laptop. It shows the code's call counts, statement counts and row counts, its
Python time on this machine, and the wall time a given per-call latency would give. It does not measure Lakebase or
SQL Server latency. The numbers that need a test on a real deployment are listed at the end.

## Running it

    python tests/scale/run_scale.py                  # 40 databases, the size the default test run uses
    python tests/scale/run_scale.py 417              # 417 databases, 100,080 tables (99,360 discovered)
    python tests/scale/run_scale.py 22000            # 22,000 databases, 5,280,000 tables
    LAKEFED_SCALE=1 python tests/scale/run_scale.py  # both sizes in one process (peak memory is then the larger run's)

Run one size per process for clean peak memory. The default `pytest` run stays small; `LAKEFED_SCALE=1 pytest
tests/scale` adds the 417-database and 22,000-database tests.

## Results

Measured by `run_scale.py`, one process per size. "Python s" is the time the code took on this laptop. "Wall" is
simulated from each database's catalog calls and the per-call latency (see the assumptions below).

### Discovery

| Measure | 417 databases (100,080 tables) | 22,000 databases (5,280,000 tables) |
|---|---|---|
| Tables discovered (refused / retried) | 99,360 (3 / 2) | 5,279,280 (3 / 2) |
| Catalog calls | 1,248 | 65,997 |
| Lakebase statements | 100,608 | 5,345,277 |
| Lakebase rows | 100,608 | 5,345,277 |
| Python time | 6.4 s | 349.5 s |
| Peak memory (process) | 138 MB | 140 MB |

Simulated wall time, in seconds, by per-call latency:

| Databases | Workers | 0.25 s | 0.5 s | 0.9 s | 1.5 s | 2.0 s |
|---|---|---|---|---|---|---|
| 417 | 8 (notebook today) | 45.2 | 84.5 | 148.5 | 243.0 | 320.5 |
| 417 | 32 (design) | 24.5 | 26.5 | 38.7 | 64.5 | 86.0 |
| 22,000 | 8 (notebook today) | 2,068.8 | 4,131.5 | 7,432.2 | 12,382.5 | 16,508.0 |
| 22,000 | 32 (design) | 517.8 | 1,034.0 | 1,859.4 | 3,097.5 | 4,130.0 |

At 0.9 s a call the 22,000-database discovery takes 31 minutes at 32 workers and 2.1 hours at the notebook's 8.

### Create by reference

| Measure | 417 databases (99,360 tables) | 22,000 databases (5,280,000 tables) |
|---|---|---|
| Result | planned: 4 groups, 214 statements, 198,738 rows, no errors | refused at the 120,000-line cap |
| CSV size | 15.2 MB | not built (refused before the writes) |
| Planning time | 5.8 s (CSV 0.3 s, writes 1.0 s) | 0.5 s to the refusal at 120,001 lines; full planning about 21.6 s (extrapolated) |
| Peak memory | 653 MB | about 35 GB (extrapolated linearly; not measured) |

### Create by database: before and after the packing

Both paths run in one process at each size. "Before" is `create_by_database`, one transaction per database, as the route
ran it. "After" is `create_by_chunk`, the route now: the databases are read in batches of 100 and written in transactions
of up to 5,000 tables, a database never split. Round trips are the statements plus a BEGIN and a COMMIT for each
transaction. The round-trip time is those round trips at 2 ms each. It is not the server's work, which the last section
estimates.

| Measure | 417 before | 417 after |
|---|---|---|
| Tables | 99,360 | 99,360 |
| Lakebase statements | 4,155 | 285 |
| of which reads | 2,072 | 91 |
| Transactions | 416 | 23 |
| Round trips | 4,987 | 331 |
| Round-trip time at 2 ms | 10.0 s | 0.7 s |
| Check and write time (Python) | 3.5 s + 4.0 s | 3.3 s + 4.3 s |
| Peak memory | 252 MB | 252 MB |

Before packing, each database cost about ten statements (4,155 over 417 databases). After packing a transaction cost about
twelve (285 over 23), and the reads of a whole batch are one statement. The round trips fell by about fifteen times at 417
databases.

At 22,000 databases (5,279,280 tables), the before run was measured once with the pre-packing runner: 176,404 statements,
21,999 transactions, 220,402 round trips, about 441 s at 2 ms. The after run was not made on the Mac: database-shaped
runs are left to a real deployment. Packing predicts about 1,056 transactions and about 15,000 round trips at 5,000 tables a
transaction, which is the same factor again, but that is an estimate until main's Databricks run measures it.

### Each statement of a database, at 1,000 tables a database

The by-database create's statements, what each one does, and what it costs at 1,000 tables (the size the main test
uses). "Before" is the per-database write. "After" is what each becomes once a transaction holds up to 5,000 tables.

| Statement | Before (per database) | Index used before | Cost at 1,000 tables before | After |
|---|---|---|---|---|
| Read the database's rows, upload source | 2 (check pass, write pass) | none: `lower()` on server and name | reads every line of the upload (100,000 at 100 databases) | exact names, an index range on `table_upload_line_database_idx`; one read per batch of 100 in the check, one per transaction in the write |
| Sink check of the group's rows | 1 | none: `lower(src_database)` skips `control_database_idx` and the partial collection index needs `task_enabled` | reads the group's control rows (about 12,500 a group) for each database | exact `src_database = any(...)` on `control_database_idx`; one per transaction |
| Mixed-sink check | 1 | none: an expression over every control row | scans all of `control`, so it grows with the whole table | three-array lookups on `control_sink_client_uq`; one per transaction |
| Split-database check | 1 | `control_database_idx` (exact name) | the database's own rows only | unchanged; one per transaction |
| Control insert of the database's rows | 1 for 1,000 rows | `control_sink_client_uq` for the conflict check, plus the table's other indexes | the real write work: 1,000 index entries across the table's indexes; not overhead | 1,000-row statements, five per transaction |
| Change log (`control_change`) | 1 | none needed (insert) | cheap | one per transaction |
| Group member | 1 | `schedule_member_uq` | cheap | one per transaction, in one statement for the groups it lacks |
| Run lock | 1 per group | advisory key | cheap | one per transaction, for all its groups |
| Task progress | 1 | primary key | cheap | one per transaction |
| Rollup rewrite | none on this branch; one per database on the `fixes` branch and main | the rewrite joins `control` on `coalesce(t.src_database, '')`, so no index applies | scans all of `control` and joins `checkpoint` and `database_check`: the likely heavy statement on main | not on this branch; after packing, once per transaction on `fixes`, which still needs its join made exact |

The statements that scan a table are the ones that grow with the whole table rather than with the database, so they are
the ones that make the cost rise faster than linearly. The control insert is the work the table really does, one index
entry per row for each of its indexes.

### Expected time for 100,000 and 5.28 million tables

These are estimates, not measurements. Lakebase's per-statement time is not measured here, and the server's work per table
is estimated, not timed. Main's real rerun is the test that decides.

The model: round trips at 2 ms each, plus the server's work per table, which I estimate at 50 to 150 microseconds (a
control row with its indexes, its change-log row, and its share of the upload read's index range).

| Tables | Transactions | Round trips (about) | Round-trip time | Server work | Expected total |
|---|---|---|---|---|---|
| 100,000 | 20 | 300 | 0.6 s | 5 to 15 s | about 6 to 16 s; central 10 s |
| 5,280,000 | 1,056 | 15,000 | 30 s | 4.4 to 13 min | about 5 to 14 min; central 9 min |

The round-trip counts take the 417-database run's 14 round trips a transaction and the check's few reads. These
figures assume three things that must hold on the real app:

- An upload source. Its database reads are exact index ranges, so a transaction's read does not scan the whole upload.
- The database rollup rewrite on `fixes` has an exact join (`t.src_database = k.src_database`, through
  `control_database_idx`, with the empty-name case handled apart). Without it the rewrite scans all of `control` on every
  transaction, and the time grows with the square of the table count.
- Not a discovery source. The discovery branch of `source_rows_sql` is a DISTINCT ON over every discovered table of the
  discovery, filtered by lower-case names, so every transaction reads the whole discovery. At 5.28 million tables that is
  about 1,056 reads of 5.28 million rows, and the 5.28 million estimate does not hold until that read is made exact too.
  The fix is to push `src_database = any(...)` into the DISTINCT ON's inner filter, which the discovered table's primary
  key serves. It is not done here because it cannot be checked without a server.

For main's own number: 84.2 s for 100,000 tables is 0.84 s a database, which is what one rollup rewrite per database
(a scan of `control` with its joins) would cost. Packing alone cuts the rewrites from 100 to 20, which would bring 84.2 s
to about 20 s if the rewrite is the cost. Main can confirm it by timing a create with the rewrite skipped, or by
`EXPLAIN (ANALYZE, BUFFERS)` on the rewrite. The exact join is what keeps the time linear in tables.

The new statements have not run against Postgres here (the `_pg` tests skip without a server), so their plans are reasoned
from the indexes in `lakebase_schema.sql`, not observed.

### Upload

| Measure | 417 databases (99,360 lines) | 22,000 databases (5,280,000 lines) |
|---|---|---|
| Result | read and written | refused at the 120,000-line cap |
| Read / write time | 0.5 s / 0.2 s | 0.4 s to the refusal at 120,001 lines |
| Statements | 101 (1 header, 100 inserts of up to 1,000 lines) | 121 per 120,000-line upload (computed) |
| Rows | 99,361 (the header, then one row per line) | not written |
| Body size | 15.2 MB (153 bytes a line) | 44 uploads of about 6.4 MB (53 bytes a line, the probe's rate) to about 18.4 MB (the 417 rate) |

The 5,280,000-line file would be 280 MB to 808 MB as one body, over the 64 MiB body limit. The 120,000-line cap splits it
into 44 uploads that are each under the limit, so the cap binds before the body limit at either line size.

### Monitoring pages (statement shapes)

These come from the app's own routes through `TestClient`, with the database stubbed to record statements and answer
with no rows. So the shape is what each page asks for, not how many rows it returns at scale. Pages that 404 or
return early against empty data show fewer statements than they issue for real.

| Page | Status | Statements | Statement kinds |
|---|---|---|---|
| /api/overview | 200 | 4 | select control, select ingest_run, select schedule, with ingest_run |
| /api/collections | 200 | 1 | select control |
| /api/errors | 200 | 1 | select ingest_event |
| /api/ingestion-jobs | 200 | 0 | none (the list reads nothing against empty data) |
| /api/ingestion-jobs/1 | 404 | 0 | none (no job to read) |
| /api/ingestion-jobs/1/live | 404 | 1 | select schedule |
| /api/runs | 200 | 1 | select ingest_run |
| /api/tables | 200 | 1 | select control |
| /api/changes | 200 | 1 | select control_change |
| /api/schedules | 200 | 1 | select schedule |
| /api/timeline | 200 | 0 | none |
| /api/discoveries | 200 | 1 | select discovery |

The kinds are the statement verb and table, with the `lakefed_ingest.` schema left off here.

## What each number assumes

- **Catalog calls.** One listing call, then three catalog queries (tables, columns, pk) per database that is read. The
  totals fit one call for each refused database and one extra call for each of the two retried databases:
  1 + 3 × 414 + 3 + 2 = 1,248 at 417, and 1 + 3 × 21,997 + 3 + 2 = 65,997 at 22,000. The split is inferred from the totals.
- **Lakebase statements.** Per database that is read: a start insert, a delete, one insert per table (the notebook's
  `executemany`, which psycopg sends as one statement per row), and a finish update. A refused database gets only its
  start and finish. At 417: 414 × 3 + 3 × 2 + 99,360 = 100,608. The sequence is the notebook's, mirrored in
  `steps.DiscoveryWriter`, and a test pins its column list to the notebook's.
- **Lakebase rows.** The fake counts one row per statement for these statements (each carries one row), so rows equal
  statements in the discovery rows.
- **Discovery Python time and peak memory.** Measured on this machine, including the simulated server's work and the
  writes' row construction. They exclude the network and the database.
- **Wall time.** The listing and each database's catalog calls (calls x per-call latency, plus retry waits) are laid out
  on a pool in listing order, as a `ThreadPoolExecutor` takes them. The per-call latency is applied afterwards, so one
  run gives every latency in the table. The 8 workers match the notebook (`min(8, databases)`); 32 is the design's cap.
- **Per-call latency 0.25 to 2.0 s.** The design's 25 to 40 minutes at 32 workers for 22,000 databases implies
  0.7 to 1.2 s a call. The 0.9 s line is the middle of that range and gives 31 minutes. This is an assumption, not a
  measurement of the catalog's round trip.
- **Retry waits.** A paused database's waits come from the discovery's retry backoff, with the jitter fixed at 0.5. They
  are booked as waits, not scaled with latency, so they form a fixed floor in the wall time.
- **Lakebase time.** Statements are counted, not timed. No number above includes a Lakebase latency, so the create,
  upload and monitoring rows say nothing about Lakebase time.
- **Create by reference.** `new_job.plan` and the writes of `create_ingestion_job` (controls in thousands, the change
  log, group members, the schedule, the selection, the touch), run through the app's own helpers with a recording
  cursor. The schedule's cron validation (which reads the Databricks jobs) and the read of the selection from Lakebase
  are not run; the selection is built in Python from the discovered rows.
- **The 120,000-line cap.** `config_import.plan_import` builds the full list of parsed lines before it compares the
  count, so an oversize create holds every row before it refuses. That is why the 22,000-database create is not run in
  full: its planning memory is extrapolated. `read_upload` stops at line 120,001.
- **Upload body.** The probe's lines are about 53 bytes; the 417 run's are about 153 bytes (15,196,485 bytes over 99,360
  lines). JSON escaping and the request's own overhead are left out.
- **Peak memory.** Peak resident memory of the process (macOS `ru_maxrss`, cumulative for the process). The
  22,000-database create figure is extrapolated linearly from the 99,360-table run and is not measured.

## Reruns after other pieces merge

- **Create by database.** Changes what the create plans (groups per database, not per server) and the writes it makes.
  Rerun the create rows and the statement counts; the 5,280,000-table create's refusal and its extrapolation move with
  the planner.
- **Rollups.** Add writes and reads per run and per collection. Rerun the write counts and the monitoring pages' shapes.
- **Run poller.** Changes how often and by which statements a run's progress is read. Rerun the monitoring shapes; the
  poll's own statement count per interval is new.
- **Re-discovery.** Writes a new discovery per run and follows `job_selection`. Rerun the discovery rows, and the
  selection's rows at the create.

## Numbers only a test on a real deployment can give

- Lakebase statement and round-trip time, for the discovery writes (100,608 at 417 databases; 5,345,277 at 22,000), the
  create's writes, the upload's lines and each monitoring page.
- The real per-call latency of the SQL Server catalog queries, and whether the source's concurrency limit holds at 32.
- The Azure Resource Manager listing time at 22,000 databases (pages of 100).
- Peak memory of the create and the selection read at 5,280,000 tables, on the app's real compute.
- Monitoring pages' latency with 22,000 databases and 5,280,000 control rows in Lakebase (the state scan's cost).
- The upload route's real 413 behaviour through the Apps proxy, and the real request size with JSON encoding.
- The by-database create's time with the packed path: main's upload of 100 databases x 1,000 tables, Create by reference
  over the whole server (84.2 s before the packing). The expected times in the section above are estimates until that rerun.
