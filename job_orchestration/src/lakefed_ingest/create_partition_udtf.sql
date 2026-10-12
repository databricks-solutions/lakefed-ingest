-- Create the generate_partition_list UDTF in the control catalog/schema.
-- This UDTF generates partition WHERE clauses from partition column boundaries,
-- implementing the same algorithm as get_partition_list() in main.py (derived from
-- Spark JDBC partitioning logic).
--
-- The function is registered via EXECUTE IMMEDIATE to allow dynamic catalog/schema
-- injection, since identifier() is not supported in DDL statements.
--
-- Parameters:
--   partition_col  - Column name used in generated WHERE clauses
--   lower_bound    - Min value of partition column cast to STRING
--   upper_bound    - Max value of partition column cast to STRING
--   col_type       - Data type of partition column (e.g. 'int', 'date', 'timestamp')
--   num_partitions - Number of partitions to generate
--
-- Returns TABLE(id INT, where_clause STRING)

DECLARE OR REPLACE fn_sql STRING;

SET VAR fn_sql =
    'CREATE OR REPLACE FUNCTION ' || :ctrl_catalog || '.' || :ctrl_schema || '.generate_partition_list'
    || '('
    ||   'partition_col STRING,'
    ||   'lower_bound STRING,'
    ||   'upper_bound STRING,'
    ||   'col_type STRING,'
    ||   'num_partitions INT'
    || ') RETURNS TABLE (id INT, where_clause STRING)'
    || ' LANGUAGE PYTHON '
    || ' HANDLER ' || chr(39) || 'GeneratePartitionList' || chr(39)
    || ' AS $$
class GeneratePartitionList:
    """Generates partition WHERE clauses.

    Ported from get_partition_list(), get_internal_bound_value(), and
    bound_value_to_str() in main.py. Implements the Spark JDBC partitioning
    algorithm: given lower/upper bounds and a partition count, yields N
    non-overlapping WHERE clauses that together cover all rows including NULLs.

    Spark source:
    https://github.com/apache/spark/blob/7bbcbb84/sql/core/src/main/scala/org/apache/spark/sql/execution/datasources/jdbc/JDBCRelation.scala#L129
    """

    def _to_int(self, val, col_type):
        """Convert bound value string to integer for stride arithmetic."""
        from datetime import date, datetime, timezone
        t = col_type.upper()
        if t in ("INT", "BIGINT", "SMALLINT", "TINYINT", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC"):
            return int(float(val))
        elif t == "DATE":
            d = date.fromisoformat(val)
            dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            return int(dt.timestamp())
        elif t in ("TIMESTAMP", "TIMESTAMP_NTZ"):
            s = val.split(".")[0]
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        raise ValueError("Unsupported col_type: " + col_type)

    def _to_str(self, int_val, col_type):
        """Convert integer bound back to a SQL literal string."""
        from datetime import datetime, timezone
        q = chr(39)  # single quote character — avoids embedding quotes in SQL string
        t = col_type.upper()
        if t in ("INT", "BIGINT", "SMALLINT", "TINYINT", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC"):
            return str(int_val)
        elif t == "DATE":
            return q + datetime.fromtimestamp(int_val, tz=timezone.utc).strftime("%Y-%m-%d") + q
        elif t in ("TIMESTAMP", "TIMESTAMP_NTZ"):
            return q + datetime.fromtimestamp(int_val, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + q
        raise ValueError("Unsupported col_type: " + col_type)

    def eval(self, partition_col, lower_bound, upper_bound, col_type, num_partitions):
        if lower_bound is None or upper_bound is None:
            yield (0, "1=1")
            return

        lb = self._to_int(lower_bound, col_type)
        ub = self._to_int(upper_bound, col_type)
        stride = int(ub / num_partitions - lb / num_partitions)

        i = 0
        current = lb
        while i < num_partitions:
            l_str = self._to_str(current, col_type)
            l_bound = partition_col + " >= " + l_str if i != 0 else None
            current += stride
            u_str = self._to_str(current, col_type)
            u_bound = partition_col + " < " + u_str if i != num_partitions - 1 else None

            if u_bound is None:
                wc = l_bound
            elif l_bound is None:
                wc = u_bound + " or " + partition_col + " is null"
            else:
                wc = l_bound + " and " + u_bound

            yield (i, wc)
            i += 1
$$
';

EXECUTE IMMEDIATE fn_sql;