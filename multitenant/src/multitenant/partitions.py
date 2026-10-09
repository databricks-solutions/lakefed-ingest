"""Size-bounded partition planning for the partitioned seed.

Strategy (kept from the accelerator's original Python partitioning, ``main.py``
``get_partition_list`` & friends, which the production ``generate_partition_list`` UDTF also
mirrors): bound each partition's SIZE with ``partition_size_mb`` rather than tying the partition
count to cluster cores. A large table therefore yields many small range queries, which the seed
runs N at a time through the same thread pool + FAIR scheduler-pool engine as the sweep
(``parallel.run_parallel``) at a configurable degree of concurrency. This is deliberately NOT
Spark's native JDBC partitioning (``numPartitions`` there also caps concurrency at the core count).

Partition math is derived from Spark's JDBC partitioning
(JDBCRelation.columnPartition): ``stride = int(upper / N - lower / N)``; the first partition also
takes NULLs (``< ub OR col IS NULL``) and the last is open-ended (``>= lb``), so every row lands in
exactly one partition even if the bounds are stale. Numeric, date, and datetime columns are
supported (date/datetime via UTC epoch seconds).

Pure Python — no Spark/dbutils imports — so it is unit-tested off-cluster.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List


def num_partitions_for(table_size_mb, partition_size_mb) -> int:
    """Number of partitions for a table: ``max(table_size_mb / partition_size_mb, 2)`` (same
    formula as the production ``generate_partitions.sql``)."""
    size = float(table_size_mb or 0)
    per = float(partition_size_mb)
    if per <= 0:
        raise ValueError("partition_size_mb must be > 0")
    return max(int(size / per), 2)


def table_size_mb_query(src_schema: str, src_table: str) -> str:
    """T-SQL returning one row ``table_size_mb`` for a SQL Server table (data + index pages), as in
    the original ``_get_table_size_sqlserver``."""
    schema = src_schema.replace("'", "''")
    table = src_table.replace("'", "''")
    return (
        "SELECT CAST(ROUND((SUM(a.total_pages) * 8) / 1024.0, 2) AS NUMERIC(36, 2)) AS table_size_mb"
        " FROM sys.tables t"
        " JOIN sys.indexes i ON t.object_id = i.object_id"
        " JOIN sys.partitions p ON i.object_id = p.object_id AND i.index_id = p.index_id"
        " JOIN sys.allocation_units a ON p.partition_id = a.container_id"
        " LEFT OUTER JOIN sys.schemas s ON t.schema_id = s.schema_id"
        f" WHERE s.name = '{schema}' AND t.name = '{table}'"
        " AND t.is_ms_shipped = 0 AND i.object_id > 255"
    )


def bounds_query(src_schema: str, src_table: str, partition_col: str) -> str:
    """T-SQL returning one row ``(lb, ub)`` = MIN/MAX of the partition column."""
    col = f"[{partition_col}]"
    return f"SELECT MIN({col}) AS lb, MAX({col}) AS ub FROM [{src_schema}].[{src_table}]"


def get_internal_bound_value(bound_value) -> int:
    """Numeric representation of a bound value for the stride calculation."""
    if isinstance(bound_value, bool):
        raise ValueError(f"Unsupported data type: {type(bound_value)}")
    if isinstance(bound_value, (int, float, Decimal)):
        return int(bound_value)
    if isinstance(bound_value, datetime):  # check datetime before date (datetime is a date)
        return int(bound_value.replace(tzinfo=timezone.utc).timestamp())
    if isinstance(bound_value, date):
        dt = datetime(year=bound_value.year, month=bound_value.month, day=bound_value.day)
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    raise ValueError(
        f"Unsupported data type: {type(bound_value)}. Only numeric, date, and datetime are supported"
    )


def bound_value_to_str(bound_value: int, bound_value_orig) -> str:
    """Render an internal (int) bound back to a SQL literal of the original column type."""
    if isinstance(bound_value_orig, (int, float, Decimal)) and not isinstance(bound_value_orig, bool):
        return str(bound_value)
    if isinstance(bound_value_orig, datetime):
        return f"'{datetime.fromtimestamp(bound_value, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}'"
    if isinstance(bound_value_orig, date):
        return f"'{datetime.fromtimestamp(bound_value, tz=timezone.utc).strftime('%Y-%m-%d')}'"
    raise ValueError(
        f"Unsupported data type: {type(bound_value_orig)}. Only int, date, and datetime are supported"
    )


def get_partition_list(partition_col: str, lower_bound, upper_bound, num_partitions: int) -> List[dict]:
    """WHERE clauses for ``num_partitions`` partitions that together cover every row exactly once.

    Faithful port of the original ``get_partition_list`` (Spark-JDBC-derived stride; NULLs in the
    first partition), with the column bracket-quoted for SQL Server. Returns
    ``[{"id": i, "where_clause": "..."}]``.
    """
    if type(lower_bound) is not type(upper_bound):
        raise TypeError(
            f"Bound values must have the same type. Lower bound: {type(lower_bound)}, "
            f"Upper bound: {type(upper_bound)}"
        )
    num_partitions = int(num_partitions)
    if num_partitions < 1:
        raise ValueError("num_partitions must be >= 1")
    col = f"[{partition_col}]"
    lower_bound_orig = lower_bound
    lower = get_internal_bound_value(lower_bound)
    upper = get_internal_bound_value(upper_bound)

    if num_partitions == 1:
        return [{"id": 0, "where_clause": "1=1"}]

    partition_list = []
    stride = int(upper / num_partitions - lower / num_partitions)
    current = lower
    for i in range(num_partitions):
        lb = f"{col} >= {bound_value_to_str(current, lower_bound_orig)}" if i != 0 else None
        current += stride
        ub = (f"{col} < {bound_value_to_str(current, lower_bound_orig)}"
              if i != num_partitions - 1 else None)
        if ub is None:
            where = lb
        elif lb is None:
            where = f"{ub} or {col} is null"
        else:
            where = f"{lb} and {ub}"
        partition_list.append({"id": i, "where_clause": where})
    return partition_list


def effective_num_partitions(lower_bound, upper_bound, num_partitions: int) -> int:
    """Clamp the partition count to the bound range, as Spark's JDBC partitioning does: when the
    column spans fewer distinct steps than ``num_partitions`` the stride would be 0 and all rows
    would land in the last partition. Never below 1."""
    span = get_internal_bound_value(upper_bound) - get_internal_bound_value(lower_bound)
    return max(1, min(int(num_partitions), span)) if span > 0 else 1
