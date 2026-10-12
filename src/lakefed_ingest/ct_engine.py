"""Source engine: Spark JDBC reads, seeds / full loads, and the Change-Tracking (CT) strategy.

Fills the functions the ingestion notebook (``ingest.ipynb``) calls. Every source-touching
operation lives here so the orchestration (threading, FAIR pools, Lakebase checkpointing, failure
isolation) stays strategy-agnostic. Dialect-specific SQL (JDBC URL, driver, quoting, SELECT /
size / bounds queries) comes from the row's source adapter (``source_adapters``, chosen by
``control.src_type``); the CT functions (CHANGETABLE, detection, retention guard) are SQL Server-only.

Transport = **Spark JDBC on the cluster** (`spark.read.format("jdbc")`; the SQL Server and
PostgreSQL drivers ship with Databricks Runtime, Oracle / MySQL need a cluster library). No SQL
warehouse and no UC connection are required: the source read lands as a natively-typed DataFrame
straight into the Delta write/MERGE.

Single source read, no caching: ``seed`` streams the JDBC read straight into the Delta overwrite and
``read_and_merge_ct`` feeds the CHANGETABLE read straight into the MERGE; row counts come from Delta
commit metrics, never from ``count()`` on the source DataFrame. Delta MERGE's own source
materialization (``spark.databricks.delta.merge.materializeSource``, default ``auto``) is what
guarantees a non-Delta (JDBC) source is evaluated once — we add no ``.cache()`` of our own.

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
import hashlib
import json
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
    """UPDATE SET of the non-PK columns; degenerate all-PK table -> harmless no-op SET on the PK.

    Targets are UNQUALIFIED (`` `c` = s.`c` ``): under ``MERGE WITH SCHEMA EVOLUTION`` a column the
    target doesn't have yet only resolves unqualified (``tgt.`c` `` fails on DBR 18)."""
    pk = set(_split(primary_key))
    non_pk = [c for c in _split(select_list) if c not in pk]
    cols = non_pk or _split(primary_key)
    return ", ".join(f"`{c}` = s.`{c}`" for c in cols)


def merge_insert_cols(select_list: str) -> str:
    return ", ".join(f"`{c}`" for c in _split(select_list))


def merge_insert_vals(select_list: str) -> str:
    return ", ".join(f"s.`{c}`" for c in _split(select_list))


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


def _jdbc_config(cfg: dict) -> dict:
    """Resolve {host, port, user, password} for this control row (cached per host/port/secret)."""
    scope, key = secret_ref(cfg)
    user, pwd, secret_host = _creds(scope, key)
    host, port = cfg.get("src_host"), cfg.get("src_port")
    if not host and cfg.get("src_connection"):
        host, uc_port = _uc_host(cfg["src_connection"])
        port = port or uc_port
    host = host or secret_host or os.environ.get("LAKEFED_SQL_HOST")
    if not host:
        raise ValueError(f"control row {cfg.get('id')}: no src_host could be resolved")
    port = int(port or source_adapters.for_cfg(cfg).default_port)
    ck = config_cache_key(host, port, scope, key)
    with _lock:
        if ck not in _config_cache:
            _config_cache[ck] = {"host": host, "port": port, "user": user, "password": pwd}
        return _config_cache[ck]


def _spark():
    from pyspark.sql import SparkSession
    return SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()


def _read(spark, cfg: dict, query: str, session_init: Optional[str] = None):
    """Run a query (in the source's dialect) via Spark JDBC; returns a natively-typed DataFrame.
    Lazy — the source is only queried when the DataFrame is consumed (once, by the write/MERGE)."""
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


def _unit(slots):
    """One unit of query work: hold ONE slot of the sweep's budget (no-op when ``slots`` is None)."""
    return slots.acquire() if slots is not None else contextlib.nullcontext()


def staging_table_name(sink_fqn: str, cfg: Optional[dict] = None) -> str:
    """Staging table a partitioned seed loads into before the atomic swap (same catalog.schema).
    Consolidated sinks get one per client — several clients may seed the same sink at once."""
    cid = client_id(cfg) if cfg else None
    if cid is None:
        return f"{sink_fqn}__seed_staging"
    slug = re.sub(r"[^a-z0-9_]", "_", cid.lower())[:40]
    return f"{sink_fqn}__seed_staging__{slug}_{hashlib.sha1(cid.encode()).hexdigest()[:8]}"


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


def write_columns(cfg: dict) -> List[str]:
    """Columns written to the sink: select_list, plus __client_id__ for a consolidated sink."""
    cols = _split(cfg["select_list"])
    return cols + [CLIENT_COLUMN] if client_id(cfg) else cols


def merge_keys(cfg: dict) -> List[str]:
    """MERGE match keys: (__client_id__, PK) for a consolidated sink, else the PK."""
    pk = _split(cfg["primary_key"])
    return [CLIENT_COLUMN] + pk if client_id(cfg) else pk


def client_predicate(cfg: dict) -> str:
    """Delta predicate selecting this client's rows (the value is validated by ``client_id``)."""
    return f"`{CLIENT_COLUMN}` = '{client_id(cfg)}'"


def _tag(df, cfg: dict):
    """Prepend the client_id literal to a source DataFrame (no-op for a dedicated sink). FIRST, so
    a new sink has it within Delta's stats columns (first 32 by default) — liquid clustering on
    __client_id__ requires stats; wide tables would otherwise fail to cluster."""
    cid = client_id(cfg)
    if cid is None:
        return df
    return df.select(_lit(cid).alias(CLIENT_COLUMN), *df.columns)


def _lit(value):
    from pyspark.sql import functions as F
    return F.lit(value)


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
    with _unit(slots):
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


def ct_table_status(cfg: dict, slots=None) -> dict:
    """{(schema, table) lower-cased: (min_valid_version, begin_version)} for every CT-enabled table in
    the source database — ONE metadata query per database per cycle (rows are table names, not data).
    Checked before the fast exit: per-table CT disable/re-enable doesn't move the database version.
    ``begin_version`` is the database version when CT (re)started on the table; a change from the
    value recorded at seed time means CT was re-enabled since, so the table must re-seed."""
    with _unit(slots):
        rows = _read(_spark(), cfg, CT_TABLE_STATUS_SQL).collect()
    as_int = lambda v: int(v) if v is not None else None
    return {(str(r["sch"]).lower(), str(r["tbl"]).lower()): (as_int(r["mv"]), as_int(r["bv"])) for r in rows}


def ct_begin_version(cfg: dict, slots=None) -> Optional[int]:
    """The table's CT begin_version (database version when CT started on it), recorded at seed time."""
    obj = quote_nstr(f"{quote_ident(cfg['src_schema'])}.{quote_ident(cfg['src_table'])}")
    with _unit(slots):
        row = _read(_spark(), cfg,
                    "SELECT CAST(begin_version AS BIGINT) AS bv FROM sys.change_tracking_tables "
                    f"WHERE object_id = OBJECT_ID({obj})").first()
    return int(row["bv"]) if row and row["bv"] is not None else None


CT_TABLE_NOT_ENABLED_MSG = ("Change Tracking is not enabled on this source table "
                            "(CHANGE_TRACKING_MIN_VALID_VERSION() is NULL); enable it, then the table re-seeds")


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
    with _unit(slots):
        row = _read(_spark(), cfg,
                    f"SELECT CAST(CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID({obj})) AS BIGINT) AS v"
                    ).first()
    return int(row["v"]) if row and row["v"] is not None else None


SINK_MODE_MISMATCH_MSG = (
    "sink {sink} {has} '{col}' column but this control row {is_} consolidated (client_id {cid}); "
    "a sink is either dedicated to one source table or shared by client_id — never both. Point the "
    "row at another sink or fix client_id.")


def check_sink_mode(cfg: dict, sink_fqn: str, sink_columns: List[str]) -> None:
    """Refuse to write a dedicated row into a consolidated sink (its overwrite/MERGE would hit every
    client's rows) or a consolidated row into a dedicated sink (rows without __client_id__)."""
    has = CLIENT_COLUMN in {c.lower() for c in sink_columns}
    cid = client_id(cfg)
    native = CLIENT_COLUMN in {c.lower() for c in _split(cfg.get("select_list"))}
    if has and cid is None and not native:     # a dedicated table may have its own such column
        raise ValueError(SINK_MODE_MISMATCH_MSG.format(sink=sink_fqn, has="has a", col=CLIENT_COLUMN,
                                                       is_="is not", cid="NULL"))
    if not has and cid is not None:
        raise ValueError(SINK_MODE_MISMATCH_MSG.format(sink=sink_fqn, has="has no", col=CLIENT_COLUMN,
                                                       is_="is", cid=repr(cid)))


def ensure_sink_table(spark, cfg: dict, sink_fqn: str, slots=None) -> None:
    """Create the Delta bronze sink if absent, typed from the source via a 0-row JDBC read (so the
    JDBC dialect infers the Spark schema natively), then check the sink's mode (dedicated vs
    consolidated) matches the row. Idempotent. The 0-row read is one unit.

    A consolidated sink is created with the ``__client_id__`` column and liquid clustering on it, so
    each client's slice is cheap to find and concurrent clients' writes touch separate files."""
    if not spark.catalog.tableExists(sink_fqn):
        def create():
            with _unit(slots):
                if spark.catalog.tableExists(sink_fqn):      # another client created it meanwhile
                    return
                empty = _tag(_read(spark, cfg, source_adapters.for_cfg(cfg).select_sql(cfg, where="1=0")), cfg)
                view = f"sink_schema_{uuid.uuid4().hex}"
                empty.createOrReplaceTempView(view)
                try:
                    spark.sql(create_sink_sql(sink_fqn, view, cfg))
                finally:
                    spark.catalog.dropTempView(view)
        with_write_retry(create, what=f"create {sink_fqn}")
    check_sink_mode(cfg, sink_fqn, spark.table(sink_fqn).columns)


def _is_partitioned(cfg: dict) -> bool:
    v = cfg.get("load_partitioned")
    return v is True or str(v).lower() == "true"


def insert_sql(mode: str, target: str, cols: List[str], source: str, evolve: bool = False) -> str:
    """``INSERT [WITH SCHEMA EVOLUTION] {INTO|OVERWRITE} <target> BY NAME SELECT `a`, `b` FROM <source>``.

    BY NAME maps columns by name, so a reordered/edited select_list can never shift values into the
    wrong columns; with ``evolve`` the target gains any select_list column it lacks (spike-validated
    on DBR 18 — an explicit column list instead would require the columns to already exist)."""
    collist = ", ".join(f"`{c}`" for c in cols)
    evo = "WITH SCHEMA EVOLUTION " if evolve else ""
    return f"INSERT {evo}{mode} {target} BY NAME SELECT {collist} FROM {source}"


def insert_overwrite_sql(sink_fqn: str, cols: List[str], source: str, cfg: Optional[dict] = None) -> str:
    """Data-only atomic replace with schema evolution: the whole target, or — consolidated sink —
    only this client's rows (``INSERT WITH SCHEMA EVOLUTION INTO <sink> BY NAME REPLACE WHERE
    __client_id__ = '<id>' SELECT ...``; validated on DBR: other clients untouched, BY NAME mapping,
    new columns added. BY NAME must precede REPLACE WHERE)."""
    if cfg is not None and client_id(cfg):
        collist = ", ".join(f"`{c}`" for c in cols)
        return (f"INSERT WITH SCHEMA EVOLUTION INTO {sink_fqn} BY NAME REPLACE WHERE {client_predicate(cfg)} "
                f"SELECT {collist} FROM {source}")
    return insert_sql("OVERWRITE", sink_fqn, cols, source, evolve=True)


def missing_sink_columns(select_list: str, sink_columns: List[str]) -> List[str]:
    """select_list columns absent from the sink (case-insensitive) — pure, unit-tested. A non-empty
    result in the cdc phase means the source table gained a column: the sweep reseeds that table so
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


def ct_retention_guard_sql(cfg: dict, from_version: int) -> str:
    """T-SQL run on the CHANGETABLE read's own connection immediately before the read (Spark JDBC
    ``sessionInitStatement``). Planning checked retention earlier, but CT auto-cleanup can raise the
    table's minimum valid version during a long sweep; reading CHANGES from a version below it
    would silently miss changes. The guard makes SQL Server raise instead, and the sweep turns that
    error into a reseed. (A check inside the SELECT itself isn't reliable: SQL Server may skip
    evaluating it when the change set is empty — exactly the lapsed-retention case.)"""
    obj = quote_nstr(f"{quote_ident(cfg['src_schema'])}.{quote_ident(cfg['src_table'])}")
    mv = f"CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID({obj}))"
    return (f"IF {mv} IS NULL OR {mv} > {int(from_version)} "
            f"THROW 51000, N'{RETENTION_LAPSED_MARKER}: change tracking retention has passed the "
            f"checkpoint (or CT is disabled on the table); reseed required', 1;")


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
            with _unit(slots):
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


def seed(spark, cfg: dict, sink_fqn: str, slots=None, executor=None, guard=None) -> int:
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
    BY NAME REPLACE WHERE __client_id__ = ...``, so only this client's rows are replaced."""
    if _is_partitioned(cfg):
        return _partitioned_seed(spark, cfg, sink_fqn, slots, executor, guard)

    def once():
        with _unit(slots):
            df = _tag(_read(spark, cfg, source_adapters.for_cfg(cfg).select_sql(cfg)), cfg)
            view = f"seed_full_{uuid.uuid4().hex}"
            df.createOrReplaceTempView(view)
            try:
                return _insert_overwrite(spark, sink_fqn, view, write_columns(cfg), cfg)  # ONE read, ONE commit
            finally:
                spark.catalog.dropTempView(view)
    return with_write_retry(once, what=f"seed {sink_fqn}")


def _partitioned_seed(spark, cfg: dict, sink_fqn: str, slots, executor, guard=None) -> int:
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
       and stages in a per-client table.
    Returns rows in the target after the swap."""
    from concurrent.futures import ThreadPoolExecutor, as_completed, wait
    import partitions

    schema, table, pcol = cfg["src_schema"], cfg["src_table"], cfg.get("partition_col")
    if not pcol:
        raise ValueError("load_partitioned is set but partition_col is empty")
    adapter = source_adapters.for_cfg(cfg)
    with _unit(slots):
        size_row = _read(spark, cfg, partitions.table_size_mb_query(schema, table, adapter)).first()
    size_mb = float(size_row["table_size_mb"] or 0) if size_row else 0.0
    with _unit(slots):
        b = _read(spark, cfg, partitions.bounds_query(schema, table, pcol, adapter)).first()
    lb, ub = (b["lb"], b["ub"]) if b else (None, None)
    if lb is None or ub is None:  # empty table (or all-NULL column): nothing to partition
        print(f"[seed] {schema}.{table}: no {pcol} bounds -> non-partitioned seed")
        return seed(spark, dict(cfg, load_partitioned=False), sink_fqn, slots=slots)  # single unit

    n = partitions.num_partitions_for(size_mb, cfg.get("partition_size_mb"))
    n = partitions.effective_num_partitions(lb, ub, n)
    plist = partitions.get_partition_list(pcol, lb, ub, n, adapter)
    staging = staging_table_name(sink_fqn, cfg)
    print(f"[seed] {schema}.{table}: size={size_mb} MB, partition_size_mb={cfg.get('partition_size_mb')}, "
          f"{pcol} in [{lb}, {ub}], num_partitions={len(plist)}, staging={staging}")

    cols = write_columns(cfg)
    with _unit(slots):
        _tag(_read(spark, cfg, adapter.select_sql(cfg, where="1=0")), cfg).write.mode("overwrite") \
            .option("overwriteSchema", "true").saveAsTable(staging)

    def load_partition(part):
        if guard is not None:
            guard()                                # e.g. raises if the sweep lost its collection lock
        with _unit(slots):
            df = _tag(_read(spark, cfg, adapter.select_sql(cfg, where=part["where_clause"])), cfg)
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
                f"{cancelled} cancelled; target untouched; first error: {str(first_err)[:300]}")
    finally:
        if own_executor:
            ex.shutdown(wait=True)

    if guard is not None:
        guard()                                    # don't swap if the sweep lost its lock
    def swap():
        with _unit(slots):                         # atomic swap: ONE data-only Delta commit
            return _insert_overwrite(spark, sink_fqn, staging, cols, cfg)
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
                      slots=None) -> Optional[int]:
    """(A) Read CT deltas since ``from_version`` and MERGE them into the Delta sink by PK.

    Projects PK from the change table and non-PK from the base table (NULL on delete) as a
    natively-typed JDBC DataFrame and feeds it straight into an idempotent I/U/D MERGE — one source
    read, no ``.cache()``/``count()``. Returns rows affected from the MERGE's own metrics (0 when the
    change set was empty).

    The staging temp view gets a unique name per call: the sweep runs many tables concurrently in
    one SparkSession, and temp views are session-scoped, so a shared name would let threads
    overwrite each other's change sets."""
    def once():
        with _unit(slots):                         # read -> MERGE is ONE unit of work
            df = _tag(_read(spark, cfg, ct_read_query(cfg, from_version),
                            session_init=ct_retention_guard_sql(cfg, from_version)), cfg)
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
