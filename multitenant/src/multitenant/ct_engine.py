"""Change-Tracking engine — IMPLEMENTED (workstreams A/B/F).

Fills the placeholders the consolidated sweep (``copy_data_sweep.ipynb``) calls. Every
source-touching / CT operation lives here so the orchestration (threading, FAIR pools, Lakebase
checkpointing, failure isolation) stays strategy-agnostic.

Transport = **Spark JDBC on the cluster** (`spark.read.format("jdbc")` with the Microsoft SQL
Server driver). This gives native Spark typing end-to-end — the source read lands as a typed
DataFrame straight into the Delta MERGE, with no manual Python-object -> DataFrame round-trip.
Reading ``CHANGETABLE`` from a cluster needs a direct driver (the governed ``remote_query`` path
is warehouse-only and ``CHANGETABLE`` is a TVF, not a federatable table); the
``com.microsoft.sqlserver:mssql-jdbc`` jar is attached to the sweep cluster in
``resources/lakefed_ingest_mt_sweep.yml``.

Connection: host/port come from the governed UC connection named in ``cfg['src_connection']``;
user/password come from a Databricks secret (so no plaintext cred lands in the control row).
``cfg`` is a control-row dict keyed by ``state_store.CONTROL_COLUMNS``.

Cred convention (override via env for testing):
  * scope = env ``MT_SQL_SECRET_SCOPE`` (default ``"lakefed_ingest_mt"``)
  * key   = env ``MT_SQL_SECRET_KEY``   (default ``f"{src_connection}_json"``)
  * value = JSON holding user/password (flat or nested); optional host override there too.
All heavy imports (pyspark, databricks.sdk) are lazy so this module (and its pure unit tests)
import without those installed — mirrors ``state_store.py``.
"""

from __future__ import annotations

import json
import os
from typing import List, Optional

_DEFAULT_SECRET_SCOPE = "lakefed_ingest_mt"
_JDBC_DRIVER = "com.microsoft.sqlserver.jdbc.SQLServerDriver"
_USER_KEYS = ("user", "username", "userName", "login", "uid")
_PWD_KEYS = ("password", "pwd", "passwd")
_HOST_KEYS = ("host", "server", "dsn")


# ======================================================================================
# Pure string builders — no DB, no Spark. Unit-tested in tests/ct_engine_test.py.
# ======================================================================================
def _split(csv: Optional[str]) -> List[str]:
    """Split a comma-separated control-table field (primary_key / select_list) into names."""
    return [c.strip() for c in (csv or "").split(",") if c.strip()]


def ct_projection(select_list: str, primary_key: str) -> str:
    """CHANGETABLE read projection: PK columns from the change table ``ct`` (they survive
    deletes), the remaining ``select_list`` columns from the base table ``t`` (NULL on delete),
    plus ``SYS_CHANGE_OPERATION AS op``."""
    pk = _split(primary_key)
    cols = _split(select_list)
    parts = [f"ct.[{c}] AS [{c}]" for c in pk]
    parts += [f"t.[{c}] AS [{c}]" for c in cols if c not in pk]
    return ", ".join(parts) + ", ct.SYS_CHANGE_OPERATION AS op"


def ct_join(primary_key: str) -> str:
    """ON clause joining the base table to the change table on the (composite) PK."""
    return " AND ".join(f"t.[{c}] = ct.[{c}]" for c in _split(primary_key))


def ct_read_query(cfg: dict, from_version: int) -> str:
    """Full T-SQL: CHANGETABLE(CHANGES <schema.table>, <from_version>) LEFT JOIN base ON pk."""
    full = f"[{cfg['src_schema']}].[{cfg['src_table']}]"
    return (
        f"SELECT {ct_projection(cfg['select_list'], cfg['primary_key'])} "
        f"FROM CHANGETABLE(CHANGES {full}, {int(from_version)}) ct "
        f"LEFT JOIN {full} t ON {ct_join(cfg['primary_key'])}"
    )


def merge_on(primary_key: str) -> str:
    """MERGE ON clause (Delta backtick-quoted) keyed on the (composite) PK."""
    return " AND ".join(f"tgt.`{c}` = s.`{c}`" for c in _split(primary_key))


def merge_set(select_list: str, primary_key: str) -> str:
    """UPDATE SET of the non-PK columns; degenerate all-PK table -> harmless no-op SET on the PK."""
    pk = set(_split(primary_key))
    non_pk = [c for c in _split(select_list) if c not in pk]
    cols = non_pk or _split(primary_key)
    return ", ".join(f"tgt.`{c}` = s.`{c}`" for c in cols)


def merge_insert_cols(select_list: str) -> str:
    return ", ".join(f"`{c}`" for c in _split(select_list))


def merge_insert_vals(select_list: str) -> str:
    return ", ".join(f"s.`{c}`" for c in _split(select_list))


# ======================================================================================
# JDBC connection (host/port from the UC connection, user/password from a secret). Lazy imports.
# ======================================================================================
_config_cache: dict = {}


def _extract_creds(blob):
    creds = json.loads(blob) if isinstance(blob, str) else dict(blob)

    def pick(d, keys):
        return next((d[k] for k in keys if isinstance(d.get(k), str)), None)

    for cand in [creds] + [v for v in creds.values() if isinstance(v, dict)]:
        user, pwd = pick(cand, _USER_KEYS), pick(cand, _PWD_KEYS)
        if user and pwd:
            return user, pwd, pick(cand, _HOST_KEYS)
    raise ValueError("could not find user/password fields in the SQL Server secret")


def _jdbc_config(cfg: dict) -> dict:
    """Resolve {host, port, user, password} for the source connection (cached per connection)."""
    conn_name = cfg["src_connection"]
    if conn_name in _config_cache:
        return _config_cache[conn_name]
    host = port = None
    try:  # host/port from the governed UC connection (no secret needed for these)
        from databricks.sdk import WorkspaceClient
        opts = (WorkspaceClient().connections.get(conn_name).options or {})
        host, port = opts.get("host"), opts.get("port")
    except Exception:
        pass
    from databricks.sdk.runtime import dbutils  # driver-side secret read
    scope = os.environ.get("MT_SQL_SECRET_SCOPE", _DEFAULT_SECRET_SCOPE)
    key = os.environ.get("MT_SQL_SECRET_KEY", f"{conn_name}_json")
    user, pwd, secret_host = _extract_creds(dbutils.secrets.get(scope, key))
    host = host or secret_host or os.environ.get("MT_SQL_HOST")
    conf = {"host": host, "port": int(port or 1433), "user": user, "password": pwd}
    _config_cache[conn_name] = conf
    return conf


def _jdbc_url(conf: dict, database: str) -> str:
    return (
        f"jdbc:sqlserver://{conf['host']}:{conf['port']};databaseName={database};"
        "encrypt=true;trustServerCertificate=true"
    )


def _spark():
    from pyspark.sql import SparkSession
    return SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()


def _read(spark, cfg: dict, query: str):
    """Run a T-SQL query against the source via Spark JDBC; returns a natively-typed DataFrame."""
    conf = _jdbc_config(cfg)
    return (
        spark.read.format("jdbc")
        .option("url", _jdbc_url(conf, cfg["src_database"]))
        .option("query", query)
        .option("user", conf["user"])
        .option("password", conf["password"])
        .option("driver", _JDBC_DRIVER)
        .load()
    )


# ======================================================================================
# A/B/F functions the sweep calls.
# ======================================================================================
def current_version(cfg: dict) -> int:
    """(B) Database-wide CHANGE_TRACKING_CURRENT_VERSION(). 0 if CT not enabled on the DB."""
    row = _read(_spark(), cfg,
                "SELECT CAST(CHANGE_TRACKING_CURRENT_VERSION() AS BIGINT) AS v").first()
    return int(row["v"]) if row and row["v"] is not None else 0


def min_valid_version(cfg: dict) -> Optional[int]:
    """(F) CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID('<schema>.<table>')). None when CT is not
    enabled on the table -> state_store.is_reseed_needed treats None as 'reseed'."""
    name = f"{cfg['src_schema']}.{cfg['src_table']}".replace("'", "''")
    row = _read(_spark(), cfg,
                f"SELECT CAST(CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID('{name}')) AS BIGINT) AS v"
                ).first()
    return int(row["v"]) if row and row["v"] is not None else None


def ensure_sink_table(spark, cfg: dict, sink_fqn: str) -> None:
    """Create the Delta bronze sink if absent, typed from the source via a 0-row JDBC read (so the
    JDBC dialect infers the Spark schema natively). Idempotent."""
    if spark.catalog.tableExists(sink_fqn):
        return
    cols = ", ".join(f"[{c}]" for c in _split(cfg["select_list"]))
    empty = _read(spark, cfg,
                  f"SELECT {cols} FROM [{cfg['src_schema']}].[{cfg['src_table']}] WHERE 1=0")
    empty.write.format("delta").mode("ignore").saveAsTable(sink_fqn)


def seed(spark, cfg: dict, sink_fqn: str) -> int:
    """(B seed) Full copy of the current table state into the Delta bronze sink (overwrite).

    Called after current_version() captured V0, so rows changed during the seed are reconciled on
    the first cdc cycle by the idempotent PK MERGE (no gap, no dupe)."""
    cols = ", ".join(f"[{c}]" for c in _split(cfg["select_list"]))
    df = _read(spark, cfg,
               f"SELECT {cols} FROM [{cfg['src_schema']}].[{cfg['src_table']}]").cache()
    n = df.count()
    df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(sink_fqn)
    return n


def read_and_merge_ct(spark, cfg: dict, sink_fqn: str, from_version: int) -> int:
    """(A) Read CT deltas since ``from_version`` and MERGE them into the Delta sink by PK.

    Projects PK from the change table and non-PK from the base table (NULL on delete) as a
    natively-typed JDBC DataFrame; applies an idempotent I/U/D MERGE. Returns rows merged
    (0 => no changes, caller skips the no-op)."""
    df = _read(spark, cfg, ct_read_query(cfg, from_version)).cache()
    n = df.count()
    if n == 0:
        return 0
    df.createOrReplaceTempView("ct_staged")
    spark.sql(
        f"MERGE INTO {sink_fqn} AS tgt USING ct_staged AS s ON {merge_on(cfg['primary_key'])} "
        "WHEN MATCHED AND s.op = 'D' THEN DELETE "
        f"WHEN MATCHED THEN UPDATE SET {merge_set(cfg['select_list'], cfg['primary_key'])} "
        "WHEN NOT MATCHED AND s.op <> 'D' THEN "
        f"INSERT ({merge_insert_cols(cfg['select_list'])}) "
        f"VALUES ({merge_insert_vals(cfg['select_list'])})"
    )
    return n
