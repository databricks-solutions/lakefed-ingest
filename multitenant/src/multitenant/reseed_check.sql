-- Workstream F (reseed detection) — owned by Alex. IMPLEMENTED.
-- Returns one row/column `min_valid_version` =
--   CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID('<schema>.<table>'))
-- from the SQL Server source via remote_query. The ingest job's `if_reseed` condition compares it
-- to the checkpoint ct_version; if min_valid_version > checkpoint, CT retention has lapsed and the
-- table is routed to mark_reseed.
--
-- Parameters (sql_task): :src_connection, :src_database, :src_schema, :src_table.
-- chr(39) is a single quote (builds OBJECT_ID('schema.table')). The inner query is bound to
-- remote_query via EXECUTE IMMEDIATE ... USING (no escaping) and carries a unique uuid() comment.

DECLARE OR REPLACE inner_q STRING;
SET VAR inner_q =
    'SELECT CAST(CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID('
    || chr(39) || :src_schema || '.' || :src_table || chr(39)
    || ')) AS BIGINT) AS min_valid_version /* ' || uuid() || ' */';

EXECUTE IMMEDIATE
    'SELECT min_valid_version FROM remote_query(:c, database => :d, query => :q)'
    USING :src_connection AS c, :src_database AS d, inner_q AS q;
