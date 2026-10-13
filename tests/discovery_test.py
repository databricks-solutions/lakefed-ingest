"""Table discovery: recommendations per table, assembly of the catalog queries, helpers."""
import pytest

import discovery as d


def _cols(*specs):
    """specs: (name, base_type[, user_type, is_identity])"""
    out = []
    for i, s in enumerate(specs, 1):
        name, base = s[0], s[1]
        out.append({"pos": i, "col": name, "base_type": base, "user_type": s[2] if len(s) > 2 else base,
                    "is_identity": s[3] if len(s) > 3 else 0})
    return out


T = {"sch": "dbo", "tbl": "t", "row_count": 10, "size_mb": 1.0, "ct_enabled": 1}


def test_ct_table_with_pk_recommends_ct():
    r = d.recommend(T, _cols(("id", "int", "int", 1), ("v", "nvarchar")), ["id"])
    assert (r["load_type"], r["cursor_column"], r["primary_key"]) == ("ct", None, "id")
    assert r["select_list"] == "id, v" and r["partition_candidate"] == "id" and r["identity_column"] == "id"


def test_no_ct_with_rowversion_recommends_cursor():
    r = d.recommend(dict(T, ct_enabled=0), _cols(("id", "bigint"), ("rv", "timestamp")), ["id"])
    assert (r["load_type"], r["cursor_column"]) == ("cursor", "rv") and "deletes not captured" in r["notes"]


def test_no_pk_or_no_rowversion_falls_back_to_full():
    assert d.recommend(T, _cols(("a", "int"), ("rv", "timestamp")), [])["load_type"] == "full"     # CT but no PK
    r = d.recommend(dict(T, ct_enabled=0), _cols(("id", "int"), ("upd", "datetime2")), ["id"])
    assert r["load_type"] == "full" and r["cursor_column"] is None                                 # never auto-picks updated_at


def test_unsupported_columns_are_excluded_and_reported():
    r = d.recommend(T, _cols(("id", "int"), ("geo", "varbinary", "geography"), ("x", "sql_variant"),
                             ("bad,name", "int"), ("v", "int")), ["id"])
    assert r["select_list"] == "id, v" and r["excluded_columns"] == "geo, x, bad,name"
    assert "excluded unsupported columns" in r["notes"]


def test_pk_on_unsupported_type_is_dropped():
    r = d.recommend(T, _cols(("h", "varbinary", "hierarchyid"), ("v", "int")), ["h"])
    assert r["primary_key"] is None and r["load_type"] == "full"


def test_partition_candidate_rules():
    assert d.recommend(T, _cols(("k", "nvarchar"), ("n", "int", "int", 1)), ["k"])["partition_candidate"] == "n"   # identity fallback
    assert d.recommend(T, _cols(("k", "nvarchar")), ["k"])["partition_candidate"] is None
    assert d.recommend(T, _cols(("d", "date"), ("k", "int")), ["d", "k"])["partition_candidate"] == "d"


def test_assemble_joins_queries_and_orders_composite_pk():
    tables = [{"oid": 2, "sch": "dbo", "tbl": "b", "row_count": 5, "size_mb": 2.5, "ct_enabled": 0},
              {"oid": 1, "sch": "dbo", "tbl": "A", "row_count": None, "size_mb": None, "ct_enabled": 1}]
    columns = [dict(c, oid=1) for c in _cols(("x", "int"), ("y", "int"))] + [dict(c, oid=2) for c in _cols(("z", "int"))]
    pks = [{"oid": 1, "ord": 2, "col": "y"}, {"oid": 1, "ord": 1, "col": "x"}]
    out = d.assemble(tables, columns, pks)
    assert [r["src_table"] for r in out] == ["A", "b"]
    assert out[0]["primary_key"] == "x, y" and out[0]["load_type"] == "ct" and out[0]["row_count"] == 0
    assert out[1]["primary_key"] is None and out[1]["load_type"] == "full" and out[1]["size_mb"] == 2.5


def test_catalog_queries_read_metadata_only():
    for sql in (d.TABLES_SQL, d.COLUMNS_SQL, d.PK_SQL):
        assert "is_ms_shipped = 0" in sql and "sys." in sql
    assert "sys.allocation_units" in d.TABLES_SQL and "sys.change_tracking_tables" in d.TABLES_SQL


def test_helpers():
    assert d.secret_key_for({"secret_key_template": "creds-{database}"}, "db1") == "creds-db1"
    assert d.secret_key_for({"secret_key": "k"}, "db1") == "k"
    assert d.parse_databases(" a, b\nc,,a ") == ["a", "b", "c"]


def test_other_sources_are_placeholders(monkeypatch):
    with pytest.raises(ValueError, match="not implemented yet for src_type 'oracle'"):
        d.discover_database({"src_type": "oracle"})


def test_discover_database_retries_connection_failures_only(monkeypatch):
    import ct_engine
    calls, sleeps = [], []

    class _R:
        def __init__(self, rows): self.rows = rows
        def collect(self): return [type("Row", (), {"asDict": lambda s, r=r: r})() for r in self.rows]

    def read(spark, cfg, sql, **kw):
        calls.append(sql)
        if len(calls) == 1:
            raise RuntimeError("[FAILED_JDBC.CONNECTION] Failed to connect to the database")
        if sql == d.TABLES_SQL:
            return _R([{"oid": 1, "sch": "dbo", "tbl": "t", "row_count": 1, "size_mb": 1, "ct_enabled": 1}])
        if sql == d.COLUMNS_SQL:
            return _R([{"oid": 1, "pos": 1, "col": "id", "base_type": "int", "user_type": "int", "is_identity": 0}])
        return _R([{"oid": 1, "ord": 1, "col": "id"}])
    monkeypatch.setattr(ct_engine, "_spark", lambda: None)
    monkeypatch.setattr(ct_engine, "_read", read)
    out = d.discover_database({"src_type": "sqlserver"}, sleep=sleeps.append)
    assert [r["load_type"] for r in out] == ["ct"] and sleeps == [d.CONNECT_RETRY_S] and len(calls) == 4

    def bad(spark, cfg, sql, **kw):
        raise RuntimeError("Login failed for user")
    monkeypatch.setattr(ct_engine, "_read", bad)
    with pytest.raises(RuntimeError, match="Login failed"):
        d.discover_database({"src_type": "sqlserver"}, sleep=sleeps.append)
    assert len(sleeps) == 1                                   # non-connection errors are not retried
