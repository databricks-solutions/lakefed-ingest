"""Cursor (high-watermark) strategy, SQL Server: incremental reads on a monotonically increasing
column (``control.cursor_column``), MERGEd into the Delta sink by PK.

Per cycle, for a seeded table (checkpoint phase ``incremental``):
  1. ``hi`` = the column's current high watermark (one small source query);
  2. ``hi`` == checkpoint position -> unchanged (no read, no MERGE);
  3. otherwise read ``cursor_column > position AND cursor_column <= hi`` (ONE source read) and
     upsert-MERGE by PK; the checkpoint advances to ``hi`` only after the MERGE commits
     (at-least-once — re-reading a window is harmless, the MERGE is idempotent).
The seed captures ``hi`` BEFORE the full copy, so rows written during the seed are re-read by the
first increment (no gap).

Supported column types (SQL Server system type of ``cursor_column``):
  * integers / exact numerics: tinyint, smallint, int, bigint, decimal, numeric (identity keys);
  * temporal: date, smalldatetime, datetime, datetime2, datetimeoffset (an application-maintained
    ``updated_at``);
  * ``rowversion`` (``timestamp``): recommended — SQL Server bumps it on every insert AND update,
    and ``hi`` is capped below ``MIN_ACTIVE_ROWVERSION()`` so rows from still-open transactions are
    never skipped.
Float / real / string columns are rejected (lossy or not reliably ordered).

Limits (by design of a watermark): deletes are not captured (use ``ct``, or a periodic ``full``);
updates are captured only if they move the cursor column (``rowversion`` always does, an identity
never does); for non-rowversion columns, a transaction that commits a value BELOW an already-read
``hi`` after the read is missed — the reason ``rowversion`` is recommended.

Position (``checkpoint.position``, text) is JSON ``{"col", "type", "value"}``: the column and its
type are recorded so a changed ``cursor_column`` re-seeds and every later cycle needs no type
lookup. ``value`` is the SQL Server string form (ISO 8601 for temporal, decimal text otherwise).

Pure builders are unit-tested in tests/cursor_engine_test.py; source access reuses ``ct_engine``'s
JDBC helpers (one slot per query unit, lazy Spark imports).
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Optional, Tuple

import ct_engine
import source_adapters

NUMERIC_TYPES = {"tinyint", "smallint", "int", "bigint", "decimal", "numeric"}
TEMPORAL_TYPES = {"date", "smalldatetime", "datetime", "datetime2", "datetimeoffset"}
ROWVERSION_TYPES = {"timestamp", "rowversion"}  # TYPE_NAME(system_type_id) reports 'timestamp'
SUPPORTED_TYPES = NUMERIC_TYPES | TEMPORAL_TYPES | ROWVERSION_TYPES

_NUMERIC_VALUE = re.compile(r"^-?\d+(\.\d+)?$")
_TEMPORAL_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d{1,7})?)?)?([+-]\d{2}:\d{2}|Z)?$")
_INTEGER_VALUE = re.compile(r"^-?\d+$")


def _kind(typ: str) -> str:
    t = (typ or "").strip().lower()
    if t in NUMERIC_TYPES:
        return "numeric"
    if t in TEMPORAL_TYPES:
        return "temporal"
    if t in ROWVERSION_TYPES:
        return "rowversion"
    raise ValueError(f"cursor_column type '{typ}' is not supported for load_type 'cursor' "
                     f"(supported: {', '.join(sorted(SUPPORTED_TYPES - {'rowversion'}))} / rowversion)")


# ---- position --------------------------------------------------------------------------------------

def encode_position(col: str, typ: str, value: Optional[str]) -> str:
    return json.dumps({"col": col, "type": typ, "value": value}, separators=(",", ":"))


def decode_position(text: Optional[str]) -> Optional[dict]:
    """{"col", "type", "value"} or None (no / unreadable position -> the planner re-seeds)."""
    if not text:
        return None
    try:
        d = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or not d.get("col") or not d.get("type"):
        return None
    return {"col": d["col"], "type": d["type"], "value": d.get("value")}


# ---- SQL builders (pure) ---------------------------------------------------------------------------

def _obj(cfg: dict) -> str:
    return ct_engine.quote_nstr(f"{ct_engine.quote_ident(cfg['src_schema'])}.{ct_engine.quote_ident(cfg['src_table'])}")


def column_type_sql(cfg: dict) -> str:
    """System type name of ``cursor_column`` (no row when the column doesn't exist)."""
    return ("SELECT TYPE_NAME(c.system_type_id) AS typ FROM sys.columns c "
            f"WHERE c.object_id = OBJECT_ID({_obj(cfg)}) AND c.name = {ct_engine.quote_nstr(cfg['cursor_column'])}")


def high_watermark_sql(cfg: dict, typ: str) -> str:
    """One row ``hi``: the cursor column's high watermark as SQL Server text (NULL on an empty table).
    rowversion is capped below MIN_ACTIVE_ROWVERSION() so open transactions are never skipped."""
    col = ct_engine.quote_ident(cfg["cursor_column"])
    kind = _kind(typ)
    if kind == "numeric":
        expr = f"CONVERT(VARCHAR(64), MAX({col}))"
    elif kind == "temporal":
        expr = f"CONVERT(VARCHAR(64), MAX({col}), 126)"
    else:
        mx, mar = f"CAST(MAX({col}) AS BIGINT)", "CAST(MIN_ACTIVE_ROWVERSION() AS BIGINT)"
        expr = f"CONVERT(VARCHAR(20), CASE WHEN {mx} < {mar} THEN {mx} ELSE {mar} - 1 END)"
    table = ct_engine.quote_ident(cfg["src_schema"]) + "." + ct_engine.quote_ident(cfg["src_table"])
    return f"SELECT {expr} AS hi FROM {table}"


def literal(value: str, typ: str) -> str:
    """A typed SQL Server literal for a position value (validated — values come from our own
    checkpoint, but are never spliced unchecked)."""
    kind = _kind(typ)
    v = str(value).strip()
    if kind == "numeric":
        if not _NUMERIC_VALUE.match(v):
            raise ValueError(f"invalid numeric cursor position {value!r}")
        return v
    if kind == "temporal":
        if not _TEMPORAL_VALUE.match(v):
            raise ValueError(f"invalid temporal cursor position {value!r}")
        return f"CONVERT({typ.strip().lower()}, {ct_engine.quote_nstr(v)}, 126)"
    if not _INTEGER_VALUE.match(v):
        raise ValueError(f"invalid rowversion cursor position {value!r}")
    return f"CAST(CAST({v} AS BIGINT) AS BINARY(8))"


def read_where(cfg: dict, typ: str, lo: Optional[str], hi: str) -> str:
    """``cursor_column > lo AND cursor_column <= hi`` (no lower bound when ``lo`` is None — a table
    that was empty at seed time). Sargable: the literal is cast to the column's type, not vice versa."""
    col = ct_engine.quote_ident(cfg["cursor_column"])
    upper = f"{col} <= {literal(hi, typ)}"
    return upper if lo is None else f"{col} > {literal(lo, typ)} AND {upper}"


def upsert_merge_sql(cfg: dict, sink_fqn: str, view: str) -> str:
    """Idempotent upsert MERGE by PK — (__client_id__, PK) for a consolidated sink — with schema
    evolution. A watermark read carries no deletes."""
    sl, keys = ", ".join(ct_engine.write_columns(cfg)), ", ".join(ct_engine.merge_keys(cfg))
    return (
        f"MERGE WITH SCHEMA EVOLUTION INTO {sink_fqn} AS tgt USING {view} AS s "
        f"ON {ct_engine.merge_on_cfg(cfg)} "
        f"WHEN MATCHED THEN UPDATE SET {ct_engine.merge_set(sl, keys)} "
        f"WHEN NOT MATCHED THEN INSERT ({ct_engine.merge_insert_cols(sl)}) "
        f"VALUES ({ct_engine.merge_insert_vals(sl)})"
    )


# ---- source access ---------------------------------------------------------------------------------

def column_type(cfg: dict, slots=None) -> str:
    """The cursor column's SQL Server type; raises a clear error if missing or unsupported."""
    with ct_engine._unit(slots):
        row = ct_engine._read(ct_engine._spark(), cfg, column_type_sql(cfg)).first()
    if row is None or row["typ"] is None:
        raise ValueError(f"cursor_column '{cfg['cursor_column']}' not found on "
                         f"{cfg['src_schema']}.{cfg['src_table']}")
    typ = str(row["typ"]).lower()
    _kind(typ)
    return typ


def high_watermark(cfg: dict, typ: str, slots=None) -> Optional[str]:
    with ct_engine._unit(slots):
        row = ct_engine._read(ct_engine._spark(), cfg, high_watermark_sql(cfg, typ)).first()
    return None if row is None or row["hi"] is None else str(row["hi"])


def read_and_merge(spark, cfg: dict, sink_fqn: str, typ: str, lo: Optional[str], hi: str,
                   slots=None) -> Optional[int]:
    """Read (lo, hi] and upsert-MERGE it into the sink — ONE source read feeding the MERGE (no
    ``.cache()``/``count()``). Returns rows affected from the MERGE metrics (None if unavailable)."""
    query = source_adapters.for_cfg(cfg).select_sql(cfg, where=read_where(cfg, typ, lo, hi))
    def once():
        with ct_engine._unit(slots):
            df = ct_engine._tag(ct_engine._read(spark, cfg, query), cfg)
            view = f"cursor_staged_{uuid.uuid4().hex}"
            df.createOrReplaceTempView(view)
            try:
                return spark.sql(upsert_merge_sql(cfg, sink_fqn, view)).first()
            finally:
                spark.catalog.dropTempView(view)
    result = ct_engine.with_write_retry(once, what=f"merge {sink_fqn}")
    if result is not None and "num_affected_rows" in result.asDict():
        return int(result["num_affected_rows"] or 0)
    return None


def plan_increment(position: dict, hi: Optional[str]) -> Tuple[str, Optional[str]]:
    """('unchanged' | 'read', new position value). An empty source (hi NULL) or hi equal to the
    checkpoint means nothing to read; the position is kept (never moved to NULL)."""
    if hi is None or hi == position.get("value"):
        return "unchanged", position.get("value")
    return "read", hi
