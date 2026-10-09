"""Validation harness for the CT engine (workstream: validation).

Two halves:
  * `reconcile(...)` — pure, unit-tested: compares a source snapshot against the bronze table
    (dicts keyed by PK). "missing" = missed insert; "extra" = missed delete; "mismatched" = missed
    update. `ok` iff identical.
  * `SqlServerTestSource` — source-side ops against a test SQL Server DB (host/database from
    MT_TEST_SQLSERVER_HOST / MT_TEST_SQLSERVER_DB or explicit args) via **pytds over TLS**
    (pymssql fails on serverless), to stand up a CT-enabled table and generate I/U/D.

Credential model:
  * READS (the ingest engine, ct_engine.py) use Spark JDBC with the per-database JSON secret named
    in control.secret_key (scope = the sweep's `secret_scope` parameter) — no UC connection.
  * WRITES / test-data (this harness) use pytds with creds from a Databricks secret (JSON
    {user, password}, flat or nested), e.g. scope `lakefed_ingest_mt`.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Hashable, Tuple


def reconcile(source: Dict[Hashable, tuple], bronze: Dict[Hashable, tuple]) -> dict:
    """Compare source vs bronze (both keyed by PK). Returns a report; `ok` iff identical."""
    s, b = set(source), set(bronze)
    missing = sorted(s - b, key=repr)
    extra = sorted(b - s, key=repr)
    mismatched = sorted((k for k in (s & b) if source[k] != bronze[k]), key=repr)
    return {"missing": missing, "extra": extra, "mismatched": mismatched,
            "ok": not (missing or extra or mismatched)}


_USER_KEYS = ("user", "username", "userName", "login", "uid")
_PWD_KEYS = ("password", "pwd", "passwd")


def extract_creds(blob) -> Tuple[str, str]:
    """(user, password) from a creds dict or JSON string; flat or nested (e.g. {"dba": {...}}).
    Faithful to the CT demo's `_sql_creds`."""
    creds = json.loads(blob) if isinstance(blob, str) else dict(blob)

    def pick(d):
        u = next((d[k] for k in _USER_KEYS if isinstance(d.get(k), str)), None)
        p = next((d[k] for k in _PWD_KEYS if isinstance(d.get(k), str)), None)
        return (u, p) if u and p else None

    for cand in [creds] + [v for v in creds.values() if isinstance(v, dict)]:
        got = pick(cand)
        if got:
            return got
    raise ValueError("could not find user/password fields in creds blob")


class SqlServerTestSource:
    """pytds (TLS) wrapper for standing up CT test data on the Azure SQL test DB."""

    # No hardcoded test environment: host/database/secret key come from env or explicit args.
    DEFAULT_HOST = os.environ.get("MT_TEST_SQLSERVER_HOST", "")
    DEFAULT_DB = os.environ.get("MT_TEST_SQLSERVER_DB", "")

    def __init__(self, host: str, database: str, user: str, password: str, port: int = 1433):
        self.host, self.database, self.user, self.password, self.port = \
            host, database, user, password, port

    @classmethod
    def from_env(cls) -> "SqlServerTestSource":
        return cls(
            host=os.environ.get("MT_TEST_SQLSERVER_HOST", cls.DEFAULT_HOST),
            database=os.environ.get("MT_TEST_SQLSERVER_DB", cls.DEFAULT_DB),
            user=os.environ["MT_TEST_SQLSERVER_USER"],
            password=os.environ["MT_TEST_SQLSERVER_PASSWORD"],
        )

    @classmethod
    def from_secret_json(cls, blob, host: str = DEFAULT_HOST, database: str = DEFAULT_DB
                         ) -> "SqlServerTestSource":
        user, password = extract_creds(blob)
        return cls(host=host, database=database, user=user, password=password)

    @classmethod
    def from_databricks(cls, dbutils, scope: str = "lakefed_ingest_mt",
                        key: str = os.environ.get("MT_TEST_SQLSERVER_SECRET_KEY", ""),
                        host: str = DEFAULT_HOST,
                        database: str = DEFAULT_DB) -> "SqlServerTestSource":
        return cls.from_secret_json(dbutils.secrets.get(scope, key), host, database)

    def _connect(self):
        import certifi  # lazy
        import pytds    # lazy: module imports fine without the driver

        return pytds.connect(dsn=self.host, port=self.port, database=self.database,
                             user=self.user, password=self.password, autocommit=True,
                             cafile=certifi.where(), validate_host=True)

    def _exec(self, tsql: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(tsql)
            while cur.nextset():
                pass

    def create_ct_table(self, schema: str, table: str) -> None:
        self._exec("ALTER DATABASE CURRENT SET CHANGE_TRACKING = ON "
                   "(CHANGE_RETENTION = 2 DAYS, AUTO_CLEANUP = ON)")
        self._exec(
            f"IF OBJECT_ID('{schema}.{table}') IS NOT NULL DROP TABLE {schema}.{table}; "
            f"CREATE TABLE {schema}.{table} "
            f"(order_id INT NOT NULL PRIMARY KEY, amount DECIMAL(18,2), status VARCHAR(20)); "
            f"ALTER TABLE {schema}.{table} ENABLE CHANGE_TRACKING;")

    def apply_changes(self, schema: str, table: str) -> None:
        """A mixed I/U/D batch to exercise all three MERGE paths (incl. delete handling)."""
        self._exec(
            f"INSERT INTO {schema}.{table} VALUES (10, 99.00, 'new'); "
            f"UPDATE {schema}.{table} SET amount = 1.00 WHERE order_id = 1; "
            f"DELETE FROM {schema}.{table} WHERE order_id = 2;")

    def snapshot(self, schema: str, table: str) -> Dict[Hashable, tuple]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT order_id, amount, status FROM {schema}.{table}")
            return {row[0]: tuple(row) for row in cur.fetchall()}
