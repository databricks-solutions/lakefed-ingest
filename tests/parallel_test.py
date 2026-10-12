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
