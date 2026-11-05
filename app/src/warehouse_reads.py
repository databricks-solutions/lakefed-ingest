"""Discovery's per-database statements on the SQL warehouse: whether the connection's login can open each database
(and how many tables it has), and the tables of the databases chosen.

Each database is one statement through the connection (remote_query). The tables of the databases chosen are read by
the app's background reader (background_reads.py), which sends each statement with a wait so its answer comes back in
the same call, whether a page is open or not. A check (the page shown) is one step of the work (advance) per request:
it reads the statements still going, saves what ended, and starts more, each with a short wait, so the page hears
most answers in the request that sent them. With the background reader off (BACKGROUND_READS=0), advance takes the
reads' steps too, as the page asks.

Where each database stands is kept on its discovery row in Lakebase (discovery.listing, a JSON object): the statement
going for it, when it was claimed, and for a check whether the login can open it and why not. Whoever starts a
database claims it first (FOR UPDATE SKIP LOCKED), so two app instances, a page and the background reader never start
the same one twice; a claim not renewed after CLAIM_S (the app stopped in between) is free again, and a statement saved
before then is read again, never sent again.

The checks and reads in flight for one server (its connection) never pass that server's cap, one fixed number (CAP)
counted across every discovery and app instance in the claim's transaction (queries.warehouse_going_sql). A check takes
a free place first; one the server had no room for is wanted for WANT_S, and reads leave that many places free
meanwhile.

The store passed in holds the Lakebase calls (main.WarehouseReadStore), and the warehouse the Statement Execution
calls (warehouse.py), so this module is tested with fakes."""
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, List, Optional, Sequence

import source_catalog

CLAIM_S = 60
# The two kinds of work: a check of a listed database (status 'listed'), and a read of a chosen one (status 'running').
CHECK, READ = "check", "read"
# Checks kept going at once for one discovery: the page shown, so a page of 100 databases is checked in a few steps.
# A second limit beside the server's cap, which checks and reads share.
IN_FLIGHT = {CHECK: 16}
# A check the server had no room for is wanted for this long, and reads leave that many places free meanwhile.
WANT_S = 10
# A check waits this long for its answer when it is sent (the Statement Execution API takes 0, or 5 to 50 seconds):
# most answer within it, so the step that sends them saves them too.
CHECK_WAIT_S = 5
# How a database is checked: opening it is the test, and the count says how many tables choosing it takes.
ACCESS_SQL = "SELECT COUNT(*) AS tables FROM sys.tables WHERE is_ms_shipped = 0"
TABLE_COLUMNS = ("discovery_id", "src_database", "src_schema", "src_table", "row_count", "size_mb", "ct_enabled",
                 "primary_key", "select_list", "excluded_columns", "rowversion_column", "identity_column", "load_type",
                 "cursor_column", "partition_candidate", "notes")


def _setting(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name) or default)
    except ValueError:
        value = default
    return max(low, min(high, value))


# The cap on one server's checks and reads in flight together. On the lab a 100-eDTU pool's CPU capped 1,000-table
# reads at about 0.65 databases a second, each read slower the more were in flight, and a 2X-Small warehouse began
# queueing at 16, while throughput stayed flat from 3 to 11 at once: so one fixed number, 8, protects a shared server
# and gives up little. The bundle variable reads_cap sets READS_CAP; a deploy that still sets READS_START_CAP is read
# when READS_CAP is not set.
DEFAULT_CAP = 8
MIN_CAP = 4
MAX_CAP = 256


def cap_from_environment() -> int:
    """The cap set for this app: READS_CAP, or READS_START_CAP when that is not set, within MIN_CAP and MAX_CAP."""
    name = "READS_CAP" if os.environ.get("READS_CAP") else "READS_START_CAP"
    return _setting(name, DEFAULT_CAP, MIN_CAP, MAX_CAP)


CAP = cap_from_environment()


def wanted_limit(free_check_slots: int, cap: int) -> int:
    """How many of a page's checks that found the server full are marked wanted: the checks that could run now (the
    discovery's free check slots), and never more than half the cap. A page of 100 unchecked names would otherwise
    hold every place for itself and pause the reads until nearly all of it was checked; with half the cap, reads
    always keep the other half while the page waits, and the page still gets its half a round."""
    return max(0, min(int(free_check_slots), int(cap) // 2))


def server_key(connection: str) -> str:
    """The server a read through the warehouse reads: its connection's name, lower case (job_settings.server_of)."""
    return str(connection or "").lower()


_NO_ACCESS = re.compile(r"Cannot open database|Login failed|not able to access the database|"
                        r"is not able to access|The server principal .* is not able to|permission was denied", re.I)
_OFFLINE = re.compile(r"is not currently available|offline|being recovered|in the middle of a restore", re.I)


def first_line(error: Optional[str]) -> str:
    return str(error or "").strip().split("\n")[0][:500]


def no_access(error: Optional[str]) -> bool:
    """Whether a database's error says the connection's login can't open it."""
    return bool(_NO_ACCESS.search(first_line(error)))


def why_closed(error: Optional[str]) -> str:
    """Why the login can't open a database, in plain words, then the server's own message."""
    message = first_line(error)
    if _NO_ACCESS.search(message):
        plain = "the connection's login has no access to this database: ask the server's admin for a user in it"
    elif _OFFLINE.search(message):
        plain = "the database isn't available on the server now (offline or restoring): check it again later"
    else:
        plain = "the database couldn't be opened through the connection"
    return f"{plain}. The server said: {message}" if message else plain


def statement_for(kind: str, connection: str, database: str):
    """The statement and parameters for one database: the check, or the read of its tables."""
    import warehouse
    query = ACCESS_SQL if kind == CHECK else source_catalog.TABLE_SUMMARY_SQL
    return warehouse.remote_query_statement(connection, database, query)


def table_rows(discovery_id: str, database: str, rows: Iterable[dict]) -> List[tuple]:
    """A read's rows as discovered_table rows (TABLE_COLUMNS), with the recommendations the discovery job makes."""
    found = source_catalog.assemble_summary(rows)
    return [tuple({**t, "discovery_id": discovery_id, "src_database": database}.get(c) for c in TABLE_COLUMNS)
            for t in found]


def count_of(rows: Optional[List[dict]]) -> Optional[int]:
    try:
        return int((rows or [{}])[0].get("tables"))
    except (TypeError, ValueError):
        return None


def finish(store, kind: str, discovery_id: str, database: str, statement_id: str, answer: dict) -> None:
    """Save a statement that has ended: a check's answer, or a read's tables (or why it failed)."""
    ok = answer["state"] == "SUCCEEDED"
    if kind == CHECK:
        store.finish_check(discovery_id, database, ok, count_of(answer["rows"]) if ok else None,
                           None if ok else why_closed(answer.get("error")))
    elif ok:
        store.finish_read(discovery_id, database, table_rows(discovery_id, database, answer["rows"] or []), None,
                          statement_id)
    else:
        store.finish_read(discovery_id, database, None,
                          first_line(answer.get("error")) or f"the read ended {answer['state']}", statement_id)


def send(warehouse, kind: str, connection: str, databases: Sequence[str], wait_s: int) -> List[tuple]:
    """Send one statement per database, all at once, each waiting up to ``wait_s`` for its answer: (database, answer)
    pairs, the answer an exception when it couldn't be sent."""
    def one(database):
        statement, parameters = statement_for(kind, connection, database)
        try:
            return database, warehouse.start(statement, parameters, wait_s=wait_s)
        except Exception as e:
            return database, e
    if wait_s == 0 or len(databases) < 2:
        return [one(d) for d in databases]
    with ThreadPoolExecutor(max_workers=len(databases), thread_name_prefix="lakefed-app-check") as pool:
        return list(pool.map(one, databases))


def cancel_quietly(warehouse, statement_id: str) -> None:
    """Cancel a statement no one waits for any more; one that has ended meanwhile is left as it is."""
    try:
        warehouse.cancel(statement_id)
    except Exception:
        pass


def standing(store, warehouse, kind: str, discovery_id: str) -> dict:
    """Where a discovery's checks or reads stand, with no step taken: what the page hears while the background reader
    does the reads."""
    progress = store.progress(kind, discovery_id)
    return {**progress, "state": "SUCCEEDED" if not progress["pending"] else "RUNNING",
            "warehouse": warehouse.warehouse_state() if progress["pending"] else "RUNNING"}


def advance(store, warehouse, kind: str, discovery_id: str, connection: str, first: Iterable[str] = (),
            cap: int = CAP) -> dict:
    """One step of a discovery's checks or reads: read the statements going, save the ended ones, start more (the
    names in ``first`` before the rest), and say where it stands (store.progress). A statement that can't be read any
    more (it expired, or the warehouse lost it) is cleared, so its database is started again. Checks and reads are both
    held to the server's cap (``cap``, CAP by default), a check taking a free place first. Checks are sent with a short
    wait (CHECK_WAIT_S), so most are saved in this step; reads are sent without one, and the next step collects them."""
    going = store.in_flight(kind, discovery_id)
    watched = []
    for database, statement_id in going:
        try:
            answer = warehouse.read(statement_id)
        except Exception:
            store.clear(kind, discovery_id, database)
            continue
        if answer["state"] not in warehouse.ENDED:
            watched.append(database)
            continue
        finish(store, kind, discovery_id, database, statement_id, answer)
    if watched:
        store.hold([(discovery_id, d) for d in watched])
    server = server_key(connection)
    if kind == CHECK:
        still = len(store.in_flight(kind, discovery_id))
        claimed = store.claim_checks(server, discovery_id, list(first), max(0, IN_FLIGHT[kind] - still), cap, CLAIM_S)
        wait_s = CHECK_WAIT_S
    else:
        claimed = [database for _, database in store.claim_reads(server, [discovery_id], cap, cap, CLAIM_S)]
        wait_s = 0
    error = None
    for database, answer in send(warehouse, kind, connection, claimed, wait_s):
        if isinstance(answer, Exception):
            store.clear(kind, discovery_id, database)
            error = error or first_line(str(answer))
        elif wait_s and answer["state"] in warehouse.ENDED:
            finish(store, kind, discovery_id, database, answer["statement_id"], answer)
        elif not store.started(kind, discovery_id, database, answer["statement_id"]):
            cancel_quietly(warehouse, answer["statement_id"])      # its draft dropped it while it was being sent
    if error:
        return {**store.progress(kind, discovery_id), "error": error, "warehouse": ""}
    return standing(store, warehouse, kind, discovery_id)
