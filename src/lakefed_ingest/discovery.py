"""Table discovery for bulk onboarding: list a source database's tables and recommend control-row
settings for each (select_list, primary key, load type, cursor column, partition candidate).

Run by the ``lakefed_ingest_discover`` job (``discover_tables.ipynb``) on the same compute and
network path as ingestion; results land in Lakebase (``discovery``, ``discovered_table``), where
the operations app shows them for review before creating control rows. SQL Server only for now;
other adapters are placeholders.

Recommendations (the reviewer can change any of them):
  * load_type ``ct`` when Change Tracking is enabled on the table and it has a primary key;
    ``cursor`` on its rowversion column when it has a PK and one; otherwise ``full``
    (identity / updated_at cursors miss updates or depend on the application, so they're never
    picked automatically).
  * ``select_list`` = every column whose type Spark JDBC reads (CLR types such as geography /
    geometry / hierarchyid, and sql_variant, are excluded and listed in ``excluded_columns``).
  * ``partition_candidate`` = the first PK column if it's an integer / date / datetime type, else
    an integer identity column. The app applies the size threshold (``load_partitioned``) at review.

Catalog views only (sys.tables / columns / indexes / partitions / allocation_units /
change_tracking_tables): no data is read and no permission beyond metadata visibility is needed.
A server's databases are listed through master, or for Azure SQL by Azure Resource Manager with a Unity Catalog
service credential (``azure_databases``). Pure Python except ``discover_database`` and the Azure call (unit-tested
in tests/discovery_test.py, with fakes for the credential and the HTTP call).
"""

from __future__ import annotations

import json
import re
from typing import Dict, List

import load_strategies as ls
import source_adapters as sa

TABLES_SQL = (
    "SELECT t.object_id AS oid, s.name AS sch, t.name AS tbl, "
    "CAST(ISNULL((SELECT SUM(p.rows) FROM sys.partitions p "
    "  WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)), 0) AS BIGINT) AS row_count, "
    "CAST(ISNULL((SELECT SUM(a.total_pages) * 8 / 1024.0 FROM sys.partitions p "
    "  JOIN sys.allocation_units a ON a.container_id = p.partition_id "
    "  WHERE p.object_id = t.object_id), 0) AS DECIMAL(18, 2)) AS size_mb, "
    "CAST(CASE WHEN ct.object_id IS NULL THEN 0 ELSE 1 END AS INT) AS ct_enabled "
    "FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
    "LEFT JOIN sys.change_tracking_tables ct ON ct.object_id = t.object_id "
    "WHERE t.is_ms_shipped = 0"
)

COLUMNS_SQL = (
    "SELECT c.object_id AS oid, c.column_id AS pos, c.name AS col, "
    "TYPE_NAME(c.system_type_id) AS base_type, TYPE_NAME(c.user_type_id) AS user_type, "
    "CAST(c.is_identity AS INT) AS is_identity, CAST(c.is_nullable AS INT) AS is_nullable "
    "FROM sys.columns c JOIN sys.tables t ON t.object_id = c.object_id WHERE t.is_ms_shipped = 0"
)

PK_SQL = (
    "SELECT ic.object_id AS oid, ic.key_ordinal AS ord, c.name AS col "
    "FROM sys.indexes i "
    "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
    "JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
    "JOIN sys.tables t ON t.object_id = i.object_id "
    "WHERE i.is_primary_key = 1 AND t.is_ms_shipped = 0"
)

# The server's user databases that are online, for Test connection and for discovering a whole server. Read through
# master; system databases (ids 1 to 4) are left out. Listed by name, not by HAS_DBACCESS: on Azure SQL a session in
# master can't reach another database (no cross-database access), so HAS_DBACCESS there is 0 for every user database,
# even for a login with a user in each; and a contained user has no login at all. A database the login can name but
# not open is listed, and fails when its tables are read. No ORDER BY: the query may be read as a subquery (remote_query,
# Spark JDBC's query option), where SQL Server refuses one, so list_databases sorts the names instead.
DATABASES_SQL = "SELECT name FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE'{name}"
SERVER_DATABASE = "master"
# A name filter as SQL Server's LIKE reads it (% any run of characters, _ any one character, [ ] a set), as typed in
# Test connection: letters, digits and those marks only, so it can go into the query as a literal.
PATTERN = re.compile(r"^[A-Za-z0-9_%\-\[\]\.$#@]{1,128}$")


def pattern_errors(pattern: str) -> List[str]:
    """Why a database name filter can't be used, or nothing: a blank one lists every database."""
    if not pattern or PATTERN.match(pattern):
        return []
    return [f"database filter {pattern!r}: use letters, digits and _ % - [ ] . $ # @ only (at most 128)"]


def databases_sql(pattern: str = "") -> str:
    """DATABASES_SQL with the name filter, if any (checked by pattern_errors first)."""
    if pattern_errors(pattern):
        raise ValueError(pattern_errors(pattern)[0])
    return DATABASES_SQL.format(name=f" AND name LIKE N'{pattern}'" if pattern else "")


# An Azure SQL server's (or Managed Instance's) databases can be listed by Azure Resource Manager instead of through
# master, for a login that can't open master: the discovery job calls it with a token from a Unity Catalog service
# credential whose managed identity has Reader on the server. Each database is then read by name through the
# connection, as a typed one is. The app never calls Azure.
ARM = "https://management.azure.com"
ARM_API_VERSION = "2023-08-01"
ARM_SCOPE = ARM + "/.default"
ARM_RETRIES = 3
AZURE_RESOURCE = re.compile(r"^/subscriptions/[0-9A-Za-z-]+/resourceGroups/[-\w.()]+/providers/Microsoft\.Sql/"
                            r"(servers|managedInstances)/[a-z0-9][a-z0-9-]{0,127}$", re.IGNORECASE)
CREDENTIAL = re.compile(r"^[A-Za-z0-9_\-]{1,255}$")
SYSTEM_DATABASES = {"master", "model", "msdb", "tempdb"}
# Azure's statuses for a database a login can open (a paused serverless one resumes on connecting); a database with
# no status is kept. Creating, restoring, offline, stopped and the like are left out, as master leaves out a
# database that isn't ONLINE.
READABLE_STATUSES = {"online", "paused", "pausing", "resuming", "scaling", "autoclosed",
                     "onlinechangingdwperformancetiers", "updating"}


def azure_errors(spec: dict) -> List[str]:
    """Why a spec's listing through Azure can't be used, or nothing: it takes both a service credential and the
    server's resource id, or neither."""
    credential, resource = spec.get("azure_credential"), spec.get("azure_resource_id")
    if not credential and not resource:
        return []
    if not (credential and resource):
        return ["listing through Azure needs both azure_credential (a Unity Catalog service credential) and "
                "azure_resource_id (the server's resource id)"]
    errors = []
    if not CREDENTIAL.match(credential):
        errors.append(f"azure_credential {credential!r}: use letters, digits, _ and - only")
    if not AZURE_RESOURCE.match(resource):
        errors.append(f"azure_resource_id {resource!r}: expected /subscriptions/<id>/resourceGroups/<group>/providers/"
                      "Microsoft.Sql/servers/<server> (or managedInstances/<instance>)")
    return errors


def lists_through_azure(spec: dict) -> bool:
    """Whether the spec's server is listed by Azure Resource Manager rather than through master."""
    return bool(spec.get("azure_credential") and spec.get("azure_resource_id"))


def like(pattern: str) -> "re.Pattern[str]":
    """A database name filter as SQL Server's LIKE reads it, for the names Azure lists: % any run of characters, _ any
    one, [ ] a set or a range. Case-insensitive, as a server's default collation is."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        end = pattern.find("]", i + 2) if c == "[" else -1
        if c == "%":
            out.append(".*")
        elif c == "_":
            out.append(".")
        elif end > 0:
            out.append("[" + pattern[i + 1:end].replace("[", r"\[") + "]")
            i = end
        else:
            out.append(re.escape(c))
        i += 1
    try:
        return re.compile("^" + "".join(out) + "$", re.IGNORECASE | re.DOTALL)
    except re.error as e:
        raise ValueError(f"database filter {pattern!r}: {e}") from e


def _arm_get(url: str, token: str) -> dict:
    """One page of an Azure Resource Manager list."""
    import urllib.request
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def _arm_refusal(error, resource: str) -> str:
    """What Azure's refusal means: its status and error code, with the fix for the usual ones."""
    try:
        detail = json.loads(error.read() or b"{}").get("error") or {}
    except Exception:
        detail = {}
    said = f"HTTP {error.code}" + (f" {detail['code']}" if detail.get("code") else "")
    fix = {401: "Azure refused the service credential's token",
           403: "give the service credential's managed identity (its access connector's) Reader on the server",
           404: "check the server's resource id"}.get(error.code, str(detail.get("message") or "").split("\n")[0][:200])
    return f"Azure refused to list the databases of {resource} ({said})" + (f": {fix}" if fix else "")


def azure_databases(spec: dict, credential, get=None, sleep=None) -> List[str]:
    """The server's databases as Azure Resource Manager lists them, page by page, with the system databases and those
    that can't be opened left out, matching spec's "pattern" if it has one. credential is the Unity Catalog service
    credential's provider (dbutils.credentials.getServiceCredentialsProvider), which gives the token."""
    import time
    import urllib.error
    get, sleep = get or _arm_get, sleep or time.sleep
    resource = spec["azure_resource_id"]
    match = like(spec.get("pattern") or "%")
    url, names = f"{ARM}{resource}/databases?api-version={ARM_API_VERSION}", []
    while url:
        for attempt in range(ARM_RETRIES + 1):
            try:
                page = get(url, credential.get_token(ARM_SCOPE).token)
                break
            except urllib.error.HTTPError as e:
                if attempt == ARM_RETRIES or e.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(_arm_refusal(e, resource)) from e
                # Azure's throttling says how long to wait; otherwise 5, 10 and 20 s.
                sleep(min(float(e.headers.get("Retry-After") or 5 * 2 ** attempt), 60.0))
        names += [str(item["name"]) for item in page.get("value") or []
                  if str((item.get("properties") or {}).get("status") or "online").lower() in READABLE_STATUSES]
        url = page.get("nextLink")
        # The token goes only to Azure Resource Manager, whatever a page says comes next.
        if url and not url.startswith(ARM + "/"):
            raise RuntimeError(f"Azure's next page is outside {ARM}, so it isn't followed: {url[:200]}")
    return sorted({n for n in names if n.lower() not in SYSTEM_DATABASES and match.match(n)},
                  key=lambda n: (n.lower(), n))

UNSUPPORTED_TYPES = {"geography", "geometry", "hierarchyid", "sql_variant"}
PARTITION_TYPES = {"tinyint", "smallint", "int", "bigint", "decimal", "numeric",
                   "date", "datetime", "datetime2", "smalldatetime"}
ROWVERSION_TYPES = {"timestamp", "rowversion"}


def recommend(table: dict, columns: List[dict], pk: List[str]) -> dict:
    """Control-row settings for one discovered table.

    table: {sch, tbl, row_count, size_mb, ct_enabled}; columns: [{pos, col, base_type, user_type,
    is_identity}] in any order; pk: PK column names in key order."""
    cols = sorted(columns, key=lambda c: int(c["pos"]))
    keep, excluded, notes = [], [], []
    for c in cols:
        types = {str(c.get("base_type") or "").lower(), str(c.get("user_type") or "").lower()}
        if types & UNSUPPORTED_TYPES or "," in str(c["col"]):
            excluded.append(c["col"])
        else:
            keep.append(c)
    if excluded:
        notes.append(f"excluded unsupported columns: {', '.join(excluded)}")
    by_name = {c["col"]: c for c in cols}
    # A cursor column must be NOT NULL (the ingestion job refuses a nullable one: NULL rows would never
    # be read incrementally), so a nullable rowversion isn't a cursor candidate.
    rowversion = next((c["col"] for c in keep if str(c["base_type"]).lower() in ROWVERSION_TYPES
                       and not int(c.get("is_nullable") or 0)), None)
    identity = next((c["col"] for c in keep if int(c.get("is_identity") or 0)), None)
    pk = [p for p in pk if p in by_name]
    if pk and any(p in excluded for p in pk):
        notes.append("primary key uses an unsupported column type")
        pk = []

    if bool(int(table.get("ct_enabled") or 0)) and pk:
        load_type, cursor = ls.CT, None
    elif pk and rowversion:
        load_type, cursor = ls.CURSOR, rowversion
        notes.append("no Change Tracking: cursor on rowversion (deletes not captured)")
    else:
        load_type, cursor = ls.FULL, None
        notes.append("no Change Tracking and no primary key + rowversion: full reload"
                     if not pk else "no Change Tracking or rowversion column: full reload")

    candidate = None
    if pk and str(by_name[pk[0]]["base_type"]).lower() in PARTITION_TYPES:
        candidate = pk[0]
    elif identity and str(by_name[identity]["base_type"]).lower() in PARTITION_TYPES:
        candidate = identity

    return {
        "select_list": ", ".join(c["col"] for c in keep),
        "excluded_columns": ", ".join(excluded) or None,
        "primary_key": ", ".join(pk) or None,
        "rowversion_column": rowversion,
        "identity_column": identity,
        "load_type": load_type,
        "cursor_column": cursor,
        "partition_candidate": candidate,
        "notes": "; ".join(notes) or None,
    }


def assemble(tables, columns, pks) -> List[dict]:
    """Join the three catalog query results (rows as dicts) into one recommendation per table."""
    cols_by: Dict[int, list] = {}
    for c in columns:
        cols_by.setdefault(int(c["oid"]), []).append(c)
    pk_by: Dict[int, list] = {}
    for p in sorted(pks, key=lambda r: (int(r["oid"]), int(r["ord"]))):
        pk_by.setdefault(int(p["oid"]), []).append(p["col"])
    out = []
    for t in tables:
        oid = int(t["oid"])
        rec = recommend(t, cols_by.get(oid, []), pk_by.get(oid, []))
        out.append({"src_schema": t["sch"], "src_table": t["tbl"], "row_count": int(t["row_count"] or 0),
                    "size_mb": float(t["size_mb"] or 0), "ct_enabled": bool(int(t["ct_enabled"] or 0)), **rec})
    return sorted(out, key=lambda r: (r["src_schema"].lower(), r["src_table"].lower()))


# A paused serverless source (e.g. Azure SQL serverless: error 40613, "not currently available")
# refuses connections for a minute or more while it resumes. Transient errors (the table retries'
# classifier, which keeps a refused login permanent) are retried with exponential backoff and equal
# jitter: pauses of 15-30 s, 30-60 s and 60-120 s, so a database gets up to ~3.5 minutes to resume.
CONNECT_RETRIES, CONNECT_RETRY_BASE_S, CONNECT_RETRY_MAX_S = 3, 30.0, 120.0


def cfg_for(spec: dict, database: str) -> dict:
    """The row a discovery reads one database with: through the spec's Unity Catalog connection (remote_query, no
    secret), or by Spark JDBC with its host, port and secret, as ingestion reads it."""
    base = {"id": 0, "src_type": spec.get("src_type") or "sqlserver", "src_database": database}
    if spec.get("src_connection"):
        return {**base, "src_connection": spec["src_connection"], "use_remote_query": True}
    return {**base, "src_host": spec["src_host"], "src_port": spec.get("src_port"),
            "secret_key": secret_key_for(spec, database)}


def spec_errors(spec: dict) -> List[str]:
    """What a discovery spec lacks: a UC connection, or a host with a secret; and the databases to read, unless it
    only lists them (kind 'list')."""
    errors = []
    if not spec.get("src_connection") and not spec.get("src_host"):
        errors.append("spec needs src_connection (a UC connection) or src_host")
    if spec.get("src_host") and not spec.get("src_connection") and not (spec.get("secret_key") or spec.get("secret_key_template")):
        errors.append("spec needs secret_key or secret_key_template with src_host")
    if spec.get("kind") != "list" and not spec.get("databases"):
        errors.append("spec needs databases: a list, or \"*\" for every database on the server")
    return errors + pattern_errors(spec.get("pattern") or "") + azure_errors(spec)


def _query(cfg: dict, sql: str, slots=None, sleep=None, rand=None) -> List[dict]:
    """One catalog query's rows, retrying a transient error (``CONNECT_RETRIES``)."""
    import random
    import time
    import ct_engine
    import table_retry
    sleep, rand = sleep or time.sleep, rand or random.random
    spark = ct_engine._spark()
    for attempt in range(CONNECT_RETRIES + 1):
        try:
            with ct_engine._unit(slots):
                return [r.asDict() for r in ct_engine._read(spark, cfg, sql).collect()]
        except Exception as e:
            if attempt == CONNECT_RETRIES or table_retry.classify(e) != table_retry.TRANSIENT:
                raise
            sleep(table_retry.backoff_delay(attempt, CONNECT_RETRY_BASE_S, CONNECT_RETRY_MAX_S, rand))
    raise AssertionError("unreachable")


def _sqlserver_only(cfg: dict) -> None:
    if sa.get_adapter(cfg.get("src_type")).src_type != sa.SQLSERVER:
        raise ValueError(f"discovery is not implemented yet for src_type '{cfg.get('src_type')}'")


def needs_listing(spec: dict) -> bool:
    """Whether a discovery lists the server's databases through master: Test connection (kind 'list') and a
    whole server ("databases": "*") do; databases named by hand or from a file are read by name, never through
    master, so a login that can't open master (a user contained in each database) can still discover them."""
    return spec.get("kind") == "list" or spec.get("databases") == "*"


def list_databases(spec: dict, query=None, credential=None, get=None) -> List[str]:
    """The server's user databases, read through master and matching spec's "pattern" if it has one: what Test
    connection shows, and what discovering a whole server ("databases": "*") reads. When the spec names a service
    credential and the server's resource id, Azure lists them instead (azure_databases), with that credential."""
    cfg = cfg_for(spec, SERVER_DATABASE)
    _sqlserver_only(cfg)
    if lists_through_azure(spec):
        if credential is None:
            raise ValueError(f"listing through Azure needs the service credential '{spec['azure_credential']}'")
        return azure_databases(spec, credential, get=get)
    names = [str(r["name"]) for r in (query or _query)(cfg, databases_sql(spec.get("pattern") or ""))]
    return sorted(names, key=lambda n: (n.lower(), n))


def discover_database(cfg: dict, slots=None, sleep=None, rand=None) -> List[dict]:
    """Discover every user table of one source database (three catalog queries), retrying a query
    that fails with a transient error (``CONNECT_RETRIES``)."""
    _sqlserver_only(cfg)
    return assemble(*(_query(cfg, sql, slots, sleep, rand) for sql in (TABLES_SQL, COLUMNS_SQL, PK_SQL)))


# Databases the discover job reads at once (the discover_workers job parameter): 32 is the design's count of concurrent
# queries on a server, each database taking its three catalog queries in turn.
DEFAULT_WORKERS = 32


def workers_for(requested: int, databases: int, max_queries_per_source: int = 0) -> int:
    """How many databases the discover job reads at once: the requested count, no more than the databases there are,
    and no more than max_queries_per_source when that is set (0 = no cap). The discover job does not take part in the
    ingest runs' shares of that cap (they are counted from advisory locks it never takes), so here it is a ceiling of
    its own."""
    workers = max(1, int(requested))
    if max_queries_per_source > 0:
        workers = min(workers, max_queries_per_source)
    return max(1, min(workers, databases))


def secret_key_for(spec: dict, database: str) -> str:
    """The secret key for a database: ``secret_key_template`` with {database} filled in, else
    ``secret_key``."""
    tpl = spec.get("secret_key_template")
    return tpl.replace("{database}", database) if tpl else spec.get("secret_key")


LISTING_KEYS = ("pattern", "azure_credential", "azure_resource_id", "secret_key_template", "jdbc_connection")


def listing_of(spec: dict) -> dict:
    """How the spec listed its databases, kept on each of its discovery rows so a later re-discovery of the same
    selection reads the same way: the LIKE pattern, the Azure resource and its credential, the secret template,
    and the name of the JDBC connection it was made through. Only the keys the spec has (the secret itself is
    never kept), so an empty dict means none of them."""
    return {key: spec[key] for key in LISTING_KEYS if str(spec.get(key) or "").strip()}
