"""Text keys compare like the source's collation (ct_engine key collation): SQL Server compares keys by
collation, usually case-insensitively and always ignoring trailing spaces, while Delta compares strings
exactly, so a key whose case changed at the source would land as a second row in the sink."""
import pytest

import ct_engine as ce
import cursor_engine as cur


@pytest.mark.parametrize("sql_server, databricks", [
    ("SQL_Latin1_General_CP1_CI_AS", ce.COLLATION_CI),           # the default: case-insensitive
    ("Latin1_General_100_CI_AS_SC_UTF8", ce.COLLATION_CI),
    ("Latin1_General_CI_AI", ce.COLLATION_CI_AI),                 # accent-insensitive too
    ("SQL_Latin1_General_CP1_CS_AS", ce.COLLATION_EXACT),         # case-sensitive: exact, trailing spaces ignored
    ("Latin1_General_BIN2", ce.COLLATION_EXACT),
    ("Japanese_XJIS_140_BIN", ce.COLLATION_EXACT),
    (None, None),                                                 # not a text column
    ("", None),
])
def test_a_sql_server_collation_maps_to_the_databricks_collation_that_compares_alike(sql_server, databricks):
    assert ce.databricks_collation(sql_server) == databricks


def test_the_merge_compares_collated_keys_under_their_collation_and_takes_the_sources_spelling():
    coll = {"id": ce.COLLATION_CI}
    assert ce.merge_on("id, seq", coll) == "tgt.`id` = s.`id` COLLATE UTF8_LCASE_RTRIM AND tgt.`seq` = s.`seq`"
    # A key matched under its collation is set too, so a key whose case changed takes the source's case.
    assert ce.merge_set("id, seq, v", "id, seq", coll) == "`v` = s.`v`, `id` = s.`id`"
    # Without collations the statements are exactly as before.
    assert ce.merge_on("id, seq") == "tgt.`id` = s.`id` AND tgt.`seq` = s.`seq`"
    assert ce.merge_set("id, seq, v", "id, seq") == "`v` = s.`v`"
    assert ce.merge_set("id", "id", coll) == "`id` = s.`id`"        # an all-key table: the no-op SET as before


def test_the_ct_and_cursor_merges_use_the_sinks_key_collations():
    cfg = {"src_schema": "dbo", "src_table": "t", "select_list": "id, v", "primary_key": "id"}
    coll = {"id": ce.COLLATION_CI}
    ct = ce.merge_sql(cfg, "c.s.t", "v1", coll)
    assert "ON tgt.`id` = s.`id` COLLATE UTF8_LCASE_RTRIM " in ct and "UPDATE SET `v` = s.`v`, `id` = s.`id` " in ct
    up = cur.upsert_merge_sql(cfg, "c.s.t", "v1", coll)
    assert "ON tgt.`id` = s.`id` COLLATE UTF8_LCASE_RTRIM " in up and "UPDATE SET `v` = s.`v`, `id` = s.`id` " in up
    assert "COLLATE" not in ce.merge_sql(cfg, "c.s.t", "v1")


def test_a_consolidated_sinks_client_column_stays_exact():
    cfg = {"src_schema": "dbo", "src_table": "t", "select_list": "id, v", "primary_key": "id", "client_id": "c1",
           "sink_catalog": "c", "sink_schema": "s", "sink_table": "t"}
    on = ce.merge_on_cfg(cfg, {"id": ce.COLLATION_CI})
    assert on.startswith("tgt.`__client_id__` = s.`__client_id__` AND tgt.`id` = s.`id` COLLATE UTF8_LCASE_RTRIM")


def test_the_marker_names_each_text_key_and_is_empty_without_one():
    assert ce.key_collation_marker({"ID": ce.COLLATION_CI, "seq": None}) == "id=UTF8_LCASE_RTRIM"
    assert ce.key_collation_marker({"b": ce.COLLATION_EXACT, "a": ce.COLLATION_CI}) == (
        "a=UTF8_LCASE_RTRIM,b=UTF8_BINARY_RTRIM")
    assert ce.key_collation_marker({"id": None}) == "" == ce.key_collation_marker({})


def test_the_duplicate_check_counts_keys_held_twice_in_this_clients_rows():
    dedicated = {"primary_key": "parent_id, seq"}
    assert ce.duplicate_keys_sql(dedicated, "c.s.t") == (
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM c.s.t GROUP BY `parent_id`, `seq` HAVING COUNT(*) > 1) AS dup")
    shared = dict(dedicated, client_id="c1")
    assert ce.duplicate_keys_sql(shared, "c.s.t") == (
        "SELECT COUNT(*) AS n FROM (SELECT 1 FROM c.s.t WHERE `__client_id__` = 'c1' "
        "GROUP BY `__client_id__`, `parent_id`, `seq` HAVING COUNT(*) > 1) AS dup")


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def collect(self):
        return self.rows


def test_source_key_collations_reads_the_keys_collations_from_sql_server_only(monkeypatch):
    seen = []

    def read(spark, cfg, q, **kw):
        seen.append(q)
        return _Rows([{"col": "ID", "coll": "SQL_Latin1_General_CP1_CI_AS"}, {"col": "seq", "coll": None},
                      {"col": "note", "coll": "SQL_Latin1_General_CP1_CI_AS"}])
    monkeypatch.setattr(ce, "_read", read)
    cfg = {"src_schema": "dbo", "src_table": "t", "primary_key": "id, seq"}
    assert ce.source_key_collations(None, cfg) == {"id": ce.COLLATION_CI, "seq": None}   # keys only
    assert seen == ["SELECT c.name AS col, c.collation_name AS coll FROM sys.columns c "
                    "WHERE c.object_id = OBJECT_ID(N'[dbo].[t]')"]
    assert ce.source_key_collations(None, dict(cfg, src_type="postgresql")) == {}       # not mapped: stays exact
    assert len(seen) == 1


class _Type:
    def __init__(self, name, text):
        self.name, self.text = name, text

    def typeName(self):
        return self.name

    def simpleString(self):
        return self.text


class _Field:
    def __init__(self, name, data_type):
        self.name, self.dataType = name, data_type


class _Schema:
    def __init__(self, fields):
        self.fields = fields


class _Table:
    def __init__(self, fields):
        self.schema = _Schema(fields)
        self.columns = [f.name for f in fields]


def test_a_sinks_string_columns_are_read_with_their_collation():
    table = _Table([_Field("ID", _Type("string", "string collate UTF8_LCASE_RTRIM")),
                    _Field("note", _Type("string", "string")), _Field("seq", _Type("integer", "int"))])
    assert ce.string_collations(table) == {"id": "UTF8_LCASE_RTRIM", "note": ce.BINARY}


class _Spark:
    def __init__(self, table):
        self.table_obj, self.statements = table, []

    def sql(self, q):
        self.statements.append(q)
        return self

    def table(self, name):
        return self.table_obj


def test_apply_key_collations_alters_only_keys_still_without_one(monkeypatch):
    monkeypatch.setattr(ce, "with_write_retry", lambda fn, what=None: fn())
    before = _Table([_Field("id", _Type("string", "string")), _Field("code", _Type("string", "string collate UNICODE_CI_AI_RTRIM")),
                     _Field("seq", _Type("integer", "int"))])
    after = _Table([_Field("id", _Type("string", "string collate UTF8_LCASE_RTRIM")),
                    _Field("code", _Type("string", "string collate UNICODE_CI_AI_RTRIM")), _Field("seq", _Type("integer", "int"))])
    spark = _Spark(after)
    monkeypatch.setattr(ce, "_has_files", lambda sp, t: True)
    ce._sink_collations["c.s.t"] = ce.string_collations(before)
    try:
        changed = ce.apply_key_collations(spark, "c.s.t", {"id": ce.COLLATION_CI, "code": ce.COLLATION_CI, "seq": None})
        assert changed == ["id"]                                  # code already has a collation: kept
        # The files' statistics were collected for exact comparison: recollected, so key filters skip files again.
        assert spark.statements == ["ALTER TABLE c.s.t ALTER COLUMN `id` TYPE STRING COLLATE UTF8_LCASE_RTRIM",
                                    "ANALYZE TABLE c.s.t COMPUTE DELTA STATISTICS"]
        assert ce.sink_key_collations("c.s.t") == {"id": "UTF8_LCASE_RTRIM", "code": "UNICODE_CI_AI_RTRIM"}
        assert ce.apply_key_collations(spark, "c.s.t", {"id": ce.COLLATION_CI}) == []   # nothing left to change
    finally:
        ce.forget_sinks()
    assert ce.sink_key_collations("c.s.t") == {}


class _FailOnce:
    """A MERGE that fails the first time as one built before another run collated the sink's keys would."""
    def __init__(self, table):
        self.table_obj, self.statements = table, []

    def sql(self, q):
        self.statements.append(q)
        if len(self.statements) == 1:
            raise RuntimeError("[INDETERMINATE_COLLATION_IN_EXPRESSION] Data type of \"(k = k)\" has indeterminate collation")
        return self

    def first(self):
        return "done"

    def table(self, name):
        return self.table_obj


def test_a_merge_built_before_another_run_collated_the_keys_reads_them_again_and_runs_once_more():
    spark = _FailOnce(_Table([_Field("id", _Type("string", "string collate UTF8_LCASE_RTRIM"))]))
    ce._sink_collations["c.s.t"] = {"id": ce.BINARY}
    try:
        built = []
        assert ce.merge_collated(spark, "c.s.t", lambda c: built.append(dict(c)) or f"MERGE {sorted(c.items())}") == "done"
        assert built == [{}, {"id": "UTF8_LCASE_RTRIM"}]
    finally:
        ce.forget_sinks()


def test_any_other_merge_error_is_raised_as_is():
    class _Boom(_FailOnce):
        def sql(self, q):
            raise RuntimeError("[DELTA_CONCURRENT_APPEND] conflict")
    with pytest.raises(RuntimeError, match="CONCURRENT_APPEND"):
        ce.merge_collated(_Boom(None), "c.s.t", lambda c: "MERGE")


def test_a_sink_about_to_be_rewritten_or_still_empty_is_not_analyzed(monkeypatch):
    monkeypatch.setattr(ce, "with_write_retry", lambda fn, what=None: fn())
    before = _Table([_Field("id", _Type("string", "string"))])
    after = _Table([_Field("id", _Type("string", "string collate UTF8_LCASE_RTRIM"))])
    try:
        for analyze, has_files in ((False, True), (True, False)):
            spark = _Spark(after)
            monkeypatch.setattr(ce, "_has_files", lambda sp, t, h=has_files: h)
            ce._sink_collations["c.s.t"] = ce.string_collations(before)
            ce.apply_key_collations(spark, "c.s.t", {"id": ce.COLLATION_CI}, analyze=analyze)
            assert spark.statements == ["ALTER TABLE c.s.t ALTER COLUMN `id` TYPE STRING COLLATE UTF8_LCASE_RTRIM"]
    finally:
        ce.forget_sinks()
