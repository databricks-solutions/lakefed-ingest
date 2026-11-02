"""The shared monitoring reads (main._shared): one compute per key at a time however many requests ask, and a
committed write (main._write) makes the next read fresh, so pages show a change at once."""
import contextlib
import threading
import time

import pytest

pytest.importorskip("fastapi")
import main  # noqa: E402


@pytest.fixture(autouse=True)
def _cached(monkeypatch):
    monkeypatch.setattr(main, "MONITOR_CACHE_S", 60)


def test_requests_arriving_together_wait_for_one_compute():
    calls, started = [], threading.Event()

    def compute():
        calls.append(1)
        started.set()
        time.sleep(0.2)
        return len(calls)

    got = []
    threads = [threading.Thread(target=lambda: got.append(main._shared("heavy", compute))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and got == [1] * 6


def test_a_committed_write_makes_the_next_read_fresh(monkeypatch):
    value = ["before"]
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield object())))
    assert main._shared("states", lambda: value[0]) == "before"
    value[0] = "after"
    assert main._shared("states", lambda: value[0]) == "before"
    with main._write():
        pass
    assert main._shared("states", lambda: value[0]) == "after"


def test_a_failed_write_keeps_the_shared_reads(monkeypatch):
    monkeypatch.setattr(main.db, "transaction", contextlib.contextmanager(lambda: (yield object())))
    main._shared("states", lambda: "kept")
    with pytest.raises(RuntimeError):
        with main._write():
            raise RuntimeError("refused")
    assert main._shared("states", lambda: "recomputed") == "kept"


def test_a_read_a_write_committed_during_is_not_kept():
    def compute():
        main._forget_shared_reads()          # a write commits while this read is running
        return "read before the write"

    assert main._shared("states", compute) == "read before the write"
    assert main._shared("states", lambda: "fresh") == "fresh"
