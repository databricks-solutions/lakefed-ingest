"""The app's copy of table discovery's catalog reads (app/src/source_catalog.py) against the engine's (discovery.py):
the same database listing, the same recommendations for the same tables, and the shape of the summary query. Plain
functions only: no database and no workspace (the engine's module is imported from the repo, never run against one)."""
import re
import sys
from pathlib import Path

import pytest

import source_catalog as catalog

REPO = Path(__file__).resolve().parents[2]
UNREADABLE_TYPES = {"geography", "geometry", "hierarchyid", "sql_variant"}
# Ten synthetic tables, one per case the recommendations sort out. A table is (oid, schema, table, ct_enabled, columns,
# key); a column is (name, base type[, user type, is_identity, is_nullable]) in column order, and the key lists its
# column names in key order.
SUMMARY_TABLES = [
    # Change Tracking on, with a key.
    (1, "dbo", "orders", 1, [("id", "int", "int", 1), ("total", "decimal")], ["id"]),
    # No Change Tracking, a key and a NOT NULL rowversion: cursor.
    (2, "dbo", "audit_log", 0, [("id", "bigint"), ("rv", "timestamp", "timestamp", 0, 0), ("note", "nvarchar")],
     ["id"]),
    # A nullable rowversion only: full reload.
    (3, "dbo", "legacy", 0, [("id", "int"), ("rv", "timestamp", "timestamp", 0, 1)], ["id"]),
    # No key, with a NOT NULL rowversion: full reload.
    (4, "dbo", "keyless", 0, [("a", "int"), ("rv", "timestamp", "timestamp", 0, 0)], []),
    # An unsupported column type, left out of the select list.
    (5, "dbo", "places", 1, [("id", "int", "int", 1), ("shape", "varbinary", "geography"), ("name", "nvarchar")],
     ["id"]),
    # A column whose name has a comma.
    (6, "dbo", "tags", 0, [("id", "int"), ("a,b", "int"), ("v", "nvarchar")], ["id"]),
    # A key on an unsupported type: the key is dropped.
    (7, "dbo", "trees", 1, [("node", "hierarchyid", "hierarchyid"), ("v", "int")], ["node"]),
    # An identity column as the partition candidate, under a text key.
    (8, "dbo", "events", 0, [("code", "nvarchar"), ("seq", "int", "int", 1)], ["code"]),
    # A key whose first column is a partition type.
    (9, "dbo", "daily_facts", 1, [("day", "date"), ("store", "int"), ("sales", "decimal")], ["day", "store"]),
    # Names with & and <.
    (10, "sales&ops", "Q&A <draft>", 0,
     [("a&b", "int", "int", 1), ("x<y", "nvarchar"), ("c&d", "timestamp", "timestamp", 0, 0)], ["a&b"]),
]


def test_the_summary_recommends_what_the_engine_recommends_for_the_same_tables():
    discovery = _engine()
    tables, columns, keys = _catalog(SUMMARY_TABLES)
    expected = [dict(row, size_mb=None) for row in discovery.assemble(tables, columns, keys)]
    assert {row["load_type"] for row in expected} == {"ct", "cursor", "full"}
    # SQL Server gives the rows in no particular order, and assemble_summary sorts them the way assemble does.
    got = catalog.assemble_summary(list(reversed(_summary_rows(tables, columns, keys))))
    assert len(got) == len(expected) == len(SUMMARY_TABLES)
    for got_row, expected_row in zip(got, expected):
        assert list(got_row) == list(expected_row)
        assert got_row == expected_row


def test_a_summary_read_as_text_gives_the_same_recommendations():
    # A SQL warehouse returns every value as a string, NULL included.
    tables, columns, keys = _catalog(SUMMARY_TABLES)
    rows = _summary_rows(tables, columns, keys)
    as_text = [{name: None if value is None else str(value) for name, value in row.items()} for row in rows]
    assert catalog.assemble_summary(as_text) == catalog.assemble_summary(rows)


def test_the_database_listing_is_the_engines_sql_for_every_filter():
    discovery = _engine()
    assert catalog.DATABASES_SQL == discovery.DATABASES_SQL
    for pattern in ["", "lfc_eng_%", "eng_0[0-4]%", "sales-2024.%"]:
        assert catalog.databases_sql(pattern) == discovery.databases_sql(pattern)
        assert catalog.pattern_errors(pattern) == discovery.pattern_errors(pattern) == []
    for pattern in ["x' OR 1=1 --", "a b", "x" * 129]:
        assert catalog.pattern_errors(pattern) == discovery.pattern_errors(pattern) != []
        with pytest.raises(ValueError):
            catalog.databases_sql(pattern)


def test_the_summary_has_no_top_level_order_by_and_no_with_clause():
    # remote_query reads the text as a subquery, where SQL Server refuses a top-level ORDER BY and a WITH clause.
    sql = catalog.TABLE_SUMMARY_SQL
    depths = _order_by_depths(sql)
    assert depths and min(depths) > 0
    assert len(re.findall(r"ORDER BY [\w.]+ FOR XML PATH\(''\), TYPE\)", sql)) == 3
    assert not re.search(r"\bWITH\b", _literals_blanked(sql))


def test_the_summary_joins_its_lists_with_for_xml_and_reads_only_catalog_views():
    sql = catalog.TABLE_SUMMARY_SQL
    assert sql.count("FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '')") == 3
    assert all(source.startswith("sys.") for source in re.findall(r"\b(?:FROM|JOIN) (\S+)", sql))
    assert "TYPE_NAME(" in sql and "sys.change_tracking_tables" in sql


def test_the_summary_names_each_output_column():
    aliases = set(re.findall(r"\bAS (\w+)", _literals_blanked(catalog.TABLE_SUMMARY_SQL))) - {"BIGINT", "INT"}
    assert aliases == {"sch", "tbl", "row_count", "ct_enabled", "select_cols", "excluded_cols", "pk_cols",
                       "pk_unreadable", "pk_first_type", "rowversion_col", "identity_col", "identity_type"}


def test_the_summary_leaves_out_microsoft_tables_and_lists_the_unsupported_types():
    sql = catalog.TABLE_SUMMARY_SQL
    assert "WHERE t.is_ms_shipped = 0" in sql
    for name in sorted(UNREADABLE_TYPES):
        assert f"'{name}'" in sql
    assert "'rowversion', 'timestamp'" in sql
    assert "CHARINDEX(',', c.name) > 0" in sql


def _engine():
    """The engine's discovery module, imported from the repo's src/lakefed_ingest for the comparison."""
    engine = str(REPO / "src" / "lakefed_ingest")
    if engine not in sys.path:
        sys.path.insert(0, engine)
    import discovery
    return discovery


def _catalog(specs):
    """The engine's three catalog results (tables, columns, key rows) for SUMMARY_TABLES-style specs."""
    tables, columns, keys = [], [], []
    for oid, schema, table, ct_enabled, column_specs, key in specs:
        tables.append({"oid": oid, "sch": schema, "tbl": table, "row_count": oid * 100, "size_mb": float(oid),
                       "ct_enabled": ct_enabled})
        for position, spec in enumerate(column_specs, 1):
            base = spec[1]
            columns.append({"oid": oid, "pos": position, "col": spec[0], "base_type": base,
                            "user_type": spec[2] if len(spec) > 2 else base,
                            "is_identity": spec[3] if len(spec) > 3 else 0,
                            "is_nullable": spec[4] if len(spec) > 4 else 0})
        keys.extend({"oid": oid, "ord": position, "col": name} for position, name in enumerate(key, 1))
    return tables, columns, keys


def _unreadable(column):
    """TABLE_SUMMARY_SQL's rule: a base or user type in UNREADABLE_TYPES, or a comma in the name."""
    return (column["base_type"].lower() in UNREADABLE_TYPES or column["user_type"].lower() in UNREADABLE_TYPES
            or "," in column["col"])


def _joined(names):
    """The STUFF(... FOR XML PATH) list: names joined with ', ', NULL when there are none."""
    return ", ".join(names) or None


def _summary_rows(tables, columns, keys):
    """What TABLE_SUMMARY_SQL returns for these catalog rows, worked out from the SQL's own definitions: one row per
    table, each list in column or key order, and each single value from the first matching column by column_id."""
    rows = []
    for table in tables:
        table_columns = sorted((column for column in columns if column["oid"] == table["oid"]),
                               key=lambda column: column["pos"])
        by_name = {column["col"]: column for column in table_columns}
        key_rows = sorted((row for row in keys if row["oid"] == table["oid"]), key=lambda row: row["ord"])
        key_columns = [by_name[row["col"]] for row in key_rows]
        readable = [column for column in table_columns if not _unreadable(column)]
        rowversion = next((column for column in readable if column["base_type"].lower() in ("timestamp", "rowversion")
                           and column["is_nullable"] == 0), None)
        identity = next((column for column in readable if column["is_identity"] == 1), None)
        rows.append({
            "sch": table["sch"], "tbl": table["tbl"], "row_count": table["row_count"],
            "ct_enabled": table["ct_enabled"],
            "select_cols": _joined(column["col"] for column in readable),
            "excluded_cols": _joined(column["col"] for column in table_columns if _unreadable(column)),
            "pk_cols": _joined(column["col"] for column in key_columns),
            "pk_unreadable": int(any(_unreadable(column) for column in key_columns)),
            "pk_first_type": key_columns[0]["base_type"].lower() if key_columns else None,
            "rowversion_col": rowversion["col"] if rowversion else None,
            "identity_col": identity["col"] if identity else None,
            "identity_type": identity["base_type"].lower() if identity else None,
        })
    return rows


def _literals_blanked(sql):
    """The SQL with each string literal emptied, so a parenthesis or a keyword inside one doesn't count."""
    return re.sub(r"'[^']*'", "''", sql)


def _order_by_depths(sql):
    """The parenthesis depth of each ORDER BY in the SQL: 0 is the top level."""
    text, depth, depths = _literals_blanked(sql), 0, []
    for position, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif text.startswith("ORDER BY", position):
            depths.append(depth)
    return depths


@pytest.mark.parametrize("message, codes", [
    ("[FAILED_JDBC.CONNECTION] Failed JDBC jdbc: on the operation: Login failed for user 'reader'. SQLSTATE: HV000",
     {"error_class": "FAILED_JDBC.CONNECTION", "sql_server_error": 18456, "sqlstate": "HV000"}),
    ("com.microsoft.sqlserver.jdbc.SQLServerException: Cannot open server 'sql-a' requested by the login. Client with IP "
     "address '10.0.0.1' is not allowed to access the server.", {"error_class": None, "sql_server_error": 40615, "sqlstate": None}),
    ("Database 'eng_001' on server 'sql-a' is not currently available. Please retry the connection later.",
     {"error_class": None, "sql_server_error": 40613, "sqlstate": None}),
    # A number the text carries wins over the words.
    ("Msg 18452, Level 14: Login failed for user ''.", {"error_class": None, "sql_server_error": 18452, "sqlstate": None}),
    ("The TCP/IP connection to the host sql-a, port 1433 has failed. SQLSTATE: 08S01",
     {"error_class": None, "sql_server_error": None, "sqlstate": "08S01"}),
    ("", {"error_class": None, "sql_server_error": None, "sqlstate": None}),
    (None, {"error_class": None, "sql_server_error": None, "sqlstate": None}),
])
def test_a_failed_connection_tests_codes_are_read_from_its_message(message, codes):
    assert catalog.error_codes(message) == codes
