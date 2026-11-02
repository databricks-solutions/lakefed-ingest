"""The app's schema upgrade (schema_upgrade.py): its expected version is the engine's; when the app starts it starts the
setup job once for a database behind, under its lock and only with no setup run going, logs it, and never raises; a
database with no schema is left to the deploy. Manage shows the database's version against the app's."""
import re
from pathlib import Path

import pytest

import queries as q
import schema_upgrade as su

STATE_STORE = Path(__file__).resolve().parents[2] / "src" / "lakefed_ingest" / "state_store.py"


class _Conn:
    """A Lakebase connection answering the check's statements: the schema_version table (or none), the version, and
    the upgrade lock (free or held elsewhere). Records the statements run."""

    def __init__(self, version, lock_free=True, fail=None):
        self.version, self.lock_free, self.fail, self.ran = version, lock_free, fail, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.ran.append((sql, tuple(params)))
        if self.fail and self.fail in sql:
            raise RuntimeError("connection dropped")
        answer = {q.schema_version_table_sql(): "lakefed_ingest.schema_version" if self.version else None,
                  q.schema_version_sql(): self.version, q.try_session_lock_sql(): self.lock_free,
                  q.session_unlock_sql(): True}.get(sql)
        return type("Cursor", (), {"fetchone": lambda self: (answer,)})()

    def statements(self):
        return [sql for sql, _ in self.ran]


def _check(conn, going=(), job_id="16", started=None):
    started = [] if started is None else started

    def run_now(job):
        started.append(job)
        return 41
    return su.check(lambda: conn, job_id, lambda job: list(going), run_now), started


def test_the_apps_schema_version_is_the_engines():
    engine = int(re.search(r"^SCHEMA_VERSION = (\d+)$", STATE_STORE.read_text(), re.M).group(1))
    assert su.SCHEMA_VERSION == engine


def test_a_database_behind_starts_the_setup_job_once_under_the_lock_and_logs_it():
    conn = _Conn(su.SCHEMA_VERSION - 1)
    found, started = _check(conn)
    assert (found["state"], found["database"], found["run_id"], found["error"]) == ("started", su.SCHEMA_VERSION - 1,
                                                                                  41, None)
    assert started == ["16"] and su.LAST["state"] == "started"
    assert conn.statements()[2:] == [q.try_session_lock_sql(), q.log_change_sql(), q.session_unlock_sql()]
    log = conn.ran[3][1]
    assert (log[1], log[4], log[5], log[6]) == ("schema_upgrade", "schema_version", str(su.SCHEMA_VERSION - 1),
                                                str(su.SCHEMA_VERSION))
    assert conn.ran[2][1] == su.UPGRADE_LOCK == conn.ran[4][1]


def test_another_instance_holding_the_lock_or_a_setup_run_going_starts_nothing():
    found, started = _check(_Conn(su.SCHEMA_VERSION - 1, lock_free=False))
    assert found["state"] == "busy" and started == []
    conn = _Conn(su.SCHEMA_VERSION - 1)
    found, started = _check(conn, going=[{"run_id": 40, "url": "https://h/run/40"}])
    assert (found["state"], found["run_id"], found["url"], started) == ("running", 40, "https://h/run/40", [])
    assert q.log_change_sql() not in conn.statements() and conn.statements()[-1] == q.session_unlock_sql()


def test_a_current_database_or_one_with_no_schema_starts_nothing():
    for version, state in ((su.SCHEMA_VERSION, "current"), (su.SCHEMA_VERSION + 1, "current"), (0, "missing")):
        conn = _Conn(version)
        found, started = _check(conn)
        assert (found["state"], started) == (state, []), version
        assert q.try_session_lock_sql() not in conn.statements()


def test_a_failure_is_kept_for_manage_and_never_raised():
    found, started = _check(_Conn(su.SCHEMA_VERSION - 1), job_id=None)
    assert found["state"] == "failed" and "SETUP_JOB_ID" in found["error"] and started == []

    def refused(job):
        raise PermissionError("no permission to run the job")
    conn = _Conn(su.SCHEMA_VERSION - 1)
    found = su.check(lambda: conn, "16", lambda job: [], refused)
    assert found["state"] == "failed" and found["error"] == "PermissionError: no permission to run the job"
    assert conn.statements()[-1] == q.session_unlock_sql(), "the lock is let go after a failed start"
    assert su.LAST["error"] == found["error"]

    found, started = _check(_Conn(su.SCHEMA_VERSION - 1, fail="control_change"))
    assert (found["state"], started) == ("started", ["16"]), "a run started stays started if its log line fails"
    assert "connection dropped" in found["error"]

    def no_database():
        raise OSError("could not connect")
    assert su.check(no_database, "16", lambda job: [], refused)["state"] == "failed"


def test_the_app_starts_the_check_when_it_starts_and_not_when_turned_off(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import main
    calls = []
    monkeypatch.setenv("RUN_POLLER", "0")
    monkeypatch.setattr(main, "REDISCOVERY_TICK_S", 0)
    monkeypatch.setattr(main.jobs, "SETUP_JOB_ID", "16")
    monkeypatch.setattr(main.schema_upgrade, "start", lambda *args: calls.append(args[1]))
    with TestClient(main.app):
        pass
    assert calls == ["16"]
    monkeypatch.setenv("SCHEMA_UPGRADE", "0")
    with TestClient(main.app):
        pass
    assert calls == ["16"]


def test_manage_shows_the_databases_version_and_the_setup_run_going(monkeypatch):
    pytest.importorskip("fastapi")
    import main
    monkeypatch.setattr(main.jobs, "SETUP_JOB_ID", "16")
    answers = {q.schema_version_table_sql(): [{"to_regclass": "lakefed_ingest.schema_version"}],
               q.schema_version_sql(): [{"version": su.SCHEMA_VERSION - 1}]}
    monkeypatch.setattr(main.db, "query", lambda sql, params=(): answers[sql])
    monkeypatch.setattr(main.run_poller, "runs_of", lambda job_id, active_only, limit: [
        {"run_id": 41, "url": "https://h/run/41"}] if str(job_id) == "16" and active_only else [])
    state = main._schema_state()
    assert (state["database"], state["app"], state["run"], state["error"]) == (
        su.SCHEMA_VERSION - 1, su.SCHEMA_VERSION, {"run_id": 41, "url": "https://h/run/41"}, None)

    def down(sql, params=()):
        raise OSError("could not connect. Check the host")
    monkeypatch.setattr(main.db, "query", down)
    monkeypatch.setattr(main.run_poller, "runs_of", lambda *a, **k: [])
    state = main._schema_state()
    assert (state["database"], state["run"]) == (None, None)
    assert state["error"] == "can't read the state database's schema version: could not connect"
