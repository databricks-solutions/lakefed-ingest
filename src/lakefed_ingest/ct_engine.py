"""Source engine: Spark JDBC reads, seeds / full loads, and the Change-Tracking (CT) strategy.

Fills the functions the ingestion notebook (``ingest.ipynb``) calls. Every source-touching
operation lives here so the orchestration (threading, FAIR pools, Lakebase checkpointing, failure
isolation) stays strategy-agnostic. Dialect-specific SQL (JDBC URL, driver, quoting, SELECT /
size / bounds queries) comes from the row's source adapter (``source_adapters``, chosen by
``control.src_type``); the CT functions (CHANGETABLE, detection, retention guard) are SQL Server-only.

Transport = **Spark JDBC on the cluster** (`spark.read.format("jdbc")`; the SQL Server and
PostgreSQL drivers ship with Databricks Runtime, Oracle / MySQL need a cluster library). No SQL
warehouse and no UC connection are required: the source read lands as a natively-typed DataFrame
straight into the Delta write/MERGE. A row with ``use_remote_query`` set reads through the Unity
Catalog connection named in ``src_connection`` instead (``remote_query``, on the same cluster), which
holds the credentials: no secret, host or port.

Single source read, no caching: ``seed`` streams the JDBC read straight into the Delta overwrite and
``read_and_merge_ct`` feeds the CHANGETABLE read straight into the MERGE; row counts come from Delta
commit metrics, never from ``count()`` on the source DataFrame. Delta MERGE's own source
materialization (``spark.databricks.delta.merge.materializeSource``, default ``auto``) is what
guarantees a non-Delta (JDBC) source is evaluated once — we add no ``.cache()`` of our own.

Write modes (``control.write_mode``): ``merge`` (default) as above; ``append`` keeps a change log:
``read_and_append_ct`` and the seed append rows tagged with the operation and CT version instead of
merging or replacing (see the write-mode section below).

Connection (per control row ``cfg``, keyed by ``state_store.CONTROL_COLUMNS``):
  * host/port: ``cfg['src_host']``/``cfg['src_port']`` -> (legacy) the UC connection named in
    ``cfg['src_connection']`` -> the secret's own host field -> env ``LAKEFED_SQL_HOST``.
  * port: ``cfg['src_port']`` -> the adapter's default (1433 / 1521 / 5432 / 3306).
  * credentials: secret ``scope``/``key`` holding JSON ``{user, password}`` (flat or nested):
      - scope = env ``LAKEFED_SQL_SECRET_SCOPE`` (default ``"lakefed_ingest"``; may be an Azure Key
        Vault-backed scope — note AKV secret names allow only alphanumerics and dashes, see
        ``validate_akv_secret_name``)
      - key   = ``cfg['secret_key']`` -> env ``LAKEFED_SQL_SECRET_KEY`` -> legacy ``f"{src_connection}_json"``
    Credentials are cached per (scope, key) — one secret per tenant database — never per server.
  * TLS: env ``LAKEFED_SQL_TRUST_SERVER_CERT`` (default ``true`` for dev; production should set
    ``false`` and present a trusted server certificate) — mapped per adapter (SQL Server
    trustServerCertificate, PostgreSQL sslmode, MySQL sslMode).
  * extra Spark JDBC read options: adapter defaults (e.g. fetchsize) + ``cfg['jdbc_options']`` (JSON).
  * through a connection (``cfg['use_remote_query']``): ``remote_query(<src_connection>, database =>
    <src_database>, query => ...)`` (Oracle: ``service_name``), with the same read options as its
    options; the change read's retention guard is then part of the query (``ct_read_query_guarded``).
Concurrency: every function that does query work takes an optional ``slots``
(``parallel.WorkSlots``) and runs each UNIT of work — one source query, one read->write/MERGE, one
partition, the staging->target swap — while holding exactly ONE slot. The sweep creates one
``WorkSlots(parallelism)`` per run, so ``parallelism`` caps all concurrent query work, whatever its
type. Slots are never held while waiting on other work. With ``slots=None`` (unit tests, ad-hoc
calls) work runs unthrottled.

All heavy imports (pyspark, databricks.sdk) are lazy so this module (and its pure unit tests)
import without those installed — mirrors ``state_store.py``.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import math
import os
import random
import re
import threading
import time
import uuid
from typing import List, Optional, Tuple

import source_adapters

_DEFAULT_SECRET_SCOPE = "lakefed_ingest"
_SQLSERVER = source_adapters.get_adapter(source_adapters.SQLSERVER)
_USER_KEYS = ("user", "username", "userName", "login", "uid")
_PWD_KEYS = ("password", "pwd", "passwd")
_HOST_KEYS = ("host", "server", "dsn")


# ======================================================================================
# Pure string builders — no DB, no Spark. Unit-tested in tests/ct_engine_test.py.
# ======================================================================================
def _split(csv: Optional[str]) -> List[str]:
    """Split a comma-separated control-table field (primary_key / select_list) into names."""
    return [c.strip() for c in (csv or "").split(",") if c.strip()]


def delta_column(name: str) -> str:
    """A column name quoted for Delta SQL. Always quoted, and any backtick inside the name is doubled,
    so ``a`b`` reaches Delta as the column it is."""
    return "`" + str(name).replace("`", "``") + "`"


def _without(cols: List[str], names: List[str]) -> List[str]:
    """``cols`` minus ``names``, compared case-insensitively (SQL Server and Delta both resolve
    column names that way, so ``OrderLineID`` in primary_key and ``orderLineID`` in
    select_list are the same column).

    Raises when ``cols`` has two names that differ only by case: on a case-sensitive collation they
    are two columns, and matching one of them to the key would silently drop the other (Delta can't
    hold both anyway)."""
    seen = {}
    for c in cols:
        if seen.setdefault(c.lower(), c) != c:
            raise ValueError(f"select_list has columns that differ only by case ({seen[c.lower()]}, {c}); "
                             "Delta compares column names ignoring case, so they can't both be written")
    drop = {n.lower() for n in names}
    return [c for c in cols if c.lower() not in drop]


def ct_projection(select_list: str, primary_key: str, append: bool = False) -> str:
    """CHANGETABLE read projection: PK columns from the change table ``ct`` (they survive
    deletes), the remaining ``select_list`` columns from the base table ``t`` (NULL on delete),
    plus ``SYS_CHANGE_OPERATION AS op``, or, for an append write, the operation and the change's
    commit version as the sink's ``__change_op`` and ``__change_version`` columns."""
    pk = _split(primary_key)
    parts = [f"ct.[{c}] AS [{c}]" for c in pk]
    parts += [f"t.[{c}] AS [{c}]" for c in _without(_split(select_list), pk)]
    if append:
        return ", ".join(parts) + (f", ct.SYS_CHANGE_OPERATION AS [{OP_COLUMN}], "
                                   f"CAST(ct.SYS_CHANGE_VERSION AS BIGINT) AS [{VERSION_COLUMN}]")
    return ", ".join(parts) + ", ct.SYS_CHANGE_OPERATION AS op"


def ct_join(primary_key: str) -> str:
    """ON clause joining the base table to the change table on the (composite) PK."""
    return " AND ".join(f"t.[{c}] = ct.[{c}]" for c in _split(primary_key))


def ct_read_query(cfg: dict, from_version: int, to_version: Optional[int] = None) -> str:
    """Full T-SQL: CHANGETABLE(CHANGES <schema.table>, <from_version>) LEFT JOIN base ON pk.

    An insert or update whose base row is gone was deleted after the change table was read (READ
    COMMITTED reads the two sides at different times). It is left out: its delete has a later
    version, so the next cycle reads it, and keeping the row would write it with NULL columns.

    ``to_version`` (the database version captured before change detection, which the checkpoint
    then advances to) bounds the read: a change committed after it is left for the next cycle, which
    starts from it, instead of being read now and again then."""
    full = f"[{cfg['src_schema']}].[{cfg['src_table']}]"
    first_pk = _split(cfg["primary_key"])[0]
    where = [f"(ct.SYS_CHANGE_OPERATION = 'D' OR t.[{first_pk}] IS NOT NULL)"]
    if to_version is not None:
        where.append(f"ct.SYS_CHANGE_VERSION <= {int(to_version)}")
    return (
        f"SELECT {ct_projection(cfg['select_list'], cfg['primary_key'], append=is_append(cfg))} "
        f"FROM CHANGETABLE(CHANGES {full}, {int(from_version)}) ct "
        f"LEFT JOIN {full} t ON {ct_join(cfg['primary_key'])} "
        f"WHERE {' AND '.join(where)}"
    )


def merge_on(primary_key: str) -> str:
    """MERGE ON clause (Delta backtick-quoted) keyed on the (composite) PK."""
    return " AND ".join(f"tgt.{delta_column(c)} = s.{delta_column(c)}" for c in _split(primary_key))


def merge_set(select_list: str, primary_key: str) -> str:
    """UPDATE SET of the non-PK columns; degenerate all-PK table -> harmless no-op SET on the PK.

    Targets are UNQUALIFIED (`` `c` = s.`c` ``): under ``MERGE WITH SCHEMA EVOLUTION`` a column the
    target doesn't have yet only resolves unqualified (``tgt.`c` `` fails on DBR 18)."""
    non_pk = _without(_split(select_list), _split(primary_key))
    cols = non_pk or _split(primary_key)
    return ", ".join(f"{delta_column(c)} = s.{delta_column(c)}" for c in cols)


def merge_insert_cols(select_list: str) -> str:
    return ", ".join(delta_column(c) for c in _split(select_list))


def merge_insert_vals(select_list: str) -> str:
    return ", ".join(f"s.{delta_column(c)}" for c in _split(select_list))


# ======================================================================================
# JDBC connection — pure helpers (unit-tested) + resolution with lazy imports.
# ======================================================================================
_AKV_NAME = re.compile(r"^[0-9A-Za-z-]{1,127}$")


def validate_akv_secret_name(name: str) -> str:
    """Return ``name`` if it is a valid Azure Key Vault secret name, else raise ValueError.

    AKV secret names allow only alphanumerics and dashes (1-127 chars) — e.g. ``tenant-db-001``,
    not ``tenant_db_001``. Databricks-backed scopes are more permissive, so this is not enforced at
    runtime (the scope type isn't cheaply detectable); use it when generating ``control.secret_key``
    values for an AKV-backed scope.
    """
    if not isinstance(name, str) or not _AKV_NAME.match(name):
        raise ValueError(
            f"invalid Azure Key Vault secret name {name!r}: use 1-127 alphanumerics or dashes")
    return name


def secret_ref(cfg: dict) -> Tuple[str, str]:
    """(scope, key) holding this task's SQL Server credentials — the creds cache key.

    key precedence: ``cfg['secret_key']`` -> env ``LAKEFED_SQL_SECRET_KEY`` -> legacy
    ``f"{src_connection}_json"``. Never derived from the server alone: each tenant database may
    carry its own credential even when it shares a server with others.
    """
    scope = os.environ.get("LAKEFED_SQL_SECRET_SCOPE", _DEFAULT_SECRET_SCOPE)
    key = cfg.get("secret_key") or os.environ.get("LAKEFED_SQL_SECRET_KEY")
    if not key and cfg.get("src_connection"):
        key = f"{cfg['src_connection']}_json"
    if not key:
        raise ValueError(f"control row {cfg.get('id')}: no secret_key (and no legacy src_connection)")
    return scope, key


def config_cache_key(host: str, port: int, scope: str, key: str) -> tuple:
    """Resolved-connection cache key: (host, port, scope, secret key)."""
    return (host, int(port), scope, key)


def trust_server_certificate() -> bool:
    return os.environ.get("LAKEFED_SQL_TRUST_SERVER_CERT", "true").strip().lower() in ("1", "true", "yes")


def jdbc_url(host: str, port: int, database: str, trust_cert: bool, src_type: str = None) -> str:
    return source_adapters.get_adapter(src_type).jdbc_url(host, port, database, trust_cert)


_lock = threading.Lock()
_creds_cache: dict = {}    # (scope, key) -> (user, password, host-from-secret)
_uc_host_cache: dict = {}  # legacy: UC connection name -> (host, port)
_config_cache: dict = {}   # config_cache_key(...) -> {host, port, user, password}


def _extract_creds(blob):
    creds = json.loads(blob) if isinstance(blob, str) else dict(blob)

    def pick(d, keys):
        return next((d[k] for k in keys if isinstance(d.get(k), str)), None)

    for cand in [creds] + [v for v in creds.values() if isinstance(v, dict)]:
        user, pwd = pick(cand, _USER_KEYS), pick(cand, _PWD_KEYS)
        if user and pwd:
            return user, pwd, pick(cand, _HOST_KEYS)
    raise ValueError("could not find user/password fields in the source credential secret")


def _creds(scope: str, key: str):
    with _lock:
        if (scope, key) not in _creds_cache:
            from databricks.sdk.runtime import dbutils  # driver-side secret read
            _creds_cache[(scope, key)] = _extract_creds(dbutils.secrets.get(scope, key))
        return _creds_cache[(scope, key)]


def _uc_host(conn_name: str):
    """Legacy fallback: host/port from a UC connection (no creds come from it)."""
    with _lock:
        if conn_name not in _uc_host_cache:
            host = port = None
            try:
                from databricks.sdk import WorkspaceClient
                opts = (WorkspaceClient().connections.get(conn_name).options or {})
                host, port = opts.get("host"), opts.get("port")
            except Exception:
                pass
            _uc_host_cache[conn_name] = (host, port)
        return _uc_host_cache[conn_name]


# The logins typed into the operations app, as recorded in lakefed_ingest.jdbc_login_write: (the scope they are in,
# {secret key: the JDBC connection its newest login was typed for, with that connection's host, port and source type}).
# Set once a run from Lakebase (guard_typed_logins); None until then, so a caller that never sets it reads as before.
_typed_logins: Optional[Tuple[str, dict]] = None


def guard_typed_logins(scope: str, logins: dict) -> None:
    """Hold this run's reads from ``scope`` to the logins typed there: each goes only to the host, port and source type
    of the connection its newest login was typed for (state_store.typed_logins reads them)."""
    global _typed_logins
    _typed_logins = (scope, dict(logins))


def typed_login_refusal(scope: str, key: str, host: str, port: int, src_type: str, typed) -> Optional[str]:
    """Why the login under (scope, key) may not be sent to this host, or None when it may. Only a key that holds a login
    typed into the app is checked: it goes to the host of the connection it was typed for and nowhere else, whatever
    the control row or the discovery spec says. A key no one typed (an admin's secret) is not checked here."""
    if typed is None or scope != typed[0] or key not in typed[1]:
        return None
    login = typed[1][key]
    if not login.get("src_host"):
        return (f"the login under {scope}/{key} was typed for the JDBC connection {login.get('connection')}, which is "
                "not stored, so it is sent nowhere")
    login_type = str(login.get("src_type") or source_adapters.SQLSERVER).lower()
    login_port = int(login.get("src_port") or source_adapters.get_adapter(login_type).default_port)
    if (str(host).strip().lower(), int(port), str(src_type).lower()) != (str(login["src_host"]).strip().lower(),
                                                                         login_port, login_type):
        return (f"the login under {scope}/{key} was typed for the JDBC connection {login.get('connection')} "
                f"({login['src_host']}:{login_port}), so it is not sent to {host}:{port}")
    return None


def _jdbc_config(cfg: dict) -> dict:
    """Resolve {host, port, user, password} for this control row (cached per host/port/secret). A login typed into the
    app is refused for any host but its own connection's (typed_login_refusal)."""
    scope, key = secret_ref(cfg)
    host, port = cfg.get("src_host"), cfg.get("src_port")
    if not host and cfg.get("src_connection"):
        host, uc_port = _uc_host(cfg["src_connection"])
        port = port or uc_port
    port = int(port or source_adapters.for_cfg(cfg).default_port)
    typed = _typed_logins
    if typed is not None and host and scope == typed[0] and key in typed[1]:
        refusal = typed_login_refusal(scope, key, host, port, source_adapters.for_cfg(cfg).src_type, typed)
        if refusal:
            raise ValueError(f"control row {cfg.get('id')}: {refusal}")
    user, pwd, secret_host = _creds(scope, key)
    if typed is not None and not host and scope == typed[0] and key in typed[1]:
        raise ValueError(f"control row {cfg.get('id')}: a login typed into the app goes only to its connection's host, "
                         "and this row names none")
    host = host or secret_host or os.environ.get("LAKEFED_SQL_HOST")
    if not host:
        raise ValueError(f"control row {cfg.get('id')}: no src_host could be resolved")
    ck = config_cache_key(host, port, scope, key)
    with _lock:
        if ck not in _config_cache:
            _config_cache[ck] = {"host": host, "port": port, "user": user, "password": pwd}
        return _config_cache[ck]


def uses_remote_query(cfg: dict) -> bool:
    """The row reads through the Unity Catalog connection ``src_connection`` (``remote_query``), not
    Spark JDBC with a secret."""
    v = cfg.get("use_remote_query")
    return v is True or str(v).lower() == "true"


def remote_query_sql(cfg: dict, query: str) -> Tuple[str, dict]:
    """``SELECT * FROM remote_query(...)`` running ``query`` (in the source's dialect) through the row's
    connection, and its named parameters for ``spark.sql``. The connection, the database (Oracle: the
    service name) and the query go in as parameter markers, so the query needs no escaping. The read
    options (adapter defaults + ``jdbc_options``) become the function's options, e.g. ``fetchsize``;
    one it doesn't support fails the read."""
    adapter = source_adapters.for_cfg(cfg)
    args = {"conn": cfg["src_connection"], "db": cfg["src_database"], "q": query}
    named = [f"{adapter.remote_database_option} => :db", "query => :q"]
    for i, (name, value) in enumerate(adapter.read_options(cfg).items()):
        named.append(f"{quote_delta_ident(name)} => :o{i}")
        args[f"o{i}"] = value
    return f"SELECT * FROM remote_query(:conn, {', '.join(named)})", args


def _spark():
    from pyspark.sql import SparkSession
    return SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()


def _read(spark, cfg: dict, query: str, session_init: Optional[str] = None):
    """Run a query (in the source's dialect) via Spark JDBC, or through the row's Unity Catalog
    connection (``use_remote_query``); returns a natively-typed DataFrame. Lazy: the source is only
    queried when the DataFrame is consumed (once, by the write/MERGE)."""
    if uses_remote_query(cfg):
        if session_init:
            raise ValueError(f"control row {cfg.get('id')}: a read through a connection runs no session statement")
        sql, args = remote_query_sql(cfg, query)
        return spark.sql(sql, args=args)
    adapter = source_adapters.for_cfg(cfg)
    conf = _jdbc_config(cfg)
    return (
        spark.read.format("jdbc")
        .options(**adapter.read_options(cfg))
        .option("url", adapter.jdbc_url(conf["host"], conf["port"], cfg["src_database"],
                                        trust_server_certificate()))
        .option("query", query)
        .option("user", conf["user"])
        .option("password", conf["password"])
        .option("driver", adapter.driver)
        .options(**({"sessionInitStatement": session_init} if session_init else {}))
        .load()
    )


def _unit(slots, cfg: Optional[dict] = None):
    """One unit of query work: hold ONE slot of the sweep's budget (no-op when ``slots`` is None) and,
    for a unit that queries ``cfg``'s source, a permit of that source when the sweep caps queries per
    source (``max_queries_per_source``). Delta-only units pass no ``cfg``."""
    if slots is None:
        return contextlib.nullcontext()
    return slots.acquire(source_key(cfg) if cfg is not None else None)


def source_key(cfg: dict) -> str:
    """The server a control row queries, as ``max_queries_per_source`` counts it: its Unity Catalog
    connection when it reads through one (or takes its host from it), else ``host:port``. Rows that
    read one server through different names are counted separately."""
    if uses_remote_query(cfg) or (cfg.get("src_connection") and not cfg.get("src_host")):
        return f"connection:{str(cfg['src_connection']).lower()}"
    port = int(cfg.get("src_port") or source_adapters.for_cfg(cfg).default_port)
    return f"host:{str(cfg.get('src_host') or '').lower()}:{port}"


def quote_delta_ident(name: str) -> str:
    """Backtick-quoted Databricks SQL identifier (`` ` `` doubled). A name that is already a quoted
    identifier is kept as it is: control rows written for earlier versions, which didn't quote sink
    names, had to quote those that needed it themselves."""
    name = str(name)
    if len(name) >= 2 and name[0] == name[-1] == "`" and "`" not in name[1:-1].replace("``", ""):
        return name
    return delta_column(name)


def sink_fqn(cfg: dict) -> str:
    """The row's sink as a quoted three-part name, so catalogs, schemas and tables whose names need
    quoting (``01-sales-dev``) work in every statement that names the sink."""
    return ".".join(quote_delta_ident(cfg[k]) for k in ("sink_catalog", "sink_schema", "sink_table"))


def _with_table_suffix(fqn: str, suffix: str) -> str:
    """``fqn`` with ``suffix`` appended to its table name, inside the quotes when it is quoted."""
    return f"{fqn[:-1]}{suffix}`" if fqn.endswith("`") else f"{fqn}{suffix}"


def staging_table_name(sink_fqn: str, cfg: Optional[dict] = None) -> str:
    """Staging table a partitioned seed loads into before the atomic swap (same catalog.schema).
    Consolidated sinks get one per client — several clients may seed the same sink at once."""
    cid = client_id(cfg) if cfg else None
    if cid is None:
        return _with_table_suffix(sink_fqn, "__seed_staging")
    slug = re.sub(r"[^a-z0-9_]", "_", cid.lower())[:40]
    return _with_table_suffix(sink_fqn, f"__seed_staging__{slug}_{hashlib.sha1(cid.encode()).hexdigest()[:8]}")


# ======================================================================================
# Consolidated sinks (optional, per control row): many source databases -> one sink table.
# A row with ``control.client_id`` set writes into a shared sink: every row it writes carries the
# value in column ``__client_id__`` (CLIENT_COLUMN — a reserved-looking name so it can't clash with
# a real source column such as ``client_id``); MERGEs match on (__client_id__, PK); seeds and full
# reloads replace only that client's rows (``INSERT ... BY NAME REPLACE WHERE __client_id__ = ...``). Rows with client_id NULL own their
# sink outright (the default). Mixing the two on one sink is rejected (planner + ensure_sink_table).
# ======================================================================================
CLIENT_COLUMN = "__client_id__"
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")   # same rule as the control CHECK constraint


def client_id(cfg: dict) -> Optional[str]:
    """The row's client_id (consolidated sink), or None for a dedicated sink."""
    v = cfg.get("client_id")
    if v is None or str(v).strip() == "":
        return None
    v = str(v).strip()
    if not _CLIENT_ID.match(v):
        raise ValueError(f"invalid client_id {v!r}: use 1-128 of A-Z a-z 0-9 _ . : -")
    return v


_ATTR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def client_attrs(cfg: dict) -> List[Tuple[str, Optional[str]]]:
    """The row's identity columns: ``control.client_attrs``, a JSON object {column: value}, as
    [(column, value as text)] in the object's order. Every row the control row writes carries them,
    as STRING columns next to __client_id__ (e.g. the tenant name or the source server), so a
    shared sink says where each row came from. Names are letters, digits and ``_``, not starting with
    ``__`` (lakefed's own columns); values are scalars (NULL allowed)."""
    raw = cfg.get("client_attrs")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return []
    attrs = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(attrs, dict):
        raise ValueError("client_attrs must be a JSON object {column: value}")
    out, seen = [], set()
    for name, value in attrs.items():
        if not _ATTR_NAME.match(str(name)) or str(name).startswith("__"):
            raise ValueError(f"invalid client_attrs column {name!r}: use letters, digits and _ "
                             "(1-128 characters, not starting with __)")
        if name.lower() in seen:
            raise ValueError(f"client_attrs column {name!r} is given twice")
        if isinstance(value, (dict, list)):
            raise ValueError(f"client_attrs column {name!r}: the value must be a string, number, boolean or null")
        seen.add(name.lower())
        out.append((name, None if value is None else str(value)))
    return out


# Write modes (``control.write_mode``). 'merge' (the default) keeps the sink in step with the source
# table: changes MERGE by key, seeds and full reloads replace. 'append' keeps a change log instead:
# every change, and every seed or full-load snapshot, is appended as new rows carrying APPEND_COLUMNS:
# the operation (I / U / D from Change Tracking, S for a snapshot row), the CT version (the change's
# commit version; the version captured before a seed; NULL for a full load), when the rows were
# extracted and the ingest_run that wrote them. Sink rows are never updated or deleted, so many source
# databases can fan in to one sink (with client_id) and downstream jobs (e.g. SCD2) read the log.
MERGE, APPEND = "merge", "append"
OP_COLUMN, VERSION_COLUMN = "__change_op", "__change_version"
EXTRACTED_AT_COLUMN, RUN_ID_COLUMN = "__extracted_at", "__run_id"
APPEND_COLUMNS = (OP_COLUMN, VERSION_COLUMN, EXTRACTED_AT_COLUMN, RUN_ID_COLUMN)
SNAPSHOT_OP = "S"


def write_mode(cfg: dict) -> str:
    """The row's write mode: 'merge' (NULL/empty) or 'append'."""
    mode = str(cfg.get("write_mode") or MERGE).strip().lower()
    if mode not in (MERGE, APPEND):
        raise ValueError(f"invalid write_mode {cfg.get('write_mode')!r}: use merge or append")
    return mode


def is_append(cfg: dict) -> bool:
    return write_mode(cfg) == APPEND


def write_columns(cfg: dict) -> List[str]:
    """Columns written to the sink: select_list, plus __client_id__ for a consolidated sink, the
    client_attrs columns, and the APPEND_COLUMNS for an append write."""
    cols = _split(cfg["select_list"])
    cols += [CLIENT_COLUMN] if client_id(cfg) else []
    cols += [name for name, _ in client_attrs(cfg)]
    return cols + list(APPEND_COLUMNS) if is_append(cfg) else cols


def merge_keys(cfg: dict) -> List[str]:
    """MERGE match keys: (__client_id__, PK) for a consolidated sink, else the PK."""
    pk = _split(cfg["primary_key"])
    return [CLIENT_COLUMN] + pk if client_id(cfg) else pk


def client_predicate(cfg: dict) -> str:
    """Delta predicate selecting this client's rows (the value is validated by ``client_id``)."""
    return f"`{CLIENT_COLUMN}` = '{client_id(cfg)}'"


def _tag(df, cfg: dict, meta=()):
    """Prepend the client_id literal (consolidated sink), the client_attrs literals and ``meta`` (an
    append write's metadata: literal columns, or names of ``df`` columns to move to the front, from
    ``append_meta``) to a source DataFrame; no-op when there is nothing to add. FIRST, so a new sink
    has them within Delta's stats columns (first 32 by default); liquid clustering on __client_id__
    requires stats; wide tables would otherwise fail to cluster."""
    # Columns go in as df[name], never as bare names: PySpark turns a bare name into a column through a
    # JVM class lookup by short name, which misses the driver's whole classpath while holding the
    # notebook class loader's lock, once per column per table, and every table worker queues on it.
    cid = client_id(cfg)
    lead = [_lit(cid).alias(CLIENT_COLUMN)] if cid is not None else []
    lead += [_lit(value, "string").alias(name) for name, value in client_attrs(cfg)]
    lead += [df[m] if isinstance(m, str) else m for m in meta]
    if not lead:
        return df
    moved = {m for m in meta if isinstance(m, str)}
    return df.select(*lead, *[df[c] for c in df.columns if c not in moved])


def _lit(value, dtype: Optional[str] = None):
    from pyspark.sql import functions as F
    col = F.lit(value)
    return col.cast(dtype) if dtype else col


def append_meta(cfg: dict, run_id=None, version=None, changes: bool = False) -> list:
    """An append write's APPEND_COLUMNS for ``_tag`` (empty in merge mode). A change read returns
    the operation and version itself (``ct_projection``); anything else is a snapshot: operation 'S'
    at ``version``. Typed even when NULL, so the 0-row read that creates a sink types them too."""
    if not is_append(cfg):
        return []
    if changes:
        head = [OP_COLUMN, VERSION_COLUMN]
    else:
        head = [_lit(SNAPSHOT_OP, "string").alias(OP_COLUMN), _lit(version, "bigint").alias(VERSION_COLUMN)]
    return head + [_lit(datetime.datetime.now(datetime.timezone.utc), "timestamp").alias(EXTRACTED_AT_COLUMN),
                   _lit(run_id, "bigint").alias(RUN_ID_COLUMN)]


def merge_on_cfg(cfg: dict) -> str:
    """MERGE ON for a control row: the match keys, plus — consolidated sink — a LITERAL client
    predicate on the target, so Delta prunes to this client's (clustered) files and scopes conflict
    detection to them instead of every client's rows."""
    on = merge_on(", ".join(merge_keys(cfg)))
    return f"{on} AND tgt.{client_predicate(cfg)}" if client_id(cfg) else on


def create_sink_sql(sink_fqn: str, view: str, cfg: dict) -> str:
    """ONE atomic statement creating the sink from a 0-row view: type widening on, and — consolidated
    sink — liquid clustering on __client_id__. A single CTAS means a sink can never exist half-configured
    (two clients racing to create it: one wins, the other sees it exists)."""
    cluster = f"CLUSTER BY (`{CLIENT_COLUMN}`) " if client_id(cfg) else ""
    # Type widening lets schema-evolving writes apply safe widenings (int->bigint, float->double,
    # decimal precision) when a client's column type is wider. Existing sinks:
    # ALTER TABLE <sink> SET TBLPROPERTIES ('delta.enableTypeWidening' = 'true').
    return (f"CREATE TABLE IF NOT EXISTS {sink_fqn} {cluster}"
            f"TBLPROPERTIES ('delta.enableTypeWidening' = 'true') AS SELECT * FROM {view}")


# ======================================================================================
# Delta write-conflict retry: several clients (or jobs) may write one sink concurrently. A Delta
# concurrency conflict retries the whole unit (re-read + write) with exponential backoff and full
# jitter, holding NO slot while it waits. Re-reading is safe: writes are idempotent (MERGE by key,
# client-scoped replace) and checkpoints advance only after the commit.
# ======================================================================================
WRITE_RETRY_ATTEMPTS = int(os.environ.get("LAKEFED_WRITE_RETRY_ATTEMPTS", "6"))
WRITE_RETRY_BASE_S = float(os.environ.get("LAKEFED_WRITE_RETRY_BASE_S", "1"))
WRITE_RETRY_MAX_S = float(os.environ.get("LAKEFED_WRITE_RETRY_MAX_S", "60"))
_CONFLICT_MARKERS = (
    "ConcurrentAppendException", "ConcurrentDeleteReadException", "ConcurrentDeleteDeleteException",
    "ConcurrentTransactionException", "MetadataChangedException", "ProtocolChangedException",
    "DELTA_CONCURRENT_", "DELTA_METADATA_CHANGED", "DELTA_PROTOCOL_CHANGED",
    "TABLE_OR_VIEW_ALREADY_EXISTS",            # two clients creating the same consolidated sink
)


def is_write_conflict(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(m in text for m in _CONFLICT_MARKERS)


def backoff_delay(attempt: int, base: float = None, cap: float = None, rand=random.random) -> float:
    """Full-jitter exponential backoff for retry ``attempt`` (0-based)."""
    base = WRITE_RETRY_BASE_S if base is None else base
    cap = WRITE_RETRY_MAX_S if cap is None else cap
    return rand() * min(cap, base * (2 ** attempt))


def with_write_retry(fn, what: str = "write", attempts: int = None, sleep=time.sleep, rand=random.random):
    """Run ``fn()``; on a Delta write conflict retry up to ``attempts`` times in total. Other errors
    (and the last conflict) propagate unchanged."""
    attempts = WRITE_RETRY_ATTEMPTS if attempts is None else attempts
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            if not is_write_conflict(e) or i == attempts - 1:
                raise
            delay = backoff_delay(i, rand=rand)
            print(f"[retry] {what}: write conflict (attempt {i + 1}/{attempts}), retrying in {delay:.1f}s: "
                  f"{str(e)[:160]}")
            sleep(delay)


# ======================================================================================
# A/B/F functions the sweep calls.
# ======================================================================================
CT_NOT_ENABLED_MSG = ("Change Tracking is not enabled on this source database "
                      "(CHANGE_TRACKING_CURRENT_VERSION() is NULL)")


def current_version(cfg: dict, slots=None) -> Optional[int]:
    """(B) Database-wide CHANGE_TRACKING_CURRENT_VERSION(), or None when Change Tracking is not
    enabled on the database. Callers must treat None as an error, never as version 0 (a 0 would
    make every checkpoint look current and silently skip the database forever)."""
    with _unit(slots, cfg):
        row = _read(_spark(), cfg,
                    "SELECT CAST(CHANGE_TRACKING_CURRENT_VERSION() AS BIGINT) AS v").first()
    return int(row["v"]) if row and row["v"] is not None else None


CT_TABLE_STATUS_SQL = (
    "SELECT s.name AS sch, t.name AS tbl, "
    "CAST(CHANGE_TRACKING_MIN_VALID_VERSION(t.object_id) AS BIGINT) AS mv, "
    "CAST(c.begin_version AS BIGINT) AS bv "
    "FROM sys.change_tracking_tables AS c "
    "JOIN sys.tables AS t ON t.object_id = c.object_id "
    "JOIN sys.schemas AS s ON s.schema_id = t.schema_id")


# current_version + ct_table_status in ONE query: one row per CT-enabled table, each carrying the
# database version (one row with NULL names when no table has CT).
CT_DATABASE_STATE_SQL = (
    "SELECT CAST(CHANGE_TRACKING_CURRENT_VERSION() AS BIGINT) AS v, x.sch, x.tbl, x.mv, x.bv "
    f"FROM (SELECT 1 AS one) AS d LEFT JOIN ({CT_TABLE_STATUS_SQL}) AS x ON 1 = 1")


def database_ct_state(cfg: dict, slots=None, spark=None) -> Tuple[Optional[int], dict]:
    """(B) ``current_version`` and ``ct_table_status`` of the database in ONE query (one unit):
    (CT version or None, {(schema, table) lower-cased: (min_valid_version, begin_version)}). Most
    databases are quiet at scale, and for them this is the only question a cycle asks. ``spark``:
    the session to read through (default: the active one)."""
    with _unit(slots, cfg):
        rows = _read(spark or _spark(), cfg, CT_DATABASE_STATE_SQL).collect()
    as_int = lambda v: int(v) if v is not None else None
    cur = as_int(rows[0]["v"]) if rows else None
    status = {(str(r["sch"]).lower(), str(r["tbl"]).lower()): (as_int(r["mv"]), as_int(r["bv"]))
              for r in rows if r["sch"] is not None}
    return cur, status


def ct_table_status(cfg: dict, slots=None) -> dict:
    """{(schema, table) lower-cased: (min_valid_version, begin_version)} for every CT-enabled table in
    the source database — ONE metadata query per database per cycle (rows are table names, not data).
    Checked before the fast exit: per-table CT disable/re-enable doesn't move the database version.
    ``begin_version`` is the database version when CT (re)started on the table; a change from the
    value recorded at seed time means CT was re-enabled since, so the table must re-seed."""
    with _unit(slots, cfg):
        rows = _read(_spark(), cfg, CT_TABLE_STATUS_SQL).collect()
    as_int = lambda v: int(v) if v is not None else None
    return {(str(r["sch"]).lower(), str(r["tbl"]).lower()): (as_int(r["mv"]), as_int(r["bv"])) for r in rows}


def ct_begin_version(cfg: dict, slots=None) -> Optional[int]:
    """The table's CT begin_version (database version when CT started on it), recorded at seed time."""
    obj = quote_nstr(f"{quote_ident(cfg['src_schema'])}.{quote_ident(cfg['src_table'])}")
    with _unit(slots, cfg):
        row = _read(_spark(), cfg,
                    "SELECT CAST(begin_version AS BIGINT) AS bv FROM sys.change_tracking_tables "
                    f"WHERE object_id = OBJECT_ID({obj})").first()
    return int(row["bv"]) if row and row["bv"] is not None else None


CT_TABLE_NOT_ENABLED_MSG = ("Change Tracking is not enabled on this source table "
                            "(CHANGE_TRACKING_MIN_VALID_VERSION() is NULL); enable it, then the table re-seeds")

# OBJECT_ID is NULL: no such table at the source, or the login cannot see it. Its CT state is NULL too, so this is
# checked first, or a missing table would read as one with Change Tracking off.
SOURCE_TABLE_NOT_FOUND_MSG = ("source table not found: {table} does not exist at the source, or the login cannot "
                              "see it; check src_schema and src_table")


def require_table_ct(cfg: dict, slots=None) -> int:
    """Raise unless Change Tracking is enabled on the TABLE; returns its min valid version. The seed
    calls this before copying: seeding a table without CT would checkpoint it as cdc, after which its
    changes are invisible (it never appears in CHANGETABLE, and a quiet database fast-exits)."""
    mv = min_valid_version(cfg, slots=slots)
    if mv is None:
        raise RuntimeError(CT_TABLE_NOT_ENABLED_MSG)
    return mv


def min_valid_version(cfg: dict, slots=None) -> Optional[int]:
    """(F) CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID('<schema>.<table>')). None when CT is not
    enabled on the table -> state_store.is_reseed_needed treats None as 'reseed'."""
    obj = quote_nstr(f"{quote_ident(cfg['src_schema'])}.{quote_ident(cfg['src_table'])}")
    with _unit(slots, cfg):
        row = _read(_spark(), cfg,
                    f"SELECT CAST(CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID({obj})) AS BIGINT) AS v"
                    ).first()
    return int(row["v"]) if row and row["v"] is not None else None


SINK_MODE_MISMATCH_MSG = (
    "sink {sink} {has} '{col}' column but this control row {is_} consolidated (client_id {cid}); "
    "a sink is either dedicated to one source table or shared by client_id — never both. Point the "
    "row at another sink or fix client_id.")


WRITE_MODE_MISMATCH_MSG = (
    "sink {sink} {has} '{col}' column but this control row's write_mode is '{mode}'; an append sink "
    "(a change log) and a merge sink (the current rows) can't be the same table. Point the row at "
    "another sink or fix write_mode.")


def check_sink_mode(cfg: dict, sink_fqn: str, sink_columns: List[str]) -> None:
    """Refuse to write a dedicated row into a consolidated sink (its overwrite/MERGE would hit every
    client's rows) or a consolidated row into a dedicated sink (rows without __client_id__); and
    a merge row into an append sink (it would MERGE into the log) or an append row into a merge sink."""
    have = {c.lower() for c in sink_columns}
    selected = {c.lower() for c in _split(cfg.get("select_list"))}
    has = CLIENT_COLUMN in have
    cid = client_id(cfg)
    native = CLIENT_COLUMN in selected
    if has and cid is None and not native:     # a dedicated table may have its own such column
        raise ValueError(SINK_MODE_MISMATCH_MSG.format(sink=sink_fqn, has="has a", col=CLIENT_COLUMN,
                                                       is_="is not", cid="NULL"))
    if not has and cid is not None:
        raise ValueError(SINK_MODE_MISMATCH_MSG.format(sink=sink_fqn, has="has no", col=CLIENT_COLUMN,
                                                       is_="is", cid=repr(cid)))
    log = OP_COLUMN in have and (is_append(cfg) or OP_COLUMN not in selected)
    if log != is_append(cfg):
        raise ValueError(WRITE_MODE_MISMATCH_MSG.format(sink=sink_fqn, has="has an" if log else "has no",
                                                        col=OP_COLUMN, mode=write_mode(cfg)))


# The sinks checked so far in this run, and their columns: ensure_sink_table asks Unity Catalog about
# each sink once per run instead of once per batch. Each run starts with forget_sinks().
_known_sinks: dict = {}
_known_schemas: set = set()

# A target schema the first run can't make: it is made as the identity the ingestion job runs as, which needs these.
SCHEMA_CREATE_MSG = ("can't create the target schema {schema}: the identity this job runs as needs USE CATALOG and "
                     "CREATE SCHEMA on {catalog} (or the schema made beforehand, with USE SCHEMA and CREATE TABLE "
                     "on it). {error}")


def forget_sinks() -> None:
    """Start a run: the next ``ensure_sink_table`` of every sink asks the catalog again."""
    _known_sinks.clear()
    _known_schemas.clear()


def ensure_sink_schema(spark, cfg: dict) -> None:
    """Create the row's target schema if it doesn't exist, as Lakeflow Connect's pipeline creation makes a new
    destination schema: a job set up in the app may name a schema nobody made yet. It is made once per run, owned
    by the identity the job runs as. A row without a catalog and schema (a test, a two-part name) is left alone."""
    if not cfg.get("sink_catalog") or not cfg.get("sink_schema"):
        return
    catalog = quote_delta_ident(cfg["sink_catalog"])
    schema = f"{catalog}.{quote_delta_ident(cfg['sink_schema'])}"
    if schema in _known_schemas:
        return
    try:
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS {schema} COMMENT 'Target of lakefed ingestion jobs'")
    except Exception as e:
        raise RuntimeError(SCHEMA_CREATE_MSG.format(schema=schema, catalog=catalog,
                                                    error=str(e).strip().split("\n")[0][:300])) from e
    _known_schemas.add(schema)


def ensure_sink_table(spark, cfg: dict, sink_fqn: str, slots=None) -> None:
    """Create the Delta bronze sink if absent, typed from the source via a 0-row JDBC read (so the
    JDBC dialect infers the Spark schema natively), then check the sink's mode (dedicated vs
    consolidated) matches the row. Idempotent. The 0-row read is one unit.

    A consolidated sink is created with the ``__client_id__`` column and liquid clustering on it, so
    each client's slice is cheap to find and concurrent clients' writes touch separate files. An
    append sink is created with the client_attrs and APPEND_COLUMNS too.

    The catalog is asked once per sink per run (``forget_sinks``): later calls check the row's mode
    against the columns found then. The mode columns never change within a run."""
    known = _known_sinks.get(sink_fqn)
    if known is not None:
        check_sink_mode(cfg, sink_fqn, known)
        return
    if not spark.catalog.tableExists(sink_fqn):
        ensure_sink_schema(spark, cfg)

        def create():
            with _unit(slots, cfg):
                if spark.catalog.tableExists(sink_fqn):      # another client created it meanwhile
                    return
                empty = _tag(_read(spark, cfg, source_adapters.for_cfg(cfg).select_sql(cfg, where="1=0")),
                             cfg, append_meta(cfg))
                view = f"sink_schema_{uuid.uuid4().hex}"
                empty.createOrReplaceTempView(view)
                try:
                    spark.sql(create_sink_sql(sink_fqn, view, cfg))
                finally:
                    spark.catalog.dropTempView(view)
        with_write_retry(create, what=f"create {sink_fqn}")
    columns = list(spark.table(sink_fqn).columns)
    check_sink_mode(cfg, sink_fqn, columns)
    _known_sinks[sink_fqn] = columns


def _bare_ident(name) -> str:
    """A sink name part without its quotes (a control row may hold an already-quoted name)."""
    name = str(name or "").strip()
    if len(name) >= 2 and name[0] == name[-1] == "`":
        name = name[1:-1].replace("``", "`")
    return name


def sink_lookup_key(cfg: dict) -> str:
    """Lower-cased, unquoted catalog.schema.table of a control row's sink, as ``existing_sinks``
    returns them (not ``sink_fqn``, which quotes each part for SQL)."""
    return ".".join(_bare_ident(cfg.get(k)).lower() for k in ("sink_catalog", "sink_schema", "sink_table"))


def existing_sinks_sql(catalog: str, schemas) -> str:
    """Tables that exist in the given schemas of one catalog — ONE information_schema query per sink
    catalog per run, so the planner can reseed an incremental table whose sink is gone."""
    cat = "`" + str(catalog).replace("`", "``") + "`"
    lits = ", ".join("'" + str(s).lower().replace("'", "''") + "'" for s in sorted(schemas))
    return (f"SELECT lower(table_schema) AS s, lower(table_name) AS t FROM {cat}.information_schema.tables "
            f"WHERE lower(table_schema) IN ({lits})")


def existing_sinks(spark, configs) -> Optional[set]:
    """{catalog.schema.table} (lower) of the sink tables that exist for these control rows, or None
    when a catalog can't be listed (the caller then skips the check rather than guessing)."""
    by_cat = {}
    for c in configs:
        by_cat.setdefault(_bare_ident(c["sink_catalog"]).lower(), set()).add(_bare_ident(c["sink_schema"]).lower())
    found = set()
    for cat, schemas in by_cat.items():
        try:
            rows = spark.sql(existing_sinks_sql(cat, schemas)).collect()
        except Exception as e:
            print(f"[warn] can't list tables in catalog {cat} ({str(e)[:160]}); skipping the missing-sink check")
            return None
        found |= {f"{cat}.{r['s']}.{r['t']}" for r in rows}
    return found


def spark_conf_settings(text) -> dict:
    """The Spark session settings of the job parameter ``spark_conf``: a JSON object of setting -> value
    (blank: none), as strings. A string or number is used as written; a boolean becomes "true" or "false".
    Anything else (null, a list, an object, NaN, or a number too large to be finite such as 1e999) is refused:
    its text would be a value nobody wrote ("None", "[1]", "inf"). Raises ValueError, naming the setting."""
    if text is None or not str(text).strip():
        return {}
    try:
        settings = json.loads(text, parse_constant=_refuse_json_constant)
    except ValueError as e:
        raise ValueError(f"spark_conf is not JSON: {e}") from e
    if not isinstance(settings, dict):
        raise ValueError("spark_conf must be a JSON object of setting -> value")
    out = {}
    for key, value in settings.items():
        if isinstance(value, bool):
            out[str(key)] = "true" if value else "false"
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"spark_conf setting {key!r} must be a finite number")
        elif isinstance(value, (str, int, float)):
            out[str(key)] = str(value)
        else:
            kind = "null" if value is None else type(value).__name__
            raise ValueError(f"spark_conf setting {key!r} must be a string, number or boolean, not {kind}")
    return out


def _refuse_json_constant(name: str):
    raise ValueError(f"{name} is not a setting value")


def apply_session_conf(spark, text) -> dict:
    """Set this run's Spark session settings from ``spark_conf`` (see ``spark_conf_settings``; all of them
    are checked before the first is set), so one cluster can run collections with different code generation
    or write settings. Returns what was set."""
    settings = spark_conf_settings(text)
    for key, value in settings.items():
        spark.conf.set(key, value)
    return settings


def _is_partitioned(cfg: dict) -> bool:
    v = cfg.get("load_partitioned")
    return v is True or str(v).lower() == "true"


def insert_sql(mode: str, target: str, cols: List[str], source: str, evolve: bool = False) -> str:
    """``INSERT [WITH SCHEMA EVOLUTION] {INTO|OVERWRITE} <target> BY NAME SELECT `a`, `b` FROM <source>``.

    BY NAME maps columns by name, so a reordered/edited select_list can never shift values into the
    wrong columns; with ``evolve`` the target gains any select_list column it lacks (spike-validated
    on DBR 18 — an explicit column list instead would require the columns to already exist)."""
    collist = ", ".join(delta_column(c) for c in cols)
    evo = "WITH SCHEMA EVOLUTION " if evolve else ""
    return f"INSERT {evo}{mode} {target} BY NAME SELECT {collist} FROM {source}"


def insert_overwrite_sql(sink_fqn: str, cols: List[str], source: str, cfg: Optional[dict] = None) -> str:
    """Data-only atomic replace with schema evolution: the whole target, or — consolidated sink —
    only this client's rows (``INSERT WITH SCHEMA EVOLUTION INTO <sink> BY NAME REPLACE WHERE
    __client_id__ = '<id>' SELECT ...``; validated on DBR: other clients untouched, BY NAME mapping,
    new columns added. BY NAME must precede REPLACE WHERE)."""
    if cfg is not None and client_id(cfg):
        collist = ", ".join(delta_column(c) for c in cols)
        return (f"INSERT WITH SCHEMA EVOLUTION INTO {sink_fqn} BY NAME REPLACE WHERE {client_predicate(cfg)} "
                f"SELECT {collist} FROM {source}")
    return insert_sql("OVERWRITE", sink_fqn, cols, source, evolve=True)


def missing_sink_columns(select_list: str, sink_columns: List[str]) -> List[str]:
    """select_list columns absent from the sink (case-insensitive) — pure, unit-tested. A non-empty
    result in the incremental phase means the source table gained a column: the sweep reseeds that table so
    historical rows get the new column (CT only returns changed rows)."""
    have = {c.lower() for c in sink_columns}
    return [c for c in _split(select_list) if c.lower() not in have]


# ======================================================================================
# Skip-no-change: database-level change detection (pure builders + one JDBC call per chunk).
# ======================================================================================
DEFAULT_DETECTION_CHUNK = 200


def quote_ident(name: str) -> str:
    """SQL Server bracket-quoted identifier (``]`` doubled) — the CT SQL below is SQL Server-only."""
    return _SQLSERVER.quote_ident(name)


def quote_nstr(value: str) -> str:
    """SQL Server N'...' unicode string literal (``'`` doubled)."""
    return _SQLSERVER.quote_str(value)


RETENTION_LAPSED_MARKER = "CT_RETENTION_LAPSED"
RETENTION_LAPSED_ERROR = (f"{RETENTION_LAPSED_MARKER}: change tracking retention has passed the checkpoint "
                          "(or CT is disabled on the table); reseed required")


def ct_retention_guard_sql(cfg: dict, from_version: int) -> str:
    """T-SQL run on the CHANGETABLE read's own connection immediately before the read (Spark JDBC
    ``sessionInitStatement``). Planning checked retention earlier, but CT auto-cleanup can raise the
    table's minimum valid version during a long sweep; reading CHANGES from a version below it
    would silently miss changes. The guard makes SQL Server raise instead, and the sweep turns that
    error into a reseed. (A check inside the SELECT itself isn't reliable: SQL Server may skip
    evaluating it when the change set is empty — exactly the lapsed-retention case.)"""
    obj = quote_nstr(f"{quote_ident(cfg['src_schema'])}.{quote_ident(cfg['src_table'])}")
    mv = f"CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID({obj}))"
    return f"IF {mv} IS NULL OR {mv} > {int(from_version)} THROW 51000, N'{RETENTION_LAPSED_ERROR}', 1;"


def ct_read_query_guarded(cfg: dict, from_version: int, to_version: Optional[int] = None) -> str:
    """``ct_read_query`` with the retention guard inside it, for a read through a connection, which
    runs no session statement. The guard is the TOP count: SQL Server evaluates it once, before the
    first row, even when there are no changes, and a filter Spark pushes down to the source stays
    above it. While retention covers ``from_version`` it is the largest BIGINT; otherwise converting
    RETENTION_LAPSED_ERROR to INT fails the read with an error that quotes it. (A guard row LEFT JOINed
    to the changes isn't reliable: under a pushed-down filter SQL Server makes that an inner join, and
    with no changes the row is never produced.)"""
    obj = quote_nstr(f"{quote_ident(cfg['src_schema'])}.{quote_ident(cfg['src_table'])}")
    mv = f"CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID({obj}))"
    return (f"SELECT TOP (CASE WHEN {mv} IS NULL OR {mv} > {int(from_version)} "
            f"THEN CAST(N'{RETENTION_LAPSED_ERROR}' AS INT) ELSE CAST({2 ** 63 - 1} AS BIGINT) END) * "
            f"FROM ({ct_read_query(cfg, from_version, to_version)}) AS c")


SNAPSHOT_ISOLATION_SQL = "SET TRANSACTION ISOLATION LEVEL SNAPSHOT;"


def snapshot_isolation() -> bool:
    """Read changes under SNAPSHOT isolation (env ``LAKEFED_SQL_SNAPSHOT_ISOLATION``, default off).

    Under the default READ COMMITTED, the change table and the base table are read at different
    times: a row updated again in between is written with the later values under the earlier change
    (rows deleted in between are skipped, see ``ct_read_query``). SNAPSHOT reads both as of one point
    in time; it needs ``ALLOW_SNAPSHOT_ISOLATION ON`` on the database (otherwise SQL Server fails the
    read with error 3952, naming the setting)."""
    return os.environ.get("LAKEFED_SQL_SNAPSHOT_ISOLATION", "false").strip().lower() in ("1", "true", "yes")


def ct_read_session_init(cfg: dict, from_version: int) -> str:
    """sessionInitStatement for a change read: the retention guard, preceded by SNAPSHOT isolation
    when enabled (session-level, so it applies to the read that follows on the same connection)."""
    guard = ct_retention_guard_sql(cfg, from_version)
    return f"{SNAPSHOT_ISOLATION_SQL} {guard}" if snapshot_isolation() else guard


def ct_read(cfg: dict, from_version: int, to_version: Optional[int] = None) -> Tuple[str, Optional[str]]:
    """(query, sessionInitStatement) of the change read in (``from_version``, ``to_version``], with the
    retention guard: over JDBC it runs on the read's connection first (``ct_read_session_init``);
    through a connection it is part of the query (``ct_read_query_guarded``). SNAPSHOT isolation needs
    the session statement, so the planner refuses ``snapshot_isolation`` for rows read through one."""
    if uses_remote_query(cfg):
        return ct_read_query_guarded(cfg, from_version, to_version), None
    return ct_read_query(cfg, from_version, to_version), ct_read_session_init(cfg, from_version)


def is_retention_lapsed_error(exc: BaseException) -> bool:
    """True when a CT read failed because the retention guard fired (route the table to reseed)."""
    return RETENTION_LAPSED_MARKER in str(exc)


def source_group_key(cfg: dict) -> tuple:
    """Tables that share a source database (host, port, database, credential) — the unit for one
    CHANGE_TRACKING_CURRENT_VERSION() capture and batched change detection."""
    host = (cfg.get("src_host") or cfg.get("src_connection") or "").lower()
    key = cfg.get("secret_key") or cfg.get("src_connection") or ""
    port = int(cfg.get("src_port") or source_adapters.for_cfg(cfg).default_port)
    return (host, port, cfg.get("src_database"), key)


def chunked(items: list, size: int) -> List[list]:
    size = max(1, int(size))
    return [items[i:i + size] for i in range(0, len(items), size)]


def all_current(checkpoint_versions, cur: int) -> bool:
    """Fast exit: every checkpoint already at/after the database's current version -> nothing
    in this database changed since the last cycle (CT versions are database-wide)."""
    return all(int(v) >= int(cur) for v in checkpoint_versions)


def detection_sql(items) -> str:
    """ONE statement that classifies many tables of the same database.

    ``items`` = [(control_id, schema, table, checkpoint_version), ...]. Per table it returns the CT
    min valid version and whether CHANGETABLE has any row since the checkpoint. ``changed`` is only
    evaluated when the checkpoint is still valid (mv NOT NULL and checkpoint >= mv) — an invalid
    checkpoint is classified reseed from ``mv`` alone. EXISTS stops at the first change row, so a
    busy table costs the same as a quiet one. A table without CT makes the whole statement fail at
    compile time; the caller then falls back to the per-table path for that chunk only."""
    parts = []
    for cid, schema, table, cp in items:
        full = f"{quote_ident(schema)}.{quote_ident(table)}"
        v = int(cp)
        parts.append(
            f"SELECT CAST({int(cid)} AS BIGINT) AS control_id, x.mv AS mv, "
            f"CASE WHEN x.mv IS NULL OR {v} < x.mv THEN NULL "
            f"WHEN EXISTS (SELECT 1 FROM CHANGETABLE(CHANGES {full}, {v}) AS ct) THEN 1 ELSE 0 END AS changed "
            f"FROM (SELECT CAST(CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID({quote_nstr(f'{full}')})) "
            "AS BIGINT) AS mv) AS x"
        )
    return " UNION ALL ".join(parts)


def classify(checkpoint_version, min_valid, changed) -> str:
    """'reseed' (CT disabled/unknown or retention lapsed) | 'changed' | 'unchanged'. Pure."""
    if min_valid is None or int(checkpoint_version) < int(min_valid):
        return "reseed"
    return "changed" if changed in (1, True, "1") else "unchanged"


def detect_changes(spark, cfg: dict, items, slots=None, chunk_size: int = DEFAULT_DETECTION_CHUNK):
    """Batched change detection for tables of ONE source database (``cfg`` = any of its rows; used
    for the connection). Each chunk is one source query (one unit). Returns
    ``(results, failed_ids, queries)`` — results {control_id: (mv, changed)}; failed_ids are the
    tables of chunks whose query failed (caller falls back to the per-table path for them)."""
    results, failed, queries = {}, [], 0
    for chunk in chunked(list(items), chunk_size):
        queries += 1
        try:
            with _unit(slots, cfg):
                rows = _read(spark, cfg, detection_sql(chunk)).collect()
            for r in rows:
                mv = r["mv"]
                results[int(r["control_id"])] = (None if mv is None else int(mv), r["changed"])
        except Exception as e:
            print(f"[detect] {cfg.get('src_database')}: chunk of {len(chunk)} failed "
                  f"({str(e).splitlines()[0][:200]}) -> per-table fallback")
            failed.extend(int(c[0]) for c in chunk)
    return results, failed, queries


def _insert_overwrite(spark, sink_fqn: str, source: str, cols: List[str], cfg: Optional[dict] = None) -> int:
    """Atomically replace the target's DATA from ``source`` (a view or table) in one Delta commit.

    ``INSERT OVERWRITE`` is data-only: unlike ``saveAsTable(mode="overwrite")`` — which commits as
    ``CREATE OR REPLACE TABLE AS SELECT`` — it leaves the table definition alone (liquid clustering
    keys, table properties, grants, row filters/column masks), which consolidated bronze depends on.
    Columns map BY NAME from select_list, and ``WITH SCHEMA EVOLUTION`` adds any the target lacks
    (a client/table whose schema gained columns); existing columns are never dropped."""
    row = spark.sql(insert_overwrite_sql(sink_fqn, cols, source, cfg)).first()
    d = row.asDict() if row is not None else {}
    return int(d.get("num_inserted_rows", d.get("num_affected_rows", 0)) or 0)


def _insert_into(spark, sink_fqn: str, source: str, cols: List[str]) -> int:
    """Append ``source``'s rows (a view or table) to the target in one Delta commit, BY NAME, with
    schema evolution. Blind appends don't conflict with each other, so many clients append at once."""
    row = spark.sql(insert_sql("INTO", sink_fqn, cols, source, evolve=True)).first()
    d = row.asDict() if row is not None else {}
    return int(d.get("num_inserted_rows", d.get("num_affected_rows", 0)) or 0)


def _write_snapshot(spark, sink_fqn: str, source: str, cfg: dict) -> int:
    """Write a whole-table read: append it (append mode) or replace the sink's / client's rows."""
    if is_append(cfg):
        return _insert_into(spark, sink_fqn, source, write_columns(cfg))
    return _insert_overwrite(spark, sink_fqn, source, write_columns(cfg), cfg)


def seed(spark, cfg: dict, sink_fqn: str, slots=None, executor=None, guard=None,
         run_id=None, version=None) -> int:
    """(B seed) Full copy of the current table state into the Delta bronze sink — the CT seed and
    every cycle of the ``full`` strategy (any source adapter).

    For CT, called after current_version() captured V0, so rows changed during the seed are
    reconciled on the first cdc cycle by the idempotent PK MERGE (no gap, no dupe).

    * Non-partitioned: ONE unit — one JDBC read streams straight into a data-only ``INSERT OVERWRITE``
      (a single atomic commit); rows come from the INSERT's own metrics.
    * Partitioned (``load_partitioned``): size-bounded range partitions load into a staging table,
      then swap into the target atomically — see ``_partitioned_seed``.
    No ``.cache()``/``count()``: every source range is read exactly once.

    Consolidated sink (``client_id`` set): the overwrite is ``INSERT WITH SCHEMA EVOLUTION INTO sink
    BY NAME REPLACE WHERE __client_id__ = ...``, so only this client's rows are replaced.

    Append mode: nothing is replaced. The rows are appended as a snapshot: operation 'S' at
    ``version`` (the CT version captured before the seed; None for a full load), written by ingest
    run ``run_id``."""
    if _is_partitioned(cfg):
        return _partitioned_seed(spark, cfg, sink_fqn, slots, executor, guard, run_id=run_id, version=version)

    def once():
        with _unit(slots, cfg):
            df = _tag(_read(spark, cfg, source_adapters.for_cfg(cfg).select_sql(cfg)), cfg,
                      append_meta(cfg, run_id=run_id, version=version))
            view = f"seed_full_{uuid.uuid4().hex}"
            df.createOrReplaceTempView(view)
            try:
                return _write_snapshot(spark, sink_fqn, view, cfg)  # ONE read, ONE commit
            finally:
                spark.catalog.dropTempView(view)
    return with_write_retry(once, what=f"seed {sink_fqn}")


def _partitioned_seed(spark, cfg: dict, sink_fqn: str, slots, executor, guard=None,
                      run_id=None, version=None) -> int:
    """Size-bounded, staged, atomic partitioned seed (the accelerator's partitioning strategy).

    1. table size (source catalog, per adapter) and MIN/MAX of ``partition_col`` — one unit each;
    2. ``num_partitions = max(size_mb / partition_size_mb, 2)``, clamped to the bound range — can be
       thousands; they are NOT limited by cores;
    3. create/replace the EMPTY staging table ``<sink>__seed_staging`` with the source schema (unit);
    4. submit every partition to the shared ``executor``; each partition is ONE unit: one JDBC range
       read -> unique temp view -> ``INSERT INTO`` staging (blind appends don't conflict). The
       sweep's ``slots`` budget (``parallelism``) decides how many run at once — across ALL tables;
    5. this coordinating thread waits holding NO slot. On the first failure it cancels the
       not-yet-started partitions, lets in-flight ones settle, and raises: the TARGET IS UNTOUCHED
       (readers keep the previous complete data); the table stays ``seeding`` and the next attempt
       replaces the staging table and starts over;
    6. on success, swap as one unit: overwrite the target from staging in ONE atomic Delta commit
       (same table — grants, row filters and lineage are preserved; never rename tables), then drop
       staging. A consolidated sink replaces only this client's rows (``REPLACE WHERE __client_id__``)
       and stages in a per-client table. Append mode appends the staged snapshot instead (one commit).
    Returns rows in the target after the swap."""
    from concurrent.futures import ThreadPoolExecutor, as_completed, wait
    import partitions

    schema, table, pcol = cfg["src_schema"], cfg["src_table"], cfg.get("partition_col")
    if not pcol:
        raise ValueError("load_partitioned is set but partition_col is empty")
    adapter = source_adapters.for_cfg(cfg)
    with _unit(slots, cfg):
        size_row = _read(spark, cfg, partitions.table_size_mb_query(schema, table, adapter)).first()
    size_mb = float(size_row["table_size_mb"] or 0) if size_row else 0.0
    with _unit(slots, cfg):
        b = _read(spark, cfg, partitions.bounds_query(schema, table, pcol, adapter)).first()
    lb, ub = (b["lb"], b["ub"]) if b else (None, None)
    if lb is None or ub is None:  # empty table (or all-NULL column): nothing to partition
        print(f"[seed] {schema}.{table}: no {pcol} bounds -> non-partitioned seed")
        return seed(spark, dict(cfg, load_partitioned=False), sink_fqn, slots=slots,
                    run_id=run_id, version=version)  # single unit

    n = partitions.num_partitions_for(size_mb, cfg.get("partition_size_mb"))
    n = partitions.effective_num_partitions(lb, ub, n)
    plist = partitions.get_partition_list(pcol, lb, ub, n, adapter)
    staging = staging_table_name(sink_fqn, cfg)
    print(f"[seed] {schema}.{table}: size={size_mb} MB, partition_size_mb={cfg.get('partition_size_mb')}, "
          f"{pcol} in [{lb}, {ub}], num_partitions={len(plist)}, staging={staging}")

    cols = write_columns(cfg)
    meta = append_meta(cfg, run_id=run_id, version=version)   # the same literals on every partition
    with _unit(slots, cfg):
        _tag(_read(spark, cfg, adapter.select_sql(cfg, where="1=0")), cfg, meta).write.mode("overwrite") \
            .option("overwriteSchema", "true").saveAsTable(staging)

    def load_partition(part):
        if guard is not None:
            guard()                                # e.g. raises if the sweep lost its collection lock
        with _unit(slots, cfg):
            df = _tag(_read(spark, cfg, adapter.select_sql(cfg, where=part["where_clause"])), cfg, meta)
            view = f"seed_part_{uuid.uuid4().hex}"
            df.createOrReplaceTempView(view)
            try:
                row = spark.sql(insert_sql("INTO", staging, cols, view)).first()
            finally:
                spark.catalog.dropTempView(view)
        d = row.asDict() if row is not None else {}
        return int(d.get("num_inserted_rows", d.get("num_affected_rows", 0)) or 0)

    own_executor = executor is None
    ex = executor or ThreadPoolExecutor(max_workers=slots.capacity if slots is not None else 8)
    try:
        futures = [ex.submit(load_partition, p) for p in plist]
        first_err = None
        for f in as_completed(futures):            # waiting here holds NO slot
            if f.exception() is not None:
                first_err = f.exception()
                for g in futures:
                    g.cancel()                     # only not-yet-started partitions cancel
                break
        if first_err is not None:
            wait(futures)                          # let in-flight partitions settle
            failed = sum(1 for g in futures if not g.cancelled() and g.exception() is not None)
            cancelled = sum(1 for g in futures if g.cancelled())
            raise RuntimeError(
                f"partitioned seed of {schema}.{table}: {failed}/{len(plist)} partitions failed, "
                f"{cancelled} cancelled; target untouched; first error: {str(first_err)[:300]}") from first_err
    finally:
        if own_executor:
            ex.shutdown(wait=True)

    if guard is not None:
        guard()                                    # don't swap if the sweep lost its lock
    def swap():
        with _unit(slots):                         # atomic swap: ONE data-only Delta commit
            return _write_snapshot(spark, sink_fqn, staging, cfg)
    rows = with_write_retry(swap, what=f"seed swap {sink_fqn}")
    with _unit(slots):
        spark.sql(f"DROP TABLE IF EXISTS {staging}")
    return int(rows or 0)


def merge_sql(cfg: dict, sink_fqn: str, view: str) -> str:
    """Idempotent I/U/D MERGE of the staged CT rows (temp view ``view``) into the sink, by PK.

    ``WITH SCHEMA EVOLUTION`` (per statement — never the session-wide autoMerge conf, which the
    threaded sweep would share): select_list columns the sink lacks are added, and with type widening
    enabled on the sink, safe widenings (int->bigint, ...) apply. Explicit column lists keep the CT
    ``op`` column out of bronze. ON keeps tgt./s. qualifiers; SET/INSERT targets are unqualified."""
    sl, keys = ", ".join(write_columns(cfg)), ", ".join(merge_keys(cfg))
    return (
        f"MERGE WITH SCHEMA EVOLUTION INTO {sink_fqn} AS tgt USING {view} AS s ON {merge_on_cfg(cfg)} "
        "WHEN MATCHED AND s.op = 'D' THEN DELETE "
        f"WHEN MATCHED THEN UPDATE SET {merge_set(sl, keys)} "
        "WHEN NOT MATCHED AND s.op <> 'D' THEN "
        f"INSERT ({merge_insert_cols(sl)}) "
        f"VALUES ({merge_insert_vals(sl)})"
    )


def read_and_merge_ct(spark, cfg: dict, sink_fqn: str, from_version: int,
                      slots=None, to_version: Optional[int] = None) -> Optional[int]:
    """(A) Read CT deltas since ``from_version`` (up to ``to_version`` when given; see
    ``ct_read_query``) and MERGE them into the Delta sink by PK.

    Projects PK from the change table and non-PK from the base table (NULL on delete) as a
    natively-typed JDBC DataFrame and feeds it straight into an idempotent I/U/D MERGE — one source
    read, no ``.cache()``/``count()``. Returns rows affected from the MERGE's own metrics (0 when the
    change set was empty).

    The staging temp view gets a unique name per call: the sweep runs many tables concurrently in
    one SparkSession, and temp views are session-scoped, so a shared name would let threads
    overwrite each other's change sets."""
    query, init = ct_read(cfg, from_version, to_version)

    def once():
        with _unit(slots, cfg):                    # read -> MERGE is ONE unit of work
            df = _tag(_read(spark, cfg, query, session_init=init), cfg)
            view = f"ct_staged_{uuid.uuid4().hex}"
            df.createOrReplaceTempView(view)
            try:
                return spark.sql(merge_sql(cfg, sink_fqn, view)).first()
            finally:
                spark.catalog.dropTempView(view)
    result = with_write_retry(once, what=f"merge {sink_fqn}")
    if result is not None and "num_affected_rows" in result.asDict():
        return int(result["num_affected_rows"] or 0)
    # Runtime without MERGE result metrics: don't guess from DESCRIBE HISTORY (an empty MERGE may
    # not commit, so the latest entry could be a previous run's). Report unknown.
    return None


def read_and_append_ct(spark, cfg: dict, sink_fqn: str, from_version: int, to_version: int,
                       slots=None, run_id=None) -> int:
    """(A, append mode) Read CT changes in (``from_version``, ``to_version``] and append them to the
    sink as change rows: the operation and commit version from the change table, the PK from the
    change table and the other columns from the base table (NULL on delete). One source read, one
    commit; returns the rows appended.

    An append is not idempotent: the caller records the pending write first and advances the
    checkpoint after it (see ``append_committed``)."""
    query, init = ct_read(cfg, from_version, to_version)

    def once():
        with _unit(slots, cfg):                    # read -> append is ONE unit of work
            df = _tag(_read(spark, cfg, query, session_init=init),
                      cfg, append_meta(cfg, run_id=run_id, changes=True))
            view = f"ct_staged_{uuid.uuid4().hex}"
            df.createOrReplaceTempView(view)
            try:
                return _insert_into(spark, sink_fqn, view, write_columns(cfg))
            finally:
                spark.catalog.dropTempView(view)
    return with_write_retry(once, what=f"append {sink_fqn}")


def append_committed_sql(sink_fqn: str, cfg: dict, run_id) -> str:
    """Does the sink hold rows this control row appended in ingest run ``run_id``?"""
    where = f"`{RUN_ID_COLUMN}` = {int(run_id)}"
    if client_id(cfg):
        where = f"{client_predicate(cfg)} AND {where}"
    return f"SELECT 1 AS found FROM {sink_fqn} WHERE {where} LIMIT 1"


def append_committed(spark, cfg: dict, sink_fqn: str, run_id, slots=None) -> bool:
    """True when the pending append of ingest run ``run_id`` committed: the sink holds its rows (an
    append is one atomic commit). For a run that stopped between an append and the checkpoint
    advance. An append of no rows looks uncommitted, and reading again is then harmless."""
    if not spark.catalog.tableExists(sink_fqn):
        return False
    with _unit(slots):
        return bool(spark.sql(append_committed_sql(sink_fqn, cfg, run_id)).collect())


# ======================================================================================
# Batched appends (append mode, consolidated sinks, orchestrated by batch_append.py): the reads of
# many control rows for ONE sink run as one Spark job, a task per source database, and land in ONE
# Delta commit, instead of a job and a commit per table.
# ======================================================================================
def _units(slots, cfgs: List[dict]):
    """One unit per row of ``cfgs`` at once (a batch: one source query per database): hold that many
    slots, and a permit of each row's source when queries per source are capped."""
    if slots is None:
        return contextlib.nullcontext()
    return slots.acquire_many(len(cfgs), sources=[source_key(c) for c in cfgs])


def union_by_name(dfs):
    """Union DataFrames by column name; a column some of them lack is NULL in their rows."""
    out = dfs[0]
    for df in dfs[1:]:
        out = out.unionByName(df, allowMissingColumns=True)
    return out


def batch_columns(cfgs) -> List[str]:
    """Columns a batch writes: every control row's write_columns, once each (case-insensitive), in order."""
    out, seen = [], set()
    for cfg in cfgs:
        for c in write_columns(cfg):
            if c.lower() not in seen:
                seen.add(c.lower())
                out.append(c)
    return out


def change_read(cfg: dict, from_version: int, to_version: int, run_id=None) -> tuple:
    """A batch member: the row's CT changes in (``from_version``, ``to_version``], with the retention guard."""
    return (cfg, *ct_read(cfg, from_version, to_version), {"run_id": run_id, "changes": True})


def snapshot_read(cfg: dict, run_id=None, version=None) -> tuple:
    """A batch member: the whole table, appended as a snapshot at ``version`` (None for a full load)."""
    return (cfg, source_adapters.for_cfg(cfg).select_sql(cfg), None, {"run_id": run_id, "version": version})


def append_batch(spark, sink_fqn: str, reads, slots=None) -> int:
    """Append several control rows' reads (``change_read`` / ``snapshot_read``) to ONE sink in ONE
    commit. Each read is a query of its own source database (over JDBC or through its connection),
    tagged with its client_id, client_attrs and append metadata; they are unioned by name (a column
    one database lacks is NULL in its rows), so they run as one Spark job, in parallel on the
    executors. Holds one slot per read. Returns the rows appended. A failure fails the whole batch
    (nothing is written)."""
    def once():
        with _units(slots, [r[0] for r in reads]):
            df = union_by_name([_tag(_read(spark, cfg, query, session_init=init), cfg, append_meta(cfg, **meta))
                                for cfg, query, init, meta in reads])
            view = f"batch_staged_{uuid.uuid4().hex}"
            df.createOrReplaceTempView(view)
            try:
                return _insert_into(spark, sink_fqn, view, batch_columns([r[0] for r in reads]))
            finally:
                spark.catalog.dropTempView(view)
    return with_write_retry(once, what=f"append batch of {len(reads)} to {sink_fqn}")


def committed_clients_sql(sink_fqn: str, client_ids, run_id) -> str:
    """Which of ``client_ids`` (validated by ``client_id``, so safe as literals) have rows from run ``run_id``."""
    ids = ", ".join(f"'{c}'" for c in client_ids)
    return (f"SELECT DISTINCT `{CLIENT_COLUMN}` AS client FROM {sink_fqn} "
            f"WHERE `{RUN_ID_COLUMN}` = {int(run_id)} AND `{CLIENT_COLUMN}` IN ({ids})")


def committed_clients(spark, sink_fqn: str, cfgs, run_id, slots=None) -> set:
    """The client_ids of ``cfgs`` with rows from ingest run ``run_id`` in the sink: after a batch
    append failed, the members whose rows landed anyway (a commit can succeed and the statement still
    fail, e.g. in a post-commit hook)."""
    if not spark.catalog.tableExists(sink_fqn):
        return set()
    with _unit(slots):
        rows = spark.sql(committed_clients_sql(sink_fqn, [client_id(c) for c in cfgs], run_id)).collect()
    return {r["client"] for r in rows}


def seed_prep_sql(cfg: dict) -> str:
    """What a CT seed needs before it reads, in one query: the database's CT version (V0, captured
    BEFORE the seed), the table's min valid version (NULL: CT is off on it), its begin_version, and its
    object id (NULL: the table is not there)."""
    obj = quote_nstr(f"{quote_ident(cfg['src_schema'])}.{quote_ident(cfg['src_table'])}")
    return (f"SELECT CAST({int(cfg['id'])} AS BIGINT) AS control_id, "
            "CAST(CHANGE_TRACKING_CURRENT_VERSION() AS BIGINT) AS v, "
            f"CAST(CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID({obj})) AS BIGINT) AS mv, "
            f"CAST(OBJECT_ID({obj}) AS BIGINT) AS oid, "
            "(SELECT CAST(begin_version AS BIGINT) FROM sys.change_tracking_tables "
            f"WHERE object_id = OBJECT_ID({obj})) AS bv")


def seed_prep(spark, cfgs, slots=None) -> dict:
    """{control_id: (v, mv, bv)} for a batch of CT seeds: one query per source database, run as ONE
    Spark job. If that job fails (a database unreachable), each database is asked on its own, so a
    failure stays with its table ({control_id: the exception})."""
    as_int = lambda v: int(v) if v is not None else None

    def ask(group):
        with _units(slots, group):
            rows = union_by_name([_read(spark, c, seed_prep_sql(c)) for c in group]).collect()
        return {int(r["control_id"]): (as_int(r["v"]), as_int(r["mv"]), as_int(r["bv"])) for r in rows}
    try:
        return ask(list(cfgs))
    except Exception:
        out = {}
        for c in cfgs:
            try:
                out.update(ask([c]))
            except Exception as e:
                out[int(c["id"])] = e
        return out


def prepare_seed(spark, cfg: dict, slots=None) -> Tuple[int, Optional[int]]:
    """(V0, begin_version) for a per-table CT seed, in ONE source query (``seed_prep_sql``), asked
    BEFORE the seed reads: the database's CT version, which the checkpoint advances to, and the table's
    CT begin_version, recorded so a later re-enable of CT shows. Raises unless CT is on for both the
    database and the table (seeding a table CT can't follow would hide its changes). One statement
    where ``current_version``, ``require_table_ct`` and ``ct_begin_version`` took three."""
    with _unit(slots, cfg):
        row = _read(spark, cfg, seed_prep_sql(cfg)).first()
    if row is None or row["v"] is None:
        raise RuntimeError(CT_NOT_ENABLED_MSG)
    if row["oid"] is None:
        raise RuntimeError(SOURCE_TABLE_NOT_FOUND_MSG.format(
            table=f"{quote_ident(cfg['src_schema'])}.{quote_ident(cfg['src_table'])}"))
    if row["mv"] is None:
        raise RuntimeError(CT_TABLE_NOT_ENABLED_MSG)
    return int(row["v"]), (int(row["bv"]) if row["bv"] is not None else None)
