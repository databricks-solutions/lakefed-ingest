"""The form of lakebase_comments.sql, without a database: only COMMENT ON statements on lakefed_ingest objects, each
object once, every text short enough to read and split cleanly (no semicolon or double hyphen inside, which the
statement splitter treats as boundaries). That every table and column has one is lakebase_comments_pg_test.py."""
import re
from pathlib import Path

import pytest
import state_store as ss

SQL = (Path(__file__).resolve().parents[1] / "src" / "lakefed_ingest" / "lakebase_comments.sql").read_text()
STATEMENT = re.compile(r"^comment on (table|column) lakefed_ingest\.([\w.]+) is '((?:[^']|'')+)'$", re.S)


def _parsed():
    out = []
    for statement in ss.split_sql_statements(SQL):
        m = STATEMENT.match(statement)
        assert m, f"not a COMMENT ON a lakefed_ingest table or column: {statement[:100]}"
        out.append(m.groups())
    return out


def test_every_statement_comments_on_a_lakefed_ingest_table_or_column_once():
    parsed = _parsed()
    objects = [(kind, name) for kind, name, _ in parsed]
    assert len(objects) == len(set(objects))
    assert all(name.count(".") == (1 if kind == "column" else 0) for kind, name, _ in parsed)


def test_every_column_belongs_to_a_described_table():
    parsed = _parsed()
    tables = {name for kind, name, _ in parsed if kind == "table"}
    assert {name.split(".")[0] for kind, name, _ in parsed if kind == "column"} <= tables


def test_the_texts_split_cleanly_and_stay_readable():
    statements = ss.split_sql_statements(SQL)
    assert len(statements) == SQL.count("\ncomment on ") + SQL.startswith("comment on ")
    for _, name, text in _parsed():
        assert 10 <= len(text) <= 600, name
        assert ";" not in text and "--" not in text, name


def test_apply_comments_refuses_a_file_with_anything_but_comments():
    class _NoDatabase:
        pass
    store = ss.StateStore(_NoDatabase())
    with pytest.raises(ValueError, match="only COMMENT ON"):
        store.apply_comments("comment on table lakefed_ingest.control is 'x';\ndrop table lakefed_ingest.control;")
