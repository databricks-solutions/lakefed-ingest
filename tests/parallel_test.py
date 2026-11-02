"""Unit tests for the in-process sweep engine (parallel.py). No Spark/cluster needed."""
import parallel


def test_run_parallel_one_result_per_item_in_order():
    items = ["a", "b", "c"]
    results = parallel.run_parallel(items, worker=lambda x: x.upper(), max_parallel=2)
    assert [r["item"] for r in results] == items          # order preserved
    assert [r["detail"] for r in results] == ["A", "B", "C"]
    assert all(r["status"] == "ok" for r in results)


def test_run_parallel_assigns_round_robin_pools():
    items = list(range(5))
    seen = []
    results = parallel.run_parallel(
        items, worker=lambda x: x, max_parallel=2, set_pool=seen.append
    )
    # Pool is deterministic by index: pool{i % max_parallel}.
    assert [r["pool"] for r in results] == ["pool0", "pool1", "pool0", "pool1", "pool0"]
    # set_pool is invoked once per item.
    assert sorted(seen) == ["pool0", "pool0", "pool0", "pool1", "pool1"]


def test_run_parallel_isolates_per_item_failures():
    def worker(x):
        if x == "boom":
            raise ValueError("kaboom")
        return "ok"

    results = parallel.run_parallel(["x", "boom", "y"], worker=worker, max_parallel=3)
    by_item = {r["item"]: r for r in results}
    assert len(results) == 3                               # a failure never drops items
    assert by_item["x"]["status"] == "ok"
    assert by_item["y"]["status"] == "ok"
    assert by_item["boom"]["status"] == "failed"
    assert "kaboom" in by_item["boom"]["detail"]


def test_run_parallel_item_id_callable():
    items = [{"id": 7, "v": "a"}, {"id": 9, "v": "b"}]
    results = parallel.run_parallel(
        items, worker=lambda c: c["v"], max_parallel=2, item_id=lambda c: c["id"]
    )
    assert [r["item"] for r in results] == [7, 9]


def test_run_parallel_empty():
    assert parallel.run_parallel([], worker=lambda x: x, max_parallel=4) == []


def test_summarize_counts_by_status_and_outcome():
    results = [
        {"item": 1, "status": "ok", "detail": "seeded"},
        {"item": 2, "status": "ok", "detail": "incremented"},
        {"item": 3, "status": "ok", "detail": "seeded"},
        {"item": 4, "status": "failed", "detail": "err"},
    ]
    s = parallel.summarize(results)
    assert s["total"] == 4
    assert s["ok"] == 3 and s["failed"] == 1
    assert s["by_outcome"] == {"seeded": 2, "incremented": 1}


def test_summarize_counts_a_cancelled_result_apart_from_the_failures():
    results = [
        {"item": 1, "status": "ok", "detail": "seeded"},
        {"item": 2, "status": "cancelled", "detail": "stopped"},
        {"item": 3, "status": "failed", "detail": "err"},
    ]
    s = parallel.summarize(results)
    assert s["ok"] == 1 and s["failed"] == 1 and s["cancelled"] == 1
    assert s["by_outcome"] == {"seeded": 1}


def test_run_parallel_records_a_cancelled_item_as_cancelled_and_a_crashed_one_as_failed():
    def worker(item):
        if item == 2:
            raise parallel.Cancelled("stopped by the cancel")
        if item == 3:
            raise RuntimeError("boom")
        return "done"
    results = parallel.run_parallel([1, 2, 3], worker, 2)
    assert [r["status"] for r in results] == ["ok", "cancelled", "failed"]
    assert results[1]["detail"] == "stopped by the cancel"


# ---- WorkSlots: ONE concurrency budget for all units of query work in a sweep -------------
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


def _hammer(slots, n_threads=24, n_units=200, hold=0.002):
    """Run many units across more threads than slots; record max concurrent + ids held at once."""
    mu = threading.Lock()
    state = {"cur": 0, "max": 0, "held": set(), "dupe": False}

    def unit(_):
        with slots.acquire() as sid:
            with mu:
                if sid in state["held"]:
                    state["dupe"] = True
                state["held"].add(sid)
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            time.sleep(hold)
            with mu:
                state["cur"] -= 1
                state["held"].discard(sid)

    with ThreadPoolExecutor(max_workers=n_threads) as ex:
        list(ex.map(unit, range(n_units)))
    return state


def test_workslots_never_exceeds_capacity_and_ids_unique():
    slots = parallel.WorkSlots(4)
    st = _hammer(slots)
    assert st["max"] <= 4 and slots.peak <= 4
    assert st["max"] == slots.peak and slots.peak >= 2     # actually ran concurrently
    assert not st["dupe"]                                  # a slot id is never held twice
    assert slots.in_flight == 0


def test_workslots_set_pool_uses_slot_id():
    seen = []
    slots = parallel.WorkSlots(3, set_pool=seen.append)
    with slots.acquire() as sid:
        assert seen == [f"pool{sid}"]
    assert all(p in {"pool0", "pool1", "pool2"} for p in seen)


def test_workslots_runs_on_when_the_pool_is_refused():
    # Standard access mode can refuse sparkContext: the units still run, without FAIR pools, and the
    # pool is asked for only until the first refusal.
    calls, ran = [], []

    def refuse(pool):
        calls.append(pool)
        raise RuntimeError("[JVM_ATTRIBUTE_NOT_SUPPORTED] sparkContext is not supported")

    slots = parallel.WorkSlots(2, set_pool=refuse)
    with slots.acquire():
        ran.append("one")
    with slots.acquire_many(2):
        ran.append("many")
    assert ran == ["one", "many"]
    assert len(calls) == 1
    assert "JVM_ATTRIBUTE_NOT_SUPPORTED" in slots.pool_error
    assert slots.in_flight == 0


def test_workslots_pool_error_is_none_when_pools_work():
    slots = parallel.WorkSlots(2, set_pool=lambda pool: None)
    with slots.acquire():
        pass
    assert slots.pool_error is None


def test_workslots_returns_slot_on_exception():
    slots = parallel.WorkSlots(1)
    with pytest.raises(ValueError):
        with slots.acquire():
            raise ValueError("boom")
    assert slots.in_flight == 0
    with slots.acquire():            # would block forever if the slot leaked
        pass


def test_workslots_nested_fanout_does_not_deadlock():
    """Table workers (outer) wait on partition futures WITHOUT holding a slot; partitions (inner)
    run on a separate executor of the same size and each hold one slot. Must finish, capped."""
    cap = 2
    slots = parallel.WorkSlots(cap)
    inner = ThreadPoolExecutor(max_workers=cap)

    def partition(_):
        with slots.acquire():
            time.sleep(0.005)
        return 1

    def table(_):
        with slots.acquire():          # a small unit of its own (e.g. bounds query)
            pass
        futs = [inner.submit(partition, i) for i in range(10)]
        return sum(f.result() for f in futs)   # waiting holds NO slot

    with ThreadPoolExecutor(max_workers=cap) as outer:
        fut = [outer.submit(table, t) for t in range(6)]
        results = [f.result(timeout=20) for f in fut]
    inner.shutdown(wait=True)
    assert results == [10] * 6
    assert slots.peak <= cap


def test_workslots_tracks_pools_per_thread():
    import threading
    slots = parallel.WorkSlots(4)
    slots.start_tracking()
    with slots.acquire():
        pass
    with slots.acquire():
        pass
    mine = slots.tracked_pools()
    assert mine and all(p.startswith("pool") for p in mine) and len(mine) == len(set(mine))
    assert slots.tracked_pools() == []              # tracking stops after reading

    slots.start_tracking()                          # another thread's units aren't mixed in

    def other_thread():
        with slots.acquire():
            pass
    t = threading.Thread(target=other_thread)
    t.start(); t.join()
    assert slots.tracked_pools() == []
    assert slots.in_flight == 0


def test_workslots_without_tracking_records_nothing():
    slots = parallel.WorkSlots(2)
    with slots.acquire():
        pass
    assert slots.tracked_pools() == []


def test_failing_set_pool_fails_that_item_not_the_batch():
    def set_pool(pool):
        if pool == "pool0":
            raise RuntimeError("JVM_ATTRIBUTE_NOT_SUPPORTED")
    res = parallel.run_parallel([1, 2, 3, 4], worker=lambda x: x * 10, max_parallel=2, set_pool=set_pool)
    assert [r["status"] for r in res] == ["failed", "ok", "failed", "ok"]
    assert "JVM_ATTRIBUTE_NOT_SUPPORTED" in res[0]["detail"] and res[1]["detail"] == 20


# ---- acquire_many: one unit doing several units of query work at once (a batch) -------------------

def test_acquire_many_holds_n_slots_capped_at_capacity_and_returns_them():
    slots = parallel.WorkSlots(4)
    with slots.acquire_many(3):
        assert slots.in_flight == 3
    with slots.acquire_many(10):                     # capped: a batch never waits for slots that don't exist
        assert slots.in_flight == 4
    assert slots.in_flight == 0 and slots.peak == 4
    with pytest.raises(ValueError):
        with slots.acquire_many(2):
            raise ValueError("boom")
    assert slots.in_flight == 0


def test_acquire_many_mixed_with_single_units_never_deadlocks_or_exceeds_capacity():
    cap = 4
    slots = parallel.WorkSlots(cap)
    mu = threading.Lock()
    state = {"cur": 0, "max": 0}

    def unit(i):
        n = 1 + i % cap                               # batches of 1..4 slots, interleaved with singles
        with slots.acquire_many(n):
            with mu:
                state["cur"] += n
                state["max"] = max(state["max"], state["cur"])
            time.sleep(0.001)
            with mu:
                state["cur"] -= n

    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(unit, range(120)))
    assert state["max"] <= cap and slots.peak <= cap and slots.in_flight == 0


# ---- per-source caps (SourceLimiter) ----------------------------------------------------------------

def _peak_per_source(slots, plan, workers=16):
    """Run ``plan`` (a list of source lists, one per unit) through ``slots``; return the most units
    seen querying each source at once."""
    mu, cur, peak = threading.Lock(), {}, {}

    def unit(sources):
        with (slots.acquire(sources[0]) if len(sources) == 1 else slots.acquire_many(len(sources), sources)):
            with mu:
                for s in sources:
                    cur[s] = cur.get(s, 0) + 1
                    peak[s] = max(peak.get(s, 0), cur[s])
            time.sleep(0.002)
            with mu:
                for s in sources:
                    cur[s] -= 1

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(unit, plan))
    return peak


def test_source_limiter_caps_each_source_but_not_the_others():
    limiter = parallel.SourceLimiter({"a": 2})
    slots = parallel.WorkSlots(8, sources=limiter)
    peak = _peak_per_source(slots, [["a"], ["b"]] * 40)
    assert peak["a"] <= 2 and limiter.peak["a"] == peak["a"] and peak["b"] > 2   # b has no limit
    assert slots.in_flight == 0


def test_source_limiter_admits_a_batch_bigger_than_the_limit_alone():
    # A batch of 3 reads of a source capped at 2 runs alone on it, rather than never.
    limiter = parallel.SourceLimiter({"a": 2})
    slots = parallel.WorkSlots(8, sources=limiter)
    peak = _peak_per_source(slots, [["a", "a", "a"], ["a"]] * 20)
    assert peak["a"] == 3 and slots.in_flight == 0


def test_source_limiter_never_starves_a_batch_behind_single_queries():
    limiter = parallel.SourceLimiter({"a": 4})
    releases = [threading.Event() for _ in range(5)]                # 4 single holders, then the batch
    batch_in, later_in = threading.Event(), threading.Event()

    def holder(release):
        with limiter.hold(["a"]):
            release.wait(5)

    def waiter(sources, admitted, release=None):
        with limiter.hold(sources):
            admitted.set()
            if release is not None:
                release.wait(5)

    def queued(n):                                                  # requests waiting for admission
        deadline = time.monotonic() + 5
        while len(limiter._waiting) < n and time.monotonic() < deadline:
            time.sleep(0.001)
        return len(limiter._waiting)

    threads = [threading.Thread(target=holder, args=(r,)) for r in releases[:4]]
    for t in threads:                                               # take every permit
        t.start()
    while limiter._in_use.get("a", 0) < 4:
        time.sleep(0.001)
    threads.append(threading.Thread(target=waiter, args=(["a"] * 3, batch_in, releases[4])))
    threads[-1].start()
    assert queued(1) == 1                                           # the batch waits first
    threads.append(threading.Thread(target=waiter, args=(["a"], later_in)))
    threads[-1].start()
    assert queued(2) == 2
    releases[0].set()                                               # 1 free: the later single would fit,
    assert not later_in.wait(0.05) and not batch_in.is_set()        # but the batch is ahead of it
    releases[1].set()
    assert not later_in.wait(0.05) and not batch_in.is_set()
    releases[2].set()                                               # 3 free: the batch goes in
    assert batch_in.wait(5) and not later_in.wait(0.05)
    releases[3].set()                                               # 1 free beside the batch
    assert later_in.wait(5)
    releases[4].set()
    for t in threads:
        t.join(5)
    assert limiter.peak == {"a": 4} and limiter._in_use == {"a": 0}


def test_source_limiter_follows_its_limit_as_it_changes():
    limiter = parallel.SourceLimiter({"a": 1})
    assert limiter.limit("a") == 1 and limiter.limit("b") == 0
    entered = threading.Event()

    def second():
        with limiter.hold(["a"]):
            entered.set()

    with limiter.hold(["a"]):
        t = threading.Thread(target=second)
        t.start()
        assert not entered.wait(0.05)                               # the limit is 1: it waits
        limiter.set_limit("a", 2)                                   # a share grows: it goes in
        assert entered.wait(1)
    t.join(1)
    assert limiter.peak["a"] == 2


def test_workslots_without_a_limiter_ignores_sources():
    slots = parallel.WorkSlots(2)
    with slots.acquire("connection:c1"), slots.acquire_many(1, ["connection:c1"]):
        assert slots.in_flight == 2
