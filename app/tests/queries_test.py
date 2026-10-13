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


# ---- bulk import ----------------------------------------------------------------------------------

def _disc(**kw):
    base = dict(src_database="Tenant-01", src_schema="dbo", src_table="Orders", row_count=10, size_mb=5.0,
                ct_enabled=True, load_type="ct", primary_key="id", cursor_column=None, partition_candidate="id",
                select_list="id, v", notes=None, registered_id=None, src_type="sqlserver", src_host="h",
                src_port=None, secret_key="creds-tenant-01")
    base.update(kw)
    return base


SHARED = dict(sink_catalog="main", sink_schema="bronze_{database}", sink_table="{table}", client_id="",
              partition_threshold_mb=1024, partition_size_mb=512, include_default=True)


def test_review_rows_templates_and_partition_threshold():
    small, big = q.review_rows([_disc(), _disc(src_table="Big", size_mb=4096)], SHARED)
    assert (small["sink_schema"], small["sink_table"], small["client_id"]) == ("bronze_tenant_01", "orders", None)
    assert (small["load_partitioned"], small["partition_col"]) == (False, None)
    assert (big["load_partitioned"], big["partition_col"], big["partition_size_mb"]) == (True, "id", 512)


def test_review_rows_large_table_without_candidate_is_not_partitioned():
    (r,) = q.review_rows([_disc(size_mb=9999, partition_candidate=None)], SHARED)
    assert r["load_partitioned"] is False


def test_review_rows_consolidated_client_template_keeps_raw_value():
    (r,) = q.review_rows([_disc()], dict(SHARED, sink_schema="bronze", client_id="{database}"))
    assert r["sink_schema"] == "bronze" and r["client_id"] == "Tenant-01"


def test_registered_and_columnless_tables_start_excluded():
    rows = q.review_rows([_disc(), _disc(registered_id=7), _disc(select_list="")], SHARED)
    assert [r["include"] for r in rows] == [True, False, False]
    assert all(r["include"] is False for r in q.review_rows([_disc()], dict(SHARED, include_default=False)))


def test_control_row_from_review_validates_and_drops_irrelevant_fields():
    (r,) = q.review_rows([_disc(load_type="full", cursor_column="rv")], SHARED)
    row = q.control_row_from_review(r, "coll")
    assert row["cursor_column"] is None and row["partition_col"] is None and row["secret_key"] == "creds-tenant-01"
    assert q.validate_control_row(row) == []
    assert len(q.control_params(row)) == len(q.CONTROL_INSERT_COLUMNS)


def test_identifier_and_insert_if_absent():
    assert q.identifier("Tenant-01 / Orders!") == "tenant_01_orders" and q.identifier("***") == "t"
    assert q.insert_control_if_absent_sql().endswith("on conflict do nothing returning id")


def test_parse_databases():
    assert q.parse_databases(" a, b\nc,,a ") == ["a", "b", "c"] and q.parse_databases(None) == []
