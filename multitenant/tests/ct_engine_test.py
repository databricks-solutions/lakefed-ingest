"""Unit tests for the pure query/type builders in ct_engine.py.

These cover the string- and type-building logic that assembles the CHANGETABLE read and the
idempotent PK MERGE — the part that must be exactly right — without a SQL Server or Spark
(ct_engine's pytds/pyspark/databricks imports are lazy, so the module imports bare). The
DB-touching functions (current_version, min_valid_version, read_and_merge_ct, seed,
ensure_sink_table) are exercised end-to-end in the Tier-3 validation, not here.
"""
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
    monkeypatch.delenv("MT_SQL_SECRET_KEY", raising=False)
    monkeypatch.setenv("MT_SQL_SECRET_SCOPE", "kv-scope")
    cfg = {"id": 1, "secret_key": "tenant-db-001", "src_connection": "legacy_conn"}
    assert ce.secret_ref(cfg) == ("kv-scope", "tenant-db-001")


def test_secret_ref_legacy_fallback_and_missing(monkeypatch):
    monkeypatch.delenv("MT_SQL_SECRET_KEY", raising=False)
    monkeypatch.delenv("MT_SQL_SECRET_SCOPE", raising=False)
    assert ce.secret_ref({"id": 1, "src_connection": "c1"}) == ("lakefed_ingest_mt", "c1_json")
    try:
        ce.secret_ref({"id": 2})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError when no secret key can be derived")


def test_creds_are_keyed_per_secret_not_per_server(monkeypatch):
    # Two DBs on the SAME server with different credentials must not share a cache entry.
    monkeypatch.delenv("MT_SQL_SECRET_KEY", raising=False)
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
    monkeypatch.setenv("MT_SQL_TRUST_SERVER_CERT", "false")
    assert ce.trust_server_certificate() is False
    monkeypatch.setenv("MT_SQL_TRUST_SERVER_CERT", "true")
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
    src = (Path(__file__).parent.parent / "src" / "multitenant" / "ct_engine.py").read_text()
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
        monkeypatch.delenv("MT_SQL_HOST", raising=False)
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
    monkeypatch.delenv("MT_SQL_HOST", raising=False)
    import pytest
    with pytest.raises(ValueError):
        ce._jdbc_config({"id": 1, "secret_key": "k"})


# ---- partitioned seed: failure leaves the target untouched; success swaps once -----------

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
    def __init__(self, spark, query):
        self.spark, self.query = spark, query
        self.write = _Writer(spark)

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
        self.statements, self.views, self.fail_where = [], {}, fail_where
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


def _seed(monkeypatch, spark):
    from concurrent.futures import ThreadPoolExecutor
    import parallel
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q: _DF(sp, q))
    cfg = {"id": 1, "src_schema": "dbo", "src_table": "big", "select_list": "id, v",
           "load_partitioned": True, "partition_col": "id", "partition_size_mb": 1}
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
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q: _EmptyDF(sp, q))
    spark = _FakeSpark()
    cfg = {"id": 1, "src_schema": "dbo", "src_table": "big", "select_list": "id, v",
           "load_partitioned": True, "partition_col": "id", "partition_size_mb": 1}
    ce.seed(spark, cfg, "c.s.big")
    overwrites = [s for s in spark.statements if s.startswith("INSERT WITH SCHEMA EVOLUTION OVERWRITE c.s.big ")]
    assert len(overwrites) == 1 and "__seed_staging" not in overwrites[0]
    assert not any("__seed_staging" in s for s in spark.statements)


def test_partitioned_seed_requires_partition_col(monkeypatch):
    import pytest
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q: _DF(sp, q))
    cfg = {"id": 1, "src_schema": "dbo", "src_table": "big", "select_list": "id, v",
           "load_partitioned": True, "partition_col": "", "partition_size_mb": 1}
    with pytest.raises(ValueError, match="partition_col"):
        ce.seed(_FakeSpark(), cfg, "c.s.big")


# ---- read_and_merge_ct: unique view per call, always dropped, rows from MERGE metrics ------

class _MergeSpark(_FakeSpark):
    def __init__(self, result=None, fail=False):
        super().__init__()
        self.result, self.fail = result, fail

    def sql(self, stmt):
        self.statements.append(stmt)
        if stmt.startswith("MERGE WITH SCHEMA EVOLUTION INTO"):
            if self.fail:
                raise RuntimeError("merge failed")
            return type("R", (), {"first": lambda s_: self.result})()
        return super().sql(stmt)


_MERGE_CFG = {"id": 1, "src_schema": "dbo", "src_table": "t", "select_list": "id, v",
              "primary_key": "id"}


def test_read_and_merge_ct_returns_affected_rows_and_drops_view(monkeypatch):
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q: _DF(sp, q))
    spark = _MergeSpark(result=_Row(num_affected_rows=7))
    assert ce.read_and_merge_ct(spark, _MERGE_CFG, "c.s.t", 5) == 7
    assert spark.views == {}                                   # view dropped
    merges = [s for s in spark.statements if s.startswith("MERGE WITH SCHEMA EVOLUTION INTO c.s.t")]
    assert len(merges) == 1 and "ct_staged_" in merges[0]


def test_read_and_merge_ct_unique_view_per_call(monkeypatch):
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q: _DF(sp, q))
    spark = _MergeSpark(result=_Row(num_affected_rows=0))
    ce.read_and_merge_ct(spark, _MERGE_CFG, "c.s.t", 5)
    ce.read_and_merge_ct(spark, _MERGE_CFG, "c.s.t", 5)
    views = [s.split(" USING ")[1].split()[0] for s in spark.statements if s.startswith("MERGE WITH SCHEMA EVOLUTION INTO")]
    assert len(set(views)) == 2


def test_read_and_merge_ct_drops_view_even_when_merge_fails(monkeypatch):
    import pytest
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q: _DF(sp, q))
    spark = _MergeSpark(fail=True)
    with pytest.raises(RuntimeError, match="merge failed"):
        ce.read_and_merge_ct(spark, _MERGE_CFG, "c.s.t", 5)
    assert spark.views == {}


def test_read_and_merge_ct_unknown_rows_without_metrics(monkeypatch):
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q: _DF(sp, q))
    assert ce.read_and_merge_ct(_MergeSpark(result=_Row()), _MERGE_CFG, "c.s.t", 5) is None


def test_partitioned_seed_guard_failure_leaves_target_untouched(monkeypatch):
    # A sweep that lost its collection lock must stop writing: the guard raising before
    # partitions / the swap fails the seed without touching the target.
    import pytest
    from concurrent.futures import ThreadPoolExecutor
    import parallel
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q: _DF(sp, q))
    cfg = {"id": 1, "src_schema": "dbo", "src_table": "big", "select_list": "id, v",
           "load_partitioned": True, "partition_col": "id", "partition_size_mb": 1}
    spark = _FakeSpark()

    def lost():
        raise RuntimeError("collection lock lost")

    with ThreadPoolExecutor(max_workers=4) as ex, pytest.raises(RuntimeError):
        ce.seed(spark, cfg, "c.s.big", slots=parallel.WorkSlots(4), executor=ex, guard=lost)
    assert not any(s.startswith("INSERT WITH SCHEMA EVOLUTION OVERWRITE c.s.big ") for s in spark.statements)



def test_ensure_sink_table_enables_type_widening_on_new_sinks(monkeypatch):
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q: _DF(sp, q))
    spark = _FakeSpark()
    spark.catalog.tableExists = lambda name: False                 # sink does not exist yet
    ce.ensure_sink_table(spark, {"src_schema": "dbo", "src_table": "t", "select_list": "id, v"}, "c.s.t")
    assert "SAVE c.s.t" in spark.statements
    assert any("ALTER TABLE c.s.t SET TBLPROPERTIES ('delta.enableTypeWidening' = 'true')" in x
               for x in spark.statements)


def test_ensure_sink_table_is_noop_when_sink_exists(monkeypatch):
    spark = _FakeSpark()
    spark.catalog.tableExists = lambda name: True
    ce.ensure_sink_table(spark, {"src_schema": "dbo", "src_table": "t", "select_list": "id"}, "c.s.t")
    assert spark.statements == []
