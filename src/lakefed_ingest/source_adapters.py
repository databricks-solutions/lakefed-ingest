"""Source adapters: everything that depends on the SOURCE DATABASE ENGINE (``control.src_type``).

The engine (``ct_engine.py``) and the partitioned seed (``partitions.py``) are written once against
this interface; an adapter supplies the dialect-specific pieces:

* the JDBC URL, driver class, and default Spark JDBC read options (e.g. fetch size), and the
  ``remote_query`` option naming the database (a read through a Unity Catalog connection);
* identifier quoting and string / bound literals;
* the queries a load strategy needs: plain ``SELECT`` of the select_list, table size in MB and
  MIN/MAX bounds (partitioned seed).

Change-Tracking SQL (CHANGETABLE, retention guard, batched detection) stays in ``ct_engine`` and is
SQL Server-only; ``load_strategies.supported`` gates which (src_type, load_type) pairs can run.

Driver availability on Databricks Runtime (the sweep uses Spark JDBC on the cluster):

* SQL Server  — ``com.microsoft.sqlserver.jdbc.SQLServerDriver``, bundled in DBR.
* PostgreSQL  — ``org.postgresql.Driver``, bundled in DBR.
* Oracle      — ``oracle.jdbc.OracleDriver``, NOT bundled: add a cluster library, e.g. Maven
  ``com.oracle.database.jdbc:ojdbc11:<version>`` (see resources/lakefed_ingest.yml).
* MySQL       — ``com.mysql.cj.jdbc.Driver``, NOT bundled: add a cluster library, e.g. Maven
  ``com.mysql:mysql-connector-j:<version>`` (see resources/lakefed_ingest.yml).

Pure Python (no Spark / DB imports) — unit-tested in tests/source_adapters_test.py.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

SQLSERVER, ORACLE, POSTGRESQL, MYSQL = "sqlserver", "oracle", "postgresql", "mysql"

# Spark JDBC options a control row must NOT override via ``control.jdbc_options``: connection target,
# credentials, driver, and the query itself are owned by the framework.
_RESERVED_OPTIONS = {"url", "user", "password", "driver", "query", "dbtable"}


def _split(csv: Optional[str]) -> List[str]:
    return [c.strip() for c in (csv or "").split(",") if c.strip()]


def parse_jdbc_options(raw) -> Dict[str, str]:
    """``control.jdbc_options`` (JSON object text, a dict, or NULL) -> {option: str(value)}.

    Extra Spark JDBC read options per control row (e.g. ``{"fetchsize": "50000"}``). Reserved
    options (url, user, password, driver, query, dbtable) are rejected — they are framework-owned."""
    if raw is None or raw == "":
        return {}
    opts = json.loads(raw) if isinstance(raw, str) else dict(raw)
    if not isinstance(opts, dict):
        raise ValueError("jdbc_options must be a JSON object")
    bad = sorted(k for k in opts if str(k).lower() in _RESERVED_OPTIONS)
    if bad:
        raise ValueError(f"jdbc_options may not set {', '.join(bad)}")
    return {str(k): (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in opts.items()}


class SourceAdapter:
    """Dialect for one source engine. Subclasses override the class attributes and the methods
    whose SQL differs; the defaults are ANSI (double-quoted identifiers, '' string escaping)."""

    src_type: str = ""
    default_port: int = 0
    driver: str = ""
    # Default Spark JDBC read options (control.jdbc_options overrides per row).
    default_options: Dict[str, str] = {}
    # The remote_query option naming src_database (a read through a Unity Catalog connection).
    remote_database_option: str = "database"

    # ---- connection ---------------------------------------------------------------------
    def jdbc_url(self, host: str, port: int, database: str, trust_cert: bool) -> str:
        raise NotImplementedError

    def read_options(self, cfg: dict) -> Dict[str, str]:
        """Adapter defaults overlaid with the control row's ``jdbc_options``."""
        return {**self.default_options, **parse_jdbc_options(cfg.get("jdbc_options"))}

    # ---- quoting / literals ---------------------------------------------------------------
    def quote_ident(self, name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    def quote_str(self, value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def table_ref(self, schema: str, table: str) -> str:
        return f"{self.quote_ident(schema)}.{self.quote_ident(table)}"

    def render_bound(self, epoch_or_number: int, original) -> str:
        """SQL literal for a partition bound. ``original`` is the MIN value read from the source (its
        type picks the literal form); numeric bounds pass through unchanged."""
        if isinstance(original, (int, float, Decimal)) and not isinstance(original, bool):
            return str(epoch_or_number)
        if isinstance(original, datetime):
            return self.timestamp_literal(_utc(epoch_or_number).strftime("%Y-%m-%d %H:%M:%S"))
        if isinstance(original, date):
            return self.date_literal(_utc(epoch_or_number).strftime("%Y-%m-%d"))
        raise ValueError(
            f"Unsupported data type: {type(original)}. Only int, date, and datetime are supported")

    def timestamp_literal(self, text: str) -> str:
        return f"TIMESTAMP '{text}'"

    def date_literal(self, text: str) -> str:
        return f"DATE '{text}'"

    # ---- queries -----------------------------------------------------------------------
    def select_cols(self, select_list: str) -> str:
        return ", ".join(self.quote_ident(c) for c in _split(select_list))

    def select_sql(self, cfg: dict, where: Optional[str] = None) -> str:
        """``SELECT <select_list> FROM <schema>.<table> [WHERE ...]`` — the seed / full-load read."""
        sql = f"SELECT {self.select_cols(cfg['select_list'])} FROM {self.table_ref(cfg['src_schema'], cfg['src_table'])}"
        return f"{sql} WHERE {where}" if where else sql

    def bounds_query(self, schema: str, table: str, partition_col: str) -> str:
        col = self.quote_ident(partition_col)
        return f"SELECT MIN({col}) AS lb, MAX({col}) AS ub FROM {self.table_ref(schema, table)}"

    def table_size_mb_query(self, schema: str, table: str) -> str:
        """One row ``table_size_mb`` (data + indexes) for the partitioned seed's partition count."""
        raise NotImplementedError


class SqlServerAdapter(SourceAdapter):
    src_type = SQLSERVER
    default_port = 1433
    driver = "com.microsoft.sqlserver.jdbc.SQLServerDriver"

    def jdbc_url(self, host, port, database, trust_cert):
        return (
            f"jdbc:sqlserver://{host}:{int(port)};databaseName={database};encrypt=true;"
            f"trustServerCertificate={'true' if trust_cert else 'false'}"
        )

    def quote_ident(self, name):
        return "[" + str(name).replace("]", "]]") + "]"

    def quote_str(self, value):
        return "N'" + str(value).replace("'", "''") + "'"

    # SQL Server converts plain string literals to the column's date/datetime type implicitly.
    def timestamp_literal(self, text):
        return f"'{text}'"

    def date_literal(self, text):
        return f"'{text}'"

    def table_size_mb_query(self, schema, table):
        # Data + index pages (as the original accelerator's _get_table_size_sqlserver).
        s = str(schema).replace("'", "''")
        t = str(table).replace("'", "''")
        return (
            "SELECT CAST(ROUND((SUM(a.total_pages) * 8) / 1024.0, 2) AS NUMERIC(36, 2)) AS table_size_mb"
            " FROM sys.tables t"
            " JOIN sys.indexes i ON t.object_id = i.object_id"
            " JOIN sys.partitions p ON i.object_id = p.object_id AND i.index_id = p.index_id"
            " JOIN sys.allocation_units a ON p.partition_id = a.container_id"
            " LEFT OUTER JOIN sys.schemas s ON t.schema_id = s.schema_id"
            f" WHERE s.name = '{s}' AND t.name = '{t}'"
            " AND t.is_ms_shipped = 0 AND i.object_id > 255"
        )


class OracleAdapter(SourceAdapter):
    """Oracle via the thin driver. ``src_database`` is the SERVICE NAME; ``src_schema`` the owner.

    Identifiers are double-quoted, which makes them case-sensitive: control values must match the
    stored (normally UPPER-case) names, and the Delta sink columns take that case."""

    src_type = ORACLE
    default_port = 1521
    driver = "oracle.jdbc.OracleDriver"
    default_options = {"fetchsize": "10000"}   # the driver default (10 rows/round trip) is far too small
    remote_database_option = "service_name"

    def jdbc_url(self, host, port, database, trust_cert):
        # TLS (TCPS) needs a wallet / truststore and is configured per deployment; see docs/deploy.md.
        return f"jdbc:oracle:thin:@//{host}:{int(port)}/{database}"

    def table_size_mb_query(self, schema, table):
        # Least privilege: ALL_TABLES optimizer statistics (num_rows x avg_row_len) need only SELECT on
        # the table. A table without statistics reports 0 MB -> the 2-partition floor applies. Exact
        # segment sizes (DBA_SEGMENTS) need extra privileges — see docs/deploy.md.
        return (
            "SELECT ROUND(NVL(num_rows, 0) * NVL(avg_row_len, 0) / 1048576, 2) AS table_size_mb"
            f" FROM all_tables WHERE owner = {self.quote_str(schema)} AND table_name = {self.quote_str(table)}"
        )


class PostgresAdapter(SourceAdapter):
    src_type = POSTGRESQL
    default_port = 5432
    driver = "org.postgresql.Driver"
    # A fetch size > 0 makes the driver stream with a cursor (Spark turns autocommit off for it);
    # with the default 0 the whole result is buffered in executor memory.
    default_options = {"fetchsize": "10000"}

    def jdbc_url(self, host, port, database, trust_cert):
        if trust_cert:
            return f"jdbc:postgresql://{host}:{int(port)}/{database}?sslmode=require"
        # verify-full against the JVM truststore (public CAs) rather than ~/.postgresql/root.crt; a
        # private CA can be supplied per row with jdbc_options {"sslrootcert": ..., "sslfactory": ...}.
        return (f"jdbc:postgresql://{host}:{int(port)}/{database}?sslmode=verify-full"
                "&sslfactory=org.postgresql.ssl.DefaultJavaSSLFactory")

    def table_size_mb_query(self, schema, table):
        rel = self.quote_str(self.table_ref(schema, table))
        return (f"SELECT ROUND(CAST(pg_total_relation_size(to_regclass({rel})) AS NUMERIC) / 1048576, 2) "
                "AS table_size_mb")


class MySqlAdapter(SourceAdapter):
    """MySQL: a schema IS a database — set ``src_schema`` to the database name (``src_database`` is
    the connection's default database and is normally the same)."""

    src_type = MYSQL
    default_port = 3306
    driver = "com.mysql.cj.jdbc.Driver"
    default_options = {"fetchsize": "10000"}   # cursor fetch is enabled in the URL

    def jdbc_url(self, host, port, database, trust_cert):
        ssl = "REQUIRED" if trust_cert else "VERIFY_IDENTITY"
        return f"jdbc:mysql://{host}:{int(port)}/{database}?sslMode={ssl}&useCursorFetch=true"

    def quote_ident(self, name):
        return "`" + str(name).replace("`", "``") + "`"

    def quote_str(self, value):
        # Backslash is an escape character in MySQL string literals (unless NO_BACKSLASH_ESCAPES).
        return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"

    def timestamp_literal(self, text):
        return f"'{text}'"

    def date_literal(self, text):
        return f"'{text}'"

    def table_size_mb_query(self, schema, table):
        return (
            "SELECT ROUND((data_length + index_length) / 1048576, 2) AS table_size_mb"
            " FROM information_schema.tables"
            f" WHERE table_schema = {self.quote_str(schema)} AND table_name = {self.quote_str(table)}"
        )


_ADAPTERS = {a.src_type: a for a in (SqlServerAdapter(), OracleAdapter(), PostgresAdapter(), MySqlAdapter())}
SRC_TYPES = tuple(_ADAPTERS)


def get_adapter(src_type: Optional[str]) -> SourceAdapter:
    """Adapter for ``control.src_type`` (NULL/empty -> sqlserver, the column default)."""
    key = (src_type or SQLSERVER).strip().lower()
    try:
        return _ADAPTERS[key]
    except KeyError:
        raise ValueError(
            f"unsupported src_type {src_type!r}; supported: {', '.join(SRC_TYPES)}") from None


def default_port(src_type: Optional[str]) -> int:
    """The port a source type is reached on when a control row's port is blank. 0 for a type with no
    adapter, so a stamp key can still be built for a row the ingest refuses."""
    adapter = _ADAPTERS.get((src_type or "").strip(" ").lower() or SQLSERVER)   # spaces only, as SQL trim() does
    return adapter.default_port if adapter else 0


def for_cfg(cfg: dict) -> SourceAdapter:
    return get_adapter(cfg.get("src_type"))


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)
