"""Operations app SQL: parameterized everywhere, filters build the expected predicates, form checks."""
import pytest

import queries as q


def test_every_statement_is_parameterized_and_schema_qualified():
    for sql in (q.collections_overview_sql(), q.running_runs_sql(), q.run_events_sql(), q.recent_errors_sql(),
                q.collections_sql(), q.set_enabled_sql(), q.force_reseed_sql(), q.log_reseed_sql(),
                q.insert_control_sql()):
        assert "lakefed_ingest." in sql
    assert q.collections_overview_sql().count("%s") == 1


def test_runs_sql_optional_collection_and_limit():
    sql, params = q.runs_sql(None, limit=50)
    assert "where" not in sql and params == (50,)
    sql, params = q.runs_sql("c1", limit=10)
    assert "where task_collection = %s" in sql and params == ("c1", 10)


@pytest.mark.parametrize("flt,frag", [
    ("failing", "cp.status = 'failed'"),
    ("stale", "make_interval(mins => %s)"),
    ("seeding", "cp.phase = 'seeding'"),
    ("disabled", "not t.task_enabled"),
])
def test_table_filters(flt, frag):
    sql, params = q.tables_sql("c1", "orders", flt, 60, 100)
    assert frag in sql and "ilike %s" in sql
    assert params[0] == "c1" and params[1] == "%orders%" and params[-1] == 100
    assert sql.count("%s") == len(params)


def test_table_filter_all_and_invalid():
    sql, params = q.tables_sql(None, None, "all", 60, 5)
    assert "where" not in sql and params == (5,)
    with pytest.raises(ValueError):
        q.tables_sql(None, None, "bogus", 60, 5)


def test_search_text_is_a_parameter_not_sql():
    sql, params = q.tables_sql(None, "x'; drop table control; --", "all", 60, 5)
    assert "drop table" not in sql and params[0] == "%x'; drop table control; --%"


def test_force_reseed_flips_phase_and_logs():
    assert "set phase = 'seeding'" in q.force_reseed_sql() and "control_id = any(%s)" in q.force_reseed_sql()
    assert q.log_reseed_sql().count("%s") == 2


def _row(**kw):
    base = dict(task_collection="c", src_type="sqlserver", src_host="h", secret_key="k", src_database="d",
                src_schema="dbo", src_table="t", select_list="id, v", primary_key="id", load_type="ct",
                sink_catalog="main", sink_schema="bronze", sink_table="t")
    base.update(kw)
    return base


def test_validate_control_row():
    assert q.validate_control_row(_row()) == []
    assert "primary_key is required for load_type cursor" in q.validate_control_row(_row(load_type="cursor", primary_key=""))
    assert "cursor_column is required for load_type cursor" in q.validate_control_row(_row(load_type="cursor"))
    assert "load_type ct is only supported for src_type sqlserver" in q.validate_control_row(_row(src_type="oracle"))
    assert q.validate_control_row(_row(load_type="full", primary_key="", src_type="oracle")) == []
    assert "select_list must list explicit columns (* isn't supported)" in q.validate_control_row(_row(select_list="*"))
    assert "src_host is required" in q.validate_control_row(_row(src_host="  "))
    assert "partitioned loads need partition_col and partition_size_mb" in q.validate_control_row(_row(load_partitioned=True))


def test_control_params_order_and_blank_to_null():
    params = q.control_params(_row(client_id="  ", cursor_column=""))
    assert len(params) == len(q.CONTROL_INSERT_COLUMNS) == q.insert_control_sql().count("%s")
    d = dict(zip(q.CONTROL_INSERT_COLUMNS, params))
    assert d["client_id"] is None and d["cursor_column"] is None and d["src_table"] == "t"
