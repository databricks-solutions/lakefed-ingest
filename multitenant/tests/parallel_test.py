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
