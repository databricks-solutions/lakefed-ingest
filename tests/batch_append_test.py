"""Batched appends: which plans batch, how failures are split, and the batch runner (with fakes)."""
import pytest

import batch_append as ba
import ct_engine as ce
import parallel
import state_store as ss


def _cfg(cid, sink="orders", client="auto", mode="append", **kw):
    return dict({"id": cid, "src_host": "h1", "src_database": f"db{cid}", "src_schema": "dbo", "src_table": sink,
                 "secret_key": f"k{cid}", "select_list": "id, v", "primary_key": "id", "sink_catalog": "main",
                 "sink_schema": "bronze", "sink_table": sink, "write_mode": mode,
                 "client_id": f"eng-{cid}" if client == "auto" else client}, **kw)


def _plan(cid, route="merge", cur=150, **kw):
    cp = {"position": "100", "phase": ss.INCREMENTAL, "strategy": "ct", "select_list": "id, v", "ct_begin_version": 7}
    return {"cfg": _cfg(cid, **kw), "route": route, "cp": cp, "cur": cur}


def _ids(plans):
    return [p["cfg"]["id"] for p in plans]


# ---- planning ----------------------------------------------------------------------------------------

def test_plan_batches_groups_append_plans_by_sink_and_kind():
    plans = [_plan(1), _plan(2), _plan(3, sink="lines"), _plan(4, route="seed"), _plan(5, route="seed"),
             _plan(6, mode="merge"), _plan(7, client=None), _plan(8, load_partitioned=True),
             _plan(9, route="fallback"), _plan(10, sink="lines")]
    batches, rest = ba.plan_batches(plans, 64)
    assert sorted((b["kind"], _ids(b["members"])) for b in batches) == [
        ("changes", [1, 2]), ("changes", [3, 10]), ("seed", [4, 5])]
    assert _ids(rest) == [6, 7, 8, 9]                 # merge mode, dedicated, partitioned, other routes
    assert ba.plan_batches(plans, 1) == ([], plans)   # batching off


def test_plan_batches_splits_evenly_and_leaves_singles_per_table():
    plans = [_plan(i) for i in range(1, 8)]
    batches, rest = ba.plan_batches(plans, 3)
    assert [len(b["members"]) for b in batches] == [3, 2, 2] and rest == []
    batches, rest = ba.plan_batches(plans[:3], 2)
    assert [len(b["members"]) for b in batches] == [2] and _ids(rest) == [3]
    assert ba.even_chunks(list(range(65)), 64) == [list(range(33)), list(range(33, 65))]


def test_plan_batches_never_reads_more_tables_at_once_than_parallelism():
    # A batch is one Spark job, so its reads all run at once: a batch can't hold more of them than
    # the run's slots, or it would query sources beyond parallelism while holding only that many.
    plans = [_plan(i) for i in range(1, 8)]
    batches, rest = ba.plan_batches(plans, 64, max_reads=3)
    assert [len(b["members"]) for b in batches] == [3, 2, 2] and rest == []
    assert ba.plan_batches(plans, 64, max_reads=1) == ([], plans)


def test_order_work_flatten_and_describe():
    b_changes = {"kind": "changes", "members": [_plan(1), _plan(2)]}
    b_seed = {"kind": "seed", "members": [_plan(3, route="seed"), _plan(4, route="seed")]}
    work = ba.order_work([b_seed, b_changes], [_plan(5, mode="merge")])
    assert [(kind, ba.work_id((kind, p))) for kind, p in work] == [
        ("batch", "batch of 2"), ("table", 5), ("batch", "batch of 2")]
    assert work[0][1] is b_changes and work[2][1] is b_seed      # changes first, seeds last
    results = [{"item": "batch of 2", "status": "ok", "detail": {"batch": True, "members": [
                   {"item": 1, "status": "ok", "detail": "incremented"},
                   {"item": 2, "status": "failed", "detail": "x"}]}},
               {"item": 5, "status": "ok", "detail": "incremented"},
               {"item": "batch of 2", "status": "failed", "detail": "boom"}]
    flat = ba.flatten(results, work)
    assert [(r["item"], r["status"]) for r in flat] == [(1, "ok"), (2, "failed"), (5, "ok"), (3, "failed"),
                                                        (4, "failed")]
    assert ba.describe([b_seed, b_changes]) == {"seed": {"batches": 1, "tables": 2},
                                                 "changes": {"batches": 1, "tables": 2}}


# ---- splitting failed batches -------------------------------------------------------------------------

def test_write_with_bisect_isolates_a_bad_member():
    calls = []

    def write(group):
        calls.append(_ids(group))
        if 3 in _ids(group):
            raise RuntimeError("table 3 is broken")
    written, single, unknown = ba.write_with_bisect([_plan(i) for i in range(1, 7)], write, committed=lambda g: [])
    assert sorted(_ids(written)) == [1, 2, 4, 5, 6] and _ids(single) == [3] and unknown == []
    assert calls == [[1, 2, 3, 4, 5, 6], [1, 2, 3], [1], [2, 3], [2], [3], [4, 5, 6]]


def test_write_with_bisect_counts_rows_that_landed_despite_the_error():
    def write(group):
        raise RuntimeError("Committing to the Delta table succeeded but error while executing post-commit hook")
    written, single, unknown = ba.write_with_bisect([_plan(1), _plan(2)], write, committed=lambda g: list(g))
    assert _ids(written) == [1, 2] and single == [] and unknown == []


def test_write_with_bisect_leaves_members_for_the_next_run_when_the_check_fails():
    def boom(_):
        raise RuntimeError("sink unreadable")
    written, single, unknown = ba.write_with_bisect([_plan(1), _plan(2)], boom, committed=boom)
    assert written == [] and single == [] and [m["cfg"]["id"] for m, _ in unknown] == [1, 2]
    assert "checking the sink" in unknown[0][1]


def test_write_with_bisect_stops_when_the_lock_is_lost():
    def write(group):
        raise ba.LockLost("collection lock lost")
    with pytest.raises(ba.LockLost):
        ba.write_with_bisect([_plan(1), _plan(2)], write, committed=lambda g: [])


# ---- the runner ---------------------------------------------------------------------------------------

class _Engine:
    """ct_engine with its Spark calls faked: appends are recorded; reads of ``bad`` ids fail; ``landed``
    ids are found in the sink after a failure."""
    sink_fqn = staticmethod(ce.sink_fqn)
    client_id = staticmethod(ce.client_id)
    change_read = staticmethod(ce.change_read)
    snapshot_read = staticmethod(ce.snapshot_read)

    def __init__(self, bad=(), landed=(), ensure_fails=False, prep=None):
        self.bad, self.landed, self.ensure_fails, self.prep = set(bad), set(landed), ensure_fails, prep or {}
        self.appends = []

    def ensure_sink_table(self, spark, cfg, sink, slots=None):
        if self.ensure_fails:
            raise RuntimeError("could not create the sink")

    def seed_prep(self, spark, cfgs, slots=None):
        return {int(c["id"]): self.prep.get(int(c["id"]), (500, 1, 7)) for c in cfgs}

    def append_batch(self, spark, sink, reads, slots=None):
        ids = [int(r[0]["id"]) for r in reads]
        if self.bad & set(ids):
            raise RuntimeError(f"read failed for {sorted(self.bad & set(ids))}")
        self.appends.append((sink, ids, [r[3] for r in reads]))
        return len(ids)

    def committed_clients(self, spark, sink, cfgs, run_id, slots=None):
        return {ce.client_id(c) for c in cfgs if int(c["id"]) in self.landed}


class _Store:
    """Records every StateStore call; ``advance_many`` can be made to fail; ``stopping_run`` answers ``stopping``."""
    def __init__(self, fail_advance=False, stopping=False):
        self.calls, self.fail_advance, self.stopping = [], fail_advance, stopping

    def __getattr__(self, name):
        def record(*args, **kw):
            if name == "advance_many" and self.fail_advance:
                raise RuntimeError("Lakebase unavailable")
            self.calls.append((name, args, kw))
            if name == "stopping_run":
                return self.stopping
        return record

    def call(self, name):
        return next(c for c in self.calls if c[0] == name)


def _runner(engine, store, alone, guard=lambda: None, outcome="incremented"):
    return ba.BatchRunner(None, store, None, 42, per_table=lambda p: alone.append(p["cfg"]["id"]) or outcome,
                          guard=guard, engine=engine)


def test_change_batch_records_pending_then_writes_once_and_advances():
    engine, store, alone = _Engine(), _Store(), []
    out = _runner(engine, store, alone).run({"kind": "changes", "members": [_plan(1), _plan(2, cur=160)]})
    assert [(r["item"], r["status"], r["detail"]) for r in out["members"]] == [(1, "ok", "incremented"),
                                                                             (2, "ok", "incremented")]
    assert [a[1] for a in engine.appends] == [[1, 2]] and alone == []
    assert engine.appends[0][2][0] == {"run_id": 42, "changes": True}
    names = [c[0] for c in store.calls]
    assert names[0] == "record_pending_many" and names[-1] == "record_finished_events"
    items, run_id = store.call("record_pending_many")[1]
    assert [(cfg["id"], pos) for cfg, pos in items] == [(1, 150), (2, 160)] and run_id == 42
    advanced = store.call("advance_many")[1][0]
    assert [(cfg["id"], pos, phase) for cfg, pos, phase, _ in advanced] == [(1, 150, ss.INCREMENTAL),
                                                                          (2, 160, ss.INCREMENTAL)]
    events = store.call("record_finished_events")[1][1]
    assert [(e[0]["id"], e[1], e[3], e[4], e[5], e[6], e[7]) for e in events] == [
        (1, "increment", "merge", "ok", 100, 150, "batch2"), (2, "increment", "merge", "ok", 100, 160, "batch2")]


def test_a_member_that_fails_on_its_own_runs_per_table():
    engine, store, alone = _Engine(bad={2}), _Store(), []
    out = _runner(engine, store, alone).run({"kind": "changes", "members": [_plan(1), _plan(2), _plan(3)]})
    assert alone == [2] and sorted(i for a in engine.appends for i in a[1]) == [1, 3]
    assert [r["status"] for r in out["members"]] == ["ok", "ok", "ok"]


def test_a_batch_that_committed_before_its_statement_failed_is_not_written_again():
    engine, store, alone = _Engine(bad={1, 2}, landed={1, 2}), _Store(), []
    out = _runner(engine, store, alone).run({"kind": "changes", "members": [_plan(1), _plan(2)]})
    assert alone == [] and engine.appends == [] and [r["detail"] for r in out["members"]] == ["incremented"] * 2
    assert [cfg["id"] for cfg, *_ in store.call("advance_many")[1][0]] == [1, 2]


def test_without_its_sink_every_member_runs_per_table():
    engine, store, alone = _Engine(ensure_fails=True), _Store(), []
    out = _runner(engine, store, alone).run({"kind": "changes", "members": [_plan(1), _plan(2)]})
    assert alone == [1, 2] and engine.appends == [] and store.calls == []
    assert [r["status"] for r in out["members"]] == ["ok", "ok"]


def test_seed_batch_uses_each_members_v0_and_leaves_tables_that_cant_seed_to_the_per_table_worker():
    engine, store, alone = _Engine(prep={2: (500, None, None)}), _Store(), []    # CT is off on table 2
    seeds = [_plan(i, route="seed") for i in (1, 2, 3)]
    out = _runner(engine, store, alone, outcome="seeded").run({"kind": "seed", "members": seeds})
    assert alone == [2] and [a[1] for a in engine.appends] == [[1, 3]]
    assert [meta["version"] for meta in engine.appends[0][2]] == [500, 500]
    assert [(cfg["id"], pos, phase, detail) for cfg, pos, phase, detail in store.call("advance_many")[1][0]] == [
        (1, 500, ss.INCREMENTAL, "seed complete"), (3, 500, ss.INCREMENTAL, "seed complete")]
    assert [(cfg["id"], bv) for cfg, bv in store.call("record_ct_begin_versions")[1][0]] == [(1, 7), (3, 7)]
    assert [r["detail"] for r in out["members"]] == ["seeded"] * 3


def test_full_batch_records_no_pending_and_checkpoints_loaded():
    engine, store, alone = _Engine(), _Store(), []
    fulls = [_plan(i, route="full", load_type="full") for i in (1, 2)]
    _runner(engine, store, alone, outcome="loaded").run({"kind": "full", "members": fulls})
    assert "record_pending_many" not in [c[0] for c in store.calls]
    assert engine.appends[0][2] == [{"run_id": 42, "version": None}] * 2
    assert {(pos, phase) for _, pos, phase, _ in store.call("advance_many")[1][0]} == {(None, ss.LOADED)}


def test_a_lost_lock_stops_the_batch_before_anything_is_written():
    engine, store, alone = _Engine(), _Store(), []

    def guard():
        raise RuntimeError("collection lock lost; table deferred to the next sweep")
    out = _runner(engine, store, alone, guard=guard).run({"kind": "changes", "members": [_plan(1), _plan(2)]})
    assert engine.appends == [] and alone == [] and store.calls == []
    assert all(r["status"] == "failed" and "lock lost" in r["detail"] for r in out["members"])


def test_written_members_whose_checkpoint_advance_failed_are_reported_failed():
    engine, store, alone = _Engine(), _Store(fail_advance=True), []
    out = _runner(engine, store, alone).run({"kind": "changes", "members": [_plan(1), _plan(2)]})
    assert [a[1] for a in engine.appends] == [[1, 2]]
    assert all(r["status"] == "failed" and "checkpoint advance failed" in r["detail"] for r in out["members"])
    assert store.call("record_failed_tables")[2] == {"stage": "checkpoint", "action": "increment"}


def test_a_member_failing_in_a_cancelled_run_is_reported_cancelled():
    engine, store, alone = _Engine(), _Store(fail_advance=True, stopping=True), []
    out = _runner(engine, store, alone).run({"kind": "changes", "members": [_plan(1), _plan(2)]})
    assert all(r["status"] == "cancelled" and "checkpoint advance failed" in r["detail"] for r in out["members"])


def test_a_member_stopped_by_a_cancel_when_it_runs_alone_is_reported_cancelled():
    def stopped(plan):
        raise parallel.Cancelled("stopped by the cancel")
    runner = ba.BatchRunner(None, _Store(), None, 42, per_table=stopped, guard=lambda: None,
                            engine=_Engine(ensure_fails=True))
    out = runner.run({"kind": "changes", "members": [_plan(1)]})
    assert out["members"] == [{"item": 1, "pool": None, "status": "cancelled", "detail": "stopped by the cancel"}]


# ---- row totals (ingest_run.detail) ------------------------------------------------------------------

def test_a_new_runner_has_written_no_rows():
    assert _runner(_Engine(), _Store(), []).row_totals() == {"batch_rows": 0, "batch_rows_unknown": 0}


def test_the_runner_adds_up_the_rows_of_every_batch_it_commits():
    engine, store, alone = _Engine(bad={2}), _Store(), []
    runner = _runner(engine, store, alone)
    runner.run({"kind": "changes", "members": [_plan(1), _plan(2), _plan(3)]})
    runner.run({"kind": "changes", "members": [_plan(4, sink="lines"), _plan(5, sink="lines")]})
    committed = sum(len(ids) for _, ids, _ in engine.appends)  # the fake commits one row per table
    assert committed == 4 and alone == [2]
    assert runner.row_totals() == {"batch_rows": 4, "batch_rows_unknown": 0}


def test_tables_whose_rows_landed_despite_the_error_are_counted_as_unknown():
    engine, store, alone = _Engine(bad={1, 2}, landed={1, 2}), _Store(), []
    runner = _runner(engine, store, alone)
    runner.run({"kind": "changes", "members": [_plan(1), _plan(2)]})
    assert runner.row_totals() == {"batch_rows": 0, "batch_rows_unknown": 2}


def test_row_totals_add_up_across_batches_running_at_once():
    from concurrent.futures import ThreadPoolExecutor
    engine, store, alone = _Engine(), _Store(), []
    runner = _runner(engine, store, alone)
    batches = [{"kind": "changes", "members": [_plan(i, sink=f"s{i}"), _plan(i + 1000, sink=f"s{i}")]}
               for i in range(200)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(runner.run, batches))
    assert runner.row_totals() == {"batch_rows": 400, "batch_rows_unknown": 0}
