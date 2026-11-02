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


def test_ct_projection_matches_keys_case_insensitively():
    # SQL Server metadata often disagrees on case (OrderLineID in the PK, orderLineID in the
    # column list); projecting both would give Spark two columns with the same name.
    parts = [p.strip() for p in ce.ct_projection("orderLineID, note", "OrderLineID").split(",")]
    assert parts == ["ct.[OrderLineID] AS [OrderLineID]", "t.[note] AS [note]",
                     "ct.SYS_CHANGE_OPERATION AS op"]


def test_columns_that_differ_only_by_case_are_refused_not_dropped():
    # On a case-sensitive collation, id and ID can be two columns. Matching them to the key ignoring
    # case would silently drop one; Delta can't hold both either, so the row fails with a clear error.
    for build in (ce.ct_projection, ce.merge_set):
        with pytest.raises(ValueError, match="differ only by case"):
            build("id, ID, qty", "id")
        with pytest.raises(ValueError, match="differ only by case"):
            build("Qty, qty", "id")


def test_merge_set_matches_keys_case_insensitively():
    assert ce.merge_set("orderLineID, note", "OrderLineID") == "`note` = s.`note`"


def test_sink_fqn_quotes_each_part():
    cfg = {"sink_catalog": "01-sales-dev", "sink_schema": "team-a-bronze",
           "sink_table": "we`ird"}
    assert ce.sink_fqn(cfg) == "`01-sales-dev`.`team-a-bronze`.`we``ird`"


def test_sink_fqn_keeps_names_that_are_already_quoted():
    # Earlier versions didn't quote, so control rows may hold quoted names already.
    cfg = {"sink_catalog": "`01-sales-dev`", "sink_schema": "bronze", "sink_table": "`we``ird`"}
    assert ce.sink_fqn(cfg) == "`01-sales-dev`.`bronze`.`we``ird`"
    assert ce.quote_delta_ident("`a`b`") == "```a``b```"   # not a quoted identifier: quote it
    assert ce.quote_delta_ident("`") == "````"


def test_delta_column_quotes_every_name_and_doubles_embedded_backticks():
    assert ce.delta_column("Order-Lines") == "`Order-Lines`"
    assert ce.delta_column("Sales (2024)") == "`Sales (2024)`"
    assert ce.delta_column("a--b") == "`a--b`"
    assert ce.delta_column("a`b") == "`a``b`"
    assert ce.delta_column("a b") == "`a b`"
    assert ce.delta_column("`a`") == "```a```"   # a column never passes through as already quoted


def test_merge_and_insert_columns_quote_names_with_dashes_spaces_and_backticks():
    assert ce.merge_on("id, a`b") == "tgt.`id` = s.`id` AND tgt.`a``b` = s.`a``b`"
    assert ce.merge_set("id, Order-Lines, Sales (2024)", "id") == (
        "`Order-Lines` = s.`Order-Lines`, `Sales (2024)` = s.`Sales (2024)`")
    assert ce.merge_insert_cols("a--b, a`b") == "`a--b`, `a``b`"
    assert ce.merge_insert_vals("a--b, a`b") == "s.`a--b`, s.`a``b`"


def test_insert_sql_quotes_column_names_for_every_insert_mode():
    cols = ["Order-Lines", "Sales (2024)", "a`b"]
    assert ce.insert_sql("INTO", "`c`.`s`.`t`", cols, "v") == (
        "INSERT INTO `c`.`s`.`t` BY NAME SELECT `Order-Lines`, `Sales (2024)`, `a``b` FROM v")
    assert ce.insert_sql("OVERWRITE", "`c`.`s`.`t`", cols, "v", evolve=True) == (
        "INSERT WITH SCHEMA EVOLUTION OVERWRITE `c`.`s`.`t` BY NAME "
        "SELECT `Order-Lines`, `Sales (2024)`, `a``b` FROM v")


def test_staging_table_name_stays_valid_for_quoted_sinks():
    assert ce.staging_table_name("`c-1`.`s`.`t`") == "`c-1`.`s`.`t__seed_staging`"
    assert ce.staging_table_name("`c-1`.`s`.`t`", _CONS).startswith("`c-1`.`s`.`t__seed_staging__tenant_001_")
    assert ce.staging_table_name("`c-1`.`s`.`t`", _CONS).endswith("`")


def test_ct_join_and_merge_on():
    assert ce.ct_join("ord, line_no") == "t.[ord] = ct.[ord] AND t.[line_no] = ct.[line_no]"
    assert ce.merge_on("order_id") == "tgt.`order_id` = s.`order_id`"


def test_ct_read_query_shape():
    cfg = {"src_schema": "dbo", "src_table": "orders",
           "select_list": "order_id, amount", "primary_key": "order_id"}
    q = ce.ct_read_query(cfg, 8)
    assert "CHANGETABLE(CHANGES [dbo].[orders], 8) ct" in q
    assert "LEFT JOIN [dbo].[orders] t ON t.[order_id] = ct.[order_id]" in q


def test_ct_read_query_is_unbounded_unless_given_a_version():
    cfg = {"src_schema": "dbo", "src_table": "orders",
           "select_list": "order_id, amount", "primary_key": "order_id"}
    assert "SYS_CHANGE_VERSION <=" not in ce.ct_read_query(cfg, 8)
    assert ce.ct_read_query(cfg, 8, 12).endswith(
        "LEFT JOIN [dbo].[orders] t ON t.[order_id] = ct.[order_id] "
        "WHERE (ct.SYS_CHANGE_OPERATION = 'D' OR t.[order_id] IS NOT NULL) AND ct.SYS_CHANGE_VERSION <= 12")


def test_ct_read_query_skips_changes_whose_base_row_is_gone():
    # An update read from the change table whose base row was deleted before the join read it would
    # otherwise land with NULL columns; its delete has a later version and comes next cycle.
    cfg = {"src_schema": "dbo", "src_table": "lines",
           "select_list": "ord, line_no, qty", "primary_key": "ord, line_no"}
    assert ce.ct_read_query(cfg, 8).endswith(
        "WHERE (ct.SYS_CHANGE_OPERATION = 'D' OR t.[ord] IS NOT NULL)")


def test_change_read_session_init_adds_snapshot_isolation_when_enabled(monkeypatch):
    cfg = {"src_schema": "dbo", "src_table": "t"}
    monkeypatch.delenv("LAKEFED_SQL_SNAPSHOT_ISOLATION", raising=False)
    assert ce.ct_read_session_init(cfg, 5) == ce.ct_retention_guard_sql(cfg, 5)
    monkeypatch.setenv("LAKEFED_SQL_SNAPSHOT_ISOLATION", "true")
    assert ce.ct_read_session_init(cfg, 5) == (
        "SET TRANSACTION ISOLATION LEVEL SNAPSHOT; " + ce.ct_retention_guard_sql(cfg, 5))


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
    src = (inspect.getsource(ct_engine.seed) + inspect.getsource(ct_engine._partitioned_seed)
           + inspect.getsource(ct_engine._write_snapshot))
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
    """Stands in for pyspark F.lit (and the cast) in ct_engine._lit."""
    def __init__(self, value, dtype=None): self.value, self.dtype, self.name = value, dtype, None
    def alias(self, name): self.name = name; return self
    def __repr__(self): return f"lit({self.value!r}) AS {self.name}"

class _Col:
    """Stands in for a pyspark Column taken from a DataFrame (``df[name]``)."""
    def __init__(self, name): self.name = name
    def __repr__(self): return f"col({self.name})"

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

    def __getitem__(self, name):                   # df[name]: a column object, as PySpark's DataFrame
        return _Col(name)

    def unionByName(self, other, allowMissingColumns=False):   # ct_engine.union_by_name
        assert allowMissingColumns
        return _DF(self.spark, f"{self.query} UNION {other.query}")

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
    with pytest.raises(RuntimeError, match="target untouched") as raised:
        _seed(monkeypatch, spark)
    assert raised.value.__cause__ is not None     # the partition's own error, for table_retry.classify
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


def test_a_missing_target_schema_is_made_once_per_run_before_its_first_table(monkeypatch):
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _DF(sp, q))
    ce.forget_sinks()
    spark = _FakeSpark()
    spark.catalog.tableExists = lambda name: False
    cfg = {"src_schema": "dbo", "src_table": "t", "select_list": "id, v", "sink_catalog": "main",
           "sink_schema": "lakefed_100k", "sink_table": "t0001"}
    ce.ensure_sink_table(spark, cfg, ce.sink_fqn(cfg))
    ce.ensure_sink_table(spark, dict(cfg, sink_table="t0002"), ce.sink_fqn(dict(cfg, sink_table="t0002")))
    schemas = [x for x in spark.statements if x.startswith("CREATE SCHEMA")]
    assert schemas == ["CREATE SCHEMA IF NOT EXISTS `main`.`lakefed_100k` COMMENT 'Target of lakefed ingestion jobs'"]
    assert spark.statements.index(schemas[0]) < next(i for i, x in enumerate(spark.statements) if x.startswith("CREATE TABLE"))
    ce.forget_sinks()                                              # a new run asks again
    ce.ensure_sink_table(spark, dict(cfg, sink_table="t0003"), ce.sink_fqn(dict(cfg, sink_table="t0003")))
    assert len([x for x in spark.statements if x.startswith("CREATE SCHEMA")]) == 2


def test_a_target_schema_the_run_cant_make_names_the_grant_it_needs(monkeypatch):
    ce.forget_sinks()
    spark = _FakeSpark()
    spark.catalog.tableExists = lambda name: False
    def refuse(stmt):
        raise Exception("[PERMISSION_DENIED] User does not have CREATE SCHEMA on Catalog 'main'.\nmore")
    spark.sql = refuse
    cfg = {"src_schema": "dbo", "src_table": "t", "select_list": "id", "sink_catalog": "main", "sink_schema": "s",
           "sink_table": "t"}
    with pytest.raises(RuntimeError) as refused:
        ce.ensure_sink_table(spark, cfg, ce.sink_fqn(cfg))
    assert "USE CATALOG and CREATE SCHEMA on `main`" in str(refused.value) and "PERMISSION_DENIED" in str(refused.value)


def test_an_existing_target_table_makes_no_schema(monkeypatch):
    ce.forget_sinks()
    spark = _FakeSpark()
    spark.catalog.tableExists = lambda name: True
    ce.ensure_sink_table(spark, {"src_schema": "dbo", "src_table": "t", "select_list": "id", "sink_catalog": "main",
                                 "sink_schema": "s", "sink_table": "t"}, "`main`.`s`.`t`")
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


def test_ct_read_is_bounded_by_to_version(monkeypatch):
    queries = []

    def fake_read(sp, cfg, q, **kw):
        queries.append(q)
        return _DF(sp, q)
    monkeypatch.setattr(ce, "_read", fake_read)
    spark = _MergeSpark(result=_Row(num_affected_rows=0))
    ce.read_and_merge_ct(spark, _MERGE_CFG, "c.s.t", 5, to_version=9)
    assert queries[-1].endswith("AND ct.SYS_CHANGE_VERSION <= 9")


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
    assert [c.name for c in cols[1:]] == ["id", "v"]      # original columns follow, in order


def test_tag_selects_columns_as_objects_never_by_name(monkeypatch):
    # A bare name makes PySpark resolve a JVM class by short name, which misses the driver's classpath
    # under the notebook class loader's lock: one miss per column per table, and every table worker
    # queues on that lock. So every column, the moved change-read ones too, goes in as df[name].
    monkeypatch.setattr(ce, "_lit", _FakeLit)
    spark = _FakeSpark()
    df = _DF(spark, "q")
    df.columns = ["id", "v", "__change_op", "__change_version"]
    ce._tag(df, _APP, ce.append_meta(_APP, run_id=42, changes=True))
    ce._tag(_DF(spark, "q"), _CONS)
    for cols in spark.tagged:
        assert not [c for c in cols if isinstance(c, str)]


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


# ---- append write mode: identity columns, change-log rows, nothing replaced -------------------------

_APP = dict(_CONS, write_mode="append",
            client_attrs='{"tenant_name": "Acme", "source_server": "sql1", "app_version": 3}')


def test_client_attrs_parse_in_order_as_text():
    assert ce.client_attrs(_APP) == [("tenant_name", "Acme"), ("source_server", "sql1"),
                                     ("app_version", "3")]
    assert ce.client_attrs({"client_attrs": None}) == [] and ce.client_attrs({"client_attrs": " "}) == []
    assert ce.client_attrs({"client_attrs": {"a": None}}) == [("a", None)]


def test_client_attrs_validation():
    for bad, match in (('["a"]', "JSON object"), ('{"__x": 1}', "invalid client_attrs column"),
                       ('{"a b": 1}', "invalid client_attrs column"), ('{"A": 1, "a": 2}', "given twice"),
                       ('{"a": {"b": 1}}', "must be a string")):
        with pytest.raises(ValueError, match=match):
            ce.client_attrs({"client_attrs": bad})


def test_write_mode_defaults_to_merge_and_validates():
    assert ce.write_mode({}) == "merge" and ce.write_mode({"write_mode": " Append "}) == "append"
    assert ce.is_append(_APP) and not ce.is_append(_CONS)
    with pytest.raises(ValueError, match="invalid write_mode"):
        ce.write_mode({"write_mode": "upsert"})


def test_append_write_columns_and_identity_columns_in_merge_mode():
    assert ce.write_columns(_APP) == ["id", "v", "__client_id__", "tenant_name", "source_server",
                                      "app_version", "__change_op", "__change_version",
                                      "__extracted_at", "__run_id"]
    assert ce.write_columns(dict(_CONS, client_attrs='{"tenant_name": "Acme"}')) == \
        ["id", "v", "__client_id__", "tenant_name"]


def test_tag_leads_with_identity_then_append_metadata(monkeypatch):
    monkeypatch.setattr(ce, "_lit", _FakeLit)
    spark = _FakeSpark()
    df = _DF(spark, "q")
    df.columns = ["id", "v", "__change_op", "__change_version"]       # what a change read returns
    ce._tag(df, _APP, ce.append_meta(_APP, run_id=42, changes=True))
    (cols,) = spark.tagged
    assert [c.name for c in cols] == [
        "__client_id__", "tenant_name", "source_server", "app_version",
        "__change_op", "__change_version", "__extracted_at", "__run_id", "id", "v"]
    lits = {c.name: c for c in cols if isinstance(c, _FakeLit)}
    assert (lits["__run_id"].value, lits["__run_id"].dtype) == (42, "bigint")
    assert (lits["app_version"].value, lits["app_version"].dtype) == ("3", "string")
    assert lits["__extracted_at"].dtype == "timestamp"


def test_snapshot_metadata_is_typed_even_when_null(monkeypatch):
    monkeypatch.setattr(ce, "_lit", _FakeLit)
    meta = ce.append_meta(_APP, run_id=7)
    assert [(m.name, m.value, m.dtype) for m in meta[:2]] == [("__change_op", "S", "string"),
                                                             ("__change_version", None, "bigint")]
    assert ce.append_meta(_CONS) == []


def test_append_change_read_projects_operation_and_version():
    q = ce.ct_read_query(dict(_MERGE_CFG, write_mode="append"), 5, 9)
    assert ("ct.SYS_CHANGE_OPERATION AS [__change_op], CAST(ct.SYS_CHANGE_VERSION AS BIGINT) AS "
            "[__change_version] FROM CHANGETABLE(CHANGES [dbo].[t], 5) ct") in q
    assert " AS op " not in q and " AS op " in ce.ct_read_query(_MERGE_CFG, 5, 9)


def test_append_and_merge_sinks_are_never_mixed():
    log = ["__client_id__", "tenant_name", "__change_op", "id", "v"]
    ce.check_sink_mode(_APP, "c.s.t", log)
    with pytest.raises(ValueError, match="has no '__change_op' column but this control row's write_mode is 'append'"):
        ce.check_sink_mode(_APP, "c.s.t", ["__client_id__", "id", "v"])
    with pytest.raises(ValueError, match="has an '__change_op' column but this control row's write_mode is 'merge'"):
        ce.check_sink_mode(_CONS, "c.s.t", log)
    ce.check_sink_mode(dict(_CONS, select_list="id, __change_op"), "c.s.t", log)   # the source's own column


def test_ensure_sink_table_creates_an_append_sink_with_its_columns(monkeypatch):
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _DF(sp, q))
    monkeypatch.setattr(ce, "_lit", _FakeLit)
    monkeypatch.setattr(_Table, "columns", ["__client_id__", "tenant_name", *ce.APPEND_COLUMNS, "id", "v"])
    spark = _FakeSpark()
    spark.catalog.tableExists = lambda name: False
    ce.ensure_sink_table(spark, _APP, "c.s.t")
    (cols,) = spark.tagged
    assert [c.name for c in cols if isinstance(c, _FakeLit)][-4:] == list(ce.APPEND_COLUMNS)
    assert any(x.startswith("CREATE TABLE IF NOT EXISTS c.s.t CLUSTER BY (`__client_id__`)") for x in spark.statements)


def test_append_seed_appends_a_snapshot_and_replaces_nothing(monkeypatch):
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _DF(sp, q))
    monkeypatch.setattr(ce, "_lit", _FakeLit)
    spark = _FakeSpark()
    assert ce.seed(spark, dict(_APP, src_table="big"), "c.s.big", run_id=42, version=500) == 1
    (write,) = [x for x in spark.statements if x.startswith("INSERT")]
    assert write.startswith("INSERT WITH SCHEMA EVOLUTION INTO c.s.big BY NAME SELECT `id`, `v`, `__client_id__`, "
                            "`tenant_name`, `source_server`, `app_version`, `__change_op`, "
                            "`__change_version`, `__extracted_at`, `__run_id` FROM seed_full_")
    assert "REPLACE WHERE" not in write and spark.views == {}
    lits = {c.name: c.value for c in spark.tagged[0] if isinstance(c, _FakeLit)}
    assert (lits["__change_op"], lits["__change_version"], lits["__run_id"]) == ("S", 500, 42)


def test_append_partitioned_seed_appends_the_staged_snapshot(monkeypatch):
    spark = _FakeSpark()
    _seed(monkeypatch, spark, client_id="tenant-001", write_mode="append")
    staging = ce.staging_table_name("c.s.big", {"client_id": "tenant-001"})
    finals = [x for x in spark.statements if x.startswith("INSERT WITH SCHEMA EVOLUTION INTO c.s.big ")]
    assert finals == ["INSERT WITH SCHEMA EVOLUTION INTO c.s.big BY NAME SELECT `id`, `v`, `__client_id__`, "
                      f"`__change_op`, `__change_version`, `__extracted_at`, `__run_id` FROM {staging}"]
    assert not any("REPLACE WHERE" in x or "OVERWRITE c.s.big" in x for x in spark.statements)
    assert spark.statements[-1] == f"DROP TABLE IF EXISTS {staging}"
    stamps = {id(c) for cols in spark.tagged for c in cols
              if isinstance(c, _FakeLit) and c.name == "__extracted_at"}
    assert len(spark.tagged) > 2 and len(stamps) == 1     # staging and every partition: the same literals


def test_read_and_append_ct_appends_change_rows(monkeypatch):
    reads = []
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: reads.append((q, kw)) or _DF(sp, q))
    monkeypatch.setattr(ce, "_lit", _FakeLit)
    spark = _FakeSpark()
    assert ce.read_and_append_ct(spark, dict(_MERGE_CFG, write_mode="append"), "c.s.t", 5, 9, run_id=42) == 1
    [(q, kw)] = reads
    assert q.endswith("AND ct.SYS_CHANGE_VERSION <= 9") and ce.RETENTION_LAPSED_MARKER in kw["session_init"]
    (write,) = [x for x in spark.statements if x.startswith("INSERT")]
    assert write.startswith("INSERT WITH SCHEMA EVOLUTION INTO c.s.t BY NAME SELECT `id`, `v`, `__change_op`, "
                            "`__change_version`, `__extracted_at`, `__run_id` FROM ct_staged_")
    assert not any(x.startswith("MERGE") for x in spark.statements) and spark.views == {}
    (cols,) = spark.tagged
    assert [c.name for c in cols[:2]] == ["__change_op", "__change_version"]   # moved to the front from the read


def test_append_committed_looks_for_this_clients_rows_from_that_run():
    assert ce.append_committed_sql("c.s.t", _APP, 42) == (
        "SELECT 1 AS found FROM c.s.t WHERE `__client_id__` = 'tenant-001' AND `__run_id` = 42 LIMIT 1")
    assert ce.append_committed_sql("c.s.t", dict(_APP, client_id=None), 42) == (
        "SELECT 1 AS found FROM c.s.t WHERE `__run_id` = 42 LIMIT 1")


def test_append_committed_needs_the_sink_and_a_row():
    spark = _FakeSpark()
    spark.catalog.tableExists = lambda name: False
    assert ce.append_committed(spark, _APP, "c.s.t", 42) is False and spark.statements == []
    spark.catalog.tableExists = lambda name: True
    for rows, expected in (([{"found": 1}], True), ([], False)):
        spark.sql = lambda stmt, rows=rows: type("R", (), {"collect": lambda s: rows})()
        assert ce.append_committed(spark, _APP, "c.s.t", 42) is expected


# ---- batched appends: one union of reads, one INSERT --------------------------------------------------

def test_append_batch_reads_every_member_and_inserts_once(monkeypatch):
    import parallel
    reads = []
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: reads.append((cfg["id"], kw.get("session_init")))
                        or _DF(sp, q))
    monkeypatch.setattr(ce, "_lit", _FakeLit)
    spark, slots = _FakeSpark(), parallel.WorkSlots(4)
    a = dict(_APP, id=1, client_id="eng-1")
    b = dict(_APP, id=2, client_id="eng-2", select_list="id, v, extra")
    rows = ce.append_batch(spark, "c.s.t", [ce.change_read(a, 5, 9, run_id=42), ce.change_read(b, 7, 9, run_id=42)],
                           slots=slots)
    assert rows == 1 and [r[0] for r in reads] == [1, 2]
    assert all(ce.RETENTION_LAPSED_MARKER in init for _, init in reads)     # each read keeps its guard
    (write,) = [x for x in spark.statements if x.startswith("INSERT")]
    assert write.startswith("INSERT WITH SCHEMA EVOLUTION INTO c.s.t BY NAME SELECT `id`, `v`, `__client_id__`, ")
    assert write.count("`extra`") == 1 and " FROM batch_staged_" in write
    assert len(spark.tagged) == 2 and spark.views == {} and slots.peak == 2   # one slot per database read


def test_batch_columns_are_every_members_columns_once():
    a = dict(_CONS, select_list="id, v")
    b = dict(_CONS, select_list="ID, v, w")
    assert ce.batch_columns([a, b]) == ["id", "v", "__client_id__", "w"]


def test_committed_clients_finds_the_members_whose_rows_landed():
    assert ce.committed_clients_sql("c.s.t", ["eng-1", "eng-2"], 42) == (
        "SELECT DISTINCT `__client_id__` AS client FROM c.s.t WHERE `__run_id` = 42 "
        "AND `__client_id__` IN ('eng-1', 'eng-2')")
    spark = _FakeSpark()
    spark.catalog.tableExists = lambda name: True
    spark.sql = lambda stmt: type("R", (), {"collect": lambda s: [{"client": "eng-2"}]})()
    cfgs = [dict(_APP, client_id="eng-1"), dict(_APP, client_id="eng-2")]
    assert ce.committed_clients(spark, "c.s.t", cfgs, 42) == {"eng-2"}
    spark.catalog.tableExists = lambda name: False
    assert ce.committed_clients(spark, "c.s.t", cfgs, 42) == set()


def test_seed_prep_asks_every_database_in_one_job_then_one_by_one_on_failure(monkeypatch):
    calls = []

    class _PrepDF:
        def __init__(self, ids): self.ids = ids
        def unionByName(self, other, allowMissingColumns=False): return _PrepDF(self.ids + other.ids)
        def collect(self):
            calls.append(list(self.ids))
            if 2 in self.ids:
                raise RuntimeError("Login failed for user")          # database 2 is unreachable
            return [{"control_id": i, "v": 100 + i, "mv": 5, "bv": None} for i in self.ids]
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: _PrepDF([cfg["id"]]))
    cfgs = [dict(_APP, id=i, client_id=f"eng-{i}") for i in (1, 2, 3)]
    got = ce.seed_prep(None, cfgs)
    assert calls == [[1, 2, 3], [1], [2], [3]]
    assert got[1] == (101, 5, None) and got[3] == (103, 5, None) and isinstance(got[2], RuntimeError)
    sql = ce.seed_prep_sql(cfgs[0])
    assert sql.startswith("SELECT CAST(1 AS BIGINT) AS control_id, CAST(CHANGE_TRACKING_CURRENT_VERSION() AS BIGINT)")
    assert "CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID(N'[dbo].[t]'))" in sql and "begin_version" in sql
    assert "CAST(OBJECT_ID(N'[dbo].[t]') AS BIGINT) AS oid" in sql


def test_prepare_seed_asks_the_version_ct_state_and_begin_version_in_one_query(monkeypatch):
    # A per-table seed used to ask three questions (current_version, require_table_ct and, after the
    # seed, ct_begin_version), each a statement the driver plans. One query answers all three, and
    # the begin_version is then the one CT had BEFORE the read, as a batched seed records it.
    import pytest
    seen = _fake_read(monkeypatch, [{"control_id": 1, "v": 500, "mv": 7, "bv": 3, "oid": 901}])
    assert ce.prepare_seed(None, _APP) == (500, 3)
    assert seen == [ce.seed_prep_sql(_APP)]
    _fake_read(monkeypatch, [{"control_id": 1, "v": None, "mv": None, "bv": None}])
    with pytest.raises(RuntimeError, match="not enabled on this source database"):
        ce.prepare_seed(None, _APP)
    _fake_read(monkeypatch, [{"control_id": 1, "v": 500, "mv": None, "bv": None, "oid": 901}])
    with pytest.raises(RuntimeError, match="not enabled on this source table"):
        ce.prepare_seed(None, _APP)
    # A table that is not there has no Change Tracking either: say it is missing, not that CT is off.
    _fake_read(monkeypatch, [{"control_id": 1, "v": 500, "mv": None, "bv": None, "oid": None}])
    with pytest.raises(RuntimeError, match=r"source table not found: \[dbo\]\.\[t\]"):
        ce.prepare_seed(None, _APP)


def test_database_ct_state_reads_the_version_and_every_tables_state_in_one_query(monkeypatch):
    seen = _fake_read(monkeypatch, [{"v": 500, "sch": "DBO", "tbl": "Orders", "mv": 5, "bv": 3},
                                    {"v": 500, "sch": "dbo", "tbl": "lines", "mv": None, "bv": None}])
    assert ce.database_ct_state({"src_database": "d"}) == (500, {("dbo", "orders"): (5, 3),
                                                                ("dbo", "lines"): (None, None)})
    assert seen == [ce.CT_DATABASE_STATE_SQL] and ce.CT_TABLE_STATUS_SQL in ce.CT_DATABASE_STATE_SQL
    _fake_read(monkeypatch, [{"v": None, "sch": None, "tbl": None, "mv": None, "bv": None}])   # CT off
    assert ce.database_ct_state({"src_database": "d"}) == (None, {})


def test_database_ct_state_reads_through_a_given_session(monkeypatch):
    # The initial check calls it from its own threads, with the notebook's session.
    sessions = []
    monkeypatch.setattr(ce, "_spark", lambda: pytest.fail("looked up the active session"))
    monkeypatch.setattr(ce, "_read", lambda spark, cfg, q, **kw: (sessions.append(spark), _Rows([]))[1])
    assert ce.database_ct_state({"src_database": "d"}, spark="the session") == (None, {})
    assert sessions == ["the session"]


# ---- reads through a Unity Catalog connection (use_remote_query) ---------------------------------------

_REMOTE = dict(_MERGE_CFG, id=7, src_type="sqlserver", src_connection="sales_conn", use_remote_query=True,
               src_database="sales")


def test_uses_remote_query_reads_the_flag_as_stored():
    assert ce.uses_remote_query(_REMOTE) and ce.uses_remote_query(dict(_REMOTE, use_remote_query="true"))
    assert not ce.uses_remote_query(dict(_REMOTE, use_remote_query=False))
    assert not ce.uses_remote_query({"src_connection": "legacy"})     # the legacy host lookup stays JDBC


def test_remote_query_sql_passes_connection_database_query_and_options_as_parameters():
    sql, args = ce.remote_query_sql(dict(_REMOTE, jdbc_options='{"fetchsize": "50000"}'), "SELECT N'it''s' AS x")
    assert sql == "SELECT * FROM remote_query(:conn, database => :db, query => :q, `fetchsize` => :o0)"
    assert args == {"conn": "sales_conn", "db": "sales", "q": "SELECT N'it''s' AS x", "o0": "50000"}


def test_remote_query_sql_names_the_oracle_service():
    sql, args = ce.remote_query_sql(dict(_REMOTE, src_type="oracle", src_database="ORCLPDB1"), "SELECT 1 FROM dual")
    assert sql == "SELECT * FROM remote_query(:conn, service_name => :db, query => :q, `fetchsize` => :o0)"
    assert args["db"] == "ORCLPDB1" and args["o0"] == "10000"          # the adapter's default fetch size


def test_read_through_a_connection_runs_remote_query_without_a_secret():
    class _Spark:
        def __init__(self):
            self.calls = []

        def sql(self, stmt, args=None):
            self.calls.append((stmt, args))
            return "df"
    spark = _Spark()
    assert ce._read(spark, _REMOTE, "SELECT 1 AS one") == "df"         # no secret lookup, no JDBC options
    [(stmt, args)] = spark.calls
    assert stmt == "SELECT * FROM remote_query(:conn, database => :db, query => :q)"
    assert args == {"conn": "sales_conn", "db": "sales", "q": "SELECT 1 AS one"}
    with pytest.raises(ValueError, match="runs no session statement"):
        ce._read(spark, _REMOTE, "SELECT 1", session_init=ce.SNAPSHOT_ISOLATION_SQL)


def test_change_read_through_a_connection_carries_the_retention_guard_in_its_query():
    query, init = ce.ct_read(_REMOTE, 5, 9)
    mv = "CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID(N'[dbo].[t]'))"
    assert init is None and query == (
        f"SELECT TOP (CASE WHEN {mv} IS NULL OR {mv} > 5 THEN CAST(N'{ce.RETENTION_LAPSED_ERROR}' AS INT) "
        f"ELSE CAST(9223372036854775807 AS BIGINT) END) * FROM ({ce.ct_read_query(_REMOTE, 5, 9)}) AS c")
    # SQL Server quotes the value it failed to convert, so the sweep sees the marker and reseeds.
    assert ce.is_retention_lapsed_error(RuntimeError(
        f"Conversion failed when converting the nvarchar value '{ce.RETENTION_LAPSED_ERROR}' to data type int."))


def test_change_read_over_jdbc_keeps_the_session_guard(monkeypatch):
    monkeypatch.delenv("LAKEFED_SQL_SNAPSHOT_ISOLATION", raising=False)
    jdbc = dict(_REMOTE, use_remote_query=False)
    assert ce.ct_read(jdbc, 5, 9) == (ce.ct_read_query(jdbc, 5, 9), ce.ct_retention_guard_sql(jdbc, 5))
    assert f"THROW 51000, N'{ce.RETENTION_LAPSED_ERROR}', 1;" in ce.ct_retention_guard_sql(jdbc, 5)


def test_changes_through_a_connection_are_read_with_the_guarded_query(monkeypatch):
    reads = []
    monkeypatch.setattr(ce, "_read", lambda sp, cfg, q, **kw: reads.append((q, kw.get("session_init"))) or _DF(sp, q))
    monkeypatch.setattr(ce, "_lit", _FakeLit)
    app = dict(_REMOTE, write_mode="append")
    assert ce.read_and_append_ct(_FakeSpark(), app, "c.s.t", 5, 9, run_id=42) == 1
    assert ce.read_and_merge_ct(_FakeSpark(), _REMOTE, "c.s.t", 5, to_version=9) is None
    guarded = [ce.ct_read_query_guarded(app, 5, 9), ce.ct_read_query_guarded(_REMOTE, 5, 9)]
    assert reads == [(guarded[0], None), (guarded[1], None)]
    assert ce.change_read(app, 5, 9, run_id=42)[1:3] == (guarded[0], None)    # a batch member, the same


# ---- per-source query caps: which server a unit queries ---------------------------------------------

def test_source_key_names_the_server_a_row_queries():
    assert ce.source_key(_REMOTE) == "connection:sales_conn"
    jdbc = dict(_MERGE_CFG, src_type="sqlserver", src_host="SQL01.example", src_port=1500)
    assert ce.source_key(jdbc) == "host:sql01.example:1500"
    assert ce.source_key(dict(jdbc, src_port=None)) == "host:sql01.example:1433"           # the adapter's port
    assert ce.source_key(dict(jdbc, src_connection="lookup")) == "host:sql01.example:1500"  # src_host wins
    assert ce.source_key({"src_type": "sqlserver", "src_connection": "Legacy"}) == "connection:legacy"


def test_units_take_a_permit_of_each_rows_source():
    import parallel
    limiter = parallel.SourceLimiter({"connection:sales_conn": 2})
    slots = parallel.WorkSlots(4, sources=limiter)
    with ce._unit(slots, _REMOTE):
        assert limiter.peak == {"connection:sales_conn": 1}
    with ce._units(slots, [_REMOTE, _REMOTE, dict(_REMOTE, src_connection="other")]):
        assert limiter.peak == {"connection:sales_conn": 2, "connection:other": 1}
    with ce._unit(slots):                                    # Delta-only units take no permit
        assert slots.in_flight == 1
    assert ce._unit(None, _REMOTE) is not None and ce._units(None, [_REMOTE]) is not None


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
    assert ce.sink_lookup_key(cfgs[0]) == "main.bronze.orders"
    # An already-quoted part (allowed by sink_fqn) is looked up unquoted, never as SQL text.
    assert ce.sink_lookup_key(dict(cfgs[0], sink_catalog="`Main`")) == "main.bronze.orders"
    assert ce.existing_sinks(_Spark(False), [dict(cfgs[0], sink_schema="`Bronze`")]) == {"main.bronze.orders"}


# ---- sink checks: once per sink per run ---------------------------------------------------------

@pytest.fixture
def counted_catalog(monkeypatch):
    """A fake Spark whose catalog lookups are counted: (spark, calls)."""
    ce.forget_sinks()
    spark, calls = _FakeSpark(), []

    def table_exists(name):
        calls.append(("exists", name))
        return True

    def table(name):
        calls.append(("columns", name))
        return _Table()
    spark.catalog.tableExists, spark.table = table_exists, table
    monkeypatch.setattr(_Table, "columns", ["__client_id__", "id", "v", "__change_op"])
    yield spark, calls
    ce.forget_sinks()


def test_ensure_sink_table_checks_each_sink_once_per_run(counted_catalog):
    spark, calls = counted_catalog
    app = dict(_CONS, write_mode="append")
    for _ in range(3):
        ce.ensure_sink_table(spark, app, "c.s.t")
    assert calls == [("exists", "c.s.t"), ("columns", "c.s.t")]
    ce.ensure_sink_table(spark, app, "c.s.other")                  # another sink is checked on its own
    assert calls[2:] == [("exists", "c.s.other"), ("columns", "c.s.other")]
    ce.forget_sinks()                                              # the next run checks again
    ce.ensure_sink_table(spark, app, "c.s.t")
    assert calls[4:] == [("exists", "c.s.t"), ("columns", "c.s.t")]


def test_a_known_sink_still_refuses_a_row_of_the_wrong_mode(counted_catalog):
    spark, calls = counted_catalog
    ce.ensure_sink_table(spark, dict(_CONS, write_mode="append"), "c.s.t")
    with pytest.raises(ValueError, match="has a '__client_id__' column"):
        ce.ensure_sink_table(spark, dict(_CONS, client_id=None, write_mode="append"), "c.s.t")
    assert len(calls) == 2                                         # judged on the known columns


def test_a_failed_check_is_not_remembered(counted_catalog):
    spark, calls = counted_catalog
    with pytest.raises(ValueError):
        ce.ensure_sink_table(spark, dict(_CONS, client_id=None, write_mode="append"), "c.s.t")
    ce.ensure_sink_table(spark, dict(_CONS, write_mode="append"), "c.s.t")
    assert calls == [("exists", "c.s.t"), ("columns", "c.s.t")] * 2


# ---- session settings for a run ---------------------------------------------------------------

class _Conf:
    def __init__(self):
        self.values = {}

    def set(self, key, value):
        self.values[key] = value


def test_apply_session_conf_sets_each_setting_as_a_string():
    spark = type("S", (), {"conf": _Conf()})()
    applied = ce.apply_session_conf(spark, '{"spark.sql.codegen.wholeStage": false, "spark.x.n": 200}')
    assert spark.conf.values == {"spark.sql.codegen.wholeStage": "false", "spark.x.n": "200"}
    assert applied == spark.conf.values


def test_apply_session_conf_refuses_a_null_or_a_container_before_setting_anything():
    spark = type("S", (), {"conf": _Conf()})()
    for bad in ('{"x": null}', '{"y": [1]}', '{"z": {"a": 1}}', '{"n": NaN}', '{"n": Infinity}',
                '{"n": 1e999}'):
        with pytest.raises(ValueError, match="spark_conf"):
            ce.apply_session_conf(spark, bad)
    with pytest.raises(ValueError, match="spark_conf"):
        ce.apply_session_conf(spark, '{"ok.setting": "1", "y": [1]}')
    assert spark.conf.values == {}


def test_spark_conf_settings_writes_booleans_as_true_and_false_and_keeps_strings_and_numbers():
    assert ce.spark_conf_settings('{"a": true, "b": false, "c": 1.5, "d": 7, "e": "x"}') == {
        "a": "true", "b": "false", "c": "1.5", "d": "7", "e": "x"}


def test_apply_session_conf_ignores_blank_and_refuses_anything_but_an_object():
    spark = type("S", (), {"conf": _Conf()})()
    assert ce.apply_session_conf(spark, "") == {} and ce.apply_session_conf(spark, "  ") == {}
    assert ce.apply_session_conf(spark, None) == {}
    for bad in ('["a"]', '"x"', "{not json"):
        with pytest.raises(ValueError, match="spark_conf"):
            ce.apply_session_conf(spark, bad)
    assert spark.conf.values == {}
