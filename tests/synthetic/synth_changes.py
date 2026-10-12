"""Change generator + reconciliation builders for the synthetic test database (pure; no DB, no Spark).

* ``pick_tables`` — deterministic (seeded) choice of a fraction of the NON-EMPTY tables.
* ``change_statements`` — T-SQL for inserts (new PKs above the current max), updates (one non-PK
  column, the table's "check column") and deletes, as separate statements so each row count can be
  read back.
* ``source_check_sql`` / ``bronze_check_sql`` — the same aggregates computed on both sides (row count,
  sum of the leading PK column, and a type-appropriate aggregate of the check column), compared by
  ``compare_checks``. Cheap, deterministic, and sensitive to missed inserts, deletes and updates.
"""

from __future__ import annotations

import random
from decimal import Decimal
from typing import List, Optional, Tuple

from synth_catalog import STRING_FILL, Column, Table
from synth_sql import _select_exprs, qname


def pick_tables(tables: List[Table], scale_factor: float, fraction: float, seed: int) -> List[Table]:
    """A deterministic sample of ``fraction`` of the tables that have rows at ``scale_factor``."""
    non_empty = [t for t in tables if t.rows(scale_factor) > 0]
    k = min(len(non_empty), max(1, round(fraction * len(non_empty)))) if non_empty else 0
    return sorted(random.Random(seed).sample(non_empty, k), key=lambda t: t.name)


def check_column(table: Table) -> Optional[Column]:
    """The non-PK column the generator updates and the reconcile aggregates (first non-PK column)."""
    return next((c for c in table.columns if c.name not in table.pk), None)


def _update_expr(col: Column) -> str:
    c, t = f"[{col.name}]", col.sql_type
    if t in ("int", "bigint") or t.startswith("decimal("):
        return f"ISNULL({c}, 0) + 1"
    if t == "datetime2":
        return f"DATEADD(DAY, 1, ISNULL({c}, CAST('2015-01-01' AS datetime2)))"
    if t == "date":
        return f"DATEADD(DAY, 1, ISNULL({c}, CAST('2015-01-01' AS date)))"
    if t == "bit":
        return f"CAST(1 - ISNULL(CAST({c} AS int), 0) AS bit)"
    if t == "uniqueidentifier":
        return "NEWID()"
    if t in STRING_FILL:
        return f"LEFT(CONCAT(N'u', ISNULL({c}, N'')), {STRING_FILL[t]})"
    if t == "nvarchar(max)":
        return f"CONCAT(N'u', ISNULL({c}, N''))"
    raise ValueError(f"unsupported type {t}")


def change_statements(table: Table, inserts: int, updates: int, deletes: int,
                      use_generate_series: bool = True) -> List[Tuple[str, str]]:
    """[(kind, sql)] for one table. Inserts continue the PK sequence above the current maximum (the
    leading PK column), reusing the build's deterministic value expressions."""
    out = []
    q = qname(table)
    pk0 = table.pk[0]
    if inserts > 0:
        cols = ", ".join(f"[{c}]" for c in table.column_names)
        exprs = ",\n  ".join(_select_exprs(table))
        # n -> PK mapping matches the build: single PK `id` = n + 1; composite (parent, seq) =
        # (n / 10 + 1, n % 10 + 1). Start n past the current max so new keys never collide.
        start = (f"(SELECT ISNULL(MAX([{pk0}]), 0) FROM {q})" if len(table.pk) == 1
                 else f"(SELECT ISNULL(MAX([{pk0}]), 0) * 10 FROM {q})")
        series = (f"SELECT value + {start} AS n FROM GENERATE_SERIES(CAST(0 AS bigint), "
                  f"CAST({inserts - 1} AS bigint))" if use_generate_series else
                  f"SELECT TOP ({inserts}) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 + {start} AS n "
                  "FROM sys.all_columns")
        out.append(("insert", f"INSERT INTO {q} ({cols})\nSELECT\n  {exprs}\nFROM ({series}) AS s"))
    col = check_column(table)
    if updates > 0 and col is not None:
        out.append(("update", f"UPDATE TOP ({updates}) {q} SET [{col.name}] = {_update_expr(col)}"))
    if deletes > 0:
        out.append(("delete", f"DELETE TOP ({deletes}) FROM {q}"))
    return out


def _check_agg(col: Optional[Column], dialect: str) -> str:
    """Type-appropriate aggregate of the check column, identical on SQL Server and Spark."""
    if col is None:
        return "CAST(0 AS BIGINT)" if dialect == "spark" else "CAST(0 AS bigint)"
    t = col.sql_type
    if dialect == "tsql":
        c = f"[{col.name}]"
        if t in ("int", "bigint") or t.startswith("decimal("):
            return f"CAST(SUM(CAST({c} AS decimal(38, 4))) AS decimal(38, 4))"
        if t in ("datetime2", "date"):
            return f"SUM(CAST(DATEDIFF(DAY, CAST('2015-01-01' AS date), {c}) AS bigint))"
        if t == "bit":
            return f"SUM(CAST({c} AS bigint))"
        if t == "uniqueidentifier":
            return f"COUNT_BIG({c})"
        return f"SUM(CAST(LEN({c}) AS bigint))"
    c = f"`{col.name}`"
    if t in ("int", "bigint") or t.startswith("decimal("):
        return f"CAST(SUM(CAST({c} AS DECIMAL(38, 4))) AS DECIMAL(38, 4))"
    if t in ("datetime2", "date"):
        return f"SUM(CAST(DATEDIFF(CAST({c} AS DATE), DATE'2015-01-01') AS BIGINT))"
    if t == "bit":
        return f"SUM(CAST({c} AS BIGINT))"
    if t == "uniqueidentifier":
        return f"COUNT({c})"
    return f"SUM(CAST(LENGTH({c}) AS BIGINT))"


def source_check_sql(table: Table) -> str:
    return (f"SELECT COUNT_BIG(*) AS n, SUM(CAST([{table.pk[0]}] AS bigint)) AS pk_sum, "
            f"{_check_agg(check_column(table), 'tsql')} AS col_agg FROM {qname(table)}")


def bronze_check_sql(table: Table, sink_fqn: str) -> str:
    return (f"SELECT COUNT(*) AS n, SUM(CAST(`{table.pk[0]}` AS BIGINT)) AS pk_sum, "
            f"{_check_agg(check_column(table), 'spark')} AS col_agg FROM {sink_fqn}")


def _norm(v):
    if v is None:
        return None
    try:
        return Decimal(str(v)).quantize(Decimal("0.0001"))
    except Exception:
        return str(v)


def compare_checks(src: dict, bronze: dict) -> List[str]:
    """Names of the aggregates that differ (empty list = reconciled)."""
    return [k for k in ("n", "pk_sum", "col_agg") if _norm(src.get(k)) != _norm(bronze.get(k))]
