"""Batched appends: the change sets, seeds and full loads of many source databases appended to ONE
sink in one Spark job and one Delta commit (append mode, consolidated sinks).

In a large append estate most of a run is per-table overhead: a Spark job and a Delta commit for a
handful of changed rows, thousands of times per sink. Control rows that append to the same
consolidated sink (``client_id`` set) only ever add their own client's rows, so their reads can run
together: one JDBC read per source database, unioned by name, one INSERT (``ct_engine.append_batch``).
Job parameter ``append_batch_size`` caps the members per batch; 1 (the default) turns batching off.

* ``plan_batches`` (pure): which plans batch (write_mode append, client_id set, not partitioned;
  routes merge / seed / full) and how: by (sink, kind), split evenly. The rest, and any group of one,
  go to the per-table worker unchanged.
* ``write_with_bisect`` (pure): one write per batch. After a failure it first asks the sink which
  members' rows landed anyway (a commit can succeed and the statement still fail), then splits the
  rest in halves and retries, down to single members, which go to the per-table worker. A bad
  database costs a few smaller batches, not the whole batch, and fails exactly as it would alone.
* ``BatchRunner``: runs one batch: records the pending appends (ct), writes, then advances the
  members' checkpoints and records their telemetry in bulk (one Lakebase round trip each).

Telemetry: a batch member gets one finished ``ingest_event`` (no 'running' row while the batch is in
flight). ``rows_merged`` is NULL: a batch's commit reports one total, printed with the batch and added
to the run's ``BatchRunner.row_totals()``, which the run writes into ``ingest_run.detail``.
``pool`` reads ``batch<size>``.

Pure Python apart from what ``BatchRunner`` calls in ``ct_engine``, unit-tested with fakes in
tests/batch_append_test.py.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import ct_engine
import parallel
import planner
import state_store as ss

# Plan routes that batch, and the batch kind each becomes; per kind, the telemetry action and stage.
KINDS = {"merge": "changes", "seed": "seed", "full": "full"}
_ACTION_STAGE = {"changes": ("increment", "merge"), "seed": ("seed", "seed"), "full": ("full", "load")}
_OUTCOME = {"changes": "incremented", "seed": "seeded", "full": "loaded"}


class LockLost(Exception):
    """The run lost its collection lock: stop the batch without writing anything more."""


def batch_kind(plan: dict) -> Optional[str]:
    """The batch kind of a plan ('changes', 'seed', 'full'), or None when it runs per table."""
    cfg = plan["cfg"]
    if plan.get("route") not in KINDS:
        return None
    try:
        if not ct_engine.is_append(cfg) or ct_engine.client_id(cfg) is None:
            return None
    except ValueError:
        return None
    return None if ct_engine._is_partitioned(cfg) else KINDS[plan["route"]]


def even_chunks(items: list, size: int) -> List[list]:
    """``items`` in the fewest chunks of at most ``size``, as even as possible (65 by 64: 33 + 32)."""
    if not items:
        return []
    n = math.ceil(len(items) / max(1, int(size)))
    q, r = divmod(len(items), n)
    out, i = [], 0
    for k in range(n):
        step = q + (1 if k < r else 0)
        out.append(items[i:i + step])
        i += step
    return out


def plan_batches(plans: List[dict], batch_size: int, max_reads: Optional[int] = None) -> Tuple[List[dict], List[dict]]:
    """Split ``plans`` into (batches, rest). A batch is {"kind", "members": [plan, ...]}: up to
    ``batch_size`` plans of one kind appending to one sink, and never more than ``max_reads`` (the
    run's parallelism): a batch's reads run at once in one Spark job, so it can't hold more of them
    than the run's slots. ``rest`` keeps every other plan in order, plus groups (and chunks) of one.
    ``batch_size`` <= 1: no batches."""
    batch_size = int(batch_size or 1)
    if max_reads is not None:
        batch_size = min(batch_size, int(max_reads))
    if batch_size <= 1:
        return [], list(plans)
    groups: Dict[tuple, list] = {}
    rest = []
    for p in plans:
        kind = batch_kind(p)
        if kind is None:
            rest.append(p)
        else:
            groups.setdefault((planner.sink_key(p["cfg"]), kind), []).append(p)
    batches = []
    for (_, kind), members in groups.items():
        for chunk in even_chunks(members, batch_size):
            if len(chunk) > 1:
                batches.append({"kind": kind, "members": chunk})
            else:
                rest.extend(chunk)
    return batches, rest


def order_work(batches: List[dict], plans: List[dict]) -> list:
    """The run's work items, ("batch", batch) or ("table", plan): change and full-load batches first
    (tables already onboarded stay fresh), then the per-table plans, then seed batches (onboarding)."""
    return ([("batch", b) for b in batches if b["kind"] != "seed"] + [("table", p) for p in plans]
            + [("batch", b) for b in batches if b["kind"] == "seed"])


def work_id(item) -> object:
    kind, payload = item
    return f"batch of {len(payload['members'])}" if kind == "batch" else payload["cfg"]["id"]


def flatten(results: List[dict], work: list) -> List[dict]:
    """``run_parallel`` results over ``work`` -> one result per TABLE (batch members expanded), for
    ``parallel.summarize``. A batch whose runner itself raised counts each member as failed."""
    out = []
    for (kind, payload), r in zip(work, results):
        if kind != "batch":
            out.append(r)
        elif r["status"] == "ok":
            out.extend(r["detail"]["members"])
        else:
            out.extend({"item": m["cfg"]["id"], "pool": r.get("pool"), "status": "failed", "detail": r["detail"]}
                       for m in payload["members"])
    return out


def describe(batches: List[dict]) -> dict:
    """Run-detail summary: {kind: {"batches", "tables"}}."""
    out: Dict[str, dict] = {}
    for b in batches:
        d = out.setdefault(b["kind"], {"batches": 0, "tables": 0})
        d["batches"] += 1
        d["tables"] += len(b["members"])
    return out


def _id(plan: dict) -> int:
    return int(plan["cfg"]["id"])


def _line(e: BaseException) -> str:
    text = str(e)
    return (text.splitlines()[0] if text else repr(e))[:300]


def write_with_bisect(members: List[dict], write: Callable, committed: Callable):
    """Write ``members`` with ``write(group)`` (one commit per call), splitting failed groups.

    After a failed write, ``committed(group)`` returns the members whose rows are in the sink anyway;
    they count as written. The rest is split in halves and retried, down to single members. Returns
    (written, single, unknown): members written by a batch; members that failed on their own (for
    the per-table worker); and [(member, error)] whose outcome couldn't be checked because
    ``committed`` failed too (left for the next run). ``LockLost`` from ``write`` propagates."""
    written, single, unknown = [], [], []
    stack = [list(members)]
    while stack:
        group = stack.pop()
        try:
            write(group)
        except LockLost:
            raise
        except Exception as error:
            try:
                landed = {_id(m) for m in committed(group)}
            except Exception as check_error:
                unknown.extend((m, f"batch append failed ({_line(error)}); checking the sink for its rows "
                                   f"failed too ({_line(check_error)})") for m in group)
                continue
            written.extend(m for m in group if _id(m) in landed)
            rest = [m for m in group if _id(m) not in landed]
            if len(rest) == 1:
                single.extend(rest)
            elif rest:
                half = len(rest) // 2
                stack.append(rest[half:])
                stack.append(rest[:half])
            continue
        written.extend(group)
    return written, single, unknown


class BatchRunner:
    """Runs the batches of one ingest run (``run`` is the work item's worker).

    ``per_table(plan)`` is the per-table worker (returns the outcome, raises on failure): members that
    can't be batched or fail on their own run through it, so they fail with the same stages and
    errors as without batching. ``guard()`` raises when the run lost its collection lock; it is
    checked before every commit point, and a lost lock stops the batch (its pending appends are
    checked by the next run)."""

    def __init__(self, spark, store, slots, run_id, per_table: Callable, guard: Callable = lambda: None,
                 engine=ct_engine):
        self.spark, self.store, self.slots, self.run_id = spark, store, slots, run_id
        self.per_table, self.guard, self.engine = per_table, guard, engine
        self._totals_lock = threading.Lock()   # batches run on many threads at once
        self._batch_rows = 0
        self._batch_rows_unknown = 0

    def row_totals(self) -> dict:
        """Rows this run's batches committed, from each commit's own metrics, and the number of tables
        whose rows landed in a commit that reported an error (their rows aren't counted). The run
        writes both into ``ingest_run.detail``."""
        with self._totals_lock:
            return {"batch_rows": self._batch_rows, "batch_rows_unknown": self._batch_rows_unknown}

    def run(self, batch: dict) -> dict:
        """Run one batch. Returns {"batch": True, "members": [one run_parallel-style result each]}."""
        started = time.monotonic()
        kind, size = batch["kind"], len(batch["members"])
        results: Dict[int, dict] = {}
        sink = self.engine.sink_fqn(batch["members"][0]["cfg"])
        members, alone = list(batch["members"]), []
        try:
            self.engine.ensure_sink_table(self.spark, members[0]["cfg"], sink, slots=self.slots)
        except Exception:                 # no batch without the sink: each member reports why on its own
            members, alone = [], members
        try:
            if kind == "seed" and members:
                members, not_ready = self._prepare_seeds(members)
                alone += not_ready
            if members:
                self._check_lock()
                if kind != "full":            # ct: record the pending appends BEFORE writing
                    self.store.record_pending_many([(m["cfg"], self._target(kind, m)) for m in members],
                                                   self.run_id)
                written, single, unknown = write_with_bisect(
                    members, write=lambda group: self._write(kind, sink, group),
                    committed=lambda group: self._committed(kind, sink, group))
                alone += single
                self._finish(kind, written, unknown, results, int((time.monotonic() - started) * 1000), size)
        except LockLost as e:
            for m in batch["members"]:
                results.setdefault(_id(m), _failed(m, str(e)))
            alone = []
        for m in alone:
            results[_id(m)] = self._run_alone(m)
        return {"batch": True, "members": [results[_id(m)] for m in batch["members"]]}

    # ---- steps -----------------------------------------------------------------------------------
    def _check_lock(self):
        try:
            self.guard()
        except Exception as e:
            raise LockLost(str(e)) from e

    @staticmethod
    def _target(kind: str, m: dict):
        """The position a member's append brings its checkpoint to."""
        return {"changes": m.get("cur"), "seed": m.get("v0")}.get(kind)

    def _prepare_seeds(self, members: List[dict]):
        """(ready, not_ready): each member's V0, captured BEFORE the seed read, and its table's CT
        state. A member whose database or table can't seed runs per table, which fails it with the
        reason (CT disabled, unreachable, ...)."""
        prep = self.engine.seed_prep(self.spark, [m["cfg"] for m in members], slots=self.slots)
        ready, not_ready = [], []
        for m in members:
            p = prep.get(_id(m))
            if isinstance(p, tuple) and p[0] is not None and p[1] is not None:
                ready.append(dict(m, v0=p[0], bv=p[2]))
            else:
                not_ready.append(m)
        return ready, not_ready

    def _write(self, kind: str, sink: str, group: List[dict]) -> int:
        self._check_lock()
        e = self.engine
        if kind == "changes":
            reads = [e.change_read(m["cfg"], planner.position_from(m), m["cur"], self.run_id) for m in group]
        else:
            reads = [e.snapshot_read(m["cfg"], self.run_id, m.get("v0")) for m in group]
        rows = e.append_batch(self.spark, sink, reads, slots=self.slots)
        print(f"[batch:{kind}] {sink}: {len(group)} tables, {rows} rows")
        with self._totals_lock:
            self._batch_rows += int(rows or 0)
        return rows

    def _committed(self, kind: str, sink: str, group: List[dict]) -> List[dict]:
        print(f"[batch:{kind}] {sink}: append of {len(group)} tables failed -> checking the sink, then splitting")
        landed = self.engine.committed_clients(self.spark, sink, [m["cfg"] for m in group], self.run_id,
                                               slots=self.slots)
        written = [m for m in group if self.engine.client_id(m["cfg"]) in landed]
        with self._totals_lock:
            self._batch_rows_unknown += len(written)
        return written

    def _finish(self, kind, written, unknown, results, duration_ms, size):
        """Advance the written members' checkpoints and record their telemetry (bulk); record the
        members whose outcome is unknown as failed (their pending appends are checked next run)."""
        action, stage = _ACTION_STAGE[kind]
        if unknown:
            self._record_failed(unknown, "verify", action, results)
        if not written:
            return
        self._check_lock()
        try:
            if kind == "changes":
                self.store.advance_many([(m["cfg"], m["cur"], ss.INCREMENTAL, None) for m in written])
                self.store.record_ct_begin_versions([(m["cfg"], m.get("ct_begin")) for m in written
                                                     if (m.get("cp") or {}).get("ct_begin_version") is None])
            elif kind == "seed":
                self.store.advance_many([(m["cfg"], m["v0"], ss.INCREMENTAL, "seed complete") for m in written])
                self.store.record_ct_begin_versions([(m["cfg"], m.get("bv")) for m in written])
            else:
                self.store.advance_many([(m["cfg"], None, ss.LOADED, "full load complete") for m in written])
        except Exception as e:            # written, not checkpointed: ct appends stay pending for next run
            self._record_failed([(m, f"checkpoint advance failed after the batch append: {_line(e)}")
                                 for m in written], "checkpoint", action, results)
            return
        pool = f"batch{size}"
        for m in written:
            results[_id(m)] = {"item": m["cfg"]["id"], "pool": pool, "status": "ok", "detail": _OUTCOME[kind]}
        try:   # telemetry only: the tables are ingested and checkpointed
            self.store.record_finished_events(self.run_id, [
                (m["cfg"], action, (m.get("cp") or {}).get("phase") or ss.SEEDING, stage, "ok",
                 planner.position_from(m), self._target(kind, m), pool, None) for m in written], duration_ms)
        except Exception as e:
            print(f"[telemetry] batch of {len(written)}: record_finished_events failed (ingest unaffected): "
                  f"{_line(e)}")

    def _record_failed(self, items, stage, action, results):
        try:
            self.store.record_failed_tables(self.run_id, [(m["cfg"], m.get("cp"), err) for m, err in items],
                                            stage=stage, action=action)
        except Exception as e:
            print(f"[telemetry] batch: record_failed_tables failed: {_line(e)}")
        stopping = self._stopping()
        for m, err in items:
            results[_id(m)] = _failed(m, err, ss.failure_status(err, stopping))

    def _stopping(self) -> bool:
        """Whether the run is being cancelled. A failed read counts as not, so the error itself decides."""
        try:
            return self.store.stopping_run(self.run_id)
        except Exception:
            return False

    def _run_alone(self, m: dict) -> dict:
        try:
            return {"item": m["cfg"]["id"], "pool": None, "status": "ok", "detail": self.per_table(m)}
        except parallel.Cancelled as e:
            return _failed(m, str(e), "cancelled")
        except Exception as e:
            return _failed(m, str(e))


def _failed(m: dict, error: str, status: str = "failed") -> dict:
    return {"item": m["cfg"]["id"], "pool": None, "status": status, "detail": error}
