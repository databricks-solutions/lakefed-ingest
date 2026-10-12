/* =============================================================================================
   Source profiling for Change Tracking ingestion sizing  (READ-ONLY)

   Run once per source database, in that database's context (USE <db> or connect to it).
   SQL Server 2016+ and Azure SQL Database. Single batch (no GO), safe to run in SSMS, sqlcmd,
   or Azure Data Studio. Changes nothing: only catalog views, DMVs, and CHANGETABLE reads.

   Permissions: VIEW DATABASE STATE, VIEW DEFINITION, and SELECT + VIEW CHANGE TRACKING on the
   Change Tracking tables (db_owner works). VIEW SERVER STATE is optional (server specs only).

   Load: one CHANGETABLE read per CT-enabled table covering the lookback window. On very busy
   databases, run off-peak or reduce @lookback_hours.

   Output — six result sets, every row tagged with server_name and database_name:
     1. database_summary  version, CT retention, sizes, table counts, server CPU/memory (on Azure SQL,
                          memory is the host's; use azure_service_objective for the database's size)
     2. table_inventory   per table: rows, size, columns, primary key, CT settings
     3. table_changes     per CT table per time bucket: rows changed (inserts / updates / deletes)
     4. bucket_summary    per time bucket: tables changed, rows changed (empty buckets included)
     5. change_summary    tables changed in the window; avg / p95 / max tables changed per bucket
     6. errors            tables that could not be profiled (normally empty)

   Interpreting change counts: Change Tracking keeps only the LATEST change per row, so a row
   changed several times counts once, in the bucket of its latest change. That matches what an
   incremental sweep reads. Commit times come from sys.dm_tran_commit_table and are retained for
   the same period as Change Tracking data; if retention is shorter than @lookback_hours, the
   window is truncated (see database_summary.window_truncated).

   Privacy (defaults): no row data is read into the output — only counts, sizes, versions, and
   timestamps. With @anonymize = 1 (the default), server, database, schema, table, and primary-key
   column names are replaced by SHA-256-derived IDs, so the same table has the same ID in every
   tenant database (useful for sizing) without revealing names. Set @hash_salt to a private value
   (same for all databases, not shared) so IDs can't be reversed by guessing names. Error text is suppressed
   (only error numbers are reported). Set @database_label to tag a database with your own neutral
   label (e.g. 'db01'); it is used instead of the hashed database ID when provided.
   ============================================================================================= */
SET NOCOUNT ON;

DECLARE @lookback_hours  int = 24;   -- profile the last N hours of changes
DECLARE @bucket_minutes  int = 15;   -- bucket size (the target sweep cadence)
DECLARE @anonymize       bit = 1;    -- 1 (default) = hash server/database/schema/table/column names
DECLARE @database_label  nvarchar(64) = NULL;  -- optional neutral label, e.g. N'db01'
DECLARE @hash_salt       nvarchar(128) = N'';  -- RECOMMENDED: set one private value, identical for every
                                               -- database you profile, and keep it to yourselves. IDs stay
                                               -- consistent across your databases but can't be reversed by
                                               -- guessing names (e.g. hashing likely client names).

-- Identifiers used on every output row (hashed unless @anonymize = 0).
DECLARE @server_id nvarchar(128) = CASE WHEN @anonymize = 1
        THEN N'srv_' + LEFT(CONVERT(varchar(64), HASHBYTES('SHA2_256', @hash_salt + CAST(@@SERVERNAME AS nvarchar(128))), 2), 12)
        ELSE CAST(@@SERVERNAME AS nvarchar(128)) END;
DECLARE @database_id nvarchar(128) = COALESCE(@database_label, CASE WHEN @anonymize = 1
        THEN N'db_' + LEFT(CONVERT(varchar(64), HASHBYTES('SHA2_256', @hash_salt + DB_NAME()), 2), 12)
        ELSE DB_NAME() END);

DECLARE @now   datetime = GETDATE();
DECLARE @start datetime = DATEADD(hour, -@lookback_hours, @now);
DECLARE @start_version bigint =
    (SELECT MIN(commit_ts) FROM sys.dm_tran_commit_table WHERE commit_time >= @start);
DECLARE @oldest_commit datetime = (SELECT MIN(commit_time) FROM sys.dm_tran_commit_table);

-- Server specs (optional permission).
DECLARE @cpu_count int = NULL, @memory_gb decimal(10,1) = NULL;
BEGIN TRY
    SELECT @cpu_count = cpu_count, @memory_gb = physical_memory_kb / 1048576.0 FROM sys.dm_os_sys_info;
END TRY
BEGIN CATCH
END CATCH;

-- Tables in scope, with optional anonymized display names.
CREATE TABLE #tables (
    object_id     int           NOT NULL PRIMARY KEY,
    schema_name   nvarchar(128) NOT NULL,
    table_name    nvarchar(128) NOT NULL,
    ct_enabled    bit           NOT NULL,
    display_schema nvarchar(128) NOT NULL,
    display_table  nvarchar(128) NOT NULL
);
INSERT #tables
SELECT t.object_id, s.name, t.name,
       CASE WHEN ctt.object_id IS NULL THEN 0 ELSE 1 END,
       CASE WHEN @anonymize = 1
            THEN N's_' + LEFT(CONVERT(varchar(64), HASHBYTES('SHA2_256', @hash_salt + s.name), 2), 10) ELSE s.name END,
       CASE WHEN @anonymize = 1
            THEN N't_' + LEFT(CONVERT(varchar(64), HASHBYTES('SHA2_256', @hash_salt + s.name + N'.' + t.name), 2), 16) ELSE t.name END
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
LEFT JOIN sys.change_tracking_tables ctt ON ctt.object_id = t.object_id
WHERE t.is_ms_shipped = 0;

-- Per-table changes per bucket.
CREATE TABLE #changes (
    object_id    int      NOT NULL,
    bucket_start datetime NOT NULL,
    changed_rows bigint   NOT NULL,
    inserts      bigint   NOT NULL,
    updates      bigint   NOT NULL,
    deletes      bigint   NOT NULL
);
CREATE TABLE #errors (object_id int NOT NULL, error_number int NULL, error_message nvarchar(4000) NULL);

DECLARE @oid int, @from_version bigint, @min_valid bigint, @sql nvarchar(max);
DECLARE ct_cur CURSOR LOCAL FAST_FORWARD FOR SELECT object_id FROM #tables WHERE ct_enabled = 1;
OPEN ct_cur;
FETCH NEXT FROM ct_cur INTO @oid;
WHILE @@FETCH_STATUS = 0 AND @start_version IS NOT NULL
BEGIN
    BEGIN TRY
        -- CHANGETABLE returns changes AFTER the given version; never ask for a version older than
        -- the table's minimum valid version.
        SET @min_valid = CHANGE_TRACKING_MIN_VALID_VERSION(@oid);
        SET @from_version = CASE WHEN @start_version - 1 < ISNULL(@min_valid, 0)
                                 THEN @min_valid ELSE @start_version - 1 END;
        SET @sql = N'INSERT #changes (object_id, bucket_start, changed_rows, inserts, updates, deletes)
            SELECT @oid, b.bucket_start, COUNT_BIG(*),
                   SUM(CASE ct.SYS_CHANGE_OPERATION WHEN N''I'' THEN 1 ELSE 0 END),
                   SUM(CASE ct.SYS_CHANGE_OPERATION WHEN N''U'' THEN 1 ELSE 0 END),
                   SUM(CASE ct.SYS_CHANGE_OPERATION WHEN N''D'' THEN 1 ELSE 0 END)
            FROM CHANGETABLE(CHANGES ' + QUOTENAME(OBJECT_SCHEMA_NAME(@oid)) + N'.' + QUOTENAME(OBJECT_NAME(@oid)) + N', @from_version) AS ct
            JOIN sys.dm_tran_commit_table AS tc ON tc.commit_ts = ct.SYS_CHANGE_VERSION
            CROSS APPLY (SELECT DATEADD(minute, (DATEDIFF(minute, 0, tc.commit_time) / @bm) * @bm, 0) AS bucket_start) AS b
            WHERE tc.commit_time >= @start
            GROUP BY b.bucket_start;';
        EXEC sys.sp_executesql @sql,
             N'@oid int, @from_version bigint, @bm int, @start datetime',
             @oid, @from_version, @bucket_minutes, @start;
    END TRY
    BEGIN CATCH
        INSERT #errors (object_id, error_number, error_message)
        VALUES (@oid, ERROR_NUMBER(), CASE WHEN @anonymize = 1 THEN NULL ELSE ERROR_MESSAGE() END);
    END CATCH;
    FETCH NEXT FROM ct_cur INTO @oid;
END;
CLOSE ct_cur;
DEALLOCATE ct_cur;

-- Every bucket in the window (so averages include quiet buckets).
CREATE TABLE #buckets (bucket_start datetime NOT NULL PRIMARY KEY);
DECLARE @first_bucket datetime = DATEADD(minute, (DATEDIFF(minute, 0, @start) / @bucket_minutes) * @bucket_minutes, 0);
DECLARE @n_buckets int = @lookback_hours * 60 / @bucket_minutes + 1;
INSERT #buckets
SELECT TOP (@n_buckets) DATEADD(minute, (ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1) * @bucket_minutes, @first_bucket)
FROM sys.all_objects a CROSS JOIN sys.all_objects b;
DELETE FROM #buckets WHERE bucket_start > @now;

-- 1. database_summary --------------------------------------------------------------------------
SELECT
    @server_id                                          AS server_name,
    @database_id                                        AS database_name,
    CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50)) AS product_version,
    CAST(SERVERPROPERTY('Edition') AS nvarchar(128))   AS edition,
    d.compatibility_level,
    d.snapshot_isolation_state_desc,
    ctd.retention_period                                AS ct_retention_period,
    ctd.retention_period_units_desc                     AS ct_retention_units,
    ctd.is_auto_cleanup_on                              AS ct_auto_cleanup,
    CHANGE_TRACKING_CURRENT_VERSION()                   AS ct_current_version,
    (SELECT COUNT(*) FROM #tables)                      AS user_tables,
    (SELECT COUNT(*) FROM #tables WHERE ct_enabled = 1) AS ct_tables,
    (SELECT CAST(SUM(a.total_pages) * 8 / 1024.0 AS decimal(18,1)) FROM sys.allocation_units a) AS reserved_mb,
    (SELECT CAST(SUM(a.used_pages)  * 8 / 1024.0 AS decimal(18,1)) FROM sys.allocation_units a) AS used_mb,
    @cpu_count                                          AS server_cpu_count,
    @memory_gb                                          AS server_memory_gb,   -- on Azure SQL this is the host's
    CAST(DATABASEPROPERTYEX(DB_NAME(), 'ServiceObjective') AS nvarchar(64)) AS azure_service_objective, -- NULL on SQL Server
    @lookback_hours                                     AS lookback_hours,
    @bucket_minutes                                     AS bucket_minutes,
    @start                                              AS window_start,
    @now                                                AS window_end,
    @oldest_commit                                      AS oldest_commit_time_retained,
    CASE WHEN @oldest_commit > @start THEN 1 ELSE 0 END AS window_truncated
FROM sys.databases d
LEFT JOIN sys.change_tracking_databases ctd ON ctd.database_id = d.database_id
WHERE d.database_id = DB_ID();

-- 2. table_inventory ---------------------------------------------------------------------------
WITH sizes AS (
    SELECT ps.object_id,
           SUM(CASE WHEN ps.index_id IN (0, 1) THEN ps.row_count ELSE 0 END) AS row_count,
           SUM(ps.reserved_page_count) * 8 / 1024.0                         AS reserved_mb,
           SUM(ps.used_page_count) * 8 / 1024.0                             AS used_mb
    FROM sys.dm_db_partition_stats ps
    GROUP BY ps.object_id
),
pk AS (
    SELECT i.object_id,
           STUFF((SELECT N',' + c.name
                  FROM sys.index_columns ic
                  JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
                  WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id
                  ORDER BY ic.key_ordinal
                  FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 1, N'') AS pk_columns,
           (SELECT COUNT(*) FROM sys.index_columns ic WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id) AS pk_column_count,
           (SELECT TYPE_NAME(c.user_type_id)
              FROM sys.index_columns ic
              JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
             WHERE ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.key_ordinal = 1) AS pk_leading_type
    FROM sys.indexes i
    WHERE i.is_primary_key = 1
)
SELECT
    @server_id AS server_name, @database_id AS database_name,
    t.display_schema AS schema_name, t.display_table AS table_name,
    ISNULL(sz.row_count, 0)                         AS row_count,
    CAST(ISNULL(sz.reserved_mb, 0) AS decimal(18,2)) AS reserved_mb,
    CAST(ISNULL(sz.used_mb, 0) AS decimal(18,2))     AS used_mb,
    (SELECT COUNT(*) FROM sys.columns c WHERE c.object_id = t.object_id) AS column_count,
    CASE WHEN @anonymize = 1 THEN NULL ELSE pk.pk_columns END AS pk_columns,
    ISNULL(pk.pk_column_count, 0)                   AS pk_column_count,
    pk.pk_leading_type,
    t.ct_enabled,
    ctt.is_track_columns_updated_on                 AS ct_track_columns_updated,
    CASE WHEN t.ct_enabled = 1 THEN CHANGE_TRACKING_MIN_VALID_VERSION(t.object_id) END AS ct_min_valid_version
FROM #tables t
LEFT JOIN sizes sz ON sz.object_id = t.object_id
LEFT JOIN pk ON pk.object_id = t.object_id
LEFT JOIN sys.change_tracking_tables ctt ON ctt.object_id = t.object_id
ORDER BY reserved_mb DESC;

-- 3. table_changes -----------------------------------------------------------------------------
SELECT
    @server_id AS server_name, @database_id AS database_name,
    t.display_schema AS schema_name, t.display_table AS table_name,
    c.bucket_start, c.changed_rows, c.inserts, c.updates, c.deletes
FROM #changes c
JOIN #tables t ON t.object_id = c.object_id
ORDER BY c.bucket_start, c.changed_rows DESC;

-- 4. bucket_summary ----------------------------------------------------------------------------
SELECT
    @server_id AS server_name, @database_id AS database_name,
    b.bucket_start,
    COUNT(DISTINCT c.object_id)  AS tables_changed,
    ISNULL(SUM(c.changed_rows), 0) AS rows_changed
FROM #buckets b
LEFT JOIN #changes c ON c.bucket_start = b.bucket_start
GROUP BY b.bucket_start
ORDER BY b.bucket_start;

-- 5. change_summary ----------------------------------------------------------------------------
WITH per_bucket AS (
    SELECT b.bucket_start,
           COUNT(DISTINCT c.object_id)    AS tables_changed,
           ISNULL(SUM(c.changed_rows), 0) AS rows_changed
    FROM #buckets b
    LEFT JOIN #changes c ON c.bucket_start = b.bucket_start
    GROUP BY b.bucket_start
),
stats AS (
    SELECT DISTINCT
        PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY tables_changed) OVER () AS p95_tables_changed_per_bucket,
        PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY rows_changed)   OVER () AS p95_rows_changed_per_bucket
    FROM per_bucket
)
SELECT
    @server_id AS server_name, @database_id AS database_name,
    (SELECT COUNT(*) FROM #tables WHERE ct_enabled = 1)          AS ct_tables,
    (SELECT COUNT(DISTINCT object_id) FROM #changes)             AS tables_changed_in_window,
    (SELECT COUNT(*) FROM per_bucket)                            AS buckets,
    (SELECT COUNT(*) FROM per_bucket WHERE tables_changed > 0)   AS buckets_with_changes,
    (SELECT CAST(AVG(CAST(tables_changed AS float)) AS decimal(18,2)) FROM per_bucket) AS avg_tables_changed_per_bucket,
    (SELECT p95_tables_changed_per_bucket FROM stats)            AS p95_tables_changed_per_bucket,
    (SELECT MAX(tables_changed) FROM per_bucket)                 AS max_tables_changed_per_bucket,
    (SELECT ISNULL(SUM(changed_rows), 0) FROM #changes)          AS rows_changed_in_window,
    (SELECT CAST(AVG(CAST(rows_changed AS float)) AS decimal(18,2)) FROM per_bucket)   AS avg_rows_changed_per_bucket,
    (SELECT p95_rows_changed_per_bucket FROM stats)              AS p95_rows_changed_per_bucket,
    (SELECT MAX(rows_changed) FROM per_bucket)                   AS max_rows_changed_per_bucket,
    (SELECT COUNT(*) FROM #errors)                               AS tables_with_errors;

-- 6. errors ------------------------------------------------------------------------------------
SELECT @server_id AS server_name, @database_id AS database_name,
       t.display_schema AS schema_name, t.display_table AS table_name, e.error_number, e.error_message
FROM #errors e
JOIN #tables t ON t.object_id = e.object_id;

DROP TABLE #changes; DROP TABLE #errors; DROP TABLE #buckets; DROP TABLE #tables;
