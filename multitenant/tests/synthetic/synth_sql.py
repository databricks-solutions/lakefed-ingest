"""T-SQL builders for the synthetic property-management test database (see synth_catalog.py).

Pure string builders (no database access) — unit-tested in tests/synthetic_test.py. Data is generated
INSIDE SQL Server with set-based INSERT ... SELECT over a numbers source, so nothing is pushed over
the network row by row. Values are deterministic functions of the row number ``n`` and the table's
seed, so a rebuild yields the same data.
"""
from __future__ import annotations

from typing import List, Tuple

from synth_catalog import STRING_FILL, Column, Table

SCHEMA = "dbo"


def qname(table: Table) -> str:
    return f"[{SCHEMA}].[{table.name}]"


def create_table_sql(table: Table) -> str:
    """CREATE TABLE with a clustered primary key (PK columns NOT NULL, others nullable)."""
    pk = set(table.pk)
    cols = ",\n  ".join(
        f"[{c.name}] {c.sql_type} {'NOT NULL' if c.name in pk else 'NULL'}" for c in table.columns
    )
    pk_cols = ", ".join(f"[{c}]" for c in table.pk)
    return (f"CREATE TABLE {qname(table)} (\n  {cols},\n"
            f"  CONSTRAINT [PK_{table.name}] PRIMARY KEY CLUSTERED ({pk_cols})\n)")


def _value_expr(table: Table, col: Column, j: int) -> str:
    """Deterministic value for column ``col`` (position ``j``) as a function of n (0-based)."""
    a = 3 + (table.seed + 7 * j) % 89          # per-column multiplier / offset from the table seed
    b = (table.seed // 7 + 131 * j) % 100_003
    t = col.sql_type
    if t == "int":
        e = f"CAST((n * {a} + {b}) % 1000000 AS int)"
    elif t == "bigint":
        e = f"CAST(n * {a} + {b} AS bigint)"
    elif t.startswith("decimal("):
        e = f"CAST(((n * {a} + {b}) % 10000000) / 100.0 AS {t})"
    elif t == "datetime2":
        e = (f"DATEADD(SECOND, CAST((n * {a} + {b}) % 315360000 AS int), "
             "CAST('2015-01-01' AS datetime2))")
    elif t == "date":
        e = f"DATEADD(DAY, CAST((n * {a} + {b}) % 3650 AS int), CAST('2015-01-01' AS date))"
    elif t == "bit":
        e = f"CAST((n + {b}) % 2 AS bit)"
    elif t == "uniqueidentifier":
        e = f"CAST(HASHBYTES('MD5', CONCAT({table.seed}, ':', {j}, ':', n)) AS uniqueidentifier)"
    elif t in STRING_FILL:
        fill = STRING_FILL[t]
        letter = "abcdefghijklmnopqrstuvwxyz"[j % 26]
        e = f"LEFT(CONCAT(N'{letter}', (n * {a} + {b}) % 99991, REPLICATE(N'{letter}', {fill})), {fill})"
    else:
        raise ValueError(f"unsupported type {t}")
    # ~6% NULLs in nullable columns, for realism.
    return f"CASE WHEN (n + {j}) % 17 = 0 THEN NULL ELSE {e} END"


def _select_exprs(table: Table) -> List[str]:
    exprs = []
    composite = len(table.pk) > 1
    for j, c in enumerate(table.columns):
        if c.name == "id":
            exprs.append(f"CAST(n + 1 AS {c.sql_type})")
        elif composite and c.name == table.pk[0]:
            exprs.append(f"CAST(n / 10 + 1 AS {c.sql_type})")     # (parent_id, seq) unique per n
        elif composite and c.name == table.pk[1]:
            exprs.append(f"CAST(n % 10 + 1 AS {c.sql_type})")
        else:
            exprs.append(_value_expr(table, c, j))
    return exprs


def numbers_source(start: int, end: int, use_generate_series: bool = True) -> str:
    """Derived table yielding n = start..end (inclusive, 0-based)."""
    if use_generate_series:  # compatibility level 160+
        return (f"(SELECT value AS n FROM GENERATE_SERIES(CAST({start} AS bigint), "
                f"CAST({end} AS bigint))) AS s")
    cnt = end - start + 1
    return (f"(SELECT TOP ({cnt}) CAST({start} - 1 + ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) "
            "AS bigint) AS n FROM sys.all_columns a CROSS JOIN sys.all_columns b "
            "CROSS JOIN sys.all_columns c) AS s")


def insert_batch_sql(table: Table, start: int, end: int, use_generate_series: bool = True) -> str:
    cols = ", ".join(f"[{c}]" for c in table.column_names)
    exprs = ",\n  ".join(_select_exprs(table))
    return (f"INSERT INTO {qname(table)} WITH (TABLOCK) ({cols})\nSELECT\n  {exprs}\n"
            f"FROM {numbers_source(start, end, use_generate_series)}")


def batches(table: Table, scale_factor: float, batch_rows: int = 1_000_000) -> List[Tuple[int, int]]:
    """Inclusive (start, end) row-number ranges covering the table at ``scale_factor``."""
    n = table.rows(scale_factor)
    return [(s, min(s + batch_rows, n) - 1) for s in range(0, n, batch_rows)]


def enable_ct_database_sql(database: str, retention_days: int = 3) -> str:
    return (f"ALTER DATABASE [{database}] SET CHANGE_TRACKING = ON "
            f"(CHANGE_RETENTION = {retention_days} DAYS, AUTO_CLEANUP = ON)")


def enable_ct_table_sql(table: Table) -> str:
    return f"ALTER TABLE {qname(table)} ENABLE CHANGE_TRACKING WITH (TRACK_COLUMNS_UPDATED = OFF)"


ROW_COUNTS_SQL = (
    "SELECT t.name AS table_name, SUM(p.rows) AS row_count "
    "FROM sys.tables t JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) "
    "WHERE t.is_ms_shipped = 0 AND SCHEMA_NAME(t.schema_id) = 'dbo' GROUP BY t.name"
)

CT_TABLES_SQL = (
    "SELECT OBJECT_NAME(object_id) AS table_name FROM sys.change_tracking_tables"
)

DB_SIZE_MB_SQL = (
    "SELECT CAST(SUM(a.total_pages) * 8 / 1024.0 AS decimal(18,1)) AS size_mb "
    "FROM sys.allocation_units a"
)
