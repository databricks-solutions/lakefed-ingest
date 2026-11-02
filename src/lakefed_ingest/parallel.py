"""In-process parallel execution engine for the consolidated sweep.

Runs many per-table ingests inside ONE job task using a thread pool, so throughput is bounded
by compute (cluster cores / warehouse slots) rather than the workspace job limits that a
task-per-table design hits (2,000 concurrent task runs; 10,000 run-submits/hour).

Adapted from the precursor accelerator's parallel-notebook helper
(lakefed_ingest_cluster_orchestration/lakehouse/config.py :: executeNotebooks), which used a
ThreadPoolExecutor and round-robin FAIR scheduler pools (poolNumber = index % maxParallel).
Here the same idea is generalized: each item is assigned a pool name and an optional
``set_pool`` callback applies it on the worker thread before the item runs. On Databricks the
callback sets ``spark.sparkContext.setLocalProperty("spark.scheduler.pool", name)`` so each
item's Spark jobs (e.g. its MERGE) land in their own FAIR pool and overlap fairly on the
cluster. The cluster must run the FAIR scheduler (``spark.scheduler.mode=FAIR``) for the pools
to take effect; otherwise execution still works, just FIFO.

This module imports no Spark/dbutils symbols so it stays unit-testable off-cluster.
"""

from __future__ import annotations

import contextlib
import queue
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Iterable, List, Optional


class SourceLimiter:
    """Per-source caps for one run: at most ``limit(source)`` units query a source at once. A limit
    can change while the run goes (``set_limit``), e.g. this run's share of a cap that the runs
    reading the source divide between them (``state_store.SourceShares``); a source with no limit
    set is not capped.

    A request names one source per query it runs at once (a batch of reads names each member's
    source). It is admitted when, for each of its sources, the permits in use plus min(its count,
    the limit) fit the limit (so a request bigger than the limit runs alone on that source rather
    than never), and no earlier waiting request needs one of its sources: admission is in arrival
    order per source, so a batch is never starved by a stream of single queries. ``peak`` records
    the most permits in use per source. Pure Python."""

    def __init__(self, limits: Optional[Dict[str, int]] = None):
        self._cv = threading.Condition()
        self._limits = {s: max(1, int(n)) for s, n in (limits or {}).items()}
        self._in_use: Dict[str, int] = {}
        self._waiting: List[dict] = []    # requests not yet admitted, in arrival order
        self.peak: Dict[str, int] = {}

    def limit(self, source: str) -> int:
        """The source's current limit; 0 when it isn't capped."""
        with self._cv:
            return self._limits.get(source, 0)

    def set_limit(self, source: str, limit: int) -> None:
        with self._cv:
            self._limits[source] = max(1, int(limit))
            self._admit()
            self._cv.notify_all()

    @contextlib.contextmanager
    def hold(self, sources: Iterable[Optional[str]]):
        """Block until admitted (see the class docstring); release on exit, even on exception."""
        need = Counter(s for s in sources if s)
        if not need:
            yield
            return
        request = {"need": need, "admitted": False}
        with self._cv:
            self._waiting.append(request)
            self._admit()
            while not request["admitted"]:
                self._cv.wait()
        try:
            yield
        finally:
            with self._cv:
                for source, n in need.items():
                    self._in_use[source] -= n
                self._admit()
                self._cv.notify_all()

    def _fits(self, need: Counter) -> bool:
        for source, n in need.items():
            cap = self._limits.get(source, 0)
            if cap and self._in_use.get(source, 0) + min(n, cap) > cap:
                return False
        return True

    def _admit(self) -> None:
        """Admit waiting requests in arrival order; one that doesn't fit keeps later requests off its
        sources. Called with the condition held."""
        blocked, waiting = set(), []
        for request in self._waiting:
            if blocked.isdisjoint(request["need"]) and self._fits(request["need"]):
                for source, n in request["need"].items():
                    self._in_use[source] = self._in_use.get(source, 0) + n
                    self.peak[source] = max(self.peak.get(source, 0), self._in_use[source])
                request["admitted"] = True
            else:
                blocked.update(request["need"])
                waiting.append(request)
        self._waiting = waiting


class WorkSlots:
    """One concurrency budget for a whole sweep run: at most ``capacity`` units of query work run
    at once, whatever their type (CT read+MERGE, a non-partitioned seed, ONE partition of a
    partitioned seed, a small source query, the staging->target swap).

    Threads vs slots: threads are cheap and may outnumber slots (table workers + partition
    workers); a thread does Spark/JDBC work only while it HOLDS a slot. Rule: never hold a slot
    while waiting on other work (e.g. a table waiting for its partitions) — that is what keeps
    nested fan-out deadlock-free.

    Each slot has a stable id; ``acquire()`` calls ``set_pool(f"pool{id}")`` on the current
    thread so the unit's Spark jobs land in that slot's FAIR scheduler pool (``capacity`` pools,
    one per concurrent unit). Where the cluster refuses that (standard access mode can refuse
    sparkContext), the units run on without FAIR pools: the first refusal is kept in ``pool_error``
    and the pool is not asked for again. ``peak`` records the most units ever in flight (for telemetry).

    With ``sources`` (a ``SourceLimiter``), a unit that names the source it queries also holds one of
    that source's permits, taken BEFORE its slot: a slot then never waits on a source, and a permit
    only waits for slots, which units that are running give back.
    Pure Python — no Spark/dbutils imports.
    """

    def __init__(self, capacity: int, set_pool: Optional[Callable[[str], None]] = None,
                 sources: Optional[SourceLimiter] = None):
        self.sources = sources
        self.capacity = max(1, int(capacity))
        self._free: "queue.Queue[int]" = queue.Queue()
        for i in range(self.capacity):
            self._free.put(i)
        self._set_pool = set_pool
        self._mu = threading.Lock()
        self._many_mu = threading.Lock()  # one multi-slot acquisition at a time (see acquire_many)
        self._local = threading.local()   # per-thread list of pools used (see start_tracking)
        self.in_flight = 0
        self.peak = 0
        self.pool_error: Optional[str] = None

    def start_tracking(self) -> None:
        """Start recording, for the CURRENT thread, the FAIR pools its units acquire."""
        self._local.pools = []

    def tracked_pools(self) -> List[str]:
        """Pools the current thread's units used since start_tracking (in order, de-duplicated);
        stops tracking. Units run on other threads (e.g. a partitioned seed's partitions on the
        shared executor) are not included."""
        pools = getattr(self._local, "pools", None) or []
        self._local.pools = None
        return pools

    @contextlib.contextmanager
    def acquire(self, source: Optional[str] = None):
        """Block until a slot is free (and, with ``sources``, a permit of ``source``); yield the slot's
        id; always return both (even on exception)."""
        with self._permits([source]):
            slot = self._free.get()
            with self._held([slot]):
                yield slot

    @contextlib.contextmanager
    def acquire_many(self, n: int, sources: Optional[List[Optional[str]]] = None):
        """Hold ``n`` slots (capped at ``capacity``) for ONE unit that runs ``n`` units of query work
        at once (e.g. a batch of source reads executed as one Spark job) and, with ``sources``, a
        permit of each read's source (``sources``: one entry per read). Yields the first slot's id;
        its FAIR pool is set on the current thread.

        Deadlock-free under the same rule as ``acquire`` (never hold a slot while waiting): multi-slot
        acquisitions are serialized, so two of them can never each hold part of what the other waits
        for, and single-slot holders never wait while holding, so the slots being waited for free up."""
        n = max(1, min(int(n), self.capacity))
        with self._permits(sources or []):
            with self._many_mu:
                taken = [self._free.get() for _ in range(n)]
            with self._held(taken):
                yield taken[0]

    def _apply_pool(self, pool: str) -> None:
        try:
            self._set_pool(pool)
        except Exception as e:
            self._set_pool = None
            self.pool_error = self.pool_error or f"{type(e).__name__}: {str(e).splitlines()[0][:300] if str(e) else ''}"
            print(f"FAIR pools unavailable on this cluster, units run without them ({self.pool_error})")

    def _permits(self, sources: List[Optional[str]]):
        if self.sources is None or not any(sources):
            return contextlib.nullcontext()
        return self.sources.hold(sources)

    @contextlib.contextmanager
    def _held(self, taken: List[int]):
        with self._mu:
            self.in_flight += len(taken)
            self.peak = max(self.peak, self.in_flight)
        try:
            pool = f"pool{taken[0]}"
            tracked = getattr(self._local, "pools", None)
            if tracked is not None and pool not in tracked:
                tracked.append(pool)
            if self._set_pool is not None:
                self._apply_pool(pool)
            yield
        finally:
            with self._mu:
                self.in_flight -= len(taken)
            for slot in taken:
                self._free.put(slot)


class Cancelled(Exception):
    """Raised by a worker whose item was stopped by a cancel: the item is recorded as 'cancelled', not 'failed'."""


def run_parallel(
    items: Iterable,
    worker: Callable,
    max_parallel: int,
    set_pool: Optional[Callable[[str], None]] = None,
    item_id: Optional[Callable] = None,
) -> List[dict]:
    """Run ``worker(item)`` for every item across a bounded thread pool.

    - ``max_parallel`` threads; each item i is assigned FAIR pool ``f"pool{i % max_parallel}"``.
      When ``set_pool`` is given it is called with that pool name on the worker thread,
      immediately before ``worker(item)`` runs (so Spark's thread-local scheduler pool is set
      for the Spark jobs the worker launches).
    - Per-item isolation: a worker that raises never aborts the batch; its failure is recorded
      and the rest continue (this is the per-DB/-table failure isolation, workstream E).
    - ``item_id`` maps an item to a short identifier for the result record (default: the item).

    Returns one result dict per item, in input order:
        {"item": <id>, "pool": <pool>, "status": "ok"|"failed"|"cancelled", "detail": <worker return | error>}
    """
    items = list(items)
    max_parallel = max(1, int(max_parallel))
    id_of = item_id or (lambda x: x)

    def _run(pair):
        idx, item = pair
        pool = f"pool{idx % max_parallel}"
        rec = {"item": id_of(item), "pool": pool, "status": "ok", "detail": None}
        try:
            # Inside the try: a failing set_pool (e.g. a cluster access mode without
            # sparkContext) fails this item, never the batch.
            if set_pool is not None:
                set_pool(pool)
            rec["detail"] = worker(item)
        except Cancelled as e:
            rec["status"] = "cancelled"
            rec["detail"] = str(e)
        except Exception as e:  # isolate: one bad item must not fail the batch
            rec["status"] = "failed"
            rec["detail"] = str(e)
        return rec

    if not items:
        return []

    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        # ex.map preserves input order, so results align with items.
        return list(executor.map(_run, enumerate(items)))


def summarize(results: List[dict]) -> dict:
    """Count results by status and by worker outcome (the 'detail' string on success). A cancelled result is
    counted apart from the failures, so a cancel never reads as a failure."""
    summary = {"total": len(results), "ok": 0, "failed": 0, "cancelled": 0, "by_outcome": {}}
    for r in results:
        if r.get("status") == "failed":
            summary["failed"] += 1
        elif r.get("status") == "cancelled":
            summary["cancelled"] += 1
        else:
            summary["ok"] += 1
            outcome = str(r.get("detail"))
            summary["by_outcome"][outcome] = summary["by_outcome"].get(outcome, 0) + 1
    return summary
