"""The Lakebase pool against a real Postgres: lakebase.connect itself (its prepared-statement setting included) opens
the connections, on a database of the test's own. What a pooled connection carries from one caller to the next must
be nothing: no transaction, no advisory lock, no stale plan; and a connection the server ended is never handed out.

Skipped unless LAKEFED_APP_TEST_PG names a Postgres server (see monitoring_pg_test.py)."""
import os
import time
import uuid

import pytest

import lakebase

URI = os.environ.get("LAKEFED_APP_TEST_PG")
pytestmark = pytest.mark.skipif(not URI, reason="LAKEFED_APP_TEST_PG not set")


@pytest.fixture
def owner(monkeypatch):
    """A database of the test's own, lakebase pointed at it, and an owner connection for the asserts."""
    psycopg = pytest.importorskip("psycopg")
    name = f"lakefed_pool_test_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(URI, autocommit=True)
    admin.execute(f"create database {name}")
    target = psycopg.conninfo.make_conninfo(URI, dbname=name)
    parts = psycopg.conninfo.conninfo_to_dict(target)
    monkeypatch.setattr(lakebase, "_conninfo", lambda: (parts["host"], parts["user"], parts.get("password")))
    monkeypatch.setattr(lakebase, "DATABASE", name)
    monkeypatch.setenv("PGPORT", str(parts.get("port") or 5432))
    lakebase.close_idle()
    try:
        with psycopg.connect(target, autocommit=True) as conn:
            conn.execute("create table t (a int)")
            conn.execute("insert into t values (1)")
        with psycopg.connect(target, autocommit=True) as conn:
            yield conn
    finally:
        lakebase.close_idle()
        admin.execute(f"drop database {name} with (force)")
        admin.close()


def _pid():
    return lakebase.query("select pg_backend_pid() as pid")[0]["pid"]


def _state(owner, pid):
    row = owner.execute("select state from pg_stat_activity where pid = %s", (pid,)).fetchone()
    return row[0] if row else None


def _gone(owner, pid, wait_s=5.0):
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _state(owner, pid) is None:
            return True
        time.sleep(0.1)
    return False


def test_reads_one_after_another_share_one_backend(owner):
    assert len({_pid() for _ in range(5)}) == 1


def test_a_transactions_lock_and_its_transaction_are_gone_before_the_backend_is_reused(owner):
    with lakebase.transaction() as cur:
        assert lakebase.fetch(cur, "select pg_try_advisory_xact_lock(4242, 7) as got")[0]["got"] is True
        cur.execute("insert into t values (2)")
        pid = lakebase.fetch(cur, "select pg_backend_pid() as pid")[0]["pid"]
    assert _pid() == pid and _state(owner, pid) == "idle"
    held = owner.execute("select count(*) from pg_locks where locktype = 'advisory' and classid = 4242 "
                         "and objid = 7").fetchone()[0]
    assert held == 0
    assert owner.execute("select count(*) from t where a = 2").fetchone()[0] == 1     # committed, seen at once


def test_a_transaction_that_raises_is_rolled_back_and_its_backend_closed(owner):
    with pytest.raises(RuntimeError):
        with lakebase.transaction() as cur:
            cur.execute("insert into t values (3)")
            pid = lakebase.fetch(cur, "select pg_backend_pid() as pid")[0]["pid"]
            raise RuntimeError("refused")
    assert _gone(owner, pid) and _pid() != pid
    assert owner.execute("select count(*) from t where a = 3").fetchone()[0] == 0


def test_a_failed_statement_closes_its_backend_and_the_next_read_gets_another(owner):
    import psycopg
    pid = _pid()
    with pytest.raises(psycopg.errors.UndefinedTable):
        lakebase.query("select * from no_such_table")
    assert _pid() != pid and _gone(owner, pid)


def test_a_backend_the_server_ended_while_idle_is_replaced_not_handed_out(owner, monkeypatch):
    monkeypatch.setattr(lakebase, "POOL_CHECK_AFTER_S", 0)
    pid = _pid()
    assert owner.execute("select pg_terminate_backend(%s)", (pid,)).fetchone()[0] is True
    assert _gone(owner, pid)
    assert lakebase.query("select a from t order by a") == [{"a": 1}]


def test_a_statement_sent_many_times_still_works_after_a_column_is_added(owner):
    # psycopg would prepare it on the server after 5 runs; a prepared select * then fails once the table changes
    # ("cached plan must not change result type"). The pool's connections never prepare.
    for _ in range(8):
        assert lakebase.query("select * from t order by a") == [{"a": 1}]
    owner.execute("alter table t add column b text default 'x'")
    assert lakebase.query("select * from t order by a") == [{"a": 1, "b": "x"}]
