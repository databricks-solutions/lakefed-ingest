"""Every query the engine sends to a source can be wrapped as a subquery: Spark JDBC's query option reads it as
SELECT * FROM (<query>) and remote_query may do the same, and SQL Server refuses an ORDER BY in a derived table
unless TOP, OFFSET or FOR XML comes with it. So no query text sent to a source may carry an ORDER BY of its own; an
ORDER BY inside OVER (...) is a window's, which is allowed."""
import ast
import pathlib
import re

import pytest

import discovery as d

SOURCE_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "lakefed_ingest"
# The modules whose SQL goes to a source (Spark JDBC or remote_query). state_store and delete_job talk to Lakebase.
SOURCE_QUERY_MODULES = ("discovery", "ct_engine", "cursor_engine", "source_adapters", "initial_check", "partitions",
                        "load_strategies", "batch_append")
ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)
ALLOWED_WITH = re.compile(r"\bover\s*\(|\btop\b|\boffset\b|\bfor\s+xml\b", re.IGNORECASE)


def test_no_query_sent_to_a_source_orders_its_rows():
    found = [f"{module}.py:{line}: {text[:120]!r}" for module in SOURCE_QUERY_MODULES
             for line, text in _string_literals(SOURCE_DIR / f"{module}.py")
             if ORDER_BY.search(text) and not ALLOWED_WITH.search(text)]
    assert found == []


def test_a_wrapped_order_by_is_what_the_check_catches():
    assert ORDER_BY.search("SELECT name FROM sys.databases ORDER BY name")
    assert not ALLOWED_WITH.search("SELECT name FROM sys.databases ORDER BY name")
    assert ALLOWED_WITH.search("SELECT ROW_NUMBER() OVER (ORDER BY id) AS n FROM t")


@pytest.mark.parametrize("pattern", ["", "lfc_eng_%"])
def test_the_database_listing_has_no_order_by_and_comes_back_sorted_by_name(pattern):
    assert not ORDER_BY.search(d.databases_sql(pattern))
    rows = [{"name": "eng_010"}, {"name": "Eng_002"}, {"name": "eng_001"}, {"name": "eng_002"}]
    names = d.list_databases({"src_connection": "sql_a", "pattern": pattern}, query=lambda cfg, sql: rows)
    assert names == ["eng_001", "Eng_002", "eng_002", "eng_010"]


def _string_literals(path: pathlib.Path):
    """Each string literal of a module with its line: a plain string, or an f-string's literal parts joined."""
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            yield node.lineno, " ".join(part.value for part in node.values
                                        if isinstance(part, ast.Constant) and isinstance(part.value, str))
