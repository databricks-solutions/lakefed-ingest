"""A synthetic SQL Server instance with many databases, for the scale runs in tests/scale/.

One server holds ``databases`` user databases of ``tables_per_database`` tables each: 22,000 x 240 is the large-estate
case, about 5.3 million tables. Nothing is stored. A database's catalog is derived from the seed and the database's
index when it is asked for, so the whole server costs no memory until a database is read. The rows match what the
catalog queries of discovery.py return (its TABLES_SQL, COLUMNS_SQL and PK_SQL), the database listing through master,
and the Azure Resource Manager list of an Azure SQL server's databases.

Shape:
  * Names: lfc_eng_NNNNN (60%), lfc_test_NNNNN (20%), lfc_heavy_NNNNN (10%) and ops_NNNNN (10%), so a name filter such as
    lfc_eng_% or lfc_test% selects a real share of the server.
  * Tables: about 85% have Change Tracking and a primary key (load type ct), 6% have a primary key and a rowversion
    column but no Change Tracking (cursor), and 9% have no primary key (full reload). About 2% of tables have a
    geography column, which discovery leaves out.
  * A few databases can't be opened (every read is refused), and a few are paused (the first read fails with a
    transient error). Which ones is a function of the seed.

Pure Python: no Spark and no database.
"""
from __future__ import annotations

import functools
import math
import random
from typing import Dict, List, Tuple

DEFAULT_DATABASES = 22_000
DEFAULT_TABLES_PER_DATABASE = 240
MAX_DATABASES = 100_000             # the names carry five digits
UNOPENABLE_DATABASES = 3
PAUSED_DATABASES = 2
CT_SHARE = 0.85
CURSOR_SHARE = 0.06
GEOGRAPHY_SHARE = 0.02
MAX_ROW_COUNT = 5_000_000
EMPTY_SHARE = 0.10
ARM_PAGE_SIZE = 100
ARM_BASE = "https://management.azure.com"

# The name prefix of database i is FAMILY_BY_DIGIT[i % 10], so the shares are exact (6, 2, 1 and 1 in ten).
FAMILY_BY_DIGIT = ("lfc_eng_",) * 6 + ("lfc_test_",) * 2 + ("lfc_heavy_",) + ("ops_",)
SCHEMAS = ("dbo", "dbo", "dbo", "sales", "hr", "fin")
ENTITIES = ("customer", "order", "invoice", "payment", "product", "stock", "ledger", "account", "contact",
            "shipment", "contract", "asset", "employee", "claim", "policy", "ticket")
VALUE_TYPES = (("int", 14), ("bigint", 3), ("decimal", 8), ("datetime2", 8), ("date", 4), ("nvarchar", 16),
               ("bit", 6), ("uniqueidentifier", 2))
MIN_COLUMNS, MAX_COLUMNS = 6, 30
AVERAGE_BYTES_PER_ROW = 120

Row = Dict[str, object]


def database_name(index: int) -> str:
    """The name of database ``index`` (0-based): its family's prefix and the index in five digits."""
    return f"{FAMILY_BY_DIGIT[index % 10]}{index:05d}"


def database_index(name: str) -> int:
    """The index a name from database_name() carries."""
    return int(name[-5:])


def _table_rows(rng: random.Random, table_index: int, oid_base: int) -> Tuple[Row, List[Row], List[Row]]:
    """One table's catalog rows: (its sys.tables row, its columns, its primary key columns)."""
    oid = oid_base + table_index + 1
    draw = rng.random()
    if draw < CT_SHARE:
        load_type = "ct"
    elif draw < CT_SHARE + CURSOR_SHARE:
        load_type = "cursor"
    else:
        load_type = "full"
    composite = load_type != "full" and rng.random() < 0.15
    has_key = load_type != "full"
    row_count = 0 if rng.random() < EMPTY_SHARE else int(round(math.exp(rng.uniform(0.0, math.log(MAX_ROW_COUNT)))))
    tables = {"oid": oid, "sch": rng.choice(SCHEMAS), "tbl": f"{ENTITIES[table_index % len(ENTITIES)]}_{table_index:03d}",
              "row_count": row_count, "size_mb": round(row_count * AVERAGE_BYTES_PER_ROW / (1024 * 1024), 2),
              "ct_enabled": 1 if load_type == "ct" else 0}
    columns: List[Row] = []
    keys: List[Row] = []
    key_names = ("parent_id", "seq") if composite else ("id",)
    if has_key:
        for ordinal, name in enumerate(key_names, start=1):
            base = "int" if name != "parent_id" else "bigint"
            columns.append({"oid": oid, "pos": len(columns) + 1, "col": name, "base_type": base, "user_type": base,
                            "is_identity": 1 if name == "id" else 0, "is_nullable": 0})
            keys.append({"oid": oid, "ord": ordinal, "col": name})
    if load_type == "cursor":
        columns.append({"oid": oid, "pos": len(columns) + 1, "col": "rv", "base_type": "timestamp",
                        "user_type": "timestamp", "is_identity": 0, "is_nullable": 0})
    value_count = rng.randint(MIN_COLUMNS, MAX_COLUMNS) - len(columns)
    types, weights = zip(*VALUE_TYPES)
    for j in range(max(value_count, 0)):
        base = rng.choices(types, weights=weights, k=1)[0]
        columns.append({"oid": oid, "pos": len(columns) + 1, "col": f"c{j:02d}", "base_type": base,
                        "user_type": base, "is_identity": 0, "is_nullable": rng.randint(0, 1)})
    if rng.random() < GEOGRAPHY_SHARE:
        columns.append({"oid": oid, "pos": len(columns) + 1, "col": "geo", "base_type": "geography",
                        "user_type": "geography", "is_identity": 0, "is_nullable": 1})
    return tables, columns, keys


@functools.lru_cache(maxsize=16)
def _catalog_of(seed: int, database_index_: int, tables_per_database: int):
    """(tables, columns, keys) of one database, as tuples of rows. Cached, so a database read by several queries (or
    by a retry) is derived once."""
    rng = random.Random(f"{seed}/database/{database_index_}")
    oid_base = database_index_ * tables_per_database
    tables, columns, keys = [], [], []
    for table_index in range(tables_per_database):
        table_row, table_columns, table_keys = _table_rows(rng, table_index, oid_base)
        tables.append(table_row)
        columns.extend(table_columns)
        keys.extend(table_keys)
    return tuple(tables), tuple(columns), tuple(keys)


class ServerCatalog:
    """One SQL Server instance: ``databases`` databases of ``tables_per_database`` tables, from ``seed``."""

    def __init__(self, databases: int = DEFAULT_DATABASES, tables_per_database: int = DEFAULT_TABLES_PER_DATABASE,
                 seed: int = 42):
        if not 1 <= databases <= MAX_DATABASES:
            raise ValueError(f"databases must be from 1 to {MAX_DATABASES:,}")
        self.databases = databases
        self.tables_per_database = tables_per_database
        self.seed = seed
        rng = random.Random(f"{seed}/server")
        special = rng.sample(range(databases), min(UNOPENABLE_DATABASES + PAUSED_DATABASES, databases))
        self._unopenable = frozenset(special[:UNOPENABLE_DATABASES])
        self._paused = frozenset(special[UNOPENABLE_DATABASES:])

    @property
    def total_tables(self) -> int:
        return self.databases * self.tables_per_database

    def database_names(self) -> List[str]:
        """Every database's name, in the order SQL Server lists them (by name)."""
        return sorted(database_name(i) for i in range(self.databases))

    def is_unopenable(self, index: int) -> bool:
        return index in self._unopenable

    def is_paused(self, index: int) -> bool:
        return index in self._paused

    def unopenable_names(self) -> List[str]:
        return sorted(database_name(i) for i in self._unopenable)

    def paused_names(self) -> List[str]:
        return sorted(database_name(i) for i in self._paused)

    def catalog(self, index: int):
        """(tables, columns, keys) of database ``index``: the rows of TABLES_SQL, COLUMNS_SQL and PK_SQL."""
        if not 0 <= index < self.databases:
            raise KeyError(f"no database {index}")
        return _catalog_of(self.seed, index, self.tables_per_database)

    def arm_page(self, page_index: int, base_url: str, page_size: int = ARM_PAGE_SIZE) -> Dict[str, object]:
        """One page of the Azure Resource Manager list of the server's databases (the value and nextLink shape). A
        paused database lists as Paused, which the listing keeps; the others list as Online."""
        start = page_index * page_size
        value = []
        for index in range(start, min(start + page_size, self.databases)):
            status = "Paused" if self.is_paused(index) else "Online"
            value.append({"name": database_name(index), "properties": {"status": status}})
        page: Dict[str, object] = {"value": value}
        if start + page_size < self.databases:
            page["nextLink"] = f"{base_url}?page={page_index + 1}"
        return page

    def arm_page_count(self, page_size: int = ARM_PAGE_SIZE) -> int:
        return max(1, math.ceil(self.databases / page_size))
