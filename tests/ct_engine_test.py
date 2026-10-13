"""Unit tests for the pure query/type builders in ct_engine.py.

These cover the string- and type-building logic that assembles the CHANGETABLE read and the
idempotent PK MERGE — the part that must be exactly right — without a SQL Server or Spark
(ct_engine's pytds/pyspark/databricks imports are lazy, so the module imports bare). The
DB-touching functions (current_version, min_valid_version, read_and_merge_ct, seed,
ensure_sink_table) are exercised end-to-end in the Tier-3 validation, not here.
"""
import pytest

import ct_engine as ce


def test_ct_projection_pk_from_ct_nonpk_from_base():
    out = ce.ct_projection("order_id, amount, status", "order_id")
    assert "ct.[order_id] AS [order_id]" in out      # PK from change table (delete-safe)
    assert "t.[amount] AS [amount]" in out            # non-PK from base table
    assert "t.[status] AS [status]" in out
    assert out.strip().endswith("ct.SYS_CHANGE_OPERATION AS op")


def test_ct_projection_composite_pk():
    out = ce.ct_projection("ord, line_no, qty", "ord, line_no")
    parts = [p.strip() for p in out.split(",")]   # exact-match to avoid 'ct.[ord]' ⊃ 't.[ord]'
    assert "ct.[ord] AS [ord]" in parts and "ct.[line_no] AS [line_no]" in parts
    assert "t.[qty] AS [qty]" in parts
    # PK never taken from the base table (would be t.[ord]/t.[line_no]):
    assert "t.[ord] AS [ord]" not in parts and "t.[line_no] AS [line_no]" not in parts


def test_ct_join_and_merge_on():
    assert ce.ct_join("ord, line_no") == "t.[ord] = ct.[ord] AND t.[line_no] = ct.[line_no]"
    assert ce.merge_on("order_id") == "tgt.`order_id` = s.`order_id`"


def test_ct_read_query_shape():
    cfg = {"src_schema": "dbo", "src_table": "orders",
           "select_list": "order_id, amount", "primary_key": "order_id"}
    q = ce.ct_read_query(cfg, 8)
    assert "CHANGETABLE(CHANGES [dbo].[orders], 8) ct" in q
    assert "LEFT JOIN [dbo].[orders] t ON t.[order_id] = ct.[order_id]" in q


def test_merge_set_excludes_pk():
    assert (ce.merge_set("order_id, amount, status", "order_id")
            == "`amount` = s.`amount`, `status` = s.`status`")      # unqualified targets (evolution)


def test_merge_set_all_pk_is_noop_safe():
    assert ce.merge_set("a, b", "a, b") == "`a` = s.`a`, `b` = s.`b`"


def test_merge_insert_cols_and_vals():
    assert ce.merge_insert_cols("order_id, amount") == "`order_id`, `amount`"
    assert ce.merge_insert_vals("order_id, amount") == "s.`order_id`, s.`amount`"


def test_split_handles_blanks_and_none():
    assert ce._split("a, b ,, c ") == ["a", "b", "c"]
    assert ce._split(None) == []


# ---- connection helpers (pure) -----------------------------------------------

def test_validate_akv_secret_name():
    assert ce.validate_akv_secret_name("tenant-db-001") == "tenant-db-001"
    for bad in ("tenant_db_001", "", "a" * 128, "has space", None):
        try:
            ce.validate_akv_secret_name(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_secret_ref_prefers_control_secret_key(monkeypatch):
    monkeypatch.delenv("LAKEFED_SQL_SECRET_KEY", raising=False)
    monkeypatch.setenv("LAKEFED_SQL_SECRET_SCOPE", "kv-scope")
    cfg = {"id": 1, "secret_key": "tenant-db-001", "src_connection": "legacy_conn"}
    assert ce.secret_ref(cfg) == ("kv-scope", "tenant-db-001")


def test_secret_ref_legacy_fallback_and_missing(monkeypatch):
    monkeypatch.delenv("LAKEFED_SQL_SECRET_KEY", raising=False)
    monkeypatch.delenv("LAKEFED_SQL_SECRET_SCOPE", raising=False)
    assert ce.secret_ref({"id": 1, "src_connection": "c1"}) == ("lakefed_ingest", "c1_json")
    try:
        ce.secret_ref({"id": 2})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError when no secret key can be derived")


def test_creds_are_keyed_per_secret_not_per_server(monkeypatch):
    # Two DBs on the SAME server with different credentials must not share a cache entry.
    monkeypatch.delenv("LAKEFED_SQL_SECRET_KEY", raising=False)
    a = {"id": 1, "src_host": "sql1", "secret_key": "db-a"}
    b = {"id": 2, "src_host": "sql1", "secret_key": "db-b"}
    assert ce.secret_ref(a) != ce.secret_ref(b)
    sa, ka = ce.secret_ref(a)
    sb, kb = ce.secret_ref(b)
    assert ce.config_cache_key("sql1", 1433, sa, ka) != ce.config_cache_key("sql1", 1433, sb, kb)


def test_jdbc_url_trust_flag():
    assert ce.jdbc_url("h", 1433, "db", True).endswith("trustServerCertificate=true")
    assert ce.jdbc_url("h", 1433, "db", False).endswith("trustServerCertificate=false")
    assert "databaseName=db;encrypt=true" in ce.jdbc_url("h", 1433, "db", True)


def test_trust_server_certificate_env(monkeypatch):
    monkeypatch.setenv("LAKEFED_SQL_TRUST_SERVER_CERT", "false")
    assert ce.trust_server_certificate() is False
    monkeypatch.setenv("LAKEFED_SQL_TRUST_SERVER_CERT", "true")
    assert ce.trust_server_certificate() is True


def test_merge_sql_uses_given_view_and_handles_deletes():
    cfg = {"select_list": "order_id, amount", "primary_key": "order_id"}
    q = ce.merge_sql(cfg, "c.s.orders", "ct_staged_abc")
    assert "USING ct_staged_abc AS s" in q
    assert "WHEN MATCHED AND s.op = 'D' THEN DELETE" in q
    assert "WHEN NOT MATCHED AND s.op <> 'D' THEN" in q
    assert q.startswith("MERGE WITH SCHEMA EVOLUTION INTO c.s.orders AS tgt")
    assert "ON tgt.`order_id` = s.`order_id`" in q                  # ON stays qualified
    assert "UPDATE SET `amount` = s.`amount`" in q                  # SET target unqualified
    assert "INSERT (`order_id`, `amount`) VALUES (s.`order_id`, s.`amount`)" in q
    assert "`op`" not in q                                          # CT op never lands in bronze


def test_engine_source_has_no_cache_or_persist():
    # Rule: read the source once, never .cache()/.persist() a source DataFrame.
    from pathlib import Path
    src = (Path(__file__).parent.parent / "src" / "lakefed_ingest" / "ct_engine.py").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith(("#", "\"", "``")))
    assert ".cache()" not in code.replace("``.cache()``", "")
    assert ".persist(" not in code


def test_is_partitioned_accepts_bool_and_string():
    assert ce._is_partitioned({"load_partitioned": True})
    assert ce._is_partitioned({"load_partitioned": "true"})
    assert not ce._is_partitioned({"load_partitioned": False})
    assert not ce._is_partitioned({"load_partitioned": None})
    assert not ce._is_partitioned({})


def test_staging_table_name():
    assert ce.staging_table_name("cat.sch.tbl") == "cat.sch.tbl__seed_staging"


def test_seed_has_no_partition_concurrency_param():
    import inspect
    params = inspect.signature(ce.seed).parameters
    assert "partition_concurrency" not in params
    assert "slots" in params and "executor" in params


def test_unit_without_slots_is_noop():
    with ce._unit(None):
        pass


def test_target_writes_are_data_only_insert_overwrite():
    # The seed + staging swap must replace DATA only (INSERT OVERWRITE), never re-create the target
    # (saveAsTable overwrite commits as CREATE OR REPLACE TABLE AS SELECT, which can reset liquid
    # clustering keys / properties / row filters that consolidated bronze relies on).
    import inspect, ct_engine
    src = inspect.getsource(ct_engine.seed) + inspect.getsource(ct_engine._partitioned_seed)
    assert "_insert_overwrite(" in src
    assert 'saveAsTable(sink_fqn)' not in src.replace('mode("ignore")', "")
    assert "INSERT OVERWRITE" in inspect.getsource(ct_engine._insert_overwrite)


# ---- explicit-column writes (columns map by NAME) ----------------------------------------

def test_insert_overwrite_sql_by_name_with_schema_evolution():
    sql = ce.insert_overwrite_sql("c.s.t", ["id", "name"], "v")
    assert sql == "INSERT WITH SCHEMA EVOLUTION OVERWRITE c.s.t BY NAME SELECT `id`, `name` FROM v"
    assert ce.insert_sql("INTO", "c.s.t_stage", ["id"], "v") == \
        "INSERT INTO c.s.t_stage BY NAME SELECT `id` FROM v"


def test_missing_sink_columns_is_case_insensitive():
    assert ce.missing_sink_columns("id, name", ["ID", "Name"]) == []
    assert ce.missing_sink_columns("id, city, updated_at", ["id"]) == ["city", "updated_at"]
    assert ce.missing_sink_columns("id", ["id", "extra"]) == []     # extra sink cols are fine


# ---- host/port resolution precedence (never send a tenant's creds to the wrong server) ----

def _resolve(monkeypatch, cfg, env_host=None):
    monkeypatch.setattr(ce, "_creds", lambda scope, key: ("u", "p", "secret-host"))
    monkeypatch.setattr(ce, "_uc_host", lambda name: ("uc-host", 1500))
    ce._config_cache.clear()
    if env_host is None:
        monkeypatch.delenv("LAKEFED_SQL_HOST", raising=False)
    return ce._jdbc_config(dict({"id": 1, "secret_key": "k"}, **cfg))


def test_jdbc_config_src_host_wins_and_port_defaults_1433(monkeypatch):
    conf = _resolve(monkeypatch, {"src_host": "h1", "src_connection": "legacy"})
    assert (conf["host"], conf["port"]) == ("h1", 1433)


def test_jdbc_config_legacy_uc_connection_when_no_src_host(monkeypatch):
    conf = _resolve(monkeypatch, {"src_connection": "legacy"})
    assert (conf["host"], conf["port"]) == ("uc-host", 1500)


def test_jdbc_config_secret_host_last_resort(monkeypatch):
    conf = _resolve(monkeypatch, {})
    assert conf["host"] == "secret-host"


def test_jdbc_config_no_host_raises(monkeypatch):
    monkeypatch.setattr(ce, "_creds", lambda scope, key: ("u", "p", None))
    ce._config_cache.clear()
    monkeypatch.delenv("LAKEFED_SQL_HOST", raising=False)
    import pytest
    with pytest.raises(ValueError):
        ce._jdbc_config({"id": 1, "secret_key": "k"})


# ---- partitioned seed: failure leaves the target untouched; success swaps once -----------

class _FakeLit:
    """Stands in for pyspark F.lit in ct_engine._tag."""
    def __init__(self, value): self.value, self.name = value, None
    def alias(self, name): self.name = name; return self
    def __repr__(self): return f"lit({self.value!r}) AS {self.name}"

class _Row(dict):
    def asDict(self):
        return dict(self)


class _Writer:
    def __init__(self, spark):
        self.spark = spark

    def mode(self, *_):
        return self

    def format(self, *_):
        return self

    def option(self, *_):
        return self

    def saveAsTable(self, name):
        self.spark.statements.append(f"SAVE {name}")


class _DF:
    columns = ["id", "v"]

    def __init__(self, spark, query):
        self.spark, self.query = spark, query
        self.write = _Writer(spark)

    def select(self, *cols):                       # ct_engine._tag: records the projection
        self.spark.tagged.append(cols)
        return _DF(self.spark, self.query)

    def first(self):
        if "table_size_mb" in self.query:
            return _Row(table_size_mb=10)
        if " AS lb" in self.query:
            return _Row(lb=1, ub=100)
        return None

    def createOrReplaceTempView(self, name):
        self.spark.views[name] = self.query


class _Catalog:
    def __init__(self, spark):
        self.spark = spark

    def dropTempView(self, name):
        self.spark.views.pop(name, None)


class _Table:
    columns = ["id", "v"]


class _FakeSpark:
    def __init__(self, fail_where=None):
        import threading
        self.statements, self.views, self.fail_where, self.tagged = [], {}, fail_where, []
        self.catalog, self._mu = _Catalog(self), threading.Lock()

    def table(self, name):
        return _Table()

    def sql(self, stmt):
        with self._mu:
            self.statements.append(stmt)
        if self.fail_where and stmt.startswith("INSERT INTO"):
            view = stmt.rsplit(" FROM ", 1)[1]
            if self.fail_where in self.views.get(view, ""):
                raise RuntimeError("partition read failed")
        return type("R", (), {"first": lambda self_: _Row(num_inserted_rows=1)})()


def _seed(monkeypatch, spark, **overrides):
    from concurrent.futures import ThreadPoolExecutor
    import parallel
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _DF(sp, q))
    monkeypatch.setattr(ce, "_lit", _FakeLit)
    cfg = {"id": 1, "src_schema": "dbo", "src_table": "big", "select_list": "id, v",
           "load_partitioned": True, "partition_col": "id", "partition_size_mb": 1, **overrides}
    with ThreadPoolExecutor(max_workers=4) as ex:
        return ce.seed(spark, cfg, "c.s.big", slots=parallel.WorkSlots(4), executor=ex)


def test_partitioned_seed_failure_leaves_target_untouched(monkeypatch):
    import pytest
    spark = _FakeSpark(fail_where="[id] >= 37 ")      # one middle partition fails (stride 9)
    with pytest.raises(RuntimeError, match="target untouched"):
        _seed(monkeypatch, spark)
    assert not any(s.startswith("INSERT WITH SCHEMA EVOLUTION OVERWRITE c.s.big ") for s in spark.statements)
    assert not any(s.startswith("DROP TABLE") for s in spark.statements)   # staging kept


def test_partitioned_seed_success_swaps_once_then_drops_staging(monkeypatch):
    spark = _FakeSpark()
    _seed(monkeypatch, spark)
    swaps = [s for s in spark.statements if s.startswith("INSERT WITH SCHEMA EVOLUTION OVERWRITE c.s.big ")]
    assert swaps == ["INSERT WITH SCHEMA EVOLUTION OVERWRITE c.s.big BY NAME SELECT `id`, `v` "
                     "FROM c.s.big__seed_staging"]
    assert spark.statements[-1] == "DROP TABLE IF EXISTS c.s.big__seed_staging"
    assert spark.statements.index(swaps[0]) < len(spark.statements) - 1


def test_partitioned_seed_empty_bounds_falls_back_to_single_read_seed(monkeypatch):
    # Empty table (or all-NULL partition column): no bounds -> plain one-read seed, no staging.
    class _EmptyDF(_DF):
        def first(self):
            return _Row(lb=None, ub=None) if " AS lb" in self.query else super().first()
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _EmptyDF(sp, q))
    spark = _FakeSpark()
    cfg = {"id": 1, "src_schema": "dbo", "src_table": "big", "select_list": "id, v",
           "load_partitioned": True, "partition_col": "id", "partition_size_mb": 1}
    ce.seed(spark, cfg, "c.s.big")
    overwrites = [s for s in spark.statements if s.startswith("INSERT WITH SCHEMA EVOLUTION OVERWRITE c.s.big ")]
    assert len(overwrites) == 1 and "__seed_staging" not in overwrites[0]
    assert not any("__seed_staging" in s for s in spark.statements)


def test_partitioned_seed_requires_partition_col(monkeypatch):
    import pytest
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _DF(sp, q))
    cfg = {"id": 1, "src_schema": "dbo", "src_table": "big", "select_list": "id, v",
           "load_partitioned": True, "partition_col": "", "partition_size_mb": 1}
    with pytest.raises(ValueError, match="partition_col"):
        ce.seed(_FakeSpark(), cfg, "c.s.big")


# ---- read_and_merge_ct: unique view per call, always dropped, rows from MERGE metrics ------

class _MergeSpark(_FakeSpark):
    def __init__(self, result=None, fail=False, conflicts=0):
        super().__init__()
        self.result, self.fail, self.conflicts = result, fail, conflicts

    def sql(self, stmt):
        self.statements.append(stmt)
        if stmt.startswith("MERGE WITH SCHEMA EVOLUTION INTO"):
            if self.conflicts:
                self.conflicts -= 1
                raise RuntimeError("[DELTA_CONCURRENT_APPEND] Files were added by a concurrent update")
            if self.fail:
                raise RuntimeError("merge failed")
            return type("R", (), {"first": lambda s_: self.result})()
        return super().sql(stmt)


_MERGE_CFG = {"id": 1, "src_schema": "dbo", "src_table": "t", "select_list": "id, v",
              "primary_key": "id"}


def test_read_and_merge_ct_returns_affected_rows_and_drops_view(monkeypatch):
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _DF(sp, q))
    spark = _MergeSpark(result=_Row(num_affected_rows=7))
    assert ce.read_and_merge_ct(spark, _MERGE_CFG, "c.s.t", 5) == 7
    assert spark.views == {}                                   # view dropped
    merges = [s for s in spark.statements if s.startswith("MERGE WITH SCHEMA EVOLUTION INTO c.s.t")]
    assert len(merges) == 1 and "ct_staged_" in merges[0]


def test_read_and_merge_ct_unique_view_per_call(monkeypatch):
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _DF(sp, q))
    spark = _MergeSpark(result=_Row(num_affected_rows=0))
    ce.read_and_merge_ct(spark, _MERGE_CFG, "c.s.t", 5)
    ce.read_and_merge_ct(spark, _MERGE_CFG, "c.s.t", 5)
    views = [s.split(" USING ")[1].split()[0] for s in spark.statements if s.startswith("MERGE WITH SCHEMA EVOLUTION INTO")]
    assert len(set(views)) == 2


def test_read_and_merge_ct_drops_view_even_when_merge_fails(monkeypatch):
    import pytest
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _DF(sp, q))
    spark = _MergeSpark(fail=True)
    with pytest.raises(RuntimeError, match="merge failed"):
        ce.read_and_merge_ct(spark, _MERGE_CFG, "c.s.t", 5)
    assert spark.views == {}


def test_read_and_merge_ct_unknown_rows_without_metrics(monkeypatch):
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _DF(sp, q))
    assert ce.read_and_merge_ct(_MergeSpark(result=_Row()), _MERGE_CFG, "c.s.t", 5) is None


def test_partitioned_seed_guard_failure_leaves_target_untouched(monkeypatch):
    # A sweep that lost its collection lock must stop writing: the guard raising before
    # partitions / the swap fails the seed without touching the target.
    import pytest
    from concurrent.futures import ThreadPoolExecutor
    import parallel
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _DF(sp, q))
    cfg = {"id": 1, "src_schema": "dbo", "src_table": "big", "select_list": "id, v",
           "load_partitioned": True, "partition_col": "id", "partition_size_mb": 1}
    spark = _FakeSpark()

    def lost():
        raise RuntimeError("collection lock lost")

    with ThreadPoolExecutor(max_workers=4) as ex, pytest.raises(RuntimeError):
        ce.seed(spark, cfg, "c.s.big", slots=parallel.WorkSlots(4), executor=ex, guard=lost)
    assert not any(s.startswith("INSERT WITH SCHEMA EVOLUTION OVERWRITE c.s.big ") for s in spark.statements)



def test_ensure_sink_table_enables_type_widening_on_new_sinks(monkeypatch):
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _DF(sp, q))
    spark = _FakeSpark()
    spark.catalog.tableExists = lambda name: False                 # sink does not exist yet
    ce.ensure_sink_table(spark, {"src_schema": "dbo", "src_table": "t", "select_list": "id, v"}, "c.s.t")
    creates = [x for x in spark.statements if x.startswith("CREATE TABLE IF NOT EXISTS c.s.t ")]
    assert len(creates) == 1 and "TBLPROPERTIES ('delta.enableTypeWidening' = 'true') AS SELECT * FROM sink_schema_" in creates[0]
    assert spark.views == {}                                       # the 0-row view is dropped


def test_ensure_sink_table_is_noop_when_sink_exists(monkeypatch):
    spark = _FakeSpark()
    spark.catalog.tableExists = lambda name: True
    ce.ensure_sink_table(spark, {"src_schema": "dbo", "src_table": "t", "select_list": "id"}, "c.s.t")
    assert spark.statements == []


# ---- skip-no-change: detection builders ---------------------------------------------------

def test_quoting_doubles_closing_bracket_and_quote():
    assert ce.quote_ident("a]b") == "[a]]b]"
    assert ce.quote_nstr("o'k") == "N'o''k'"


def test_detection_sql_one_branch_per_table_mv_guard_first():
    sql = ce.detection_sql([(1, "dbo", "a", 10), (2, "dbo", "b]x", 20)])
    assert sql.count(" UNION ALL ") == 1
    assert "CAST(1 AS BIGINT) AS control_id" in sql and "CAST(2 AS BIGINT) AS control_id" in sql
    assert "CHANGETABLE(CHANGES [dbo].[a], 10)" in sql and "[dbo].[b]]x]" in sql
    assert "OBJECT_ID(N'[dbo].[a]')" in sql
    # invalid checkpoint is decided from mv alone, before CHANGETABLE is consulted
    assert sql.index("x.mv IS NULL OR 10 < x.mv THEN NULL") < sql.index("CHANGETABLE(CHANGES [dbo].[a]")


def test_classify():
    assert ce.classify(10, None, None) == "reseed"      # CT disabled / unknown object
    assert ce.classify(5, 10, 1) == "reseed"            # retention lapsed
    assert ce.classify(10, 10, 1) == "changed"
    assert ce.classify(10, 3, 0) == "unchanged"


def test_all_current_and_chunked():
    assert ce.all_current([5, 7], 5) and not ce.all_current([5, 4], 5)
    assert ce.chunked(list(range(5)), 2) == [[0, 1], [2, 3], [4]]


def test_source_group_key_separates_databases_and_credentials():
    a = {"src_host": "H", "src_port": 1433, "src_database": "d1", "secret_key": "k1"}
    assert ce.source_group_key(a) == ce.source_group_key(dict(a, src_host="h"))
    assert ce.source_group_key(a) != ce.source_group_key(dict(a, src_database="d2"))
    assert ce.source_group_key(a) != ce.source_group_key(dict(a, secret_key="k2"))


def test_detect_changes_chunk_failure_falls_back_for_that_chunk_only(monkeypatch):
    class _Rows:
        def __init__(self, q): self.q = q
        def collect(self):
            if "[bad]" in self.q:
                raise RuntimeError("Change tracking is not enabled on table 'bad'")
            ids = [int(x.split(" AS BIGINT")[0]) for x in self.q.split("CAST(")[1:] if " AS BIGINT) AS control_id" in x]
            return [{"control_id": i, "mv": 1, "changed": 1 if i == 1 else 0} for i in ids]
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q: _Rows(q))
    items = [(1, "dbo", "a", 5), (2, "dbo", "b", 5), (3, "dbo", "bad", 5), (4, "dbo", "c", 5)]
    res, failed, queries = ce.detect_changes(None, {"src_database": "d"}, items, chunk_size=2)
    assert queries == 2
    assert res == {1: (1, 1), 2: (1, 0)}
    assert sorted(failed) == [3, 4]


# ---- review fixes: strict current_version; read-time retention guard ------------------------------

def test_current_version_none_when_ct_disabled(monkeypatch):
    class _Null:
        def first(self):
            return {"v": None}
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _Null())
    monkeypatch.setattr(ce, "_spark", lambda: None)
    assert ce.current_version({"src_database": "d"}) is None


def test_retention_guard_sql_raises_with_marker():
    sql = ce.ct_retention_guard_sql({"src_schema": "dbo", "src_table": "o'k]t"}, 42)
    assert sql.startswith("IF CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID(N'[dbo].[o''k]]t]'))")
    assert "IS NULL OR" in sql and "> 42" in sql
    assert "THROW 51000" in sql and ce.RETENTION_LAPSED_MARKER in sql


def test_is_retention_lapsed_error():
    assert ce.is_retention_lapsed_error(RuntimeError(f"SQLServerException: {ce.RETENTION_LAPSED_MARKER}: ..."))
    assert not ce.is_retention_lapsed_error(RuntimeError("connection reset"))


def test_ct_read_carries_the_retention_guard(monkeypatch):
    seen = {}

    def fake_read(sp, cfg, q, **kw):
        seen.update(kw)
        return _DF(sp, q)
    monkeypatch.setattr(ce, "_read", fake_read)
    spark = _MergeSpark(result=_Row(num_affected_rows=0))
    ce.read_and_merge_ct(spark, _MERGE_CFG, "c.s.t", 5)
    assert ce.RETENTION_LAPSED_MARKER in seen.get("session_init", "") and "> 5" in seen["session_init"]


# ---- source adapters: the JDBC read and the seed / full-load SQL follow control.src_type ----------

class _Reader:
    def __init__(self):
        self.opts = {}

    def format(self, f):
        self.opts["format"] = f
        return self

    def option(self, k, v):
        self.opts[k] = v
        return self

    def options(self, **kw):
        self.opts.update(kw)
        return self

    def load(self):
        return self.opts


def test_read_uses_the_rows_adapter_url_driver_and_options(monkeypatch):
    monkeypatch.setattr(ce, "_creds", lambda scope, key: ("u", "p", None))
    monkeypatch.setenv("LAKEFED_SQL_TRUST_SERVER_CERT", "false")
    ce._config_cache.clear()
    spark = type("S", (), {"read": property(lambda self: _Reader())})()
    cfg = {"id": 1, "secret_key": "k", "src_type": "oracle", "src_host": "ora1", "src_database": "SVC",
           "jdbc_options": '{"fetchsize": "50000"}'}
    got = ce._read(spark, cfg, "SELECT 1 FROM dual")
    assert got["url"] == "jdbc:oracle:thin:@//ora1:1521/SVC"          # adapter default port
    assert got["driver"] == "oracle.jdbc.OracleDriver" and got["fetchsize"] == "50000"
    assert "sessionInitStatement" not in got
    got = ce._read(spark, dict(cfg, src_type="sqlserver", jdbc_options=None, src_port=1500), "SELECT 1")
    assert got["url"].startswith("jdbc:sqlserver://ora1:1500;") and "fetchsize" not in got


def test_jdbc_config_port_defaults_per_adapter(monkeypatch):
    for src_type, port in (("postgresql", 5432), ("mysql", 3306), (None, 1433)):
        conf = _resolve(monkeypatch, {"src_host": "h", "src_type": src_type})
        assert conf["port"] == port


def test_seed_selects_in_the_sources_dialect(monkeypatch):
    queries = []
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: queries.append(q) or _DF(sp, q))
    cfg = {"id": 1, "src_type": "postgresql", "src_schema": "public", "src_table": "orders",
           "select_list": "id, v"}
    ce.seed(_FakeSpark(), cfg, "c.s.orders")
    assert queries == ['SELECT "id", "v" FROM "public"."orders"']


def test_partitioned_full_load_on_mysql_uses_mysql_sql(monkeypatch):
    queries = []
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: queries.append(q) or _DF(sp, q))
    cfg = {"id": 1, "src_type": "mysql", "load_type": "full", "src_schema": "app", "src_table": "big",
           "select_list": "id, v", "load_partitioned": True, "partition_col": "id", "partition_size_mb": 1}
    from concurrent.futures import ThreadPoolExecutor
    import parallel
    spark = _FakeSpark()
    with ThreadPoolExecutor(max_workers=4) as ex:
        ce.seed(spark, cfg, "c.s.big", slots=parallel.WorkSlots(4), executor=ex)
    assert "information_schema.tables" in queries[0]
    assert queries[1] == "SELECT MIN(`id`) AS lb, MAX(`id`) AS ub FROM `app`.`big`"
    parts = [q for q in queries[2:] if "1=0" not in q]
    assert len(parts) == 10 and all(q.startswith("SELECT `id`, `v` FROM `app`.`big` WHERE ") for q in parts)
    assert any("`id` >= " in q for q in parts)
    assert spark.statements[-1] == "DROP TABLE IF EXISTS c.s.big__seed_staging"   # staged swap


def test_source_group_key_uses_adapter_default_port():
    a = {"src_type": "postgresql", "src_host": "h", "src_database": "d", "secret_key": "k"}
    assert ce.source_group_key(a) == ("h", 5432, "d", "k")


def test_require_table_ct_refuses_tables_without_ct(monkeypatch):
    import pytest
    monkeypatch.setattr(ce, "min_valid_version", lambda cfg, slots=None: None)
    with pytest.raises(RuntimeError, match="not enabled on this source table"):
        ce.require_table_ct({"src_schema": "dbo", "src_table": "t"})
    monkeypatch.setattr(ce, "min_valid_version", lambda cfg, slots=None: 467)
    assert ce.require_table_ct({"src_schema": "dbo", "src_table": "t"}) == 467


# ---- per-table CT status (drives ct_disabled / retention_miss / ct_reenabled reseeds) --------------

class _Rows:
    def __init__(self, rows): self._rows = rows
    def collect(self): return self._rows
    def first(self): return self._rows[0] if self._rows else None


def _fake_read(monkeypatch, rows):
    seen = []
    monkeypatch.setattr(ce, "_spark", lambda: None)
    monkeypatch.setattr(ce, "_read", lambda spark, cfg, q, **kw: (seen.append(q), _Rows(rows))[1])
    return seen


def test_ct_table_status_lowercases_keys_and_maps_mv_bv(monkeypatch):
    seen = _fake_read(monkeypatch, [{"sch": "DBO", "tbl": "MixedCase", "mv": 5, "bv": 3},
                                    {"sch": "s", "tbl": "t", "mv": None, "bv": None}])
    got = ce.ct_table_status({"src_schema": "dbo", "src_table": "x"})
    assert got == {("dbo", "mixedcase"): (5, 3), ("s", "t"): (None, None)}
    assert seen == [ce.CT_TABLE_STATUS_SQL]


def test_single_table_ct_lookups_bracket_quote_object_id(monkeypatch):
    cfg = {"src_schema": "dbo", "src_table": "odd.name]x"}
    seen = _fake_read(monkeypatch, [{"v": 7, "bv": 4}])
    assert ce.min_valid_version(cfg) == 7 and ce.ct_begin_version(cfg) == 4
    for q in seen:
        assert "OBJECT_ID(N'[dbo].[odd.name]]x]')" in q


# ---- consolidated sinks ----------------------------------------------------------------------------

_CONS = {"id": 1, "src_schema": "dbo", "src_table": "t", "select_list": "id, v", "primary_key": "id",
         "client_id": "tenant-001"}


def test_client_id_validation():
    assert ce.client_id({"client_id": " tenant-001 "}) == "tenant-001"
    assert ce.client_id({"client_id": None}) is None and ce.client_id({"client_id": ""}) is None and ce.client_id({}) is None
    for bad in ("it's", "a b", "x" * 129, "a`b"):
        with pytest.raises(ValueError, match="invalid client_id"):
            ce.client_id({"client_id": bad})


def test_consolidated_columns_and_keys():
    assert ce.write_columns(_CONS) == ["id", "v", "__client_id__"]
    assert ce.merge_keys(_CONS) == ["__client_id__", "id"]
    ded = dict(_CONS, client_id=None)
    assert ce.write_columns(ded) == ["id", "v"] and ce.merge_keys(ded) == ["id"]


def test_consolidated_ct_merge_matches_on_client_and_pk():
    sql = ce.merge_sql(_CONS, "c.s.t", "v")
    assert "ON tgt.`__client_id__` = s.`__client_id__` AND tgt.`id` = s.`id` " in sql
    assert "UPDATE SET `v` = s.`v` " in sql                         # never rewrites the keys
    assert "INSERT (`id`, `v`, `__client_id__`) VALUES (s.`id`, s.`v`, s.`__client_id__`)" in sql
    assert "WHEN MATCHED AND s.op = 'D' THEN DELETE" in sql           # scoped by the ON keys


def test_consolidated_overwrite_replaces_only_this_client():
    sql = ce.insert_overwrite_sql("c.s.t", ce.write_columns(_CONS), "src", _CONS)
    assert sql == ("INSERT WITH SCHEMA EVOLUTION INTO c.s.t BY NAME REPLACE WHERE `__client_id__` = 'tenant-001' "
                   "SELECT `id`, `v`, `__client_id__` FROM src")
    assert ce.insert_overwrite_sql("c.s.t", ["id"], "src", dict(_CONS, client_id=None)).startswith(
        "INSERT WITH SCHEMA EVOLUTION OVERWRITE c.s.t BY NAME")


def test_staging_table_is_per_client():
    a = ce.staging_table_name("c.s.t", _CONS)
    b = ce.staging_table_name("c.s.t", dict(_CONS, client_id="tenant_001"))   # same slug, other client
    assert a.startswith("c.s.t__seed_staging__tenant_001_") and a != b
    assert ce.staging_table_name("c.s.t", dict(_CONS, client_id=None)) == "c.s.t__seed_staging"


def test_sink_mode_mismatch_is_refused():
    ce.check_sink_mode(_CONS, "c.s.t", ["id", "v", "__client_id__"])
    ce.check_sink_mode(dict(_CONS, client_id=None), "c.s.t", ["id", "v"])
    with pytest.raises(ValueError, match="has a '__client_id__' column but this control row is not consolidated"):
        ce.check_sink_mode(dict(_CONS, client_id=None), "c.s.t", ["id", "__CLIENT_ID__"])
    with pytest.raises(ValueError, match="has no '__client_id__' column but this control row is consolidated"):
        ce.check_sink_mode(_CONS, "c.s.t", ["id", "v"])


# ---- write-conflict retry ---------------------------------------------------------------------------

class ConcurrentAppendException(Exception):
    pass


def test_write_retry_backs_off_on_conflicts_then_succeeds():
    calls, sleeps = [], []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise ConcurrentAppendException("Files were added to the table by a concurrent update")
        return "done"
    assert ce.with_write_retry(fn, attempts=5, sleep=sleeps.append, rand=lambda: 1.0) == "done"
    assert len(calls) == 3 and sleeps == [ce.WRITE_RETRY_BASE_S, ce.WRITE_RETRY_BASE_S * 2]


def test_write_retry_gives_up_and_ignores_other_errors():
    sleeps = []
    with pytest.raises(RuntimeError, match="DELTA_CONCURRENT_APPEND"):
        ce.with_write_retry(lambda: (_ for _ in ()).throw(RuntimeError("[DELTA_CONCURRENT_APPEND] x")),
                            attempts=3, sleep=sleeps.append, rand=lambda: 0.5)
    assert len(sleeps) == 2
    calls = []
    with pytest.raises(ValueError):
        ce.with_write_retry(lambda: calls.append(1) or (_ for _ in ()).throw(ValueError("bad column")),
                            attempts=3, sleep=sleeps.append)
    assert calls == [1]


def test_backoff_is_capped_full_jitter():
    assert ce.backoff_delay(0, base=1, cap=60, rand=lambda: 1.0) == 1
    assert ce.backoff_delay(3, base=1, cap=60, rand=lambda: 1.0) == 8
    assert ce.backoff_delay(10, base=1, cap=60, rand=lambda: 1.0) == 60
    assert ce.backoff_delay(10, base=1, cap=60, rand=lambda: 0.0) == 0


def test_sink_created_in_one_statement_clustered_by_client():
    assert ce.create_sink_sql("c.s.t", "v", _CONS) == (
        "CREATE TABLE IF NOT EXISTS c.s.t CLUSTER BY (`__client_id__`) "
        "TBLPROPERTIES ('delta.enableTypeWidening' = 'true') AS SELECT * FROM v")
    assert ce.create_sink_sql("c.s.t", "v", dict(_CONS, client_id=None)) == (
        "CREATE TABLE IF NOT EXISTS c.s.t TBLPROPERTIES ('delta.enableTypeWidening' = 'true') AS SELECT * FROM v")


def test_consolidated_merge_has_literal_client_predicate():
    sql = ce.merge_sql(_CONS, "c.s.t", "v")
    assert ("ON tgt.`__client_id__` = s.`__client_id__` AND tgt.`id` = s.`id` AND tgt.`__client_id__` = 'tenant-001' "
            in sql)
    assert "AND tgt.`__client_id__` = '" not in ce.merge_sql(dict(_CONS, client_id=None), "c.s.t", "v")


def test_tag_prepends_client_id_literal_first(monkeypatch):
    monkeypatch.setattr(ce, "_lit", _FakeLit)
    spark = _FakeSpark()
    df = _DF(spark, "q")
    assert ce._tag(df, dict(_CONS, client_id=None)) is df and spark.tagged == []
    ce._tag(df, _CONS)
    (cols,) = spark.tagged
    assert isinstance(cols[0], _FakeLit) and cols[0].value == "tenant-001" and cols[0].name == "__client_id__"
    assert list(cols[1:]) == ["id", "v"]                  # original columns follow, in order


def test_dedicated_sink_may_have_a_native_client_id_column():
    ce.check_sink_mode(dict(_CONS, client_id=None, select_list="id, client_id, v"), "c.s.t", ["id", "client_id", "v"])


def test_ensure_sink_table_refuses_mode_mismatch(monkeypatch):
    spark = _FakeSpark()
    spark.catalog.tableExists = lambda name: True
    monkeypatch.setattr(_Table, "columns", ["id", "v", "__client_id__"])
    with pytest.raises(ValueError, match="has a '__client_id__' column"):
        ce.ensure_sink_table(spark, dict(_CONS, client_id=None), "c.s.t")
    assert not any(x.startswith("CREATE") for x in spark.statements)


def test_ensure_sink_table_consolidated_creates_clustered_in_one_statement(monkeypatch):
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _DF(sp, q))
    monkeypatch.setattr(ce, "_lit", _FakeLit)
    monkeypatch.setattr(_Table, "columns", ["__client_id__", "id", "v"])
    spark = _FakeSpark()
    spark.catalog.tableExists = lambda name: False
    ce.ensure_sink_table(spark, _CONS, "c.s.t")
    creates = [x for x in spark.statements if x.startswith("CREATE TABLE")]
    assert len(creates) == 1 and "CLUSTER BY (`__client_id__`)" in creates[0] and len(spark.tagged) == 1


def test_ensure_sink_table_skips_create_when_another_client_created_it(monkeypatch):
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _DF(sp, q))
    monkeypatch.setattr(_Table, "columns", ["__client_id__", "id", "v"])
    spark = _FakeSpark()
    answers = iter([False, True])                         # outer check: missing; inner re-check: exists
    spark.catalog.tableExists = lambda name: next(answers)
    ce.ensure_sink_table(spark, _CONS, "c.s.t")
    assert not any(x.startswith("CREATE") for x in spark.statements)


def test_consolidated_seed_replaces_only_this_client(monkeypatch):
    spark = _FakeSpark()
    _seed(monkeypatch, spark, load_partitioned=False, client_id="tenant-001")
    writes = [x for x in spark.statements if x.startswith("INSERT")]
    assert len(writes) == 1 and writes[0].startswith(
        "INSERT WITH SCHEMA EVOLUTION INTO c.s.big BY NAME REPLACE WHERE `__client_id__` = 'tenant-001' "
        "SELECT `id`, `v`, `__client_id__` FROM seed_full_")
    assert len(spark.tagged) == 1                         # the source read was tagged


def test_consolidated_partitioned_seed_uses_per_client_staging(monkeypatch):
    spark = _FakeSpark()
    _seed(monkeypatch, spark, client_id="tenant-001")
    staging = ce.staging_table_name("c.s.big", {"client_id": "tenant-001"})
    assert f"SAVE {staging}" in spark.statements
    parts = [x for x in spark.statements if x.startswith(f"INSERT INTO {staging} ")]
    assert parts and all("`__client_id__`" in x for x in parts)
    swaps = [x for x in spark.statements if "c.s.big BY NAME REPLACE WHERE" in x]
    assert swaps == [f"INSERT WITH SCHEMA EVOLUTION INTO c.s.big BY NAME REPLACE WHERE `__client_id__` = 'tenant-001' "
                     f"SELECT `id`, `v`, `__client_id__` FROM {staging}"]
    assert not any(x.startswith("INSERT WITH SCHEMA EVOLUTION OVERWRITE c.s.big") for x in spark.statements)
    assert f"DROP TABLE IF EXISTS {staging}" in spark.statements
    assert len(spark.tagged) == 1 + len(parts)            # staging schema + every partition read


def test_read_and_merge_ct_retries_whole_unit_on_conflict(monkeypatch):
    reads = []
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: reads.append(q) or _DF(sp, q))
    monkeypatch.setattr(ce, "WRITE_RETRY_BASE_S", 0.0)
    spark = _MergeSpark(result=_Row(num_affected_rows=3), conflicts=1)
    assert ce.read_and_merge_ct(spark, _MERGE_CFG, "c.s.t", 5) == 3
    merges = [x for x in spark.statements if x.startswith("MERGE")]
    views = {x.split(" USING ")[1].split()[0] for x in merges}
    assert len(reads) == 2 and len(merges) == 2 and len(views) == 2 and spark.views == {}


def test_existing_sinks_query_and_fallback():
    sql = ce.existing_sinks_sql("Main", ["Bronze", "it's"])
    assert sql.startswith("SELECT lower(table_schema) AS s, lower(table_name) AS t FROM `Main`.information_schema.tables")
    assert "IN ('bronze', 'it''s')" in sql

    class _Spark:
        def __init__(self, fail): self.fail = fail
        def sql(self, q):
            if self.fail:
                raise RuntimeError("PERMISSION_DENIED")
            return type("R", (), {"collect": lambda s: [{"s": "bronze", "t": "orders"}]})()
    cfgs = [{"sink_catalog": "Main", "sink_schema": "Bronze", "sink_table": "Orders"}]
    assert ce.existing_sinks(_Spark(False), cfgs) == {"main.bronze.orders"}
    assert ce.existing_sinks(_Spark(True), cfgs) is None
    assert ce.sink_fqn(cfgs[0]) == "main.bronze.orders"
