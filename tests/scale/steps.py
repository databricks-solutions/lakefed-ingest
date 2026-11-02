"""The scale steps. Each one runs the code the app or the discover job runs, against the fakes in simulation.py and
lakebase_fake.py, and reports what it sent (calls, rows, statements) and how long the Python took.
Server times come from the ledger (see simulation.py): they are simulated from the latency given, not measured.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import discovery
import paths  # noqa: F401  (sys.path)
import lakebase_fake
import server_catalog
import simulation as sim

# discover_tables.ipynb ran 8 databases at once before the discovery fix (min(8, databases)), and runs
# discovery.DEFAULT_WORKERS now (32, the design's cap). Both are kept, so one run shows the before and the after.
PREVIOUS_WORKERS = 8
NOTEBOOK_WORKERS = discovery.DEFAULT_WORKERS

FoundCallback = Callable[[str, List[dict]], None]
RefusalCallback = Callable[[str, str], None]


@dataclass
class DiscoveryRun:
    """One discover run over a server: what the listing and each database cost, and what discovery found."""
    listed: List[str]
    refused: List[str]
    retried: List[str]
    discovered: int
    tables: int
    ledger: Dict[str, sim.Ledger]
    calls_by_kind: Counter
    rows_by_kind: Counter
    retry_waits_s: float
    cpu_s: float
    azure: bool = False
    order: List[str] = field(default_factory=list)

    def calls(self) -> int:
        return sum(self.calls_by_kind.values())

    def wall_s(self, latency: sim.Latency, workers: int) -> float:
        """Simulated wall time: the listing (one call, or the Azure pages in order), then the databases on a pool."""
        listing_key = "azure" if self.azure else sim.MASTER
        listing = self.ledger[listing_key].seconds(latency) if listing_key in self.ledger else 0.0
        return listing + sim.pool_makespan([self.ledger[name].seconds(latency) for name in self.order], workers)


def discover(catalog: server_catalog.ServerCatalog, pattern: str = "", via_azure: bool = False,
             limit: Optional[int] = None, on_found: Optional[FoundCallback] = None,
             on_refused: Optional[RefusalCallback] = None) -> DiscoveryRun:
    """Run the discover job's reads over ``catalog``: list its databases (through master, or through Azure's list), then
    discover each one in listing order, as discover_tables.ipynb does. ``on_found(database, rows)`` gets each database's
    rows as they are read, so a whole server is never held in memory; ``on_refused(database, message)`` gets each
    database the server refuses. ``limit`` takes the first databases of the listing (for the small runs)."""
    server = sim.SimulatedServer(catalog)
    spec = sim.connection_spec(pattern=pattern)
    started = time.perf_counter()
    refused: List[str] = []
    retried: List[str] = []
    discovered = tables_found = 0
    with sim.discovery_boundary(server):
        listed = sim.list_through_azure(server, spec) if via_azure else sim.list_through_master(spec)
        if limit is not None:
            listed = listed[:limit]
        for name in listed:
            waits_before = server.ledger[name].waits_s if name in server.ledger else 0.0
            try:
                found = sim.discover_one(server, spec, name)
            except RuntimeError as error:
                refused.append(name)
                if on_refused is not None:
                    on_refused(name, str(error))
                continue
            if server.ledger[name].waits_s > waits_before:
                retried.append(name)
            discovered += 1
            tables_found += len(found)
            if on_found is not None:
                on_found(name, found)
    return DiscoveryRun(listed=listed, refused=refused, retried=retried, discovered=discovered, tables=tables_found,
                        ledger=server.ledger, calls_by_kind=server.calls_by_kind, rows_by_kind=server.rows_by_kind,
                        retry_waits_s=sum(server.waits_by_kind.values()), cpu_s=time.perf_counter() - started,
                        azure=via_azure, order=list(listed))


# The columns discover_tables.ipynb writes to lakefed_ingest.discovered_table, in its order;
# scale_test.py checks that the two lists agree.
TABLE_COLS = ["discovery_id", "src_database", "src_schema", "src_table", "row_count", "size_mb", "ct_enabled",
              "primary_key", "select_list", "excluded_columns", "rowversion_column", "identity_column",
              "load_type", "cursor_column", "partition_candidate", "notes"]
DISCOVERY_ID = "scale-discovery"
START_SQL = ("insert into lakefed_ingest.discovery (discovery_id, src_type, src_connection, src_host, src_port, "
             "src_database, secret_key, requested_by, status, finished_at) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
             "case when %s = 'running' then null else now() end) on conflict (discovery_id, src_database) "
             "do update set status = excluded.status, error = null, started_at = now(), "
             "finished_at = excluded.finished_at")
FINISH_SQL = ("update lakefed_ingest.discovery set status = %s, tables = %s, error = %s, finished_at = now() "
              "where discovery_id = %s and src_database = %s")
DELETE_SQL = "delete from lakefed_ingest.discovered_table where discovery_id = %s and src_database = %s"
TABLE_BATCH = 1000   # rows to an insert statement (discover_tables.ipynb's TABLE_BATCH)


def insert_sql(rows: int) -> str:
    """The insert of ``rows`` discovered tables in one statement, as discover_tables.ipynb writes them."""
    one_row = f"({', '.join(['%s'] * len(TABLE_COLS))})"
    return f"insert into lakefed_ingest.discovered_table ({', '.join(TABLE_COLS)}) values {', '.join([one_row] * rows)}"


class DiscoveryWriter:
    """The Lakebase writes discover_tables.ipynb makes for each database: start it, replace its tables in one transaction
    (TABLE_BATCH rows to a statement), finish it. The notebook starts a database before it reads it; the writer makes the
    same statements after each database is read, and the counts come out the same."""

    def __init__(self, connection: lakebase_fake.RecordingConnection, discovery_id: str = DISCOVERY_ID,
                 spec: Optional[dict] = None):
        self.connection = connection
        self.discovery_id = discovery_id
        self.spec = spec or sim.connection_spec()

    def start(self, database: str, status: str = "running") -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(START_SQL, (self.discovery_id, self.spec["src_type"], self.spec["src_connection"],
                                       None, None, database, None, None, status, status))

    def finish(self, database: str, status: str, tables: Optional[int] = None, error: Optional[str] = None) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(FINISH_SQL, (status, tables, error, self.discovery_id, database))

    def found(self, database: str, found_rows: List[dict]) -> None:
        self.start(database)
        rows = [tuple({**t, "discovery_id": self.discovery_id, "src_database": database}.get(c) for c in TABLE_COLS)
                for t in found_rows]
        with self.connection.transaction() as cursor:
            cursor.execute(DELETE_SQL, (self.discovery_id, database))
            for at in range(0, len(rows), TABLE_BATCH):
                chunk = rows[at:at + TABLE_BATCH]
                cursor.execute(insert_sql(len(chunk)), tuple(v for row in chunk for v in row))
        self.finish(database, "ok", tables=len(rows))

    def refused(self, database: str, message: str) -> None:
        self.start(database)
        self.finish(database, "failed", error=message[:2000])


def discover_writes(catalog: server_catalog.ServerCatalog, pattern: str = "", limit: Optional[int] = None):
    """The discover job over ``catalog`` with its Lakebase writes recorded: the same run as discover(), and the
    connection that holds what it wrote (statements and rows, by kind)."""
    connection = lakebase_fake.RecordingConnection()
    writer = DiscoveryWriter(connection)
    run = discover(catalog, pattern=pattern, limit=limit, on_found=writer.found, on_refused=writer.refused)
    return run, connection
