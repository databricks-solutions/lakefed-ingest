"""The app never shares a Lakebase connection between threads or requests at once: every query, statement and
transaction borrows one no one else is using (lakebase's pool, or a new one), so one transaction's rollback can never
take another's work and transactions never nest. A connection goes back to the pool only clean: open, idle and in
autocommit, after a caller that didn't fail. The run poller's lock connection is the one long-lived connection, and only
the poller's thread uses it; the schema upgrade check (schema_upgrade.check) holds its lock on a connection it is
handed, in its own thread, once. Both open theirs with lakebase.connect, never from the pool, so a session lock is
never handed on. The connections are fakes here."""
import pathlib
import re
import threading

import psycopg
import pytest

import lakebase

SOURCE_DIR = pathlib.Path(__file__).resolve().parents[1] / "src"
# Where a connection may be opened directly: the helper itself, and the poller's lock connection.
CONNECTION_OPENERS = {"lakebase.py", "run_poller.py"}


class _Info:
    def __init__(self):
        self.transaction_status = psycopg.pq.TransactionStatus.IDLE


class _Connection:
    def __init__(self, name):
        self.name, self.autocommit, self.statements, self.ended = name, True, [], None
        self.closed, self.broken, self.info, self.answers = False, False, _Info(), True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def cursor(self):
        return _Cursor(self)

    def execute(self, sql, params=()):
        if not self.answers:
            self.broken = True
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        self.statements.append(sql)
        return _Cursor(self)

    def commit(self):
        self.ended = self.ended or "commit"

    def rollback(self):
        self.ended = self.ended or "rollback"

    def close(self):
        self.closed = True


class _Cursor:
    description = ()

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def execute(self, sql, params=()):
        if self.connection.closed or self.connection.broken:
            raise psycopg.OperationalError("the connection is closed")
        if sql == "select broken":
            raise psycopg.errors.UndefinedTable("no such table")
        self.connection.statements.append(sql)

    def fetchall(self):
        return []

    def fetchone(self):
        return (1,)


def _fake_connections(monkeypatch):
    opened = []
    lock = threading.Lock()

    def connect():
        with lock:
            connection = _Connection(f"c{len(opened)}")
            opened.append(connection)
            return connection
    monkeypatch.setattr(lakebase, "connect", connect)
    return opened


def test_two_transactions_at_once_each_have_their_own_connection_and_one_rollback_takes_nothing_of_the_other(
        monkeypatch):
    opened = _fake_connections(monkeypatch)
    both_open = threading.Barrier(2, timeout=5)
    errors = []

    def write(sql, fail):
        try:
            with lakebase.transaction() as cur:
                cur.execute(sql)
                both_open.wait()
                if fail:
                    raise RuntimeError("this write fails")
        except RuntimeError:
            pass
        except Exception as e:      # a barrier timeout: the two were not open at once
            errors.append(e)

    threads = [threading.Thread(target=write, args=("update a", False)),
               threading.Thread(target=write, args=("update b", True))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert errors == [] and len(opened) == 2
    by_statement = {connection.statements[0]: connection for connection in opened}
    assert by_statement["update a"].ended == "commit" and by_statement["update a"].statements == ["update a"]
    assert by_statement["update b"].ended == "rollback" and by_statement["update b"].statements == ["update b"]
    # The committed one goes back to the pool in autocommit; the rolled-back one is closed, never handed on.
    assert by_statement["update a"].autocommit is True and not by_statement["update a"].closed
    assert by_statement["update b"].closed


def test_a_read_or_a_statement_during_a_transaction_runs_on_a_connection_of_its_own(monkeypatch):
    opened = _fake_connections(monkeypatch)
    with lakebase.transaction() as cur:
        cur.execute("update a")
        lakebase.query("select 1")
        lakebase.execute("update heartbeat")
    # The read borrows a second connection while the transaction holds the first, and hands it back; the statement
    # after it reuses that one, never the transaction's.
    assert [connection.statements for connection in opened] == [["update a"], ["select 1", "update heartbeat"]]


def test_only_the_helper_and_the_poller_open_connections():
    opening = re.compile(r"\b(?:db|lakebase|psycopg)\.connect\(")
    found = sorted(path.name for path in SOURCE_DIR.glob("*.py") if opening.search(path.read_text()))
    assert set(found) <= CONNECTION_OPENERS, found


# ---- the pool ------------------------------------------------------------------------------------------------------
class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(lakebase.time, "monotonic", fake)
    return fake


def test_reads_one_after_another_reuse_one_connection(monkeypatch, clock):
    opened = _fake_connections(monkeypatch)
    for _ in range(5):
        lakebase.query("select 1")
    lakebase.execute("update heartbeat")
    with lakebase.transaction() as cur:
        cur.execute("update a")
    assert len(opened) == 1 and opened[0].statements == ["select 1"] * 5 + ["update heartbeat", "update a"]
    assert opened[0].autocommit is True and opened[0].closed is False


def test_with_the_pool_off_every_call_opens_and_closes_its_own(monkeypatch, clock):
    monkeypatch.setattr(lakebase, "POOL_IDLE", 0)
    opened = _fake_connections(monkeypatch)
    lakebase.query("select 1")
    lakebase.query("select 2")
    with lakebase.transaction() as cur:
        cur.execute("update a")
    assert [connection.statements for connection in opened] == [["select 1"], ["select 2"], ["update a"]]
    assert all(connection.closed for connection in opened)


def test_a_call_that_fails_closes_its_connection_and_the_next_gets_a_new_one(monkeypatch, clock):
    opened = _fake_connections(monkeypatch)
    lakebase.query("select 1")
    with pytest.raises(psycopg.errors.UndefinedTable):
        lakebase.query("select broken")
    lakebase.query("select 2")
    assert opened[0].closed is True and len(opened) == 2 and opened[1].statements == ["select 2"]


def test_a_transaction_that_raises_is_rolled_back_and_closed_never_handed_on(monkeypatch, clock):
    opened = _fake_connections(monkeypatch)
    with pytest.raises(RuntimeError):
        with lakebase.transaction() as cur:
            cur.execute("update a")
            raise RuntimeError("refused")
    lakebase.query("select 1")
    assert opened[0].ended == "rollback" and opened[0].closed is True
    assert len(opened) == 2 and opened[1].statements == ["select 1"]


def test_a_committed_transaction_hands_back_its_connection_in_autocommit(monkeypatch, clock):
    opened = _fake_connections(monkeypatch)
    with lakebase.transaction() as cur:
        cur.execute("update a")
        assert opened[0].autocommit is False
    assert opened[0].ended == "commit" and opened[0].autocommit is True
    lakebase.query("select 1")
    assert len(opened) == 1


def test_a_connection_left_in_a_transaction_or_out_of_autocommit_is_never_reused(monkeypatch, clock):
    opened = _fake_connections(monkeypatch)
    lakebase.query("select 1")
    opened[0].info.transaction_status = psycopg.pq.TransactionStatus.INTRANS
    lakebase.query("select 2")
    opened[1].autocommit = False
    lakebase.query("select 3")
    assert [connection.statements for connection in opened] == [["select 1"], ["select 2"], ["select 3"]]
    assert opened[0].closed and opened[1].closed


def test_an_old_connection_is_retired_and_so_is_one_idle_too_long(monkeypatch, clock):
    opened = _fake_connections(monkeypatch)
    lakebase.query("select 1")
    clock.now += lakebase.POOL_MAX_AGE_S
    lakebase.query("select 2")                          # opened[0] is too old: retired at checkout
    clock.now += lakebase.POOL_MAX_IDLE_S
    lakebase.query("select 3")                          # opened[1] sat idle too long
    assert len(opened) == 3 and opened[0].closed and opened[1].closed and not opened[2].closed


def test_one_idle_a_while_must_answer_a_select_1_first_and_a_dropped_one_is_replaced(monkeypatch, clock):
    opened = _fake_connections(monkeypatch)
    lakebase.query("select 1")
    clock.now += lakebase.POOL_CHECK_AFTER_S + 1
    lakebase.query("select 2")                          # answers: reused after its check
    assert len(opened) == 1 and opened[0].statements == ["select 1", "select 1", "select 2"]
    clock.now += lakebase.POOL_CHECK_AFTER_S + 1
    opened[0].answers = False                           # the server dropped it while idle
    lakebase.query("select 3")
    assert len(opened) == 2 and opened[0].closed and opened[1].statements == ["select 3"]


def test_a_recently_used_connection_is_handed_out_without_a_check(monkeypatch, clock):
    opened = _fake_connections(monkeypatch)
    lakebase.query("select 1")
    clock.now += lakebase.POOL_CHECK_AFTER_S - 1
    lakebase.query("select 2")
    assert opened[0].statements == ["select 1", "select 2"]


def test_the_pool_keeps_at_most_its_idle_count_and_closes_the_rest(monkeypatch, clock):
    monkeypatch.setattr(lakebase, "POOL_IDLE", 2)
    opened = _fake_connections(monkeypatch)
    held = [lakebase._checkout() for _ in range(4)]
    for conn, opened_at in held:
        lakebase._checkin(conn, opened_at, True)
    assert len(lakebase._idle) == 2 and sum(connection.closed for connection in opened) == 2


def test_threads_at_once_never_share_a_connection(monkeypatch, clock):
    opened = _fake_connections(monkeypatch)
    in_use, problems, guard = set(), [], threading.Lock()
    start = threading.Barrier(8, timeout=5)

    def work():
        start.wait()
        for _ in range(50):
            conn, opened_at = lakebase._checkout()
            with guard:
                if id(conn) in in_use:
                    problems.append(conn.name)
                in_use.add(id(conn))
            with guard:
                in_use.discard(id(conn))
            lakebase._checkin(conn, opened_at, True)
    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert problems == [] and 1 <= len(opened) <= 8


def test_connect_turns_off_server_side_prepared_statements(monkeypatch):
    made = []

    class Made:
        prepare_threshold = 5
    monkeypatch.setattr(lakebase, "_conninfo", lambda: ("host", "user", "token"))
    monkeypatch.setattr(lakebase.psycopg, "connect", lambda **kwargs: made.append(kwargs) or Made())
    conn = lakebase.connect()
    assert conn.prepare_threshold is None and made[0]["autocommit"] is True


def test_session_locks_are_only_taken_on_connections_of_their_own():
    # A session advisory lock lives as long as its connection, so it must never be taken on a pooled one: only the
    # run poller (its own lock connection) and the schema upgrade check (handed lakebase.connect) take them.
    session_lock = re.compile(r"pg_(?:try_)?advisory_lock(?:_shared)?\(|try_session_lock_sql\(\)")
    takers = sorted(path.name for path in SOURCE_DIR.glob("*.py") if session_lock.search(path.read_text()))
    assert takers == ["queries.py", "run_poller.py", "schema_upgrade.py"], takers
    main = (SOURCE_DIR / "main.py").read_text()
    assert "schema_upgrade.start(db.connect," in main
    assert "conn = db.connect()" in (SOURCE_DIR / "run_poller.py").read_text()
