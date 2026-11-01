"""The background reader (background_reads.Reader) against the fake store and warehouse of warehouse_reads_test: each
server's cap across discoveries and instances, answers in the call that sent them, a warehouse that is starting, a
restart with statements saved, dropped databases, and the page's own steps beside it. Its workers are real threads;
the looks (step) are taken by the test."""
import time

import pytest

import background_reads as br
import warehouse_reads as wr
from warehouse_reads_test import SUMMARY_ROW, _Store, _Warehouse, _ok

PENDING = {"state": "PENDING", "rows": None, "error": None}
RUNNING = {"state": "RUNNING", "rows": None, "error": None}
CANCELED = {"state": "CANCELED", "rows": None, "error": "the statement was cancelled"}


def _fixed_caps(start=8):
    """A cap that never moves, so a test sees the cap it set."""
    return wr.ServerCaps(start=start, window=10 ** 6)


def _reader(store, fake, caps=None, workers=32, clock=time.monotonic):
    reader = br.Reader(store, fake, caps=caps or _fixed_caps(), workers=workers, clock=clock,
                       sleep=lambda s: time.sleep(0.001))
    reader.start_workers()
    return reader


def _drain(store, *readers, timeout=10.0):
    """Take looks until no read is left to hand and none is held."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for reader in readers:
            reader.step()
        if not any(r.held() for r in readers) and not store.read_work(wr.CLAIM_S, br.MAX_AGE_S, 7200):
            return
        time.sleep(0.002)
    raise AssertionError("the reads didn't end")


def _until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("waited too long")
        time.sleep(0.002)


def _names(prefix, n):
    return [f"{prefix}{i:02d}" for i in range(n)]


@pytest.fixture(autouse=True)
def _warehouse_up(monkeypatch):
    """The warehouse's state is looked at afresh on each look, so a test that changes it is heard at once."""
    monkeypatch.setattr(br, "WAREHOUSE_LOOK_S", 0.0)


def test_two_discoveries_of_one_server_share_its_cap_across_two_instances_and_another_server_has_its_own():
    store = _Store()
    store.add("d1", _names("a", 20), connection="sql_a")
    store.add("d2", _names("b", 20), connection="sql_a")
    store.add("d3", _names("c", 20), connection="sql_b")
    every = _names("a", 20) + _names("b", 20) + _names("c", 20)
    fake = _Warehouse({n: [_ok([SUMMARY_ROW])] for n in every}, in_call=True, hold_s=0.02)
    _drain(store, _reader(store, fake), _reader(store, fake))
    assert fake.most["sql_a"] <= 8 and fake.most["sql_b"] <= 8
    assert fake.most["sql_a"] > 2 and fake.most["sql_b"] > 2, "sent truly in parallel"
    assert sorted(fake.started_for) == sorted(every), "each database sent once"
    assert all(r["status"] == "ok" for r in store.rows.values())
    assert all(cap == 8 and going + claimed <= 8 for _, cap, going, claimed in store.claims)


def test_each_answer_comes_back_in_the_call_that_sent_it():
    store = _Store(_names("db", 10))
    fake = _Warehouse({n: [_ok([SUMMARY_ROW])] for n in _names("db", 10)}, in_call=True)
    _drain(store, _reader(store, fake))
    assert fake.waits == [br.READ_WAIT_S] * 10 and fake.read_ids == []
    assert store.progress(wr.READ, "d1") == {"databases": 10, "pending": 0, "done": 10, "failed": 0, "tables": 10}
    assert all(r["statement"] is None and r["claimed_at"] is None for r in store.rows.values())


def test_a_statement_still_going_after_its_wait_is_read_again_until_it_ends():
    store = _Store(["eng_001"])
    fake = _Warehouse({"eng_001": [PENDING, RUNNING, RUNNING, _ok([SUMMARY_ROW])]}, in_call=True)
    _drain(store, _reader(store, fake))
    assert fake.started_for == ["eng_001"] and fake.read_ids == ["s-eng_001"] * 3
    assert store.row("d1", "eng_001")["status"] == "ok"


def test_a_warehouse_starting_takes_one_statement_at_a_time_and_its_slow_first_answer_sets_no_cap():
    store = _Store(_names("db", 12))
    timed = []

    class Caps(wr.ServerCaps):
        def timed(self, server, seconds):
            timed.append(server)
            return super().timed(server, seconds)

    class Starting(_Warehouse):
        def read(self, statement_id):
            answer = super().read(statement_id)
            if answer["state"] == "SUCCEEDED":
                self.sent_before_the_first_answer = len(self.started_for)
                self.state = "RUNNING"
            return answer
    answers = {n: [_ok([SUMMARY_ROW])] for n in _names("db", 12)}
    answers["db00"] = [PENDING, RUNNING, RUNNING, _ok([SUMMARY_ROW])]
    fake = Starting(answers, state="STARTING", in_call=True)
    reader = _reader(store, fake, caps=Caps(start=8, window=10 ** 6))
    for _ in range(20):
        reader.step()
    _drain(store, reader)
    assert fake.started_for[0] == "db00" and fake.sent_before_the_first_answer == 1
    assert sorted(fake.started_for) == _names("db", 12) and len(timed) == 11, "the first answer isn't timed"


def test_after_a_restart_a_saved_statement_is_read_again_not_sent_again_and_a_claim_with_none_is_sent_once_it_lapses():
    store = _Store(["eng_001", "eng_002", "eng_003"])
    store.row("d1", "eng_001").update(statement="s-eng_001", claimed_at=0.0)     # sent before the restart
    store.row("d1", "eng_002").update(claimed_at=0.0)                            # claimed, then the app stopped
    fake = _Warehouse({n: [_ok([SUMMARY_ROW])] for n in ("eng_001", "eng_002", "eng_003")}, in_call=True)
    reader = _reader(store, fake)
    _drain(store, reader)
    assert fake.started_for == ["eng_003"] and fake.read_ids == [], "a live claim is someone else's"
    store.now = wr.CLAIM_S + 1
    _drain(store, reader)
    assert fake.started_for == ["eng_003", "eng_002"] and fake.read_ids == ["s-eng_001"]
    assert all(r["status"] == "ok" for r in store.rows.values())


def test_a_dropped_database_isnt_started_and_one_dropped_while_going_is_cancelled_not_failed_then_read_when_ticked_again():
    store = _Store(["eng_001", "eng_002", "eng_003"])
    store.drop("d1", "eng_002")
    fake = _Warehouse({"eng_001": [_ok([SUMMARY_ROW])], "eng_002": [_ok([SUMMARY_ROW])],
                       "eng_003": [PENDING, RUNNING]}, in_call=True)
    reader = _reader(store, fake)
    reader.step()
    _until(lambda: store.row("d1", "eng_003")["statement"] == "s-eng_003")
    going = store.drop("d1", "eng_003")
    fake.cancel(going)
    fake.answers["eng_003"] = [CANCELED]
    _drain(store, reader)
    assert fake.cancelled == ["s-eng_003"] and "eng_002" not in fake.started_for
    dropped = store.row("d1", "eng_003")
    assert (dropped["status"], dropped["error"], dropped["statement"]) == ("running", None, None)
    assert store.progress(wr.READ, "d1") == {"databases": 1, "pending": 0, "done": 1, "failed": 0, "tables": 1}
    store.row("d1", "eng_003")["dropped"] = False                                # ticked again
    fake.answers["eng_003"] = [_ok([SUMMARY_ROW])]
    _drain(store, reader)
    assert fake.started_for.count("eng_003") == 2 and store.row("d1", "eng_003")["status"] == "ok"


def test_a_database_dropped_while_its_statement_is_being_sent_has_it_cancelled_not_saved():
    store = _Store(["eng_001"])

    class DroppedMeanwhile(_Warehouse):
        def start(self, statement, parameters, wait_s=None):
            answer = super().start(statement, parameters, wait_s)
            store.drop("d1", "eng_001")
            return answer
    fake = DroppedMeanwhile({"eng_001": [PENDING, RUNNING]}, in_call=True)
    reader = _reader(store, fake)
    reader.step()
    _until(lambda: not reader.held())
    assert fake.cancelled == ["s-eng_001"] and fake.read_ids == []
    assert store.row("d1", "eng_001")["statement"] is None and store.row("d1", "eng_001")["status"] == "running"


def test_the_pages_steps_beside_the_reader_never_pass_the_cap_or_start_a_database_twice():
    store = _Store(_names("db", 30))
    fake = _Warehouse({n: [RUNNING, RUNNING, _ok([SUMMARY_ROW])] for n in _names("db", 30)})
    caps = _fixed_caps()
    reader = _reader(store, fake, caps=caps)
    deadline = time.monotonic() + 10
    while store.progress(wr.READ, "d1")["pending"]:
        assert time.monotonic() < deadline
        reader.step()
        assert store.going("sql_a") <= 8
        wr.advance(store, fake, wr.READ, "d1", "sql_a", caps=caps)
        assert store.going("sql_a") <= 8
        time.sleep(0.002)
    _until(lambda: not reader.held())
    assert sorted(fake.started_for) == _names("db", 30)
    assert all(going + claimed <= 8 for _, _, going, claimed in store.claims)


def test_a_statement_that_cant_be_sent_frees_its_database_and_pauses_the_sends_saying_why():
    now = [100.0]
    store = _Store(["eng_001"])

    class Broken(_Warehouse):
        def start(self, statement, parameters, wait_s=None):
            self.started_for.append("x")
            raise RuntimeError("the warehouse was deleted\nmore")
    fake = Broken({})
    reader = _reader(store, fake, clock=lambda: now[0])
    reader.step()
    _until(lambda: not reader.held())
    assert store.row("d1", "eng_001")["claimed_at"] is None and reader.recent_error("d1") == "the warehouse was deleted"
    assert reader.recent_error("d2") is None, "said only to the page that asks about that read"
    assert reader.step() == 0 and fake.started_for == ["x"], "paused"
    now[0] += br.BACKOFF_S + 1
    reader.step()
    _until(lambda: len(fake.started_for) == 2)
    now[0] += br.ERROR_SHOWN_S + 1
    _until(lambda: not reader.held())
    assert reader.recent_error("d1") is None


def test_the_claims_of_the_reads_held_are_renewed_well_inside_their_time():
    now = [0.0]
    store = _Store(["eng_001"])
    fake = _Warehouse({"eng_001": [PENDING, RUNNING]}, in_call=True)
    reader = _reader(store, fake, clock=lambda: now[0])
    reader.step()
    _until(lambda: store.row("d1", "eng_001")["statement"] is not None)
    store.now = now[0] = br.HOLD_S
    reader.step()
    assert store.row("d1", "eng_001")["claimed_at"] == br.HOLD_S
    store.now = br.HOLD_S + wr.CLAIM_S - 1
    assert store.going("sql_a") == 1, "still counted under the cap"
    fake.answers["eng_001"] = [_ok([SUMMARY_ROW])]
    _until(lambda: store.row("d1", "eng_001")["status"] == "ok")


def test_the_reads_the_reader_times_move_its_servers_cap():
    store = _Store(_names("db", 40))
    fake = _Warehouse({n: [_ok([SUMMARY_ROW])] for n in _names("db", 40)}, in_call=True, hold_s=0.01)
    caps = wr.ServerCaps(start=8, window=4, flat=100.0)
    _drain(store, _reader(store, fake, caps=caps))
    assert caps.cap("sql_a") > 8


def test_the_app_starts_the_reader_with_a_warehouse_and_not_when_turned_off(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import main
    started = []
    monkeypatch.setenv("RUN_POLLER", "0")
    monkeypatch.setenv("SCHEMA_UPGRADE", "0")
    monkeypatch.setattr(main, "REDISCOVERY_TICK_S", 0)
    monkeypatch.setattr(main.warehouse, "configured", lambda: True)
    monkeypatch.setattr(main.READER, "start", lambda: started.append(True))
    with TestClient(main.app):
        pass
    assert started == [True]
    monkeypatch.setenv("BACKGROUND_READS", "0")
    with TestClient(main.app):
        pass
    assert started == [True]
    monkeypatch.delenv("BACKGROUND_READS")
    monkeypatch.setattr(main.warehouse, "configured", lambda: False)
    with TestClient(main.app):
        pass
    assert started == [True], "no warehouse, no reads through it"
