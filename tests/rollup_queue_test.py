"""The monitoring rollups: the batched writes of a RollupQueue, the rewrite statements of the state store, and the
delete job's rollup deletes."""
import threading
from contextlib import contextmanager, nullcontext

import delete_job as dj
import pytest
import state_store as ss

RECOMPUTE_DATABASES = ss.rollup_database_rewrite_sql()


def test_a_full_batch_is_written_once_with_its_names_sorted():
    store = _Store()
    queue = ss.RollupQueue(store, "c1", batch_size=3, clock=_Clock())
    queue.add(["b", "a"])
    assert store.writes == []
    queue.add(["c", "a"])
    assert store.writes == [("c1", ["a", "b", "c"])]


def test_a_batch_older_than_max_age_is_written_before_it_is_full():
    clock = _Clock()
    store = _Store()
    queue = ss.RollupQueue(store, "c1", batch_size=200, max_age_s=5.0, clock=clock)
    queue.add(["a"])
    clock.now = 4.9
    queue.add(["b"])
    assert store.writes == []
    clock.now = 5.0
    queue.add(["c"])
    assert store.writes == [("c1", ["a", "b", "c"])]


def test_flush_writes_what_is_left_and_nothing_once_empty():
    store = _Store()
    queue = ss.RollupQueue(store, "c1", batch_size=200, clock=_Clock())
    queue.flush()
    assert store.writes == []
    queue.add(["x", None])
    queue.flush()
    queue.flush()
    assert store.writes == [("c1", ["", "x"])]


def test_a_failed_write_keeps_its_names_for_the_next_write():
    store = _Store(fail_times=1)
    queue = ss.RollupQueue(store, "c1", batch_size=2, clock=_Clock())
    with pytest.raises(RuntimeError):
        queue.add(["a", "b"])
    assert store.writes == []
    queue.flush()
    assert store.writes == [("c1", ["a", "b"])]


def test_databases_added_from_many_threads_are_each_written_once():
    store = _Store()
    queue = ss.RollupQueue(store, "c1", batch_size=7, max_age_s=1e9, clock=_Clock())
    names = [f"db{i}" for i in range(200)]
    threads = [threading.Thread(target=_add_each, args=(queue, names[at::8])) for at in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    queue.flush()
    written = [name for _, batch in store.writes for name in batch]
    assert sorted(written) == sorted(names)


def test_a_worker_that_adds_while_a_flush_is_writing_is_not_held_up_by_the_write():
    store = _BlockingStore()
    queue = ss.RollupQueue(store, "c1", batch_size=200, max_age_s=1e9, clock=_Clock())
    queue.add(["a"])
    flusher = threading.Thread(target=queue.flush, daemon=True)
    adder = threading.Thread(target=queue.add, args=(["b"],), daemon=True)
    try:
        flusher.start()
        assert store.entered.wait(5)
        adder.start()
        adder.join(5)
        assert not adder.is_alive()
    finally:
        store.release.set()
        flusher.join(5)
    queue.flush()
    assert store.writes == [("c1", ["a"]), ("c1", ["b"])]


def test_names_added_while_a_failing_write_is_in_flight_are_written_with_the_failed_ones():
    store = _BlockingStore(fail_times=1)
    queue = ss.RollupQueue(store, "c1", batch_size=200, max_age_s=1e9, clock=_Clock())
    queue.add(["a"])
    failures = []

    def flush_and_record_failure():
        try:
            queue.flush()
        except RuntimeError:
            failures.append(True)

    flusher = threading.Thread(target=flush_and_record_failure, daemon=True)
    adder = threading.Thread(target=queue.add, args=(["b"],), daemon=True)
    try:
        flusher.start()
        assert store.entered.wait(5)
        adder.start()
        adder.join(5)
        assert not adder.is_alive()
    finally:
        store.release.set()
        flusher.join(5)
    assert failures == [True] and store.writes == []
    queue.flush()
    assert store.writes == [("c1", ["a", "b"])]


def test_rewrite_rollups_recomputes_databases_in_chunks_then_the_group_in_one_transaction():
    store, cursor = _recording_store()
    names = [f"db{i:03d}" for i in range(450)]
    store.rewrite_rollups("c1", names)
    # The group's recount lock comes right before its recount, in the same transaction (two runs of one group).
    assert [sql for sql, _ in cursor.calls] == [RECOMPUTE_DATABASES] * 3 + [ss.rollup_group_lock_sql(),
                                                                           ss.rollup_group_rewrite_sql()]
    assert [params for _, params in cursor.calls[:3]] == [
        (["c1"] * 200, names[:200]), (["c1"] * 200, names[200:400]), (["c1"] * 50, names[400:])]
    assert cursor.calls[3][1] == (ss.ROLLUP_LOCK_NAMESPACE, ["c1"]) and cursor.calls[4][1] == (["c1"],)
    assert cursor.events[0] == "begin" and cursor.events[-1] == "commit" and cursor.events.count("execute") == 5


def test_rewrite_rollups_names_each_database_once_with_a_blank_for_none():
    store, cursor = _recording_store()
    store.rewrite_rollups("c1", ["b", None, "a", "a"])
    assert cursor.calls[0][1] == (["c1"] * 3, ["", "a", "b"])


def test_rewrite_rollups_with_no_databases_touches_nothing():
    store, cursor = _recording_store()
    store.rewrite_rollups("c1", [])
    assert cursor.calls == [] and cursor.events == []


def test_the_database_rewrite_takes_two_parameters_and_zeroes_the_grain_rows_it_no_longer_has():
    assert RECOMPUTE_DATABASES.count("%s") == 2
    assert "on conflict (task_collection, src_database, server, src_connection, src_host, instance_label)" in (
        RECOMPUTE_DATABASES)
    assert "where r.task_collection = k.task_collection and r.src_database = k.src_database" in RECOMPUTE_DATABASES
    assert "and not exists (select 1 from agg a" in RECOMPUTE_DATABASES


def test_the_group_rewrite_takes_the_groups_and_reads_the_database_rows():
    sql = ss.rollup_group_rewrite_sql()
    assert sql.count("%s") == 1
    assert f"left join {ss.ROLLUP_DATABASE_TABLE} r" in sql
    assert "on conflict (task_collection) do update" in sql


def test_the_rollup_tables_and_chunk_size_are_the_schema_tables():
    assert ss.ROLLUP_DATABASE_TABLE == "lakefed_ingest.rollup_database"
    assert ss.ROLLUP_GROUP_TABLE == "lakefed_ingest.rollup_group"
    assert ss.ROLLUP_CHUNK == 200


def test_the_delete_job_removes_a_groups_rollup_rows_by_group():
    assert ss.delete_rollups_sql(ss.ROLLUP_DATABASE_TABLE) == (
        "delete from lakefed_ingest.rollup_database where task_collection = any(%s)")
    assert ss.delete_rollups_sql(ss.ROLLUP_GROUP_TABLE) == (
        "delete from lakefed_ingest.rollup_group where task_collection = any(%s)")
    assert dict(dj.DELETES)["rollup_database"] == ss.delete_rollups_sql(ss.ROLLUP_DATABASE_TABLE)
    assert dict(dj.DELETES)["rollup_group"] == ss.delete_rollups_sql(ss.ROLLUP_GROUP_TABLE)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _Store:
    """Records each rewrite; fails the first ``fail_times`` of them."""

    def __init__(self, fail_times=0):
        self.writes = []
        self.fail_times = fail_times

    def rewrite_rollups(self, task_collection, src_databases):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("lakebase unreachable")
        self.writes.append((task_collection, list(src_databases)))


class _BlockingStore(_Store):
    """Like _Store, but its first write is in flight until ``release`` is set: ``entered`` is set once it has started."""

    def __init__(self, fail_times=0):
        super().__init__(fail_times)
        self.entered, self.release = threading.Event(), threading.Event()
        self.first_write = True

    def rewrite_rollups(self, task_collection, src_databases):
        if self.first_write:
            self.first_write = False
            self.entered.set()
            self.release.wait(5)
        super().rewrite_rollups(task_collection, src_databases)


class _Connection:
    def __init__(self, events):
        self.events = events

    @contextmanager
    def transaction(self):
        self.events.append("begin")
        yield
        self.events.append("commit")


class _Cursor:
    def __init__(self):
        self.calls, self.events = [], []
        self.connection = _Connection(self.events)

    def execute(self, sql, params=None):
        self.events.append("execute")
        self.calls.append((sql, params))


def _recording_store():
    cursor = _Cursor()
    store = ss.StateStore.__new__(ss.StateStore)
    store._cursor = lambda: nullcontext(cursor)
    return store, cursor


def _add_each(queue, names):
    for name in names:
        queue.add([name])
