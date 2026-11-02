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

The reads in flight for one server (its connection) never pass that server's cap, counted across every discovery and
app instance in the claim's transaction (queries.warehouse_claim_reads_sql). The cap starts at START_CAP and moves with
the time a read takes (next_cap, ServerCaps).

The store passed in holds the Lakebase calls (main.WarehouseReadStore), and the warehouse the Statement Execution
calls (warehouse.py), so this module is tested with fakes."""
import os
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import source_catalog

CLAIM_S = 60
# The two kinds of work: a check of a listed database (status 'listed'), and a read of a chosen one (status 'running').
CHECK, READ = "check", "read"
# Checks kept going at once for one discovery: the page shown, so a page of 100 databases is checked in a few steps.
# Reads keep instead to their server's cap.
IN_FLIGHT = {CHECK: 16}
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


# The cap on one server's reads in flight. On the lab a 100-eDTU pool's CPU capped 1,000-table reads at about 0.65
# databases a second, each read slower the more were in flight, and a 2X-Small warehouse began queueing at 16: so it
# starts at 8 and rises only while reads keep their time. Bundle variables reads_start_cap and reads_max_cap set
# READS_START_CAP and READS_MAX_CAP.
MIN_CAP = 4
MAX_CAP = _setting("READS_MAX_CAP", 32, MIN_CAP, 256)
START_CAP = _setting("READS_START_CAP", 8, MIN_CAP, MAX_CAP)
CAP_STEP = 4
CAP_WINDOW = 8          # reads timed at one level before the cap moves
FLAT = 1.25             # a level's median read within this of the starting level's is flat: there is room for more
GROWN = 1.5             # past this, the source or the warehouse's queue is slowing each read down: step back
CAP_FORGET_S = 15 * 60  # a server with no read timed for this long starts again at START_CAP


def next_cap(cap: int, times: Sequence[float], baseline: Optional[float], saturated: bool, *, step: int = CAP_STEP,
             floor: int = MIN_CAP, ceiling: int = MAX_CAP, window: int = CAP_WINDOW, flat: float = FLAT,
             grown: float = GROWN) -> int:
    """A server's next cap on reads in flight, from the time its recent reads took at this cap (send to answer, so a
    warehouse that queues them counts too) against the median at the starting level (baseline). With fewer than
    ``window`` reads timed, or no baseline yet, it holds. The median of the last ``window`` past ``grown`` times the
    baseline steps it down by ``step``, never below ``floor``; within ``flat`` times it, and only while the server is
    held at its cap (``saturated``: there is more to read than places), it steps up by ``step``, never past
    ``ceiling``. In between it holds."""
    cap = max(floor, min(ceiling, cap))
    if len(times) < window or not baseline:
        return cap
    median = statistics.median(list(times)[-window:])
    if median > grown * baseline:
        return max(floor, cap - step)
    if median <= flat * baseline and saturated:
        return min(ceiling, cap + step)
    return cap


def server_key(connection: str) -> str:
    """The server a read through the warehouse reads: its connection's name, lower case (job_settings.server_of)."""
    return str(connection or "").lower()


class ServerCaps:
    """Each server's cap on reads in flight, as this app instance sets it, and the read times it is set from.

    The count the cap is held to is shared (Lakebase, in each claim's transaction), but the cap itself is kept here, in
    memory, per instance: a few numbers per server, with no table and no schema change. Each instance times the reads
    its own workers send, which is a fair sample of how the server and the warehouse cope, and claims only while the
    shared count is under its own cap, so all instances together stay within the highest cap any one has set. An
    instance that restarts starts again at START_CAP, which is the safe side."""

    def __init__(self, start: int = START_CAP, clock: Callable[[], float] = time.monotonic, **rule):
        self.start, self.clock, self.rule = start, clock, rule
        self._mu = threading.Lock()
        self._servers: Dict[str, dict] = {}

    def _state(self, server: str) -> dict:
        state = self._servers.get(server)
        if state is None or self.clock() - state["seen"] > CAP_FORGET_S:
            state = {"cap": self.start, "baseline": None, "times": [], "saturated": False, "seen": self.clock()}
            self._servers[server] = state
        return state

    def cap(self, server: str) -> int:
        with self._mu:
            return self._state(server)["cap"]

    def saturated(self, server: str, held: bool) -> None:
        """Whether the server's last claim found it at its cap."""
        with self._mu:
            self._state(server)["saturated"] = held

    def timed(self, server: str, seconds: float) -> int:
        """A read of the server that took ``seconds`` from send to answer. Returns the server's cap after it."""
        with self._mu:
            state = self._state(server)
            state["seen"] = self.clock()
            state["times"].append(float(seconds))
            window = self.rule.get("window", CAP_WINDOW)
            if state["baseline"] is None and state["cap"] == self.start and len(state["times"]) >= window:
                state["baseline"] = statistics.median(state["times"][-window:])
            cap = next_cap(state["cap"], state["times"], state["baseline"], state["saturated"], **self.rule)
            if cap != state["cap"]:
                state.update(cap=cap, times=[])
            else:
                state["times"] = state["times"][-window:]
            return cap


CAPS = ServerCaps()

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
            caps: Optional[ServerCaps] = None) -> dict:
    """One step of a discovery's checks or reads: read the statements going, save the ended ones, start more (the
    names in ``first`` before the rest), and say where it stands (store.progress). A statement that can't be read any
    more (it expired, or the warehouse lost it) is cleared, so its database is started again. Checks are sent with a
    short wait (CHECK_WAIT_S), so most are saved in this step; reads are sent without one, within the server's cap
    (caps, CAPS by default), and the next step collects them."""
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
    if kind == CHECK:
        still = len(store.in_flight(kind, discovery_id))
        claimed = store.claim(kind, discovery_id, list(first), max(0, IN_FLIGHT[kind] - still), CLAIM_S)
        wait_s = CHECK_WAIT_S
    else:
        if watched:
            store.hold([(discovery_id, d) for d in watched])
        server = server_key(connection)
        cap = (caps or CAPS).cap(server)
        taken, _ = store.claim_reads(server, [discovery_id], cap, cap, CLAIM_S)
        claimed = [database for _, database in taken]
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
