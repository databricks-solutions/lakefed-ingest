"""Table discovery for bulk onboarding: list a source database's tables and recommend control-row
settings for each (select_list, primary key, load type, cursor column, partition candidate).

Run by the ``lakefed_ingest_discover`` job (``discover_tables.ipynb``) on the same compute and
network path as ingestion; results land in Lakebase (``discovery``, ``discovered_table``), where
the operations app shows them for review before creating control rows. SQL Server only for now;
other adapters are placeholders.

Recommendations (the reviewer can change any of them):
  * load_type ``ct`` when Change Tracking is enabled on the table and it has a primary key;
    ``cursor`` on its rowversion column when it has a PK and one; otherwise ``full``
    (identity / updated_at cursors miss updates or depend on the application, so they're never
    picked automatically).
  * ``select_list`` = every column whose type Spark JDBC reads (CLR types such as geography /
    geometry / hierarchyid, and sql_variant, are excluded and listed in ``excluded_columns``).
  * ``partition_candidate`` = the first PK column if it's an integer / date / datetime type, else
    an integer identity column. The app applies the size threshold (``load_partitioned``) at review.

Catalog views only (sys.tables / columns / indexes / partitions / allocation_units /
change_tracking_tables): no data is read and no permission beyond metadata visibility is needed.
Pure Python except ``discover_database`` (unit-tested in tests/discovery_test.py).
"""

from __future__ import annotations

from typing import Dict, List, Optional

import load_strategies as ls
import source_adapters as sa

TABLES_SQL = (
    "SELECT t.object_id AS oid, s.name AS sch, t.name AS tbl, "
    "CAST(ISNULL((SELECT SUM(p.rows) FROM sys.partitions p "
    "  WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)), 0) AS BIGINT) AS row_count, "
    "CAST(ISNULL((SELECT SUM(a.total_pages) * 8 / 1024.0 FROM sys.partitions p "
    "  JOIN sys.allocation_units a ON a.container_id = p.partition_id "
    "  WHERE p.object_id = t.object_id), 0) AS DECIMAL(18, 2)) AS size_mb, "
    "CAST(CASE WHEN ct.object_id IS NULL THEN 0 ELSE 1 END AS INT) AS ct_enabled "
    "FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
    "LEFT JOIN sys.change_tracking_tables ct ON ct.object_id = t.object_id "
    "WHERE t.is_ms_shipped = 0"
)

COLUMNS_SQL = (
    "SELECT c.object_id AS oid, c.column_id AS pos, c.name AS col, "
    "TYPE_NAME(c.system_type_id) AS base_type, TYPE_NAME(c.user_type_id) AS user_type, "
    "CAST(c.is_identity AS INT) AS is_identity "
    "FROM sys.columns c JOIN sys.tables t ON t.object_id = c.object_id WHERE t.is_ms_shipped = 0"
)

PK_SQL = (
    "SELECT ic.object_id AS oid, ic.key_ordinal AS ord, c.name AS col "
    "FROM sys.indexes i "
    "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
    "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
    "JOIN sys.tables t ON t.object_id = i.object_id "
    "WHERE i.is_primary_key = 1 AND t.is_ms_shipped = 0"
)

UNSUPPORTED_TYPES = {"geography", "geometry", "hierarchyid", "sql_variant"}
PARTITION_TYPES = {"tinyint", "smallint", "int", "bigint", "decimal", "numeric",
                   "date", "datetime", "datetime2", "smalldatetime"}
ROWVERSION_TYPES = {"timestamp", "rowversion"}


def recommend(table: dict, columns: List[dict], pk: List[str]) -> dict:
    """Control-row settings for one discovered table.

    table: {sch, tbl, row_count, size_mb, ct_enabled}; columns: [{pos, col, base_type, user_type,
    is_identity}] in any order; pk: PK column names in key order."""
    cols = sorted(columns, key=lambda c: int(c["pos"]))
    keep, excluded, notes = [], [], []
    for c in cols:
        types = {str(c.get("base_type") or "").lower(), str(c.get("user_type") or "").lower()}
        if types & UNSUPPORTED_TYPES or "," in str(c["col"]):
            excluded.append(c["col"])
        else:
            keep.append(c)
    if excluded:
        notes.append(f"excluded unsupported columns: {', '.join(excluded)}")
    by_name = {c["col"]: c for c in cols}
    rowversion = next((c["col"] for c in keep if str(c["base_type"]).lower() in ROWVERSION_TYPES), None)
    identity = next((c["col"] for c in keep if int(c.get("is_identity") or 0)), None)
    pk = [p for p in pk if p in by_name]
    if pk and any(p in excluded for p in pk):
        notes.append("primary key uses an unsupported column type")
        pk = []

    if bool(int(table.get("ct_enabled") or 0)) and pk:
        load_type, cursor = ls.CT, None
    elif pk and rowversion:
        load_type, cursor = ls.CURSOR, rowversion
        notes.append("no Change Tracking: cursor on rowversion (deletes not captured)")
    else:
        load_type, cursor = ls.FULL, None
        notes.append("no Change Tracking and no primary key + rowversion: full reload"
                     if not pk else "no Change Tracking or rowversion column: full reload")

    candidate = None
    if pk and str(by_name[pk[0]]["base_type"]).lower() in PARTITION_TYPES:
        candidate = pk[0]
    elif identity and str(by_name[identity]["base_type"]).lower() in PARTITION_TYPES:
        candidate = identity

    return {
        "select_list": ", ".join(c["col"] for c in keep),
        "excluded_columns": ", ".join(excluded) or None,
        "primary_key": ", ".join(pk) or None,
        "rowversion_column": rowversion,
        "identity_column": identity,
        "load_type": load_type,
        "cursor_column": cursor,
        "partition_candidate": candidate,
        "notes": "; ".join(notes) or None,
    }


def assemble(tables, columns, pks) -> List[dict]:
    """Join the three catalog query results (rows as dicts) into one recommendation per table."""
    cols_by: Dict[int, list] = {}
    for c in columns:
        cols_by.setdefault(int(c["oid"]), []).append(c)
    pk_by: Dict[int, list] = {}
    for p in sorted(pks, key=lambda r: (int(r["oid"]), int(r["ord"]))):
        pk_by.setdefault(int(p["oid"]), []).append(p["col"])
    out = []
    for t in tables:
        oid = int(t["oid"])
        rec = recommend(t, cols_by.get(oid, []), pk_by.get(oid, []))
        out.append({"src_schema": t["sch"], "src_table": t["tbl"], "row_count": int(t["row_count"] or 0),
                    "size_mb": float(t["size_mb"] or 0), "ct_enabled": bool(int(t["ct_enabled"] or 0)), **rec})
    return sorted(out, key=lambda r: (r["src_schema"].lower(), r["src_table"].lower()))


CONNECT_RETRIES, CONNECT_RETRY_S = 3, 20


def _is_connect_failure(exc: BaseException) -> bool:
    text = str(exc)
    return "FAILED_JDBC.CONNECTION" in text or "Failed to connect" in text


def discover_database(cfg: dict, slots=None, sleep=None) -> List[dict]:
    """Discover every user table of one source database (three catalog queries). A failed first
    connection is retried a few times: serverless sources (e.g. Azure SQL serverless) may be
    auto-paused and refuse connections while they resume."""
    import time
    import ct_engine
    if sa.get_adapter(cfg.get("src_type")).src_type != sa.SQLSERVER:
        raise ValueError(f"discovery is not implemented yet for src_type '{cfg.get('src_type')}'")
    sleep = sleep or time.sleep
    spark = ct_engine._spark()
    rows = []
    for sql in (TABLES_SQL, COLUMNS_SQL, PK_SQL):
        for attempt in range(CONNECT_RETRIES):
            try:
                with ct_engine._unit(slots):
                    rows.append([r.asDict() for r in ct_engine._read(spark, cfg, sql).collect()])
                break
            except Exception as e:
                if not _is_connect_failure(e) or attempt == CONNECT_RETRIES - 1:
                    raise
                sleep(CONNECT_RETRY_S * (attempt + 1))
    return assemble(*rows)


def secret_key_for(spec: dict, database: str) -> str:
    """The secret key for a database: ``secret_key_template`` with {database} filled in, else
    ``secret_key``."""
    tpl = spec.get("secret_key_template")
    return tpl.replace("{database}", database) if tpl else spec["secret_key"]


def parse_databases(text: Optional[str]) -> List[str]:
    """Database names from a comma- / newline-separated list (order kept, duplicates dropped)."""
    seen, out = set(), []
    for part in str(text or "").replace("\n", ",").split(","):
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out
