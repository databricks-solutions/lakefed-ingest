"""The app never shares a Lakebase connection between threads or requests: every query, statement and transaction
opens its own (lakebase.connect), so one transaction's rollback can never take another's work and transactions never
nest. The run poller's lock connection is the one long-lived connection, and only the poller's thread uses it; the
schema upgrade check (schema_upgrade.check) holds its lock on a connection it is handed, in its own thread, once. The
connections are fakes here."""
import pathlib
import re
import threading

import lakebase

SOURCE_DIR = pathlib.Path(__file__).resolve().parents[1] / "src"
# Where a connection may be opened directly: the helper itself, and the poller's lock connection.
CONNECTION_OPENERS = {"lakebase.py", "run_poller.py"}


class _Connection:
    def __init__(self, name):
        self.name, self.autocommit, self.statements, self.ended = name, True, [], None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.ended = self.ended or "commit"

    def rollback(self):
        self.ended = self.ended or "rollback"

    def close(self):
        pass


class _Cursor:
    description = ()

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def execute(self, sql, params=()):
        self.connection.statements.append(sql)

    def fetchall(self):
        return []


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
    assert all(connection.autocommit is False for connection in opened)


def test_a_read_or_a_statement_during_a_transaction_runs_on_a_connection_of_its_own(monkeypatch):
    opened = _fake_connections(monkeypatch)
    with lakebase.transaction() as cur:
        cur.execute("update a")
        lakebase.query("select 1")
        lakebase.execute("update heartbeat")
    assert [connection.statements for connection in opened] == [["update a"], ["select 1"], ["update heartbeat"]]


def test_only_the_helper_and_the_poller_open_connections():
    opening = re.compile(r"\b(?:db|lakebase|psycopg)\.connect\(")
    found = sorted(path.name for path in SOURCE_DIR.glob("*.py") if opening.search(path.read_text()))
    assert set(found) <= CONNECTION_OPENERS, found
