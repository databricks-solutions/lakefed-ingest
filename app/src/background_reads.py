"""The app's background reader: it keeps every discovery's reads through the SQL warehouse going, whether a page is open
or not, and sends them truly in parallel.

Driven by the page, each read's answer sat on the warehouse until the page's next poll collected it (2.4 to 2.6
seconds, on a source where the read itself took about 0.5 s and the warehouse under 1 s), and a loop that sent one
statement after another kept only 1 or 2 of them in flight. So a thread of the app looks every IDLE_S (at once when a
page starts a read: wake) for the discoveries with databases left to read (store.read_work) and hands them to a bounded
pool of WORKERS threads. Each worker sends one statement with a wait of READ_WAIT_S, so its answer comes back in the
same call; reads it again every READ_POLL_S only while it is still going; saves it through the store; and takes the
next.

- Each database is claimed before it is sent, with the claim the page's own steps take (FOR UPDATE SKIP LOCKED,
  warehouse_reads.CLAIM_S), so two app instances, a page and re-discovery's loop never start one database twice. The
  claims of the reads this instance's workers hold are renewed every HOLD_S, well inside CLAIM_S.
- A server's reads in flight never pass its cap, counted in Lakebase in the claim's transaction under that server's
  lock, across every discovery and instance (store.claim_reads). warehouse_reads.ServerCaps sets the cap from the time
  this instance's reads took, send to answer.
- While the warehouse is starting (or stopped), one statement is sent at a time, and it isn't timed: its slow first
  answer holds one worker, not all of them, and sets no cap. Its answer says the warehouse is up.
- After a restart nothing is lost and nothing is sent twice: a claim with no statement is free again after CLAIM_S and
  sent then; a statement saved before the restart is read again from its id once its claim has lapsed (store.collect).

It reads only what a person started through the app's checks (main._start_warehouse_read, and re-discovery's reads of
a job's own selection): rows marked as read through the warehouse, each through the connection saved on its own row,
never one a request names. It never starts a discovery itself. BACKGROUND_READS=0 turns it off; the page's steps
(warehouse_reads.advance) then take the reads as before, within the same cap."""
import logging
import os
import queue
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import warehouse_reads as wr

READ_WAIT_S = 30            # the most a statement waits for its answer when it is sent (the API takes 5 to 50)
READ_POLL_S = 1.0           # how often a statement still going after that is read again
READ_ERRORS = 3             # reads of a statement failing in a row before it is let go and its database sent again
WORKERS = max(1, int(os.environ.get("BACKGROUND_READ_WORKERS", "32")))
IDLE_S = 2.0                # how long the reader waits between looks when nothing wakes it
LOOK_GAP_S = 0.2            # the least time between two looks, however often pages and workers wake it
HOLD_S = 20.0               # how often the claims of the reads held here are renewed
MAX_AGE_S = 6 * 3600        # a read started longer ago than this is left alone
WAREHOUSE_LOOK_S = 5.0      # how long a look at the warehouse's state is trusted
BACKOFF_S = 10.0            # after a statement couldn't be sent, how long before the next is
ERROR_SHOWN_S = 30.0        # how long a statement that couldn't be sent is said to the page that asks about its read
# The warehouse states that take reads at full pace; blank is a state that couldn't be read, which holds nothing back.
READY = ("RUNNING", "")

log = logging.getLogger(__name__)


class Reader:
    """One background reader per app process. ``start`` runs its look in a thread and its workers in theirs; ``wake``
    looks at once. ``step`` is one look, which the tests take themselves."""

    def __init__(self, store, warehouse, caps: Optional[wr.ServerCaps] = None, workers: int = WORKERS,
                 draft_ttl_s: float = 2 * 3600, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.store, self.warehouse = store, warehouse
        self.caps = caps or wr.CAPS
        self.workers, self.draft_ttl_s, self.clock, self.sleep = workers, draft_ttl_s, clock, sleep
        self._mu = threading.Lock()
        self._held: Dict[Tuple[str, str], str] = {}     # (discovery_id, database): its server, for each read held here
        self._jobs: "queue.Queue[tuple]" = queue.Queue()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._renewed: Optional[float] = None
        self._warehouse_seen: Tuple[str, float] = ("", float("-inf"))
        self._paused_until = float("-inf")
        self._turn = 0
        self.last_error: Optional[str] = None
        self._errors: Dict[str, Tuple[str, float]] = {}     # discovery_id: why a statement of it couldn't be sent, when

    def start(self) -> None:
        """The workers, then the thread that looks for work."""
        if self._thread is not None:
            return
        self.start_workers()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lakefed-app-reads")
        self._thread.start()

    def start_workers(self) -> None:
        for n in range(self.workers):
            threading.Thread(target=self._work, daemon=True, name=f"lakefed-app-read-{n}").start()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wake(self) -> None:
        self._wake.set()

    def held(self) -> int:
        with self._mu:
            return len(self._held)

    def recent_error(self, discovery_id: str) -> Optional[str]:
        """Why a statement of this discovery couldn't be sent, while that is recent."""
        with self._mu:
            found = self._errors.get(discovery_id)
        return found[0] if found and self.clock() - found[1] < ERROR_SHOWN_S else None

    def _loop(self) -> None:
        while True:
            looked = time.monotonic()
            try:
                self.step()
            except Exception as e:
                self._failed(e)
                log.exception("the background reader's look failed")
            self._wake.wait(IDLE_S)
            self._wake.clear()
            time.sleep(max(0.0, LOOK_GAP_S - (time.monotonic() - looked)))

    def step(self) -> int:
        """One look: renew the claims held here, read again the statements nobody watches, then claim and hand to the
        free workers the databases waiting, server by server within each one's cap. Returns how many it handed."""
        now = self.clock()
        self._renew(now)
        free = self.workers - self.held()
        if free <= 0:
            return 0
        work = self.store.read_work(wr.CLAIM_S, MAX_AGE_S, self.draft_ttl_s)
        handed = 0
        for row in work:
            if free <= 0:
                break
            if int(row.get("orphaned") or 0):
                for database, statement_id in self.store.collect(row["discovery_id"], wr.CLAIM_S, free):
                    self._hand(row["discovery_id"], database, row["src_connection"], statement_id, timed=False)
                    free, handed = free - 1, handed + 1
        if now < self._paused_until:
            return handed
        servers: Dict[str, List[dict]] = {}
        for row in work:
            if int(row.get("waiting") or 0):
                servers.setdefault(wr.server_key(row["src_connection"]), []).append(row)
        ready = self._warehouse_ready(now) if servers else True
        # Each look starts at the next server, so one server whose cap fills the workers never keeps the rest waiting.
        order = sorted(servers)
        if order:
            self._turn = (self._turn + 1) % len(order)
            order = order[self._turn:] + order[:self._turn]
        for server in order:
            rows = servers[server]
            # A warehouse starting takes one statement at a time, until one answers.
            limit = free if ready else min(free, 1 - self.held())
            if limit <= 0:
                break
            # The discoveries with least to read first, so a page's few databases don't wait behind a whole server's.
            rows.sort(key=lambda r: int(r["waiting"]))
            connection_of = {r["discovery_id"]: r["src_connection"] for r in rows}
            claimed, saturated = self.store.claim_reads(server, list(connection_of), self.caps.cap(server), limit,
                                                        wr.CLAIM_S)
            self.caps.saturated(server, saturated)
            for discovery_id, database in claimed:
                self._hand(discovery_id, database, connection_of[discovery_id], None, timed=ready)
                free, handed = free - 1, handed + 1
        return handed

    def _hand(self, discovery_id: str, database: str, connection: str, statement_id: Optional[str], timed: bool) -> None:
        with self._mu:
            self._held[(discovery_id, database)] = wr.server_key(connection)
        self._jobs.put((discovery_id, database, connection, statement_id, timed))

    def _work(self) -> None:
        while True:
            self.read(*self._jobs.get())

    def read(self, discovery_id: str, database: str, connection: str, statement_id: Optional[str],
             timed: bool) -> None:
        """One worker's read of one database: send its statement with a wait (or, given one saved before, read that
        again), read it again while it is still going, save it, and let it go."""
        try:
            sent = self.clock()
            answer = None
            if statement_id is None:
                statement, parameters = wr.statement_for(wr.READ, connection, database)
                try:
                    answer = self.warehouse.start(statement, parameters, wait_s=READ_WAIT_S)
                except Exception as e:
                    self.store.clear(wr.READ, discovery_id, database)
                    self._failed(e, pause=True, discovery_id=discovery_id)
                    return
                statement_id = answer["statement_id"]
                if answer["state"] not in self.warehouse.ENDED and not self.store.started(wr.READ, discovery_id,
                                                                                          database, statement_id):
                    wr.cancel_quietly(self.warehouse, statement_id)      # its draft dropped it while it was being sent
                    return
            errors = 0
            while answer is None or answer["state"] not in self.warehouse.ENDED:
                if answer is not None or errors:
                    self.sleep(READ_POLL_S)
                try:
                    answer, errors = self.warehouse.read(statement_id), 0
                except Exception as e:
                    errors += 1
                    if errors >= READ_ERRORS:
                        self.store.clear(wr.READ, discovery_id, database)
                        self._failed(e)
                        return
            self._warehouse_seen = ("RUNNING", self.clock())
            wr.finish(self.store, wr.READ, discovery_id, database, statement_id, answer)
            if timed and answer["state"] == "SUCCEEDED":
                self.caps.timed(wr.server_key(connection), self.clock() - sent)
        except Exception as e:
            self._failed(e)
            log.exception("a background read of %s failed", database)
        finally:
            with self._mu:
                self._held.pop((discovery_id, database), None)
            self._wake.set()

    def _renew(self, now: float) -> None:
        with self._mu:
            held = list(self._held)
        if held and (self._renewed is None or now - self._renewed >= HOLD_S):
            self.store.hold(held)
            self._renewed = now

    def _warehouse_ready(self, now: float) -> bool:
        state, looked = self._warehouse_seen
        if now - looked >= WAREHOUSE_LOOK_S:
            try:
                state = self.warehouse.warehouse_state() or ""
            except Exception:
                state = ""
            self._warehouse_seen = (state, now)
        return state in READY

    def _failed(self, error: BaseException, pause: bool = False, discovery_id: Optional[str] = None) -> None:
        self.last_error = wr.first_line(str(error)) or type(error).__name__
        now = self.clock()
        if discovery_id is not None:
            with self._mu:
                self._errors = {d: e for d, e in self._errors.items() if now - e[1] < ERROR_SHOWN_S}
                self._errors[discovery_id] = (self.last_error, now)
        if pause:
            self._paused_until = now + BACKOFF_S
        log.warning("background reads: %s", self.last_error)
