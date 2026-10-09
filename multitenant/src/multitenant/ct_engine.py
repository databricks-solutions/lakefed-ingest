"""Change-Tracking engine — IMPLEMENTED (workstreams A/B/F).

Fills the functions the consolidated sweep (``copy_data_sweep.ipynb``) calls. Every
source-touching / CT operation lives here so the orchestration (threading, FAIR pools, Lakebase
checkpointing, failure isolation) stays strategy-agnostic.

Transport = **Spark JDBC on the cluster** (`spark.read.format("jdbc")` with the Microsoft SQL
Server driver bundled in Databricks Runtime). No SQL warehouse and no UC connection are required:
the source read lands as a natively-typed DataFrame straight into the Delta write/MERGE.

Single source read, no caching: ``seed`` streams the JDBC read straight into the Delta overwrite and
``read_and_merge_ct`` feeds the CHANGETABLE read straight into the MERGE; row counts come from Delta
commit metrics, never from ``count()`` on the source DataFrame. Delta MERGE's own source
materialization (``spark.databricks.delta.merge.materializeSource``, default ``auto``) is what
guarantees a non-Delta (JDBC) source is evaluated once — we add no ``.cache()`` of our own.

Connection (per control row ``cfg``, keyed by ``state_store.CONTROL_COLUMNS``):
  * host/port: ``cfg['src_host']``/``cfg['src_port']`` -> (legacy) the UC connection named in
    ``cfg['src_connection']`` -> the secret's own host field -> env ``MT_SQL_HOST``.
  * credentials: secret ``scope``/``key`` holding JSON ``{user, password}`` (flat or nested):
      - scope = env ``MT_SQL_SECRET_SCOPE`` (default ``"lakefed_ingest_mt"``; may be an Azure Key
        Vault-backed scope — note AKV secret names allow only alphanumerics and dashes, see
        ``validate_akv_secret_name``)
      - key   = ``cfg['secret_key']`` -> env ``MT_SQL_SECRET_KEY`` -> legacy ``f"{src_connection}_json"``
    Credentials are cached per (scope, key) — one secret per tenant database — never per server.
  * TLS: ``trustServerCertificate`` from env ``MT_SQL_TRUST_SERVER_CERT`` (default ``true`` for dev;
    production should set ``false`` and present a trusted server certificate).
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
import json
import os
import re
import threading
import uuid
from typing import List, Optional, Tuple

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

    key precedence: ``cfg['secret_key']`` -> env ``MT_SQL_SECRET_KEY`` -> legacy
    ``f"{src_connection}_json"``. Never derived from the server alone: each tenant database may
    carry its own credential even when it shares a server with others.
    """
    scope = os.environ.get("MT_SQL_SECRET_SCOPE", _DEFAULT_SECRET_SCOPE)
    key = cfg.get("secret_key") or os.environ.get("MT_SQL_SECRET_KEY")
    if not key and cfg.get("src_connection"):
        key = f"{cfg['src_connection']}_json"
    if not key:
        raise ValueError(f"control row {cfg.get('id')}: no secret_key (and no legacy src_connection)")
    return scope, key


def config_cache_key(host: str, port: int, scope: str, key: str) -> tuple:
    """Resolved-connection cache key: (host, port, scope, secret key)."""
    return (host, int(port), scope, key)


def trust_server_certificate() -> bool:
    return os.environ.get("MT_SQL_TRUST_SERVER_CERT", "true").strip().lower() in ("1", "true", "yes")


def jdbc_url(host: str, port: int, database: str, trust_cert: bool) -> str:
    return (
        f"jdbc:sqlserver://{host}:{int(port)};databaseName={database};encrypt=true;"
        f"trustServerCertificate={'true' if trust_cert else 'false'}"
    )


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
    raise ValueError("could not find user/password fields in the SQL Server secret")


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
    host = host or secret_host or os.environ.get("MT_SQL_HOST")
    if not host:
        raise ValueError(f"control row {cfg.get('id')}: no src_host could be resolved")
    port = int(port or 1433)
    ck = config_cache_key(host, port, scope, key)
    with _lock:
        if ck not in _config_cache:
            _config_cache[ck] = {"host": host, "port": port, "user": user, "password": pwd}
        return _config_cache[ck]


def _spark():
    from pyspark.sql import SparkSession
    return SparkSession.getActiveSession() or SparkSession.builder.getOrCreate()


def _read(spark, cfg: dict, query: str):
    """Run a T-SQL query against the source via Spark JDBC; returns a natively-typed DataFrame.
    Lazy — the source is only queried when the DataFrame is consumed (once, by the write/MERGE)."""
    conf = _jdbc_config(cfg)
    return (
        spark.read.format("jdbc")
        .option("url", jdbc_url(conf["host"], conf["port"], cfg["src_database"],
                                trust_server_certificate()))
        .option("query", query)
        .option("user", conf["user"])
        .option("password", conf["password"])
        .option("driver", _JDBC_DRIVER)
        .load()
    )


def _unit(slots):
    """One unit of query work: hold ONE slot of the sweep's budget (no-op when ``slots`` is None)."""
    return slots.acquire() if slots is not None else contextlib.nullcontext()


def staging_table_name(sink_fqn: str) -> str:
    """Staging table a partitioned seed loads into before the atomic swap (same catalog.schema)."""
    return f"{sink_fqn}__seed_staging"


# ======================================================================================
# A/B/F functions the sweep calls.
# ======================================================================================
def current_version(cfg: dict, slots=None) -> int:
    """(B) Database-wide CHANGE_TRACKING_CURRENT_VERSION(). 0 if CT not enabled on the DB."""
    with _unit(slots):
        row = _read(_spark(), cfg,
                    "SELECT CAST(CHANGE_TRACKING_CURRENT_VERSION() AS BIGINT) AS v").first()
    return int(row["v"]) if row and row["v"] is not None else 0


def min_valid_version(cfg: dict, slots=None) -> Optional[int]:
    """(F) CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID('<schema>.<table>')). None when CT is not
    enabled on the table -> state_store.is_reseed_needed treats None as 'reseed'."""
    name = f"{cfg['src_schema']}.{cfg['src_table']}".replace("'", "''")
    with _unit(slots):
        row = _read(_spark(), cfg,
                    f"SELECT CAST(CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID('{name}')) AS BIGINT) AS v"
                    ).first()
    return int(row["v"]) if row and row["v"] is not None else None


def ensure_sink_table(spark, cfg: dict, sink_fqn: str, slots=None) -> None:
    """Create the Delta bronze sink if absent, typed from the source via a 0-row JDBC read (so the
    JDBC dialect infers the Spark schema natively). Idempotent. The 0-row read is one unit."""
    if spark.catalog.tableExists(sink_fqn):
        return
    cols = ", ".join(f"[{c}]" for c in _split(cfg["select_list"]))
    with _unit(slots):
        empty = _read(spark, cfg,
                      f"SELECT {cols} FROM [{cfg['src_schema']}].[{cfg['src_table']}] WHERE 1=0")
        empty.write.format("delta").mode("ignore").saveAsTable(sink_fqn)
        # Type widening lets schema-evolving writes apply safe widenings (int->bigint, float->double,
        # decimal precision) when a client's column type is wider. New sinks only; existing sinks:
        # ALTER TABLE <sink> SET TBLPROPERTIES ('delta.enableTypeWidening' = 'true').
        spark.sql(f"ALTER TABLE {sink_fqn} SET TBLPROPERTIES ('delta.enableTypeWidening' = 'true')")


def _select_cols(cfg: dict) -> str:
    return ", ".join(f"[{c}]" for c in _split(cfg["select_list"]))


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


def insert_overwrite_sql(sink_fqn: str, cols: List[str], source: str) -> str:
    """Data-only atomic replace of the target with schema evolution. Consolidated bronze (future,
    spike-validated): ``INSERT WITH SCHEMA EVOLUTION INTO <sink> REPLACE WHERE client_id = ...``."""
    return insert_sql("OVERWRITE", sink_fqn, cols, source, evolve=True)


def missing_sink_columns(select_list: str, sink_columns: List[str]) -> List[str]:
    """select_list columns absent from the sink (case-insensitive) — pure, unit-tested. A non-empty
    result in the cdc phase means the source table gained a column: the sweep reseeds that table so
    historical rows get the new column (CT only returns changed rows)."""
    have = {c.lower() for c in sink_columns}
    return [c for c in _split(select_list) if c.lower() not in have]


def _insert_overwrite(spark, sink_fqn: str, source: str, cols: List[str]) -> int:
    """Atomically replace the target's DATA from ``source`` (a view or table) in one Delta commit.

    ``INSERT OVERWRITE`` is data-only: unlike ``saveAsTable(mode="overwrite")`` — which commits as
    ``CREATE OR REPLACE TABLE AS SELECT`` — it leaves the table definition alone (liquid clustering
    keys, table properties, grants, row filters/column masks), which consolidated bronze depends on.
    Columns map BY NAME from select_list, and ``WITH SCHEMA EVOLUTION`` adds any the target lacks
    (a client/table whose schema gained columns); existing columns are never dropped."""
    row = spark.sql(insert_overwrite_sql(sink_fqn, cols, source)).first()
    d = row.asDict() if row is not None else {}
    return int(d.get("num_inserted_rows", d.get("num_affected_rows", 0)) or 0)


def seed(spark, cfg: dict, sink_fqn: str, slots=None, executor=None, guard=None) -> int:
    """(B seed) Full copy of the current table state into the Delta bronze sink.

    Called after current_version() captured V0, so rows changed during the seed are reconciled on the
    first cdc cycle by the idempotent PK MERGE (no gap, no dupe).

    * Non-partitioned: ONE unit — one JDBC read streams straight into a data-only ``INSERT OVERWRITE``
      (a single atomic commit); rows come from the INSERT's own metrics.
    * Partitioned (``load_partitioned``): size-bounded range partitions load into a staging table,
      then swap into the target atomically — see ``_partitioned_seed``.
    No ``.cache()``/``count()``: every source range is read exactly once.

    Consolidated bronze (future, spike-validated): the overwrite becomes ``INSERT WITH SCHEMA
    EVOLUTION INTO sink REPLACE WHERE client_id = ...`` so only this tenant's slice is replaced."""
    if _is_partitioned(cfg):
        return _partitioned_seed(spark, cfg, sink_fqn, slots, executor, guard)
    with _unit(slots):
        df = _read(spark, cfg, f"SELECT {_select_cols(cfg)} FROM [{cfg['src_schema']}].[{cfg['src_table']}]")
        view = f"seed_full_{uuid.uuid4().hex}"
        df.createOrReplaceTempView(view)
        try:
            return _insert_overwrite(spark, sink_fqn, view, _split(cfg["select_list"]))  # ONE read, ONE commit
        finally:
            spark.catalog.dropTempView(view)


def _partitioned_seed(spark, cfg: dict, sink_fqn: str, slots, executor, guard=None) -> int:
    """Size-bounded, staged, atomic partitioned seed (the accelerator's partitioning strategy).

    1. table size (SQL Server catalog) and MIN/MAX of ``partition_col`` — one unit each;
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
       staging. Consolidated bronze (future): ``INSERT WITH SCHEMA EVOLUTION INTO sink REPLACE WHERE
       client_id = ...``.
    Returns rows in the target after the swap."""
    from concurrent.futures import ThreadPoolExecutor, as_completed, wait
    import partitions

    schema, table, pcol = cfg["src_schema"], cfg["src_table"], cfg.get("partition_col")
    if not pcol:
        raise ValueError("load_partitioned is set but partition_col is empty")
    with _unit(slots):
        size_row = _read(spark, cfg, partitions.table_size_mb_query(schema, table)).first()
    size_mb = float(size_row["table_size_mb"] or 0) if size_row else 0.0
    with _unit(slots):
        b = _read(spark, cfg, partitions.bounds_query(schema, table, pcol)).first()
    lb, ub = (b["lb"], b["ub"]) if b else (None, None)
    if lb is None or ub is None:  # empty table (or all-NULL column): nothing to partition
        print(f"[seed] {schema}.{table}: no {pcol} bounds -> non-partitioned seed")
        return seed(spark, dict(cfg, load_partitioned=False), sink_fqn, slots=slots)  # single unit

    n = partitions.num_partitions_for(size_mb, cfg.get("partition_size_mb"))
    n = partitions.effective_num_partitions(lb, ub, n)
    plist = partitions.get_partition_list(pcol, lb, ub, n)
    staging = staging_table_name(sink_fqn)
    print(f"[seed] {schema}.{table}: size={size_mb} MB, partition_size_mb={cfg.get('partition_size_mb')}, "
          f"{pcol} in [{lb}, {ub}], num_partitions={len(plist)}, staging={staging}")

    base = f"SELECT {_select_cols(cfg)} FROM [{schema}].[{table}]"
    cols = _split(cfg["select_list"])
    with _unit(slots):
        _read(spark, cfg, f"{base} WHERE 1=0").write.mode("overwrite") \
            .option("overwriteSchema", "true").saveAsTable(staging)

    def load_partition(part):
        if guard is not None:
            guard()                                # e.g. raises if the sweep lost its collection lock
        with _unit(slots):
            df = _read(spark, cfg, f"{base} WHERE {part['where_clause']}")
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
    with _unit(slots):                             # atomic swap: ONE data-only Delta commit
        rows = _insert_overwrite(spark, sink_fqn, staging, cols)
        spark.sql(f"DROP TABLE IF EXISTS {staging}")
    return int(rows or 0)


def merge_sql(cfg: dict, sink_fqn: str, view: str) -> str:
    """Idempotent I/U/D MERGE of the staged CT rows (temp view ``view``) into the sink, by PK.

    ``WITH SCHEMA EVOLUTION`` (per statement — never the session-wide autoMerge conf, which the
    threaded sweep would share): select_list columns the sink lacks are added, and with type widening
    enabled on the sink, safe widenings (int->bigint, ...) apply. Explicit column lists keep the CT
    ``op`` column out of bronze. ON keeps tgt./s. qualifiers; SET/INSERT targets are unqualified."""
    return (
        f"MERGE WITH SCHEMA EVOLUTION INTO {sink_fqn} AS tgt USING {view} AS s ON {merge_on(cfg['primary_key'])} "
        "WHEN MATCHED AND s.op = 'D' THEN DELETE "
        f"WHEN MATCHED THEN UPDATE SET {merge_set(cfg['select_list'], cfg['primary_key'])} "
        "WHEN NOT MATCHED AND s.op <> 'D' THEN "
        f"INSERT ({merge_insert_cols(cfg['select_list'])}) "
        f"VALUES ({merge_insert_vals(cfg['select_list'])})"
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
    with _unit(slots):                             # read -> MERGE is ONE unit of work
        df = _read(spark, cfg, ct_read_query(cfg, from_version))
        view = f"ct_staged_{uuid.uuid4().hex}"
        df.createOrReplaceTempView(view)
        try:
            result = spark.sql(merge_sql(cfg, sink_fqn, view)).first()
        finally:
            spark.catalog.dropTempView(view)
    if result is not None and "num_affected_rows" in result.asDict():
        return int(result["num_affected_rows"] or 0)
    # Runtime without MERGE result metrics: don't guess from DESCRIBE HISTORY (an empty MERGE may
    # not commit, so the latest entry could be a previous run's). Report unknown.
    return None
