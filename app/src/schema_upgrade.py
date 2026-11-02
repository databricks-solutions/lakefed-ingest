"""The state database's schema upgrade, started by the app: when it starts and finds the database behind the schema
version this code expects, it starts the setup job (lakefed_ingest_setup), which runs as the deployer, the schema's
owner. The app itself has no DDL rights.

Off the request path and never raising: the app starts the check in a thread (main._lifespan), and what it found is
kept in ``LAST`` for Manage to show. Two app instances, or a restart while a setup run goes, start one run between
them: the start is taken under a session advisory lock (try-lock: another instance holding it means it is starting
the run), and a setup job with a run going isn't started again; the job's own max_concurrent_runs of 1 is the second
guard.

A database with no schema at all (a first install) is left alone: the setup job is then the deploy's manual step,
since the app's Postgres role and grants (lakefed_ingest_app_grants) are made on a schema that exists. An upgrade
still also happens at the next ingestion or check run, as before (ingest.ipynb, check_launch.ipynb)."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, List, Optional

import queries as q

# The schema version this app's code expects; tests/schema_upgrade_test.py pins it to the engine's
# state_store.SCHEMA_VERSION, so the two can't drift.
SCHEMA_VERSION = 17
# The upgrade's own advisory lock, two int keys in a class of its own (not the poller's, a collection's or the
# migration's, state_store.SCHEMA_LOCK).
UPGRADE_LOCK = (0x6C667375, 0)

LAST: dict = {"state": "not checked"}
_lock = threading.Lock()


def database_version(conn) -> int:
    """The newest schema version applied to the database, 0 when none is (no schema_version table)."""
    if conn.execute(q.schema_version_table_sql()).fetchone()[0] is None:
        return 0
    return int(conn.execute(q.schema_version_sql()).fetchone()[0])


def check(connect: Callable, job_id, active_runs: Callable[[object], List[dict]], run_now: Callable[[object], int],
          now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    """Read the database's schema version and, when it is behind (and not missing), start the setup job once. Returns
    what happened, {state, database, app, run_id, url, error, at}, and keeps it in LAST; never raises.

    state: current, missing (no schema: a first install, left to the deploy), busy (another instance holds the lock),
    running (a setup run is already going), started, or failed (with error). ``active_runs(job_id)`` lists the setup
    job's runs going, from the Jobs API (newest first), and ``run_now(job_id)`` starts one."""
    out = {"state": "failed", "database": None, "app": SCHEMA_VERSION, "run_id": None, "url": None, "error": None,
           "at": now().isoformat()}
    try:
        if not job_id:
            raise RuntimeError("the setup job is not configured here (SETUP_JOB_ID)")
        with connect() as conn:
            out["database"] = have = database_version(conn)
            if have == 0:
                out["state"] = "missing"
            elif have >= SCHEMA_VERSION:
                out["state"] = "current"
            elif not conn.execute(q.try_session_lock_sql(), UPGRADE_LOCK).fetchone()[0]:
                out["state"] = "busy"
            else:
                try:
                    going = active_runs(job_id)
                    if going:
                        out.update(state="running", run_id=going[0].get("run_id"), url=going[0].get("url"))
                    else:
                        out["run_id"] = run_now(job_id)
                        out["state"] = "started"
                        conn.execute(q.log_change_sql(), (None, "schema_upgrade", None, None, "schema_version",
                                                          str(have), str(SCHEMA_VERSION), None, None))
                finally:
                    conn.execute(q.session_unlock_sql(), UPGRADE_LOCK)
    except Exception as error:
        if out["state"] != "started":   # a run started stays started, even if its log line failed
            out["state"] = "failed"
        out["error"] = f"{type(error).__name__}: {str(error)[:300]}"
        print(f"[schema] the state database's upgrade check failed: {out['error']}")
    with _lock:
        LAST.clear()
        LAST.update(out)
    return dict(out)


def start(connect: Callable, job_id, active_runs: Callable, run_now: Callable) -> Optional[threading.Thread]:
    """Run ``check`` in a thread of its own, so the app's start never waits on Lakebase or the Jobs API."""
    thread = threading.Thread(target=check, args=(connect, job_id, active_runs, run_now), daemon=True,
                              name="lakefed-app-schema-upgrade")
    thread.start()
    return thread
