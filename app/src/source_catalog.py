"""Table discovery's SQL Server catalog reads, as the app runs them: the server's database list (DATABASES_SQL) and
TABLE_SUMMARY_SQL, one query per database with one row per user table. assemble_summary turns those rows into the same
recommendations as the engine's discovery.assemble (load type, select list, primary key, cursor column, partition
candidate, notes).

The engine's discovery.py keeps the three-query path for servers reached by host and password. This module is a copy,
because the app deploys only app/src and can't import the engine. Its rules match discovery.recommend, and
app/tests/source_catalog_test.py runs both on the same tables, so the two can't drift apart unnoticed.

error_codes reads the codes a failed connection test's message names, for the page to show.

Standard library only. Nothing here reads a database: the app runs the statements itself."""
import re
from typing import List

# The server's user databases that are online, for the connection test and for discovering a whole server. Read through
# master; system databases (ids 1 to 4) are left out. Listed by name, not by HAS_DBACCESS: on Azure SQL a session in
# master can't reach another database, so HAS_DBACCESS there is 0 for every user database. No ORDER BY: the query may
# be read as a subquery (remote_query), where SQL Server refuses one, so the caller sorts the names instead.
DATABASES_SQL = "SELECT name FROM sys.databases WHERE database_id > 4 AND state_desc = 'ONLINE'{name}"
# A name filter as SQL Server's LIKE reads it (% any run of characters, _ any one character, [ ] a set), as typed in
# the connection step: letters, digits and those marks only, so it can go into the query as a literal.
PATTERN = re.compile(r"^[A-Za-z0-9_%\-\[\]\.$#@]{1,128}$")


def pattern_errors(pattern: str) -> List[str]:
    """Why a database name filter can't be used, or nothing: a blank one lists every database."""
    if not pattern or PATTERN.match(pattern):
        return []
    return [f"database filter {pattern!r}: use letters, digits and _ % - [ ] . $ # @ only (at most 128)"]


# What a failed connection test's message names, so the page can show its code and say what to do. Databricks puts
# its error class in brackets ([FAILED_JDBC.CONNECTION]); SQL Server's driver gives its message without the error
# number, so the number is read from the message's own words, or from "Error 18456" where the text carries it.
ERROR_CLASS = re.compile(r"\[([A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*)\]")
SQLSTATE = re.compile(r"SQLSTATE:?\s*([0-9A-Z]{5})\b")
SQL_SERVER_NUMBER = re.compile(r"\b(?:Error|Msg|error number|errorCode)[ :=]*(\d{3,6})\b", re.IGNORECASE)
SQL_SERVER_MESSAGES = [("Login failed for user", 18456), ("Cannot open database", 4060),
                       ("is not allowed to access the server", 40615), ("is not currently available", 40613)]


def error_codes(message) -> dict:
    """The codes in a failed connection test's message: Databricks' error class, SQL Server's error number and the
    SQLSTATE, each None when the message has none."""
    text = str(message or "")
    error_class = ERROR_CLASS.search(text)
    sqlstate = SQLSTATE.search(text)
    number = SQL_SERVER_NUMBER.search(text)
    sql_server_error = int(number.group(1)) if number else next(
        (code for words, code in SQL_SERVER_MESSAGES if words.lower() in text.lower()), None)
    return {"error_class": error_class.group(1) if error_class else None, "sql_server_error": sql_server_error,
            "sqlstate": sqlstate.group(1) if sqlstate else None}


def databases_sql(pattern: str = "") -> str:
    """DATABASES_SQL with the name filter, if any (checked by pattern_errors first)."""
    errors = pattern_errors(pattern)
    if errors:
        raise ValueError(errors[0])
    return DATABASES_SQL.format(name=f" AND name LIKE N'{pattern}'" if pattern else "")


UNSUPPORTED_TYPES = {"geography", "geometry", "hierarchyid", "sql_variant"}
PARTITION_TYPES = {"tinyint", "smallint", "int", "bigint", "decimal", "numeric",
                   "date", "datetime", "datetime2", "smalldatetime"}
ROWVERSION_TYPES = {"timestamp", "rowversion"}

UNSUPPORTED_SQL_TYPES = ", ".join(f"'{name}'" for name in sorted(UNSUPPORTED_TYPES))
ROWVERSION_SQL_TYPES = ", ".join(f"'{name}'" for name in sorted(ROWVERSION_TYPES))
# A column the engine leaves out of the select list: a base or user type in UNSUPPORTED_TYPES, or a name with a comma.
# ISNULL keeps a type name SQL Server can't give from making NOT(...) unknown, which would drop a readable column.
UNREADABLE_COLUMN_SQL = (f"(ISNULL(LOWER(TYPE_NAME(c.system_type_id)), '') IN ({UNSUPPORTED_SQL_TYPES}) "
                         f"OR ISNULL(LOWER(TYPE_NAME(c.user_type_id)), '') IN ({UNSUPPORTED_SQL_TYPES}) "
                         "OR CHARINDEX(',', c.name) > 0)")
# One row per user table. remote_query reads this text as a subquery, so there is no WITH and no top-level ORDER BY
# (ORDER BY inside a FOR XML or TOP subquery is fine). The lists use STUFF with FOR XML PATH, TYPE read back by
# .value, so a name with & < or > comes back as written on every SQL Server version.
TABLE_SUMMARY_SQL = (
    "SELECT s.name AS sch, t.name AS tbl, "
    "CAST(ISNULL((SELECT SUM(p.rows) FROM sys.partitions p "
    "  WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)), 0) AS BIGINT) AS row_count, "
    "CAST(CASE WHEN ct.object_id IS NULL THEN 0 ELSE 1 END AS INT) AS ct_enabled, "
    "STUFF((SELECT ', ' + c.name FROM sys.columns c "
    f"  WHERE c.object_id = t.object_id AND NOT {UNREADABLE_COLUMN_SQL} "
    "  ORDER BY c.column_id FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') AS select_cols, "
    "STUFF((SELECT ', ' + c.name FROM sys.columns c "
    f"  WHERE c.object_id = t.object_id AND {UNREADABLE_COLUMN_SQL} "
    "  ORDER BY c.column_id FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') AS excluded_cols, "
    "STUFF((SELECT ', ' + c.name FROM sys.indexes i "
    "  JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
    "  JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
    "  WHERE i.object_id = t.object_id AND i.is_primary_key = 1 "
    "  ORDER BY ic.key_ordinal FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') AS pk_cols, "
    "CAST(CASE WHEN EXISTS (SELECT 1 FROM sys.indexes i "
    "  JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
    "  JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
    f"  WHERE i.object_id = t.object_id AND i.is_primary_key = 1 AND {UNREADABLE_COLUMN_SQL}) "
    "  THEN 1 ELSE 0 END AS INT) AS pk_unreadable, "
    "(SELECT TOP (1) LOWER(TYPE_NAME(c.system_type_id)) FROM sys.indexes i "
    "  JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
    "  JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id "
    "  WHERE i.object_id = t.object_id AND i.is_primary_key = 1 ORDER BY ic.key_ordinal) AS pk_first_type, "
    "(SELECT TOP (1) c.name FROM sys.columns c "
    f"  WHERE c.object_id = t.object_id AND NOT {UNREADABLE_COLUMN_SQL} AND c.is_nullable = 0 "
    f"  AND LOWER(TYPE_NAME(c.system_type_id)) IN ({ROWVERSION_SQL_TYPES}) "
    "  ORDER BY c.column_id) AS rowversion_col, "
    "(SELECT TOP (1) c.name FROM sys.columns c "
    f"  WHERE c.object_id = t.object_id AND c.is_identity = 1 AND NOT {UNREADABLE_COLUMN_SQL} "
    "  ORDER BY c.column_id) AS identity_col, "
    "(SELECT TOP (1) LOWER(TYPE_NAME(c.system_type_id)) FROM sys.columns c "
    f"  WHERE c.object_id = t.object_id AND c.is_identity = 1 AND NOT {UNREADABLE_COLUMN_SQL} "
    "  ORDER BY c.column_id) AS identity_type "
    "FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
    "LEFT JOIN sys.change_tracking_tables ct ON ct.object_id = t.object_id "
    "WHERE t.is_ms_shipped = 0"
)


def assemble_summary(rows) -> List[dict]:
    """One recommendation per table from TABLE_SUMMARY_SQL's rows, with the keys, values and sort order of
    discovery.assemble. size_mb is None: the summary doesn't read table sizes. Values may come back as text (a SQL
    warehouse returns every value as a string), so numbers and flags are read through int()."""
    out = []
    for row in rows:
        out.append({"src_schema": row["sch"], "src_table": row["tbl"], "row_count": int(row["row_count"] or 0),
                    "size_mb": None, "ct_enabled": bool(int(row["ct_enabled"] or 0)),
                    **_recommend_summary_row(row)})
    return sorted(out, key=lambda row: (row["src_schema"].lower(), row["src_table"].lower()))


def _split_names(joined) -> List[str]:
    """The names TABLE_SUMMARY_SQL joined with ', ', or none when the list is NULL or empty. A readable name holds no
    comma, so the split is exact for those. An excluded name that holds ', ' splits in two, but the outputs join the
    parts back with ', ', so the text comes out the same."""
    return str(joined).split(", ") if joined else []


def _recommend_summary_row(row: dict) -> dict:
    """The control-row settings for one summary row, by the same rules as the engine's recommend(): a key with an
    unsupported column is dropped, CT needs a key, a cursor needs a key and a NOT NULL rowversion, and the partition
    candidate is the first key column (or the identity) when its type is a partition type."""
    readable = _split_names(row.get("select_cols"))
    excluded = _split_names(row.get("excluded_cols"))
    key = _split_names(row.get("pk_cols"))
    ct_enabled = bool(int(row["ct_enabled"] or 0))
    rowversion = row.get("rowversion_col") or None
    identity = row.get("identity_col") or None
    identity_type = str(row.get("identity_type") or "")
    first_key_type = str(row.get("pk_first_type") or "")

    notes = []
    if excluded:
        notes.append(f"excluded unsupported columns: {', '.join(excluded)}")
    if int(row.get("pk_unreadable") or 0):
        notes.append("primary key uses an unsupported column type")
        key = []

    if ct_enabled and key:
        load_type, cursor = "ct", None
    elif key and rowversion:
        load_type, cursor = "cursor", rowversion
        notes.append("no Change Tracking: cursor on rowversion (deletes not captured)")
    else:
        load_type, cursor = "full", None
        notes.append("no Change Tracking and no primary key + rowversion: full reload"
                     if not key else "no Change Tracking or rowversion column: full reload")

    candidate = None
    if key and first_key_type in PARTITION_TYPES:
        candidate = key[0]
    elif identity and identity_type in PARTITION_TYPES:
        candidate = identity

    return {
        "select_list": ", ".join(readable),
        "excluded_columns": ", ".join(excluded) or None,
        "primary_key": ", ".join(key) or None,
        "rowversion_column": rowversion,
        "identity_column": identity,
        "load_type": load_type,
        "cursor_column": cursor,
        "partition_candidate": candidate,
        "notes": "; ".join(notes) or None,
    }
