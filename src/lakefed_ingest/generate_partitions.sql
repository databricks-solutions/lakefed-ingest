-- Generate partition WHERE clauses for partitioned table ingestion.
--
-- Logic:
--   1. Get partition column data type from upstream src_tbl_metadata
--   2. Get MIN/MAX bounds from the source table via remote_query
--   3. Get source table size via remote_query (source-type specific)
--   4. Calculate num_partitions = max(table_size_mb / partition_size_mb, 2)
--   5. Call generate_partition_list UDTF and write results to _partitions table
--   6. Return batch_id_list for the downstream for_each_task
--
-- remote_query is used for all source-type queries (sqlserver, oracle, postgresql, redshift)
-- to enable native SQL dialect passthrough and better performance.
-- Synapse uses Lakehouse Federation (remote_query not supported).
--
-- Output (task output first_row):
--   batch_id_list  ARRAY<INT>  - Sorted distinct batch IDs for for_each_task
--   cnt_partitions BIGINT      - Total number of partitions generated

DECLARE OR REPLACE partitions_tbl  STRING;
DECLARE OR REPLACE conn_opts       STRING;
DECLARE OR REPLACE col_type        STRING;
DECLARE OR REPLACE lower_bound_str STRING;
DECLARE OR REPLACE upper_bound_str STRING;
DECLARE OR REPLACE table_size_mb   DOUBLE DEFAULT 0;
DECLARE OR REPLACE num_partitions  INT    DEFAULT 2;
DECLARE OR REPLACE num_batches     INT    DEFAULT 1;
DECLARE OR REPLACE qry             STRING;

SET VAR partitions_tbl = :tgt_catalog || '.' || :tgt_schema || '.' || :tgt_table || '_partitions';

-- conn_opts holds the connection name + db option for reuse across remote_query calls.
-- chr(39) = single-quote character, used to inject quotes into dynamic SQL strings.
-- Oracle uses service_name instead of database.
SET VAR conn_opts =
  chr(39) || :src_connection || chr(39)
  || CASE :src_type
       WHEN 'oracle' THEN ', service_name => ' || chr(39) || :src_database || chr(39)
       ELSE               ', database => '     || chr(39) || :src_database || chr(39)
     END;

-- 1. Partition column data type from upstream src_tbl_metadata JSON
SET VAR col_type = (
  SELECT col.type.name
  FROM (
    SELECT explode(
      from_json(
        :src_tbl_metadata,
        'struct<columns: array<struct<name: string, type: struct<length: bigint, name: string, precision: bigint, scale: bigint>>>>'
      ).columns
    ) AS col
  )
  WHERE col.name = :partition_col
);

-- 2. Partition boundaries via remote_query (native passthrough)
--    MIN/MAX are standard SQL; CAST to STRING is applied in the outer Databricks layer.
--    Synapse and delta fall back to Lakehouse Federation.
SET VAR qry = CASE
  WHEN :src_type IN ('sqlserver', 'oracle', 'postgresql', 'redshift') THEN
    'SELECT CAST(min_val AS STRING), CAST(max_val AS STRING)'
    || ' FROM remote_query(' || conn_opts
    || ', query => ' || chr(39)
    ||   'SELECT MIN(' || :partition_col || ') AS min_val, MAX(' || :partition_col || ') AS max_val'
    ||   ' FROM ' || :src_schema || '.' || :src_table
    || chr(39) || ')'
  ELSE
    'SELECT CAST(MIN(' || :partition_col || ') AS STRING),'
    || ' CAST(MAX(' || :partition_col || ') AS STRING)'
    || ' FROM ' || :src_catalog || '.' || :src_schema || '.' || :src_table
  END;

EXECUTE IMMEDIATE qry INTO lower_bound_str, upper_bound_str;

-- 3. Source table size via remote_query
--    WHERE clause string values use backslash escaping: chr(92)||chr(39) produces \'value\'
--    in the qry string, which the SQL parser resolves to 'value' for the remote query.
SET VAR qry = CASE :src_type
  WHEN 'sqlserver' THEN
    'SELECT table_size_mb FROM remote_query(' || conn_opts
    || ', query => ' || chr(39)
    ||   'SELECT CAST(ROUND((SUM(a.total_pages) * 8) / 1024, 2) AS NUMERIC(36,2)) AS table_size_mb'
    ||   ' FROM sys.tables t'
    ||   ' JOIN sys.indexes i ON t.OBJECT_ID = i.object_id'
    ||   ' JOIN sys.partitions p ON i.object_id = p.OBJECT_ID AND i.index_id = p.index_id'
    ||   ' JOIN sys.allocation_units a ON p.partition_id = a.container_id'
    ||   ' LEFT JOIN sys.schemas s ON t.schema_id = s.schema_id'
    ||   ' WHERE s.Name = ' || chr(92) || chr(39) || :src_schema || chr(92) || chr(39)
    ||   ' AND t.name = ' || chr(92) || chr(39) || :src_table || chr(92) || chr(39)
    ||   ' AND t.is_ms_shipped = 0 AND i.object_id > 255'
    || chr(39) || ')'
  WHEN 'oracle' THEN
    -- Requires permission to read sys.dba_segments
    'SELECT table_size_mb FROM remote_query(' || conn_opts
    || ', query => ' || chr(39)
    ||   'SELECT bytes/1024/1024 AS table_size_mb'
    ||   ' FROM sys.dba_segments'
    ||   ' WHERE owner = '        || chr(92) || chr(39) || UPPER(:src_schema) || chr(92) || chr(39)
    ||   ' AND segment_name = '   || chr(92) || chr(39) || UPPER(:src_table)  || chr(92) || chr(39)
    || chr(39) || ')'
  WHEN 'postgresql' THEN
    'SELECT table_size_mb FROM remote_query(' || conn_opts
    || ', query => ' || chr(39)
    ||   'SELECT CEIL(pg_total_relation_size(c.oid) / 1024.0 / 1024.0) AS table_size_mb'
    ||   ' FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid'
    ||   ' WHERE n.nspname = ' || chr(92) || chr(39) || :src_schema || chr(92) || chr(39)
    ||   ' AND c.relname = '   || chr(92) || chr(39) || :src_table  || chr(92) || chr(39)
    || chr(39) || ')'
  WHEN 'redshift' THEN
    'SELECT table_size_mb FROM remote_query(' || conn_opts
    || ', query => ' || chr(39)
    ||   'SELECT size AS table_size_mb'
    ||   ' FROM pg_catalog.svv_table_info'
    ||   ' WHERE schema = '   || chr(92) || chr(39) || :src_schema || chr(92) || chr(39)
    ||   ' AND "table" = '    || chr(92) || chr(39) || :src_table  || chr(92) || chr(39)
    || chr(39) || ')'
  WHEN 'synapse' THEN
    -- remote_query does not support Synapse; use Lakehouse Federation
    -- if better performance is needed, move this query to a view in Synapse; then query the view
    'WITH base AS ('
    ||   'SELECT ((nps.in_row_data_page_count + nps.row_overflow_used_page_count + nps.lob_used_page_count) * 8.0) / 1000 AS mb'
    ||   ' FROM ' || :src_catalog || '.sys.schemas s'
    ||   ' JOIN ' || :src_catalog || '.sys.tables t ON s.schema_id = t.schema_id'
    ||   ' JOIN ' || :src_catalog || '.sys.indexes i ON t.object_id = i.object_id AND i.index_id <= 1'
    ||   ' JOIN ' || :src_catalog || '.sys.pdw_table_distribution_properties tp ON t.object_id = tp.object_id'
    ||   ' JOIN ' || :src_catalog || '.sys.pdw_table_mappings tm ON t.object_id = tm.object_id'
    ||   ' JOIN ' || :src_catalog || '.sys.pdw_nodes_tables nt ON tm.physical_name = nt.name'
    ||   ' JOIN ' || :src_catalog || '.sys.dm_pdw_nodes pn ON nt.pdw_node_id = pn.pdw_node_id'
    ||   ' JOIN ' || :src_catalog || '.sys.pdw_distributions di ON nt.distribution_id = di.distribution_id'
    ||   ' JOIN ' || :src_catalog || '.sys.dm_pdw_nodes_db_partition_stats nps'
    ||     ' ON nt.object_id = nps.object_id AND nt.pdw_node_id = nps.pdw_node_id'
    ||     ' AND nt.distribution_id = nps.distribution_id AND i.index_id = nps.index_id'
    ||   ' WHERE pn.type = ' || chr(39) || 'COMPUTE' || chr(39) || ' AND s.name = ' || chr(39) || :src_schema || chr(39) || ' AND t.name = ' || chr(39) || :src_table || chr(39)
    || ') SELECT SUM(mb) FROM base'
  ELSE
    'SELECT raise_error(' || chr(39) || 'Unsupported src_type: ' || :src_type || chr(39) || ')'
  END;

EXECUTE IMMEDIATE qry INTO table_size_mb;

-- 4. Calculate num_partitions and num_batches (batch_size = 1000, matches Python logic)
SET VAR num_partitions = GREATEST(CAST(table_size_mb / :partition_size_mb AS INT), 2);
SET VAR num_batches    = GREATEST(CAST(CEIL(num_partitions / 1000.0) AS INT), 1);

-- 5. Generate partitions via UDTF and write to _partitions table with batch assignments
--    ntile(num_batches) mirrors the Window + ntile() logic from get_partition_df()
EXECUTE IMMEDIATE
  'CREATE OR REPLACE TABLE ' || partitions_tbl || ' AS'
  || ' SELECT id, where_clause, ntile(' || num_batches || ') OVER (ORDER BY id) AS batch_id'
  || ' FROM ' || :ctrl_catalog || '.' || :ctrl_schema || '.generate_partition_list(?, ?, ?, ?, ' || num_partitions || ')'
  USING :partition_col, lower_bound_str, upper_bound_str, col_type;

-- 6. Return task output — batch_id_list is consumed by the downstream for_each_task.
--    Extra columns are diagnostic and visible in the job run UI without affecting downstream.
EXECUTE IMMEDIATE
  'SELECT sort_array(array_agg(DISTINCT batch_id)) AS batch_id_list,'
  || ' COUNT(*) AS cnt_partitions,'
  || ' ? AS lower_bound,'
  || ' ? AS upper_bound,'
  || ' ? AS table_size_mb,'
  || ' ? AS num_partitions,'
  || ' ? AS num_batches'
  || ' FROM ' || partitions_tbl
  USING lower_bound_str, upper_bound_str, table_size_mb, num_partitions, num_batches;
