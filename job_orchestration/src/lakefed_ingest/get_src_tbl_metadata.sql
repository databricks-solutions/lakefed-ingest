-- Create a WHERE 1=0 view of the source table to infer schema.
-- When use_remote_query is true, uses remote_query() so no foreign catalog is needed.
-- Otherwise uses Lakehouse Federation (src_catalog.src_schema.src_table).
-- Result of describe extended is returned to the job for use downstream.
DECLARE OR REPLACE qry_str STRING;

SET VAR qry_str = CASE WHEN :use_remote_query = 'true' THEN
    'CREATE OR REPLACE TEMP VIEW vw_src AS '
    || 'SELECT * FROM remote_query('
    || chr(39) || :src_connection || chr(39)
    || CASE :src_type
         WHEN 'oracle'  THEN ', service_name => ' || chr(39) || :src_database || chr(39)
         WHEN 'db2_luw' THEN ''  -- database is embedded in the JDBC URL; cannot be passed externally
         ELSE                ', database => '     || chr(39) || :src_database || chr(39)
       END
    || ', query => ' || chr(39)
    ||   'SELECT ' || :select_list || ' FROM ' || :src_schema || '.' || :src_table
    ||   ' WHERE 1=0'
    || chr(39) || ')'
  ELSE
    'CREATE OR REPLACE TEMP VIEW vw_src AS '
    || 'SELECT ' || :select_list || ' FROM ' || :src_catalog || '.' || :src_schema || '.' || :src_table
    || ' WHERE 1 = 0'
  END;

EXECUTE IMMEDIATE qry_str;

DESCRIBE EXTENDED vw_src AS JSON;
