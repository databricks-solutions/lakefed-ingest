"""The discovery boundary of a simulated SQL Server instance.

discovery.py reads a source through ct_engine: ``_spark()`` and ``_read(spark, row, sql)`` return a frame whose
``collect()`` gives the rows. ``discovery_boundary`` swaps those two for a SimulatedServer, which answers the database
listing through master, the three catalog queries of each database, and the Azure Resource Manager list, from a
synthetic server (tests/synthetic/server_catalog.py). Nothing sleeps and nothing connects: each call is booked to the
database it reads (or to master), as a count of calls and rows, and the waits discovery takes between retries are booked
the same way. Latency is applied afterwards, so one run gives the wall time for any per-call latency: ``Latency`` turns
a database's ledger into seconds, and ``pool_makespan`` lays the databases out on a pool of workers in the order a
ThreadPoolExecutor takes them, which is how the discover job runs them.
"""
from __future__ import annotations

import contextlib
import heapq
import re
from collections import Counter
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Dict, Iterator, List, Optional, Tuple

import paths  # noqa: F401  (puts src/lakefed_ingest, tests/synthetic and app/src on sys.path)
import ct_engine
import discovery
import server_catalog

MASTER = "master"
LISTING_PREFIX = "SELECT name FROM sys.databases"
LIKE_PATTERN = re.compile(r"name LIKE N'([^']*)'")
CATALOG_QUERIES = (("tables", discovery.TABLES_SQL), ("columns", discovery.COLUMNS_SQL), ("pk", discovery.PK_SQL))
# The messages Spark raises for a JDBC read, as discovery_test.py has them: the classifier reads the code in brackets.
PAUSED_MESSAGE = ("[FAILED_JDBC.CONNECTION] Failed JDBC | caused by SQLServerException[code=40613,state=S0001]: "
                  "Database '{database}' on server '{server}' is not currently available.")
UNOPENABLE_MESSAGE = ('[FAILED_JDBC.CONNECTION] Failed JDBC | caused by SQLServerException[code=4060,state=S0001]: '
                      'Cannot open database "{database}" requested by the login. The login failed.')
# Discovery's retry waits use random jitter; the simulation takes the middle of the range, so a run is repeatable.
JITTER = 0.5


@dataclass(frozen=True)
class Latency:
    """Simulated seconds for one call to the server: a fixed round trip, plus a cost for each row it sends back. The
    design's 25 to 40 minutes for 22,000 databases at 32 at a time works out to 0.7 to 1.2 s a call, with no per-row
    cost (see docs/scale.md)."""
    per_call_s: float = 0.9
    per_row_s: float = 0.0

    def seconds(self, calls: int, rows: int, waits_s: float = 0.0) -> float:
        return self.per_call_s * calls + self.per_row_s * rows + waits_s


@dataclass
class Ledger:
    """What one database (or master) cost the server: calls, rows sent back, and waits taken before a retry."""
    calls: int = 0
    rows: int = 0
    waits_s: float = 0.0
    failures: int = 0

    def seconds(self, latency: Latency) -> float:
        return latency.seconds(self.calls, self.rows, self.waits_s)


class _Row:
    def __init__(self, values: dict):
        self._values = values

    def asDict(self) -> dict:
        return dict(self._values)


class _Frame:
    """The part of a Spark DataFrame discovery uses: collect()."""

    def __init__(self, rows: List[dict]):
        self._rows = rows

    def collect(self) -> List[_Row]:
        return [_Row(r) for r in self._rows]


def like_matches(pattern: str, name: str) -> bool:
    """SQL Server's LIKE for the patterns the harness uses: % any run of characters, _ any one character. Other
    characters match themselves, case-insensitively, as the server's default collation does."""
    regex = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.fullmatch(regex, name, re.IGNORECASE | re.DOTALL) is not None


def _kind(sql: str) -> str:
    if sql.startswith(LISTING_PREFIX):
        return "listing"
    for kind, text in CATALOG_QUERIES:
        if sql == text:
            return kind
    raise AssertionError(f"the simulated server doesn't answer this query: {sql[:120]}")


class SimulatedServer:
    """One synthetic SQL Server instance answering discovery's reads. ``ledger`` holds what each database cost, by name
    (master for the listing, and "azure" for the Azure list); a read of a database the catalog can't open is refused as
    SQL Server refuses it, and a paused database fails its first read with a transient error."""

    def __init__(self, catalog: server_catalog.ServerCatalog, server_name: str = "scale-sql"):
        self.catalog = catalog
        self.server_name = server_name
        self.ledger: Dict[str, Ledger] = {}
        self.calls_by_kind: Counter = Counter()
        self.rows_by_kind: Counter = Counter()
        self.waits_by_kind: Counter = Counter()
        self._resumed: set = set()

    def _book(self, database: str, kind: str, rows: int = 0, failed: bool = False) -> None:
        entry = self.ledger.setdefault(database, Ledger())
        entry.calls += 1
        entry.rows += rows
        entry.failures += int(failed)
        self.calls_by_kind[kind] += 1
        self.rows_by_kind[kind] += rows

    def book_wait(self, database: str, seconds: float) -> None:
        self.ledger.setdefault(database, Ledger()).waits_s += seconds
        self.waits_by_kind["retry"] += seconds

    def _index(self, database: str) -> Optional[int]:
        if not database[-5:].isdigit():
            return None
        index = int(database[-5:])
        return index if index < self.catalog.databases and server_catalog.database_name(index) == database else None

    def query(self, source_row: dict, sql: str) -> List[dict]:
        """One read of the server: the rows of a listing or of a catalog query, or the refusal a database gives."""
        database, kind = source_row["src_database"], _kind(sql)
        if kind == "listing":
            match = LIKE_PATTERN.search(sql)
            pattern = match.group(1) if match else "%"
            names = [server_catalog.database_name(i) for i in range(self.catalog.databases)]
            names = sorted(n for n in names if like_matches(pattern, n))
            self._book(MASTER, kind, rows=len(names))
            return [{"name": n} for n in names]
        index = self._index(database)
        if index is None:
            self._book(database, kind, failed=True)
            raise RuntimeError(UNOPENABLE_MESSAGE.format(database=database))
        if self.catalog.is_unopenable(index):
            self._book(database, kind, failed=True)
            raise RuntimeError(UNOPENABLE_MESSAGE.format(database=database))
        if self.catalog.is_paused(index) and database not in self._resumed:
            self._resumed.add(database)
            self._book(database, kind, failed=True)
            raise RuntimeError(PAUSED_MESSAGE.format(database=database, server=self.server_name))
        tables, columns, keys = self.catalog.catalog(index)
        rows = {"tables": tables, "columns": columns, "pk": keys}[kind]
        self._book(database, kind, rows=len(rows))
        return [dict(r) for r in rows]

    def arm_get(self, url: str, token: str) -> dict:
        """One page of the Azure Resource Manager list of the server's databases (the ``get`` discovery takes)."""
        match = re.search(r"[?&]page=(\d+)", url)
        page = int(match.group(1)) if match else 0
        body = self.catalog.arm_page(page, url.split("?")[0])
        self._book("azure", "arm", rows=len(body["value"]))
        return body

    def credential(self) -> SimpleNamespace:
        """A Unity Catalog service credential's provider: only get_token, which the listing calls."""
        return SimpleNamespace(get_token=lambda scope: SimpleNamespace(token="simulated-token"))


@contextlib.contextmanager
def discovery_boundary(server: SimulatedServer) -> Iterator[SimulatedServer]:
    """While the block runs, discovery's reads go to ``server``. Restores the real reads afterwards."""
    saved = (ct_engine._spark, ct_engine._read)
    ct_engine._spark = lambda: None
    ct_engine._read = lambda spark, source_row, sql, session_init=None: _Frame(server.query(source_row, sql))
    try:
        yield server
    finally:
        ct_engine._spark, ct_engine._read = saved


def connection_spec(connection: str = "lab_sqlserver_scale", pattern: str = "") -> dict:
    """A discovery spec that reads the server through a Unity Catalog connection (no secret), as a whole server does."""
    return {"src_type": "sqlserver", "src_connection": connection, "databases": "*", "pattern": pattern}


def list_through_master(spec: dict) -> List[str]:
    """The databases discovery lists through master (Test connection, or a whole server). Run it inside
    discovery_boundary."""
    return discovery.list_databases(spec)


def list_through_azure(server: SimulatedServer, spec: dict) -> List[str]:
    """The databases discovery lists through Azure Resource Manager, with the simulated credential and list."""
    spec = {**spec, "azure_credential": "lab-scale-credential",
            "azure_resource_id": "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/scale"
                                 f"/providers/Microsoft.Sql/servers/{server.server_name}"}
    return discovery.list_databases(spec, credential=server.credential(), get=server.arm_get)


def discover_one(server: SimulatedServer, spec: dict, database: str) -> List[dict]:
    """Discovery of one database, as the discover job runs it: the same three catalog queries and retries, with the
    waits booked to the database instead of slept."""
    source_row = discovery.cfg_for(spec, database)
    return discovery.discover_database(source_row, sleep=lambda seconds: server.book_wait(database, seconds),
                                       rand=lambda: JITTER)


def pool_makespan(durations: List[float], workers: int) -> float:
    """Wall time of tasks run on ``workers`` threads, each task taken by the worker free soonest, in the order given."""
    if not durations:
        return 0.0
    free = [0.0] * max(1, min(workers, len(durations)))
    heapq.heapify(free)
    end = 0.0
    for seconds in durations:
        start = heapq.heappop(free)
        finish = start + seconds
        end = max(end, finish)
        heapq.heappush(free, finish)
    return end
