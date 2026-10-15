-- Get the maximum watermark value from the source table.
-- When use_remote_query is true, pushes the MAX() query to the source via remote_query
-- (required when no Unity Catalog foreign catalog is configured for the source).
-- Otherwise uses Lakehouse Federation.

DECLARE OR REPLACE qry_str STRING;

SET VAR qry_str = CASE WHEN :use_remote_query = 'true' THEN
    'SELECT CAST(max_val AS STRING) AS watermark FROM remote_query('
    || chr(39) || :src_connection || chr(39)
    || CASE :src_type
         WHEN 'oracle'  THEN ', service_name => ' || chr(39) || :src_database || chr(39)
         WHEN 'db2_luw' THEN ''  -- database is embedded in the JDBC URL; cannot be passed externally
         ELSE                ', database => '     || chr(39) || :src_database || chr(39)
       END
    || ', query => ' || chr(39)
    ||   'SELECT MAX(' || :watermark_col || ') AS max_val'
    ||   ' FROM ' || :src_schema || '.' || :src_table
    || chr(39) || ')'
  ELSE
    'SELECT CAST(MAX(' || :watermark_col || ') AS STRING) AS watermark'
    || ' FROM ' || :src_catalog || '.' || :src_schema || '.' || :src_table
  END;

EXECUTE IMMEDIATE qry_str;
