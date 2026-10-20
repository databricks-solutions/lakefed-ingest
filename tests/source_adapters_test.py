"""Source adapters: connection, quoting, literals, and the size / bounds / select SQL per engine."""
from datetime import date, datetime
from decimal import Decimal

import pytest

import partitions
import source_adapters as sa

CFG = {"src_schema": "hr", "src_table": "emp", "select_list": "id, name"}


def test_registry_and_default():
    assert sa.SRC_TYPES == ("sqlserver", "oracle", "postgresql", "mysql")
    assert sa.get_adapter(None).src_type == "sqlserver"          # the column default
    assert sa.get_adapter(" Oracle ").src_type == "oracle"
    assert sa.for_cfg({"src_type": "mysql"}).src_type == "mysql"


def test_registry_rejects_unknown_src_type():
    with pytest.raises(ValueError, match="unsupported src_type 'db2'.*sqlserver, oracle"):
        sa.get_adapter("db2")


def test_jdbc_urls_and_drivers():
    ss, ora, pg, my = (sa.get_adapter(t) for t in sa.SRC_TYPES)
    assert ss.jdbc_url("h", 1433, "db", False) == \
        "jdbc:sqlserver://h:1433;databaseName=db;encrypt=true;trustServerCertificate=false"
    assert ora.jdbc_url("h", 1521, "ORCLPDB1", True) == "jdbc:oracle:thin:@//h:1521/ORCLPDB1"
    assert pg.jdbc_url("h", 5432, "app", True) == "jdbc:postgresql://h:5432/app?sslmode=require"
    assert pg.jdbc_url("h", 5432, "app", False) == ("jdbc:postgresql://h:5432/app?sslmode=verify-full"
                                                    "&sslfactory=org.postgresql.ssl.DefaultJavaSSLFactory")
    assert my.jdbc_url("h", 3306, "app", False) == \
        "jdbc:mysql://h:3306/app?sslMode=VERIFY_IDENTITY&useCursorFetch=true"
    assert (ss.driver, ora.driver, pg.driver, my.driver) == (
        "com.microsoft.sqlserver.jdbc.SQLServerDriver", "oracle.jdbc.OracleDriver",
        "org.postgresql.Driver", "com.mysql.cj.jdbc.Driver")
    assert (ss.default_port, ora.default_port, pg.default_port, my.default_port) == (1433, 1521, 5432, 3306)


def test_identifier_quoting_escapes_per_dialect():
    assert sa.get_adapter("sqlserver").quote_ident("a]b") == "[a]]b]"
    assert sa.get_adapter("oracle").quote_ident('A"B') == '"A""B"'
    assert sa.get_adapter("postgresql").quote_ident("order") == '"order"'
    assert sa.get_adapter("mysql").quote_ident("a`b") == "`a``b`"


def test_string_literals_escape_per_dialect():
    assert sa.get_adapter("sqlserver").quote_str("o'b") == "N'o''b'"
    assert sa.get_adapter("oracle").quote_str("o'b") == "'o''b'"
    assert sa.get_adapter("mysql").quote_str("a\\'b") == "'a\\\\''b'"   # backslash escaped too


def test_select_sql_per_dialect():
    assert sa.get_adapter("sqlserver").select_sql(CFG) == "SELECT [id], [name] FROM [hr].[emp]"
    assert sa.get_adapter("oracle").select_sql(CFG, where="1=0") == \
        'SELECT "id", "name" FROM "hr"."emp" WHERE 1=0'
    assert sa.get_adapter("mysql").select_sql(CFG) == "SELECT `id`, `name` FROM `hr`.`emp`"


def test_bounds_queries_quote_identifiers():
    assert sa.get_adapter("postgresql").bounds_query("s", "t", "id") == \
        'SELECT MIN("id") AS lb, MAX("id") AS ub FROM "s"."t"'
    assert sa.get_adapter("mysql").bounds_query("s", "t", "id") == \
        "SELECT MIN(`id`) AS lb, MAX(`id`) AS ub FROM `s`.`t`"


def test_table_size_queries_return_table_size_mb_and_escape():
    for t in sa.SRC_TYPES:
        q = sa.get_adapter(t).table_size_mb_query("s", "o'b")
        assert "AS table_size_mb" in q and "o''b" in q, t
    ora = sa.get_adapter("oracle").table_size_mb_query("HR", "EMP")
    assert "FROM all_tables" in ora and "dba_segments" not in ora.lower()   # least privilege
    assert "owner = 'HR' AND table_name = 'EMP'" in ora
    pg = sa.get_adapter("postgresql").table_size_mb_query("s", "t")
    assert "pg_total_relation_size(to_regclass('\"s\".\"t\"'))" in pg
    my = sa.get_adapter("mysql").table_size_mb_query("app", "t")
    assert "information_schema.tables" in my and "table_schema = 'app'" in my


def test_render_bound_literals_per_dialect():
    ts, d = datetime(2024, 1, 2, 3, 4, 5), date(2024, 1, 2)
    epoch_ts, epoch_d = partitions.get_internal_bound_value(ts), partitions.get_internal_bound_value(d)
    assert sa.get_adapter("sqlserver").render_bound(epoch_ts, ts) == "'2024-01-02 03:04:05'"
    assert sa.get_adapter("oracle").render_bound(epoch_ts, ts) == "TIMESTAMP '2024-01-02 03:04:05'"
    assert sa.get_adapter("oracle").render_bound(epoch_d, d) == "DATE '2024-01-02'"
    assert sa.get_adapter("postgresql").render_bound(epoch_d, d) == "DATE '2024-01-02'"
    assert sa.get_adapter("mysql").render_bound(epoch_ts, ts) == "'2024-01-02 03:04:05'"
    for t in sa.SRC_TYPES:
        assert sa.get_adapter(t).render_bound(42, Decimal("1.5")) == "42"
    with pytest.raises(ValueError):
        sa.get_adapter("oracle").render_bound(1, "x")


def test_partition_list_uses_the_adapter_dialect():
    ora = sa.get_adapter("oracle")
    parts = partitions.get_partition_list("HIRED", date(2024, 1, 1), date(2024, 1, 5), 2, ora)
    assert parts[0]["where_clause"] == '"HIRED" < DATE \'2024-01-03\' or "HIRED" is null'
    assert parts[1]["where_clause"] == '"HIRED" >= DATE \'2024-01-03\''
    # default stays SQL Server (backward compatible)
    assert partitions.get_partition_list("id", 1, 100, 2)[1]["where_clause"] == "[id] >= 50"


def test_read_options_defaults_and_control_overrides():
    assert sa.get_adapter("sqlserver").read_options({}) == {}
    assert sa.get_adapter("oracle").read_options({}) == {"fetchsize": "10000"}
    got = sa.get_adapter("postgresql").read_options({"jdbc_options": '{"fetchsize": 50000, "x": true}'})
    assert got == {"fetchsize": "50000", "x": "true"}


def test_jdbc_options_cannot_override_framework_options():
    for bad in ('{"url": "jdbc:x"}', '{"Password": "p"}', '{"query": "select 1"}', '{"driver": "d"}'):
        with pytest.raises(ValueError, match="may not set"):
            sa.parse_jdbc_options(bad)
    with pytest.raises(ValueError, match="JSON object"):
        sa.parse_jdbc_options("[1]")
    assert sa.parse_jdbc_options(None) == {} and sa.parse_jdbc_options("") == {}


def test_remote_query_names_the_database_per_engine():
    assert [sa.get_adapter(t).remote_database_option for t in sa.SRC_TYPES] == [
        "database", "service_name", "database", "database"]      # sqlserver, oracle, postgresql, mysql
