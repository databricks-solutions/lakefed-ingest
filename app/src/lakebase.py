"""Lakebase access for the operations app.

Connects as the app's service principal with a short-lived, SDK-minted Lakebase credential (no
stored password), the same way the ingestion job does. The `lakefed_ingest_app_grants` job gives the
service principal instance access, a Postgres role and the table privileges.

Run locally (`python main.py` with DATABRICKS_CONFIG_PROFILE set) it connects as you.

`query`, `execute` and `transaction` borrow a connection from a small pool and hand it back when they're done, so a
page's many small reads don't each pay for a new TLS handshake and login. A connection is only ever used by one caller
at a time, and it goes back to the pool only when it's healthy, idle (no transaction left open) and in autocommit;
anything else is closed. Connections are retired well before the login they opened with would expire. `connect`
still opens a connection of its own, for the callers that hold a session lock on it (the run poller, the schema
upgrade check): a session lock must never be handed on to another caller. LAKEBASE_POOL_IDLE=0 turns the pool off.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

import psycopg
from databricks.sdk import WorkspaceClient

import app_product

INSTANCE = os.environ.get("LAKEBASE_INSTANCE", "lakefed-ingest")
DATABASE = os.environ.get("LAKEBASE_DATABASE", "lakefed_ingest")
_TOKEN_TTL_S = 40 * 60          # Lakebase credentials last about an hour; refresh well before
POOL_IDLE = int(os.environ.get("LAKEBASE_POOL_IDLE", 8))   # idle connections kept per app process; 0 = no pool
POOL_MAX_AGE_S = 10 * 60        # retire a connection this long after it opened, well inside its credential's life
POOL_MAX_IDLE_S = 2 * 60        # and one that sat unused this long, before a proxy or the server drops it
POOL_CHECK_AFTER_S = 30         # one idle longer than this answers a select 1 before it's handed out

_w: Optional[WorkspaceClient] = None
_mu = threading.Lock()
_cache = {"token": None, "expires": 0.0, "host": None, "user": None}
_pool_mu = threading.Lock()
_idle: list = []                # (connection, opened_at, returned_at), monotonic seconds, newest last


def _new_client() -> WorkspaceClient:
    """The app's SDK client, identifying itself as the product (app_product) in every API call's user agent."""
    return WorkspaceClient(product=app_product.PRODUCT_NAME, product_version=app_product.PRODUCT_VERSION)


def workspace() -> WorkspaceClient:
    global _w
    if _w is None:
        _w = _new_client()
    return _w


def _conninfo():
    with _mu:
        w = workspace()
        if _cache["host"] is None:
            _cache["host"] = os.environ.get("PGHOST") or w.database.get_database_instance(name=INSTANCE).read_write_dns
            _cache["user"] = os.environ.get("PGUSER") or w.current_user.me().user_name
        if time.time() >= _cache["expires"]:
            cred = w.database.generate_database_credential(request_id=str(uuid.uuid4()),
                                                           instance_names=[INSTANCE])
            _cache["token"], _cache["expires"] = cred.token, time.time() + _TOKEN_TTL_S
        return _cache["host"], _cache["user"], _cache["token"]


def connect():
    host, user, token = _conninfo()
    # No server-side prepared statements: a pooled connection lives for minutes, and a plan prepared before a schema
    # upgrade adds a column fails afterwards with "cached plan must not change result type".
    conn = psycopg.connect(host=host, port=int(os.environ.get("PGPORT", 5432)), dbname=DATABASE,
                           user=user, password=token, sslmode="require", autocommit=True,
                           application_name="lakefed_ingest_app")
    conn.prepare_threshold = None
    return conn


def _reusable(conn) -> bool:
    """Whether a connection can be handed to the next caller: open, in autocommit, with no transaction left open."""
    try:
        return (not conn.closed and not conn.broken and conn.autocommit
                and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE)
    except Exception:
        return False


def _answers(conn) -> bool:
    """Whether an idle connection still reaches the server, so a dropped one is never handed to a caller."""
    try:
        conn.execute("select 1").fetchone()
        return _reusable(conn)
    except Exception:
        return False


def _close_quietly(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _checkout():
    """A connection for one caller and when it opened: the newest idle one still fresh, or a new one."""
    while True:
        with _pool_mu:
            if not _idle:
                break
            conn, opened, returned = _idle.pop()
        now = time.monotonic()
        fresh = now - opened < POOL_MAX_AGE_S and now - returned < POOL_MAX_IDLE_S and _reusable(conn)
        if fresh and (now - returned < POOL_CHECK_AFTER_S or _answers(conn)):
            return conn, opened
        _close_quietly(conn)
    return connect(), time.monotonic()


def _checkin(conn, opened, healthy: bool) -> None:
    """Hand a connection back after use. Closed instead when the caller failed, the pool is full or off, or it isn't
    clean enough to reuse."""
    now = time.monotonic()
    if healthy and now - opened < POOL_MAX_AGE_S and _reusable(conn):
        with _pool_mu:
            if len(_idle) < POOL_IDLE:
                _idle.append((conn, opened, now))
                return
    _close_quietly(conn)


@contextlib.contextmanager
def _borrowed():
    """A pooled connection for the block; any error in the block closes it rather than handing it on."""
    conn, opened = _checkout()
    healthy = False
    try:
        yield conn
        healthy = True
    finally:
        _checkin(conn, opened, healthy)


def close_idle() -> None:
    """Close every idle pooled connection (on shutdown, and between tests)."""
    with _pool_mu:
        idle = [conn for conn, _, _ in _idle]
        _idle.clear()
    for conn in idle:
        _close_quietly(conn)


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def query(sql: str, params: tuple = ()) -> List[dict]:
    """Rows as JSON-ready dicts (timestamps as ISO 8601 strings, numerics as floats)."""
    with _borrowed() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [{c: _jsonable(v) for c, v in zip(cols, row)} for row in cur.fetchall()]


def execute(sql: str, params: tuple = ()) -> None:
    """A statement that returns no rows, committed at once (autocommit) on a connection no one else is using."""
    with _borrowed() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def fetch(cur, sql: str, params: tuple = ()) -> List[dict]:
    """Like ``query``, on a cursor of an open transaction."""
    cur.execute(sql, params)
    cols = [d.name for d in cur.description]
    return [{c: _jsonable(v) for c, v in zip(cols, row)} for row in cur.fetchall()]


@contextlib.contextmanager
def transaction():
    """A cursor in one transaction: committed when the block succeeds, rolled back on any error
    (advisory xact locks taken inside are released either way). The connection goes back to the pool only after a
    clean commit, and back in autocommit."""
    conn, opened = _checkout()
    healthy = False
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            yield cur
        conn.commit()
        conn.autocommit = True
        healthy = True
    except BaseException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _checkin(conn, opened, healthy)
