"""Every text column in Databricks is a plain STRING: reads drop CHAR / VARCHAR lengths, and a sink made with them
has those columns changed to STRING (ct_engine.plain_strings, widen_char_varchar). A CHAR(n) / VARCHAR(n) column
fails any write of a longer value (DELTA_EXCEED_CHAR_VARCHAR_LIMIT) after the whole table has been read."""
import types

import ct_engine as ce


class _Type:
    def __init__(self, shown):
        self.shown = shown

    def simpleString(self):
        return self.shown

    def typeName(self):
        return self.shown.split("(")[0]


class _Field:
    def __init__(self, name, shown="string", metadata=None):
        self.name, self.dataType, self.metadata = name, _Type(shown), metadata or {}


class _Schema:
    def __init__(self, *fields):
        self.fields = list(fields)


def test_a_length_limited_text_type_is_found_whichever_way_spark_reports_it():
    assert ce.char_varchar_type(_Field("a", "varchar(10)")) == "varchar(10)"            # a VarcharType
    assert ce.char_varchar_type(_Field("a", "CHAR(1)")) == "char(1)"                      # a CharType
    assert ce.char_varchar_type(_Field("a", metadata={ce.CHAR_VARCHAR_METADATA: "char(4)"})) == "char(4)"
    assert ce.char_varchar_type(_Field("a", metadata={ce.CHAR_VARCHAR_METADATA: "VARCHAR(255)"})) == "varchar(255)"
    for plain in (_Field("a"), _Field("a", "int"), _Field("a", "string collate UTF8_LCASE_RTRIM"),
                  _Field("a", metadata={ce.CHAR_VARCHAR_METADATA: "string"}), _Field("a", "array<varchar(3)>")):
        assert ce.char_varchar_type(plain) is None


def test_char_varchar_columns_keeps_schema_order():
    schema = _Schema(_Field("id", "int"), _Field("code", "char(1)"), _Field("name"),
                     _Field("note", metadata={ce.CHAR_VARCHAR_METADATA: "varchar(40)"}))
    assert ce.char_varchar_columns(schema) == ["code", "note"]
    assert ce.char_varchar_columns(_Schema()) == [] and ce.char_varchar_columns(None) == []


class _Col:
    def __init__(self, ref, ops=()):
        self.ref, self.ops = ref, tuple(ops)

    def cast(self, t):
        return _Col(self.ref, self.ops + (("cast", t),))

    def alias(self, name, **kw):
        return _Col(self.ref, self.ops + (("alias", name, tuple(sorted(kw.items()))),))


class _DF:
    def __init__(self, schema):
        self.schema, self.selected = schema, None

    def __getitem__(self, ref):
        return _Col(ref)

    def select(self, *cols):
        out = _DF(_Schema())
        out.selected = cols
        return out


def test_plain_strings_casts_only_the_length_limited_columns():
    # Columns come from df[...], never F.col: no pyspark.sql.functions here (a fake df answers df[name]).
    df = _DF(_Schema(_Field("id", "int"), _Field("status_code", metadata={ce.CHAR_VARCHAR_METADATA: "char(1)"}),
                     _Field("we`ird", "varchar(3)"), _Field("name")))
    out = ce.plain_strings(df)
    got = [(c.ref, c.ops) for c in out.selected]
    assert got == [("`id`", ()),
                   ("`status_code`", (("cast", "string"), ("alias", "status_code", ()))),
                   ("`we``ird`", (("cast", "string"), ("alias", "we`ird", ()))),
                   ("`name`", ())]


def test_plain_strings_leaves_a_read_without_them_alone():
    df = _DF(_Schema(_Field("id", "int"), _Field("name")))
    assert ce.plain_strings(df) is df
    assert ce.plain_strings("no schema") == "no schema"                      # a test double passes through


def test_to_string_sql_quotes_the_column():
    assert ce.to_string_sql("`c`.`s`.`t`", "we`ird") == "ALTER TABLE `c`.`s`.`t` ALTER COLUMN `we``ird` TYPE STRING"


class _Table:
    def __init__(self, schema):
        self.schema = schema
        self.columns = [f.name for f in schema.fields]


class _Spark:
    """A sink that exists with CHAR / VARCHAR columns until an ALTER changes them."""

    def __init__(self, *fields):
        self.fields, self.statements = {f.name: f for f in fields}, []
        self.catalog = types.SimpleNamespace(tableExists=lambda name: True)

    def table(self, name):
        return _Table(_Schema(*self.fields.values()))

    def sql(self, stmt):
        self.statements.append(stmt)
        column = stmt.split("ALTER COLUMN `", 1)[1].split("`", 1)[0]
        self.fields[column] = _Field(column)                          # now a plain STRING
        return None


def test_widen_char_varchar_alters_each_limited_column_once():
    spark = _Spark(_Field("id", "int"), _Field("code", "char(1)"), _Field("note", "varchar(40)"), _Field("name"))
    assert ce.widen_char_varchar(spark, "c.s.t", spark.table("c.s.t")) == ["code", "note"]
    assert spark.statements == ["ALTER TABLE c.s.t ALTER COLUMN `code` TYPE STRING",
                                "ALTER TABLE c.s.t ALTER COLUMN `note` TYPE STRING"]
    assert ce.widen_char_varchar(spark, "c.s.t", spark.table("c.s.t")) == []      # idempotent
    assert len(spark.statements) == 2


def test_an_existing_sink_with_char_columns_is_changed_to_strings_when_a_run_first_meets_it():
    ce.forget_sinks()
    spark = _Spark(_Field("id", "int"), _Field("status_code", metadata={ce.CHAR_VARCHAR_METADATA: "char(1)"}))
    cfg = {"src_schema": "dbo", "src_table": "orders", "select_list": "id, status_code"}
    ce.ensure_sink_table(spark, cfg, "c.s.orders")
    assert spark.statements == ["ALTER TABLE c.s.orders ALTER COLUMN `status_code` TYPE STRING"]
    # a plain STRING at Delta's default collation, so the key collation change still finds the column
    assert ce.string_collations(spark.table("c.s.orders")) == {"status_code": ce.BINARY}
    ce.ensure_sink_table(spark, cfg, "c.s.orders")              # known this run: no more catalog work
    assert len(spark.statements) == 1
    ce.forget_sinks()


def _wired(monkeypatch):
    """_read with plain_strings marking what it was given, so a test sees whether the read went through it."""
    monkeypatch.setattr(ce, "plain_strings", lambda df: ("plain", df))


def test_a_jdbc_read_goes_through_plain_strings(monkeypatch):
    _wired(monkeypatch)
    monkeypatch.setattr(ce, "_jdbc_config", lambda cfg: {"host": "h", "port": 1433, "user": "u", "password": "p"})

    class _Reader:
        def format(self, f): return self
        def options(self, **kw): return self
        def option(self, k, v): return self
        def load(self): return "jdbc df"
    spark = types.SimpleNamespace(read=_Reader())
    cfg = {"id": 1, "src_type": "sqlserver", "src_database": "d", "secret_key": "k"}
    assert ce._read(spark, cfg, "SELECT 1") == ("plain", "jdbc df")


def test_a_remote_query_read_goes_through_plain_strings(monkeypatch):
    _wired(monkeypatch)
    spark = types.SimpleNamespace(sql=lambda stmt, args=None: "remote df")
    cfg = {"id": 1, "src_type": "sqlserver", "src_connection": "conn", "src_database": "d", "use_remote_query": True}
    assert ce._read(spark, cfg, "SELECT 1") == ("plain", "remote df")
