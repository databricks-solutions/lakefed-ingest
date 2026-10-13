"""Lakebase access for the operations app.

Connects as the app's service principal with a short-lived, SDK-minted Lakebase credential (no
stored password), the same way the ingestion job does. The `lakefed_ingest_app_grants` job gives the
service principal instance access, a Postgres role and the table privileges.

Run locally (`streamlit run app.py` with DATABRICKS_CONFIG_PROFILE set) it connects as you.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Optional

import pandas as pd
import psycopg
from databricks.sdk import WorkspaceClient

INSTANCE = os.environ.get("LAKEBASE_INSTANCE", "lakefed-ingest")
DATABASE = os.environ.get("LAKEBASE_DATABASE", "lakefed_ingest")
_TOKEN_TTL_S = 40 * 60          # Lakebase credentials last about an hour; refresh well before

_w: Optional[WorkspaceClient] = None
_mu = threading.Lock()
_cache = {"token": None, "expires": 0.0, "host": None, "user": None}


def workspace() -> WorkspaceClient:
    global _w
    if _w is None:
        _w = WorkspaceClient()
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
    return psycopg.connect(host=host, port=int(os.environ.get("PGPORT", 5432)), dbname=DATABASE,
                           user=user, password=token, sslmode="require", autocommit=True,
                           application_name="lakefed_ingest_app")


def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def execute(sql: str, params: tuple = ()) -> int:
    """Run a write; returns the affected row count."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def execute_returning(sql: str, params: tuple = ()):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()
