"""Deterministic catalog of a synthetic property-management suite (~1,800 base tables).

Used to stand up a representative multi-tenant source database for ingestion / cost tests when no
real table inventory is available. Pure Python (no Spark, no database) so it is unit-testable.

Shape (approximating a large property-management ERP):
  * 18 modules x 100 tables = 1,800 tables, named ``<module>_<entity>`` plus numbered variants.
  * Archetype mix: ~15% empty, ~50% lookup/config, ~25% master/detail, ~9% transaction/history,
    ~1% very large. Row counts are defined at scale_factor = 1.0 and multiplied at build time.
  * 10–60 columns typical, a few wide masters with 100+ columns; ~15% composite primary keys.

Everything is derived from a seeded RNG, so the same seed always yields the same catalog.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

NUM_TABLES = 1800
TABLES_PER_MODULE = 100

MODULES: List[Tuple[str, List[str]]] = [
    ("prop", ["property", "building", "unit", "floor", "space", "amenity", "parking", "portfolio"]),
    ("cml", ["lease", "lease_term", "rent_step", "option", "clause", "guarantor", "sales_report"]),
    ("res", ["lease", "renewal", "occupant", "pet", "vehicle", "move_in", "move_out", "notice"]),
    ("ten", ["tenant", "resident", "contact", "address", "phone", "email", "note", "credit_check"]),
    ("ar", ["charge", "receipt", "ledger", "deposit", "nsf", "late_fee", "batch", "allocation"]),
    ("cam", ["pool", "expense", "recovery", "estimate", "reconciliation", "cap", "gross_up"]),
    ("gl", ["account", "journal", "journal_line", "period", "entity", "balance", "segment"]),
    ("bud", ["budget", "budget_line", "forecast", "version", "assumption", "variance"]),
    ("ap", ["vendor", "invoice", "invoice_line", "payment", "check", "1099", "approval"]),
    ("po", ["requisition", "purchase_order", "po_line", "receipt", "catalog_item", "contract"]),
    ("wo", ["work_order", "task", "technician", "schedule", "asset_link", "inspection"]),
    ("jc", ["job", "cost_code", "commitment", "draw", "change_order", "retention"]),
    ("fa", ["asset", "depreciation", "disposal", "category", "location", "valuation"]),
    ("lsg", ["prospect", "lead", "tour", "application", "quote", "campaign", "source"]),
    ("util", ["meter", "reading", "rate", "bill", "allocation", "submeter", "provider"]),
    ("ins", ["policy", "certificate", "claim", "coverage", "compliance_item", "expiry"]),
    ("doc", ["document", "attachment", "template", "signature", "folder", "version_log"]),
    ("sec", ["user", "role", "permission", "audit_log", "login_history", "session", "setting"]),
]

ARCHETYPE_SHARES = [               # (archetype, share of tables)
    ("empty", 0.15),
    ("lookup", 0.50),
    ("master", 0.25),
    ("transaction", 0.09),
    ("very_large", 0.01),
]

# Approximate stored bytes per value as GENERATED (SQL Server rowstore). String columns are filled
# to a fixed number of characters (STRING_FILL) — 2 bytes/char + 2 bytes variable-length overhead.
STRING_FILL = {"nvarchar(20)": 12, "nvarchar(50)": 20, "nvarchar(100)": 30, "nvarchar(255)": 40,
               "nvarchar(max)": 60}
TYPE_BYTES = {
    "int": 4, "bigint": 8, "bit": 1, "date": 3, "datetime2": 8,
    "decimal(18,2)": 9, "decimal(19,4)": 9, "uniqueidentifier": 16,
    **{t: 2 * n + 2 for t, n in STRING_FILL.items()},
}
ROW_OVERHEAD_BYTES = 12


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str


@dataclass(frozen=True)
class Table:
    name: str
    module: str
    archetype: str
    columns: Tuple[Column, ...]
    pk: Tuple[str, ...]
    base_rows: int                       # rows at scale_factor = 1.0
    seed: int = field(default=0)         # per-table value seed (deterministic data)

    @property
    def column_names(self) -> List[str]:
        return [c.name for c in self.columns]

    @property
    def est_bytes_per_row(self) -> int:
        return ROW_OVERHEAD_BYTES + sum(TYPE_BYTES[c.sql_type] for c in self.columns)

    def rows(self, scale_factor: float = 1.0) -> int:
        return int(round(self.base_rows * scale_factor))

    def est_mb(self, scale_factor: float = 1.0) -> float:
        return self.rows(scale_factor) * self.est_bytes_per_row / (1024 * 1024)

    def partition_col(self, scale_factor: float = 1.0, threshold_mb: float = 1024) -> Optional[str]:
        """Leading int/bigint PK column for tables large enough to warrant a partitioned seed.

        ``threshold_mb`` is the full-scale threshold; it is scaled with ``scale_factor`` so a scaled
        test database partitions the same tables (into the same number of partitions, when
        ``partition_size_mb`` is scaled the same way — see ``scaled_partition_size_mb``)."""
        if self.est_mb(scale_factor) < threshold_mb * scale_factor:
            return None
        lead = self.pk[0]
        lead_type = next(c.sql_type for c in self.columns if c.name == lead)
        return lead if lead_type in ("int", "bigint") else None


def scaled_partition_size_mb(scale_factor: float, partition_size_mb: int = 512) -> int:
    """Partition size for a scaled test DB, so partition COUNTS match the full-scale database."""
    return max(1, int(round(partition_size_mb * scale_factor)))


def _log_uniform(rng: random.Random, lo: float, hi: float) -> int:
    return int(round(math.exp(rng.uniform(math.log(lo), math.log(hi)))))


_ROW_RANGES = {
    "empty": (0, 0),
    "lookup": (10, 1_000),
    "master": (1_000, 100_000),
    "transaction": (100_000, 5_000_000),
    "very_large": (5_000_000, 30_000_000),
}

_COL_RANGES = {
    "empty": (10, 40),
    "lookup": (8, 20),
    "master": (20, 60),
    "transaction": (12, 30),
    "very_large": (12, 25),
}

_VALUE_TYPES = [                     # (type, weight) for non-key columns
    ("int", 14), ("bigint", 4), ("decimal(18,2)", 10), ("decimal(19,4)", 4),
    ("datetime2", 9), ("date", 6), ("nvarchar(20)", 10), ("nvarchar(50)", 14),
    ("nvarchar(100)", 8), ("nvarchar(255)", 4), ("bit", 8), ("uniqueidentifier", 3),
    ("nvarchar(max)", 1),
]


# Transaction / history tables are mostly keys, amounts, dates and flags with few strings.
_BIG_VALUE_TYPES = [
    ("int", 22), ("bigint", 8), ("decimal(18,2)", 18), ("decimal(19,4)", 6), ("datetime2", 12),
    ("date", 8), ("bit", 10), ("nvarchar(20)", 8), ("nvarchar(50)", 3), ("uniqueidentifier", 2),
]


def _pick_type(rng: random.Random, big: bool = False) -> str:
    """Weighted random value type; large (transaction) tables use a numeric-heavy mix."""
    types, weights = zip(*(_BIG_VALUE_TYPES if big else _VALUE_TYPES))
    return rng.choices(types, weights=weights, k=1)[0]


def _table_names() -> List[Tuple[str, str]]:
    names = []
    for prefix, entities in MODULES:
        base = [f"{prefix}_{e}" for e in entities]
        i = 0
        while len(base) < TABLES_PER_MODULE:
            e = entities[i % len(entities)]
            base.append(f"{prefix}_{e}_{len(base):03d}")
            i += 1
        names.extend((prefix, n) for n in base[:TABLES_PER_MODULE])
    return names


def build_catalog(seed: int = 42) -> List[Table]:
    """Return the deterministic 1,800-table catalog for ``seed``."""
    rng = random.Random(seed)
    names = _table_names()
    assert len(names) == NUM_TABLES

    archetypes: List[str] = []
    for arch, share in ARCHETYPE_SHARES:
        archetypes += [arch] * int(round(share * NUM_TABLES))
    archetypes = (archetypes + ["lookup"] * NUM_TABLES)[:NUM_TABLES]
    rng.shuffle(archetypes)

    wide_masters = set(rng.sample(
        [i for i, a in enumerate(archetypes) if a == "master"], k=12))
    composite = set(rng.sample(range(NUM_TABLES), k=int(round(0.15 * NUM_TABLES))))

    tables: List[Table] = []
    for i, ((module, name), arch) in enumerate(zip(names, archetypes)):
        lo, hi = _ROW_RANGES[arch]
        base_rows = 0 if hi == 0 else (rng.randint(lo, hi) if arch == "lookup" else _log_uniform(rng, lo, hi))
        clo, chi = _COL_RANGES[arch]
        ncols = rng.randint(100, 140) if i in wide_masters else rng.randint(clo, chi)

        big = arch in ("transaction", "very_large")
        if i in composite:
            pk_cols = [Column("parent_id", "bigint" if big else "int"), Column("seq", "int")]
        else:
            pk_cols = [Column("id", "bigint" if big else "int")]
        cols = list(pk_cols)
        for j in range(ncols - len(pk_cols)):
            cols.append(Column(f"c{j:03d}", _pick_type(rng, big)))
        tables.append(Table(
            name=name, module=module, archetype=arch, columns=tuple(cols),
            pk=tuple(c.name for c in pk_cols), base_rows=base_rows, seed=rng.randint(1, 10**9),
        ))
    return tables


def summarize(tables: List[Table], scale_factor: float = 1.0) -> Dict[str, dict]:
    """Counts, rows and estimated size per archetype (plus a ``total`` entry)."""
    out: Dict[str, dict] = {}
    for t in tables:
        s = out.setdefault(t.archetype, {"tables": 0, "rows": 0, "est_mb": 0.0})
        s["tables"] += 1
        s["rows"] += t.rows(scale_factor)
        s["est_mb"] += t.est_mb(scale_factor)
    out["total"] = {
        "tables": len(tables),
        "rows": sum(t.rows(scale_factor) for t in tables),
        "est_mb": sum(t.est_mb(scale_factor) for t in tables),
    }
    for v in out.values():
        v["est_mb"] = round(v["est_mb"], 1)
    return out
