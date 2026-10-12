"""Cursor (high-watermark) strategy, SQL Server: position encoding, typed SQL, increment decision."""
import pytest

import cursor_engine as cur

CFG = {"src_schema": "dbo", "src_table": "orders", "cursor_column": "updated_at",
       "select_list": "id, amount, updated_at", "primary_key": "id"}


def test_position_round_trip_and_garbage():
    text = cur.encode_position("updated_at", "datetime2", "2026-01-01T10:00:00.1234567")
    assert cur.decode_position(text) == {"col": "updated_at", "type": "datetime2",
                                         "value": "2026-01-01T10:00:00.1234567"}
    assert cur.decode_position(cur.encode_position("id", "int", None))["value"] is None
    for bad in (None, "", "287493", "not json", '{"col": "x"}', "[1]"):
        assert cur.decode_position(bad) is None


def test_type_lookup_sql_quotes_names():
    sql = cur.column_type_sql(dict(CFG, src_table="odd]name", cursor_column="it's"))
    assert "OBJECT_ID(N'[dbo].[odd]]name]')" in sql and "c.name = N'it''s'" in sql


@pytest.mark.parametrize("typ,expr", [
    ("bigint", "CONVERT(VARCHAR(64), MAX([updated_at]))"),
    ("datetime2", "CONVERT(VARCHAR(64), MAX([updated_at]), 126)"),
])
def test_high_watermark_sql(typ, expr):
    assert cur.high_watermark_sql(CFG, typ) == f"SELECT {expr} AS hi FROM [dbo].[orders]"


@pytest.mark.parametrize("typ", ["timestamp", "rowversion"])
def test_rowversion_high_watermark_capped_below_min_active_rowversion(typ):
    # Never past the oldest open transaction: rows it commits later must not fall below the watermark.
    mx, mar = "CAST(MAX([updated_at]) AS BIGINT)", "CAST(MIN_ACTIVE_ROWVERSION() AS BIGINT)"
    assert cur.high_watermark_sql(CFG, typ) == (
        f"SELECT CONVERT(VARCHAR(20), CASE WHEN {mx} < {mar} THEN {mx} ELSE {mar} - 1 END) AS hi "
        "FROM [dbo].[orders]")


@pytest.mark.parametrize("typ", ["float", "real", "nvarchar", "uniqueidentifier", "bit"])
def test_unsupported_types_rejected(typ):
    with pytest.raises(ValueError, match="not supported for load_type 'cursor'"):
        cur.high_watermark_sql(CFG, typ)


def test_typed_literals_are_validated_and_sargable():
    assert cur.literal("42", "int") == "42"
    assert cur.literal("12.50", "decimal") == "12.50"
    assert cur.literal("2026-01-01T10:00:00.123", "datetime") == "CONVERT(datetime, N'2026-01-01T10:00:00.123', 126)"
    assert cur.literal("2026-01-01T10:00:00.1234567+05:30", "datetimeoffset").startswith("CONVERT(datetimeoffset, N'")
    assert cur.literal("2026-01-01", "date") == "CONVERT(date, N'2026-01-01', 126)"
    assert cur.literal("2001", "timestamp") == "CAST(CAST(2001 AS BIGINT) AS BINARY(8))"
    for value, typ in (("1; DROP TABLE x", "int"), ("2026-01-01' OR 1=1 --", "datetime2"), ("0x07D1", "timestamp")):
        with pytest.raises(ValueError, match="invalid"):
            cur.literal(value, typ)


def test_read_where_bounds():
    assert cur.read_where(CFG, "int", "10", "20") == "[updated_at] > 10 AND [updated_at] <= 20"
    assert cur.read_where(CFG, "int", None, "20") == "[updated_at] <= 20"   # empty at seed


def test_upsert_merge_has_no_delete_branch():
    sql = cur.upsert_merge_sql(CFG, "c.s.orders", "v")
    assert sql.startswith("MERGE WITH SCHEMA EVOLUTION INTO c.s.orders AS tgt USING v AS s ON tgt.`id` = s.`id`")
    assert "DELETE" not in sql and "s.op" not in sql
    assert "UPDATE SET `amount` = s.`amount`, `updated_at` = s.`updated_at`" in sql
    assert "INSERT (`id`, `amount`, `updated_at`) VALUES (s.`id`, s.`amount`, s.`updated_at`)" in sql


def test_plan_increment():
    pos = {"col": "c", "type": "int", "value": "20"}
    assert cur.plan_increment(pos, "20") == ("unchanged", "20")
    assert cur.plan_increment(pos, None) == ("unchanged", "20")          # source emptied: keep position
    assert cur.plan_increment(pos, "25") == ("read", "25")
    assert cur.plan_increment(dict(pos, value=None), "5") == ("read", "5")


class _Rows:
    def __init__(self, rows): self._rows = rows
    def first(self): return self._rows[0] if self._rows else None


def test_column_type_lookup_errors_clearly(monkeypatch):
    import ct_engine
    monkeypatch.setattr(ct_engine, "_spark", lambda: None)
    monkeypatch.setattr(ct_engine, "_read", lambda sp, cfg, q, **kw: _Rows([]))
    with pytest.raises(ValueError, match="cursor_column 'updated_at' not found on dbo.orders"):
        cur.column_type(CFG)
    monkeypatch.setattr(ct_engine, "_read", lambda sp, cfg, q, **kw: _Rows([{"typ": "float"}]))
    with pytest.raises(ValueError, match="not supported"):
        cur.column_type(CFG)
    monkeypatch.setattr(ct_engine, "_read", lambda sp, cfg, q, **kw: _Rows([{"typ": "DateTime2"}]))
    assert cur.column_type(CFG) == "datetime2"


# ---- read_and_merge: one read feeding one MERGE, per-call temp view, always dropped -----------------

class _Result:
    def __init__(self, d): self._d = d
    def asDict(self): return dict(self._d)
    def __getitem__(self, k): return self._d[k]


class _Spark:
    def __init__(self, result=None, fail=False):
        self.result, self.fail, self.views, self.statements = result, fail, {}, []
        spark = self

        class _Catalog:
            def dropTempView(self, name): spark.views.pop(name, None)
        self.catalog = _Catalog()

    def sql(self, stmt):
        self.statements.append(stmt)
        if self.fail:
            raise RuntimeError("merge failed")
        return type("R", (), {"first": lambda s_: self.result})()


class _DF:
    def __init__(self, spark, query): self.spark, self.query = spark, query
    def createOrReplaceTempView(self, name): self.spark.views[name] = self.query


def _merge(monkeypatch, spark, lo="10", hi="20"):
    import ct_engine
    queries = []
    monkeypatch.setattr(ct_engine, "_read", lambda sp, cfg, q, **kw: (queries.append(q), _DF(sp, q))[1])
    return cur.read_and_merge(spark, CFG, "c.s.t", "int", lo, hi), queries


def test_read_and_merge_reads_window_once_and_drops_view(monkeypatch):
    spark = _Spark(result=_Result({"num_affected_rows": 7}))
    n, queries = _merge(monkeypatch, spark)
    assert n == 7 and spark.views == {}
    assert queries == ["SELECT [id], [amount], [updated_at] FROM [dbo].[orders] "
                       "WHERE [updated_at] > 10 AND [updated_at] <= 20"]
    assert len(spark.statements) == 1 and "USING cursor_staged_" in spark.statements[0]


def test_read_and_merge_views_are_unique_per_call(monkeypatch):
    spark = _Spark(result=_Result({"num_affected_rows": 0}))
    _merge(monkeypatch, spark); _merge(monkeypatch, spark)
    views = [st.split(" USING ")[1].split(" ")[0] for st in spark.statements]
    assert len(set(views)) == 2


def test_read_and_merge_failure_propagates_and_drops_view(monkeypatch):
    spark = _Spark(fail=True)
    with pytest.raises(RuntimeError, match="merge failed"):
        _merge(monkeypatch, spark)
    assert spark.views == {}


def test_read_and_merge_without_metrics_returns_none(monkeypatch):
    assert _merge(monkeypatch, _Spark(result=_Result({})))[0] is None
    assert _merge(monkeypatch, _Spark(result=None))[0] is None


def test_consolidated_cursor_merge_matches_on_client_and_pk():
    sql = cur.upsert_merge_sql(dict(CFG, client_id="t1"), "c.s.orders", "v")
    assert "ON tgt.`client_id` = s.`client_id` AND tgt.`id` = s.`id` " in sql
    assert "INSERT (`id`, `amount`, `updated_at`, `client_id`)" in sql
