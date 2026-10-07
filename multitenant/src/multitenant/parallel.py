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

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, List, Optional


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
        {"item": <id>, "pool": <pool>, "status": "ok"|"failed", "detail": <worker return | error>}
    """
    items = list(items)
    max_parallel = max(1, int(max_parallel))
    id_of = item_id or (lambda x: x)

    def _run(pair):
        idx, item = pair
        pool = f"pool{idx % max_parallel}"
        if set_pool is not None:
            set_pool(pool)
        rec = {"item": id_of(item), "pool": pool, "status": "ok", "detail": None}
        try:
            rec["detail"] = worker(item)
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
    """Count results by status and by worker outcome (the 'detail' string on success)."""
    summary = {"total": len(results), "ok": 0, "failed": 0, "by_outcome": {}}
    for r in results:
        if r.get("status") == "failed":
            summary["failed"] += 1
        else:
            summary["ok"] += 1
            outcome = str(r.get("detail"))
            summary["by_outcome"][outcome] = summary["by_outcome"].get(outcome, 0) + 1
    return summary
