"""The check: which source databases of a group of collections need an ingest.

``check_launch.ipynb`` runs this first, on serverless, over one group of collections (a schedule's members, or every
collection with enabled rows except those only a deleted job holds). For each database it asks the same questions a
sweep's planning asks (``planner``): has any table never been loaded, is any append pending or reseed due, and did the
change tracking version move past the checkpoints (then, did any of our tables change). A database with nothing to do
gets its check stamp (``planner.unchanged_writes``) and no ingest. The others are their collection's work list: the
check job records it (``check_work``) and starts that collection's run for those databases only (``launcher.py``). A
quiet cycle starts no ingest cluster.

``check_collection`` is pure (no Spark, no Lakebase): the notebook passes the source queries in, as
``detect_one``. ``run_check`` takes the Lakebase calls as objects, so it is tested with fakes.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Iterable, List, Optional

import parallel
import planner
import state_store as ss


def check_collection(configs: List[dict], checkpoints: Dict[int, dict], detect_one: Callable, *,
                     existing_sinks: Optional[set], stamps=None, interval_s: float = 0, mixed_sinks=frozenset(),
                     reseed_on_new_columns: bool = True, snapshot_isolation: bool = False, force=frozenset(),
                     now=None, map_fn=map) -> dict:
    """Check one collection's databases.

    ``existing_sinks`` is the set of sink tables that exist (``ct_engine.existing_sinks``). It is
    required, so a check can't run without knowing whether a sink was dropped (a dropped sink would
    otherwise be stamped as unchanged). None means the sinks couldn't be listed: then nothing is
    vouched for, and every database with a table to check goes to the ingest, which checks them itself.

    ``detect_one(members) -> planner.detect_group output`` checks one detection group (one source
    database); ``map_fn`` runs it over the groups (the notebook passes a parallel map). ``force``
    names databases to send to the ingest without asking them anything.

    A database needs an ingest when one of its rows is routed to work before detection (seed,
    pending append, reseed, a full or cursor table, an unsupported row, which the ingest reports),
    when its detection planned anything (changed tables, reseeds, fallbacks) or failed (the ingest
    records the failure), or when it is forced. Detection runs only for the others. The rest are
    unchanged: ``rows`` and ``database_stamps`` are their bookkeeping, for the caller to write while
    it holds the collection's lock.

    ``weights`` says how much work each database with work has, for sharing a cycle out (``plan_cycle``): its
    tables with work (each table routed to work, or found changed by its detection) and, of those, its first loads
    (a seed). A database sent whole without asking it (forced, its detection failed, or the sinks unknown) counts
    every one of its tables.

    Returns {work: sorted [source, src_database] pairs, weights: sorted [source, src_database, tables,
    first_loads], rows, database_stamps, unchanged_databases, unchanged_tables, work_tables, detected, queries,
    fast_exits}.
    """
    plans, candidates, legacy = planner.route_tables(configs, checkpoints, mixed_sinks=mixed_sinks,
                                                     reseed_on_new_columns=reseed_on_new_columns,
                                                     snapshot_isolation=snapshot_isolation,
                                                     existing_sinks=existing_sinks)
    forced = {str(name) for name in force}
    elsewhere = (planner.database_keys(p["cfg"] for p in plans) | planner.database_keys(c for c, _ in legacy)
                 | planner.database_keys(c for c in configs if c.get("src_database") in forced))
    if existing_sinks is None:
        elsewhere |= planner.database_keys(c for c, _ in candidates)
    to_detect = [members for members in planner.group_by_source(candidates).values()
                 if not (planner.database_keys(c for c, _ in members) & elsewhere)]
    outputs = list(map_fn(detect_one, to_detect))
    unchanged_members, unchanged_outputs = [], []
    for members, out in zip(to_detect, outputs):
        if out["plans"] or out["failed"] or out["cur"] is None:
            elsewhere |= planner.database_keys(c for c, _ in members)
        else:
            unchanged_members.append(members)
            unchanged_outputs.append(out)
    rows, database_stamps = planner.unchanged_writes(unchanged_members, unchanged_outputs, checkpoints, interval_s,
                                                     stamps=stamps, now=now)
    whole = planner.database_keys(c for c in configs if c.get("src_database") in forced)
    if existing_sinks is None:
        whole |= planner.database_keys(c for c, _ in candidates)
    counted = [p["cfg"] for p in plans] + [c for c, _ in legacy]
    seeds = [p["cfg"] for p in plans if p.get("route") in FIRST_LOADS]
    for members, out in zip(to_detect, outputs):
        if out["failed"] or out["cur"] is None:
            whole |= planner.database_keys(c for c, _ in members)
        else:
            counted += [p["cfg"] for p in out["plans"]]
    weights = _weights(elsewhere, configs, counted, seeds, whole)
    return {"work": sorted(list(key) for key in elsewhere), "weights": weights, "rows": rows,
            "database_stamps": database_stamps,
            "unchanged_databases": len(unchanged_members),
            "unchanged_tables": sum(len(m) for m in unchanged_members),
            "work_tables": sum(1 for c in configs if planner.database_key(c) in elsewhere),
            "detected": len(to_detect), "queries": sum(o["queries"] for o in outputs),
            "fast_exits": sum(int(o["fast_exit"]) for o in outputs)}


# The routes of a table never loaded: its database's whole run goes to loading it, so a group with one keeps a run of
# its own (plan_cycle).
FIRST_LOADS = frozenset({"seed", "cursor_seed"})


def _weights(work: set, configs: List[dict], counted: List[dict], seeds: List[dict], whole: set) -> List[list]:
    """[source, src_database, tables, first_loads] for each database with work (check_collection's ``weights``)."""
    tables: Dict[tuple, int] = {}
    first: Dict[tuple, int] = {}
    for cfg in configs:
        key = planner.database_key(cfg)
        if key in whole:
            tables[key] = tables.get(key, 0) + 1
    for cfg in counted:
        key = planner.database_key(cfg)
        if key not in whole:
            tables[key] = tables.get(key, 0) + 1
    for cfg in seeds:
        key = planner.database_key(cfg)
        first[key] = first.get(key, 0) + 1
    return [[source, database, max(1, tables.get((source, database), 0)), first.get((source, database), 0)]
            for source, database in sorted(work)]


def group_size_of(tables_by_group: Dict[str, int]) -> int:
    """A job's group size: its enabled tables over its groups, rounded up (at least 1). A cycle with this many
    changed tables starts one run (``plan_cycle``)."""
    counts = [int(n or 0) for n in tables_by_group.values()]
    return max(1, -(-sum(counts) // max(1, len(counts))))


def plan_cycle(per_collection: Iterable[dict], group_size: int) -> List[dict]:
    """A cycle's runs, from the check's result per collection (``check_collection``'s ``weights``, with
    ``collection``). Each database with work goes whole to exactly one run.

    A collection with a first load (a table never loaded) keeps a run of its own with all its work, one cluster per
    group as before: loading weighs far more than changes, and it would hold up the others' changes. The rest are
    shared out by the tables that changed: as many runs as those tables over the job's ``group_size`` (rounded up),
    a database bigger than a run's share on a run of its own, and the others biggest first, each into the run with
    the fewest tables so far (the lowest run on a tie), so changes concentrated in one group are spread over several
    clusters and small changes in many groups share one. A cycle can start one run more than the division gives,
    when a database alone is more than a share.

    Returns [{"databases": [[collection, source, src_database], ...], "collections": [...], "tables": n,
    "first_loads": bool}], the first-load runs first (by collection), then the shared ones, biggest first."""
    loading, shared = {}, []
    for result in per_collection:
        collection = result["collection"]
        weights = result.get("weights") or []
        if any(first for *_, first in weights):
            loading[collection] = weights
        else:
            shared += [(collection, source, database, int(tables)) for source, database, tables, _ in weights]
    runs = [{"databases": [[collection, source, database] for source, database, _, _ in weights],
             "collections": [collection], "tables": sum(int(t) for _, _, t, _ in weights), "first_loads": True}
            for collection, weights in sorted(loading.items())]
    total = sum(tables for *_, tables in shared)
    if not shared:
        return runs
    count = max(1, -(-total // max(1, int(group_size))))
    share = total / count
    order = sorted(shared, key=lambda item: (-item[3], item[0], item[1], item[2]))
    alone = [item for item in order if item[3] > share]
    rest = [item for item in order if item[3] <= share]
    bins = [[item] for item in alone] + [[] for _ in range(max(count - len(alone), 1 if rest else 0))]
    loads = [sum(item[3] for item in run) for run in bins]
    for item in rest:
        index = min(range(len(alone), len(bins)), key=lambda i: (loads[i], i))
        bins[index].append(item)
        loads[index] += item[3]
    made = [{"databases": [[c, source, database] for c, source, database, _ in run],
             "collections": sorted({c for c, *_ in run}), "tables": load, "first_loads": False}
            for run, load in zip(bins, loads) if run]
    return runs + sorted(made, key=lambda run: -run["tables"])


class SourceCap:
    """The check's share of ``max_queries_per_source``, so its source queries count under the same cap as the
    ingest runs reading those servers. Like a run, the check joins each server's shared lock (on its own
    connection, through ``join``), and takes the cap divided by the sessions reading the server now
    (``state_store.source_shares``); each database's detection then holds one permit of its server
    (``parallel.SourceLimiter``), since a database is asked one query at a time. ``source_of(cfg)`` names a row's
    server as the runs count it (``ct_engine.source_key``). A cap of 0 caps nothing."""

    def __init__(self, cap: int, join: Callable[[Iterable[str]], Dict[str, int]],
                 runs: Callable[[Iterable[int]], Dict[int, int]], source_of: Callable[[dict], str]):
        self.cap = int(cap or 0)
        self.limiter = parallel.SourceLimiter()
        self._join, self._runs, self._source_of = join, runs, source_of
        self._keys: Dict[str, int] = {}
        self._mu = threading.Lock()

    def wrap(self, configs: List[dict], detect_one: Callable) -> Callable:
        """``detect_one`` for one collection's rows, holding a permit of each database's server while it asks it."""
        if self.cap <= 0:
            return detect_one
        with self._mu:
            new = {self._source_of(c) for c in configs} - set(self._keys)
            if new:
                self._keys.update(self._join(new))
            self._refresh()

        def capped(members):
            with self.limiter.hold([self._source_of(members[0][0])]):
                return detect_one(members)
        return capped

    def _refresh(self) -> None:
        try:
            shares = ss.source_shares(self.cap, self._keys, self._runs(self._keys.values()))
        except Exception as e:   # the sessions can't be counted: the check alone takes the whole cap
            print(f"[caps] could not count the runs per source ({str(e)[:200]}); the check takes the whole cap")
            shares = {source: self.cap for source in self._keys}
        for source, share in shares.items():
            self.limiter.set_limit(source, share)


def run_check(store, lock_store, collections: List[str], detect_one: Callable, *,
              existing_sinks_of: Callable, job_run_id: str = "", cluster_id: str = "", parallelism: int = 32,
              interval_s: float = 0, reseed_on_new_columns: bool = True, snapshot_isolation: bool = False,
              force=frozenset(), map_fn=map, collection_workers: int = 8, source_cap: Optional[SourceCap] = None,
              hold_work_locks: bool = False, share_locks: bool = False) -> dict:
    """Check every collection of the group and write the unchanged databases' stamps.

    ``existing_sinks_of(configs) -> set or None`` lists the sink tables of a collection's rows (the
    notebook passes ``ct_engine.existing_sinks``); see ``check_collection``.

    Each collection is checked under its lock (``lock_store``'s session holds it), with an
    ``ingest_run`` row whose ``detail.check`` records what was found; a collection whose lock another
    sweep holds is left to that sweep and listed as busy. ``map_fn`` runs ``detect_one`` over each
    collection's databases (pass a shared thread pool's map to check databases of every collection at
    once); up to ``collection_workers`` collections are checked at a time. ``source_cap`` counts the check's
    source queries under ``max_queries_per_source``.

    A collection whose check fails is marked failed and listed in ``failed`` with its error, and starts no run;
    the other collections are checked as usual, so one bad row doesn't hold up the whole job. With
    ``hold_work_locks`` a collection with work keeps its lock (listed in ``held``) until the caller has started
    its run and releases it, so a second check of the same collection meanwhile finds it busy and starts
    nothing.

    With ``share_locks`` (a cycle shared out by its changes, ``plan_cycle``) the check holds each collection's lock
    shared, as a work-list run does, and an exclusive lock on each database while it checks it: a database a run of
    an earlier cycle still holds is left out (``busy_databases``) and checked again next cycle, the rest are checked
    as usual, and every lock is let go when the collection is done (``hold_work_locks`` doesn't apply: the runs take
    their databases' locks themselves, and a database two overlapping checks both give out is taken by one run and
    skipped by the other). Each result also has the collection's ``tables``, for the job's group size.

    Returns the summary; ``per_collection`` holds each checked collection's ``work``.
    """
    mixed_sinks = store.mixed_sinks()

    def check_one(collection):
        started = time.monotonic()
        if not (lock_store.try_lock_collection_shared(collection) if share_locks
                else lock_store.try_lock_collection(collection)):
            return None
        run_id, keep_lock, held, busy = None, False, [], []
        try:
            # Marked as the check's own record from the start, so a monitor tells it from a group run while it runs.
            run_id = store.start_run(collection, job_run_id, cluster_id, parallelism, work_id=f"check-{job_run_id}")
            if not share_locks or lock_store.alone_on_collection(collection):
                _close_abandoned_runs(store, collection, run_id)
            configs = store.list_task_configs(collection)
            tables = len(configs)
            if share_locks:
                keys = planner.work_database_keys(collection, planner.database_keys(configs))
                held = sorted(lock_store.try_lock_databases(keys))
                free, busy = planner.work_after_locks(planner.database_keys(configs), keys, set(held))
                configs = [c for c in configs if planner.database_key(c) in free]
            checkpoints = store.get_checkpoints([c["id"] for c in configs])
            detect = source_cap.wrap(configs, detect_one) if source_cap else detect_one
            result = check_collection(configs, checkpoints, detect, existing_sinks=existing_sinks_of(configs),
                                      stamps=store.get_database_checks(collection), interval_s=interval_s,
                                      mixed_sinks=mixed_sinks,
                                      reseed_on_new_columns=reseed_on_new_columns,
                                      snapshot_isolation=snapshot_isolation, force=force, map_fn=map_fn)
            if not lock_store.is_alive():
                raise RuntimeError("the collection's lock connection dropped during the check, so the lock is gone "
                                   "and nothing was written")
            writes_started = time.monotonic()
            store.bulk_advance_unchanged(result["rows"])
            store.stamp_databases(collection, result["database_stamps"], run_id)
            # The rows and stamps above move last successes: their databases' rollups are recomputed.
            database_of = {int(c["id"]): c.get("src_database") for c in configs}
            store.rewrite_rollups(collection, {database for _, database, _, _ in result["database_stamps"]}
                                  | {database_of[int(row[0])] for row in result["rows"]})
            check = {k: result[k] for k in ("work", "weights", "unchanged_databases", "unchanged_tables", "work_tables",
                                            "detected", "queries", "fast_exits")}
            check.update(tables=tables, busy_databases=len(busy),
                         table_rows=len(result["rows"]), database_stamps=len(result["database_stamps"]),
                         write_ms=int((time.monotonic() - writes_started) * 1000),
                         check_s=round(time.monotonic() - started, 1))
            unchanged = result["unchanged_tables"]
            store.finish_run(run_id, total=unchanged, ok=unchanged, failed=0, skipped=unchanged, status="completed",
                             detail=json.dumps({"check": check}))
            keep_lock = hold_work_locks and not share_locks and bool(check["work"])
            return {"collection": collection, "run_id": run_id, **check}
        except Exception as error:
            if run_id is not None:
                _finish_failed(store, run_id, error)
            return {"collection": collection, "error": f"{type(error).__name__}: {str(error)[:400]}"}
        except BaseException as error:      # the step itself is stopped: nothing more is checked
            if run_id is not None:
                _finish_failed(store, run_id, error)
            raise
        finally:
            if share_locks:
                _release_shared(lock_store, collection, held)
            elif not keep_lock:
                _release_lock(lock_store, collection)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, min(len(collections), collection_workers)),
                            thread_name_prefix="collection") as pool:
        checked = list(pool.map(check_one, collections))
    failed = [r for r in checked if r is not None and "error" in r]
    results = [r for r in checked if r is not None and "error" not in r]
    return {"collections": len(collections), "busy": [c for c, r in zip(collections, checked) if r is None],
            "failed": failed,
            "held": sorted(r["collection"] for r in results if hold_work_locks and not share_locks and r["work"]),
            "busy_databases": sum(r.get("busy_databases", 0) for r in results),
            "check_s": round(time.monotonic() - started, 1),
            "collections_with_work": sum(1 for r in results if r["work"]),
            "databases_with_work": sum(len(r["work"]) for r in results),
            "unchanged_databases": sum(r["unchanged_databases"] for r in results),
            "unchanged_tables": sum(r["unchanged_tables"] for r in results),
            "table_rows": sum(r["table_rows"] for r in results),
            "database_stamps": sum(r["database_stamps"] for r in results),
            "per_collection": results}


def _close_abandoned_runs(store, collection: str, run_id) -> None:
    """Close the collection's runs a dead sweep or check left open. The check holds the collection's lock
    here, as the sweep does before it starts. Telemetry only: a failure never stops the check."""
    try:
        store.close_abandoned_runs(collection, run_id)
    except Exception as e:
        print(f"[telemetry] could not close earlier unfinished runs of {collection}: {str(e)[:200]}")


def release_locks(lock_store, collections: Iterable[str]) -> None:
    """Release the locks ``run_check`` held for collections with work (``held``), once their runs are started."""
    for collection in collections:
        _release_lock(lock_store, collection)


def failure_message(failed: List[dict]) -> str:
    """One line per collection whose check failed, for the step's error."""
    return "\n".join([f"{len(failed)} collection(s) could not be checked and started no run:"]
                     + [f"  {r['collection']}: {r['error']}" for r in failed])


def _release_lock(lock_store, collection: str) -> None:
    """Release the collection's lock. Best effort: a dropped lock connection has already let go of it, and
    a failure here must not replace the outcome of the check."""
    try:
        lock_store.unlock_collection(collection)
    except Exception as e:
        print(f"[telemetry] could not release the lock of {collection}: {str(e)[:200]}")


def _release_shared(lock_store, collection: str, database_keys: List[str]) -> None:
    """Let go of a collection's shared lock and the database locks the check held. Best effort, as _release_lock."""
    try:
        lock_store.unlock_databases(database_keys)
        lock_store.unlock_collection_shared(collection)
    except Exception as e:
        print(f"[telemetry] could not release the locks of {collection}: {str(e)[:200]}")


def _finish_failed(store, run_id, error: BaseException) -> None:
    """Mark the check's run failed, with what stopped it. Best effort: the error that stopped the check is
    the one that goes on."""
    detail = json.dumps({"check": {"error": f"{type(error).__name__}: {str(error)[:400]}"}})
    try:
        store.finish_run(run_id, total=0, ok=0, failed=0, status="failed", detail=detail)
    except Exception as e:
        print(f"[telemetry] could not mark check run {run_id} failed: {str(e)[:200]}")
