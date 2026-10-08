-- Workstream B (version capture) — owned by Alex. IMPLEMENTED.
-- Returns one row/column `ct_version` = CHANGE_TRACKING_CURRENT_VERSION() from the SQL Server
-- source via the governed UC connection (remote_query). Captured as the upper-bound version
-- BEFORE the seed (seeding branch) and before each CT read (cdc branch); the checkpoint advances
-- to it only AFTER the bronze MERGE commits (at-least-once + idempotent PK MERGE).
--
-- Parameters (sql_task): :src_connection, :src_database.
-- The inner query is bound to remote_query as a parameter (:q) via EXECUTE IMMEDIATE ... USING,
-- so its text needs no quote-escaping (the "clean" style). A per-execution uuid() comment makes
-- each call's text unique, defeating remote_query's identical-text result cache (which otherwise
-- silently stalls the watermark on classic compute).

DECLARE OR REPLACE inner_q STRING;
SET VAR inner_q =
    'SELECT CAST(CHANGE_TRACKING_CURRENT_VERSION() AS BIGINT) AS ct_version /* ' || uuid() || ' */';

EXECUTE IMMEDIATE
    'SELECT ct_version FROM remote_query(:c, database => :d, query => :q)'
    USING :src_connection AS c, :src_database AS d, inner_q AS q;
