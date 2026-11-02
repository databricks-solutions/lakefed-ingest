"""Operations app SQL: parameterized everywhere, filters build the expected predicates, form checks."""
from pathlib import Path

import pytest

import queries as q


def test_every_statement_is_parameterized_and_schema_qualified():
    for sql in (q.collections_overview_sql(), q.running_runs_sql(), q.run_events_sql(), q.recent_errors_sql(), q.failed_tables_sql(),
                q.collections_sql(), q.set_enabled_sql(), q.force_reseed_sql(), q.log_reseed_sql(),
                q.insert_control_sql()):
        assert "lakefed_ingest." in sql
    assert "%s" not in q.collections_overview_sql()   # the SLA is per table, from control


def test_runs_sql_optional_collection_and_limit():
    sql, params = q.runs_sql(None, limit=50)
    assert "task_collection" not in sql.split("where")[1] and params == (50,)
    sql, params = q.runs_sql("c1", limit=10)
    assert "where task_collection = %s" in sql and params == ("c1", 10)


@pytest.mark.parametrize("flt,frag", [
    ("failing", "cp.status = 'failed'"),
    ("sla_breached", "make_interval(mins => t.freshness_sla_minutes)"),
    ("no_sla", "t.freshness_sla_minutes is null"),
    ("seeding", "cp.phase = 'seeding'"),
    ("disabled", "not t.task_enabled"),
])
def test_table_filters(flt, frag):
    sql, params = q.tables_sql("c1", "orders", flt, 100)
    assert frag in sql and "ilike %s" in sql
    assert params[0] == "c1" and params[1] == "%orders%" and params[-1] == 100
    assert sql.count("%s") == len(params)


def test_table_filter_all_and_invalid():
    sql, params = q.tables_sql(None, None, "all", 5)
    assert sql.split(" as ingestion_job,")[1].count("where") == 0 and params == (5,)      # the job name's subquery aside
    sql, params = q.tables_sql(None, None, "all", 5, job=7)
    assert "m.schedule_id = %s" in sql and params == (7, 5)
    with pytest.raises(ValueError):
        q.tables_sql(None, None, "bogus", 5)


def test_search_text_is_a_parameter_not_sql():
    sql, params = q.tables_sql(None, "x'; drop table control; --", "all", 5)
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


def test_unity_catalog_identifier_only_replaces_what_unity_catalog_refuses():
    assert q.unity_catalog_identifier("Order-Lines") == "order-lines"
    assert q.unity_catalog_identifier("Sales (2024)") == "sales_(2024)"
    assert q.unity_catalog_identifier("a--b") == "a--b"
    assert q.unity_catalog_identifier("a`b") == "a`b"
    assert q.unity_catalog_identifier("Order Lines") == "order_lines"
    assert q.unity_catalog_identifier("a.b/c\td\x7fe") == "a_b_c_d_e"


def test_parse_databases():
    assert q.parse_databases(" a, b\nc,,a ") == ["a", "b", "c"] and q.parse_databases(None) == []


def test_merge_review_edits_takes_only_editable_fields():
    review = q.review_rows([_disc(), _disc(src_table="Other")], SHARED)
    edits = [{"src_database": "Tenant-01", "src_schema": "dbo", "src_table": "Orders", "include": False,
              "sink_table": "x", "_secret_key": "stolen", "_src_host": "evil", "notes": "ignored"}]
    merged = q.merge_review_edits(review, edits)
    assert merged[0]["include"] is False and merged[0]["sink_table"] == "x"
    assert merged[0]["_secret_key"] == "creds-tenant-01" and merged[0]["_src_host"] == "h"   # never from the client
    assert merged[1] == review[1]                                                           # untouched row
    assert not any(k.startswith("_") for k in q.public_review_row(merged[0]))


# ---- editing ----------------------------------------------------------------------------------

ROW = dict(id=1, task_collection="c1", task_enabled=True, tier=None, priority=None, src_type="sqlserver", src_host="Srv",
           src_port=None, secret_key="k", src_database="db", src_schema="dbo", src_table="t", select_list="id, v",
           primary_key="id", load_type="ct", cursor_column=None, sink_catalog="main", sink_schema="bronze",
           sink_table="t", client_id=None, load_partitioned=False, partition_col=None, partition_size_mb=None,
           jdbc_options=None)


def test_plan_edit_reseed_reasons_and_notes():
    (p,) = q.plan_edit([ROW], {"sink_schema": "silver"}, bulk=False)
    assert p["reseed"] == "sink_changed" and any("old sink main.bronze.t is left in place" in n for n in p["notes"])
    (p,) = q.plan_edit([ROW], {"primary_key": "id, v"}, bulk=False)
    assert p["reseed"] == "primary_key_changed"
    (p,) = q.plan_edit([ROW], {"src_host": "other"}, bulk=False)
    assert p["reseed"] == "source_changed"
    (p,) = q.plan_edit([dict(ROW, load_type="full", primary_key=None)], {"sink_table": "t2"}, bulk=False)
    assert p["reseed"] is None and p["changes"]          # full reloads every run: no reseed needed


def test_plan_edit_ignores_cosmetic_changes():
    for change in ({"src_host": "SRV"}, {"src_port": 1433}, {"primary_key": " ID "}, {"sink_table": "T"}, {"tier": ""}):
        (p,) = q.plan_edit([ROW], change, bulk=False)
        assert p["changes"] == {} and p["reseed"] is None, change


def test_plan_edit_move_secret_and_select_list_notes():
    (p,) = q.plan_edit([ROW], {"task_collection": "c2", "secret_key": "k2"}, bulk=False)
    assert p["reseed"] is None and any("moves to ingestion group c2" in n for n in p["notes"])
    (p,) = q.plan_edit([ROW], {"select_list": "id, w"}, bulk=False)
    assert any("new columns w" in n for n in p["notes"]) and any("removed columns v stay in the sink" in n for n in p["notes"])


def test_plan_edit_bulk_whitelist_types_and_validation():
    (p,) = q.plan_edit([ROW], {"src_table": "x"}, bulk=True)
    assert p["errors"] == ["cannot be changed in bulk: src_table"]
    (p,) = q.plan_edit([ROW], {"priority": "5", "task_enabled": "false"}, bulk=True)
    assert p["changes"] == {"priority": [None, 5], "task_enabled": [True, False]} and p["errors"] == []
    (p,) = q.plan_edit([ROW], {"priority": "high"}, bulk=True)
    assert p["errors"] and "invalid value" in p["errors"][0]
    (p,) = q.plan_edit([ROW], {"jdbc_options": "[1]"}, bulk=True)
    assert "jdbc_options must be a JSON object" in p["errors"]
    (p,) = q.plan_edit([ROW], {"client_id": "a b"}, bulk=False)
    assert any("client_id may only use" in e for e in p["errors"])


def test_update_sql_whitelists_columns():
    assert q.update_control_sql(["sink_table", "tier"]) == "update lakefed_ingest.control set sink_table = %s, tier = %s where id = %s"
    with pytest.raises(AssertionError):
        q.update_control_sql(["id; drop table control"])
    assert q.collection_lock_key("c1") == "lakefed_ingest:c1"


def test_sla_breach_rule_uses_control_sla_and_creation_for_never_succeeded():
    rule = q.SLA_BREACHED
    assert "t.task_enabled" in rule and "t.freshness_sla_minutes is not null" in rule
    assert f"coalesce({q.LAST_SUCCESS}, t.created_at)" in rule and "cp.last_success_at" in q.LAST_SUCCESS
    assert rule in q.collections_overview_sql()
    sql, _ = q.tables_sql(None, None, "all", 5)
    assert "as sla_breached" in sql and "as sla_min" in sql


def test_the_documented_sla_alert_counts_database_check_stamps_as_the_app_does():
    # A quiet database checked and found unchanged stamps the database, not each table: the alert query in
    # docs/monitoring.md must count that stamp, as LAST_SUCCESS does, or a quiet table alerts while the app shows it current.
    doc = (Path(__file__).parents[2] / "docs" / "monitoring.md").read_text()
    alert = doc.split("Freshness SLA breaches.")[1].split("```sql")[1].split("```")[0]
    assert "database_check dc" in alert and "dc.checked_at" in alert and "greatest(cp.last_success_at" in alert
    assert "coalesce(last_success_at, created_at)" in alert and "c.freshness_sla_minutes IS NOT NULL" in alert
    assert "dc.checked_at" in q.LAST_SUCCESS


@pytest.mark.parametrize("value,ok", [(None, True), ("", True), (15, True), ("30", True),
                                      (0, False), (-5, False), ("abc", False), (2.5, False)])
def test_validate_freshness_sla(value, ok):
    row = {"task_collection": "c", "src_type": "sqlserver", "src_host": "h", "secret_key": "k", "src_database": "d",
           "src_schema": "dbo", "src_table": "t", "select_list": "a", "primary_key": "a", "load_type": "ct",
           "sink_catalog": "c", "sink_schema": "s", "sink_table": "t", "freshness_sla_minutes": value}
    errs = [e for e in q.validate_control_row(row) if "freshness_sla_minutes" in e]
    assert (not errs) == ok


def test_sla_is_a_cosmetic_bulk_edit_and_flows_through_bulk_import():
    assert "freshness_sla_minutes" in q.BULK_EDIT_FIELDS and "freshness_sla_minutes" not in q.RESEED_FIELDS
    row = {"id": 1, "task_collection": "c", "task_enabled": True, "src_type": "sqlserver", "src_host": "h",
           "secret_key": "k", "src_database": "d", "src_schema": "dbo", "src_table": "t", "select_list": "a",
           "primary_key": "a", "load_type": "ct", "sink_catalog": "c", "sink_schema": "s", "sink_table": "t",
           "freshness_sla_minutes": None}
    [p] = q.plan_edit([row], {"freshness_sla_minutes": "30"}, bulk=True)
    assert p["changes"] == {"freshness_sla_minutes": [None, 30]} and p["reseed"] is None and not p["errors"]
    discovered = [{"src_database": "d", "src_schema": "dbo", "src_table": "t", "select_list": "a", "primary_key": "a",
                   "load_type": "ct", "src_type": "sqlserver", "src_host": "h", "secret_key": "k"}]
    [r] = q.review_rows(discovered, {"sink_catalog": "c", "sink_schema": "s", "sink_table": "{table}",
                                     "freshness_sla_minutes": 45})
    assert q.control_row_from_review(r, "c")["freshness_sla_minutes"] == 45
    assert "freshness_sla_minutes" in q.CONTROL_INSERT_COLUMNS


# ---- UC-connection reads, write modes, client_attrs -------------------------------------------------

def test_validate_remote_query_rows_need_the_connection_not_host_or_secret():
    remote = _row(src_host="", secret_key="", use_remote_query=True, src_connection="conn")
    assert q.validate_control_row(remote) == []
    assert "src_connection is required when use_remote_query is set" in q.validate_control_row(dict(remote, src_connection=""))
    assert q.validate_control_row(_row(src_host="", src_connection="conn")) == []      # JDBC, host from the connection
    assert "secret_key is required" in q.validate_control_row(_row(secret_key=""))


def test_validate_write_mode_and_client_attrs():
    assert q.validate_control_row(_row(write_mode="append", client_attrs='{"tenant": "Acme", "n": 3}')) == []
    assert "write_mode append supports load_type ct and full, not cursor" in q.validate_control_row(
        _row(write_mode="append", load_type="cursor", cursor_column="rv"))
    assert "write_mode must be merge or append" in q.validate_control_row(_row(write_mode="upsert"))
    errs = " | ".join(q.validate_control_row(_row(client_attrs='{"__x": 1, "v": 2, "ok": [1]}')))
    assert "'__x'" in errs and "'v' is also in select_list" in errs and "'ok': the value must be" in errs
    assert "client_attrs must be a JSON object {column: value}" in q.validate_control_row(_row(client_attrs="[1]"))


def test_control_params_defaults_for_new_fields():
    d = dict(zip(q.CONTROL_INSERT_COLUMNS, q.control_params(_row(write_mode="", client_attrs={"a": "b"}))))
    assert d["write_mode"] == "merge" and d["use_remote_query"] is False and d["client_attrs"] == '{"a": "b"}'
    assert d["load_partitioned"] is False                         # omitted: the column's default, not NULL
    d = dict(zip(q.CONTROL_INSERT_COLUMNS, q.control_params(_row(use_remote_query="true", write_mode="Append"))))
    assert d["use_remote_query"] is True and d["write_mode"] == "append"


def test_plan_edit_new_fields():
    row = dict(ROW, src_connection=None, use_remote_query=False, write_mode="merge", client_attrs=None)
    (p,) = q.plan_edit([row], {"write_mode": "append"}, bulk=False)
    assert any("write_mode can't change on the same sink" in e for e in p["errors"])
    (p,) = q.plan_edit([row], {"write_mode": "append", "sink_table": "t_log"}, bulk=False)
    assert not p["errors"] and p["reseed"] == "sink_changed"
    (p,) = q.plan_edit([row], {"use_remote_query": True, "src_connection": "conn"}, bulk=True)
    assert not p["errors"] and p["reseed"] is None and any("map source types differently" in n for n in p["notes"])
    (p,) = q.plan_edit([dict(row, src_host=None, src_connection="a")], {"src_connection": "b"}, bulk=False)
    assert p["reseed"] == "source_changed"                       # the connection is the source's host
    (p,) = q.plan_edit([dict(row, client_attrs='{"a": 1}')], {"client_attrs": '{ "a" : 1 }'}, bulk=False)
    assert p["changes"] == {}                                    # same object, different spelling
    (p,) = q.plan_edit([row], {"client_attrs": {"tenant": "Acme"}}, bulk=False)
    assert p["changes"]["client_attrs"][1] == '{"tenant": "Acme"}' and any("keep their client_attrs" in n for n in p["notes"])


def test_reseed_flips_clear_status_and_any_pending_append():
    # The job settles a pending append before it looks at the phase: a reseed must drop it.
    for sql in (q.force_reseed_sql(), q.flip_to_seeding_sql()):
        assert "phase = 'seeding'" in sql and "status = 'ok'" in sql
        assert "pending_position = null" in sql and "pending_run_id = null" in sql


def test_tables_page_after_a_cursor_with_an_exact_database():
    sql, params = q.tables_sql("c1", None, "all", 100, database="db1", after=["c1", "db1", "dbo", "t5", 42])
    assert "coalesce(t.src_database, '') = %s" in sql and params == ("c1", "db1", "c1", "db1", "dbo", "t5", 42, 100)
    assert sql.rstrip().endswith("order by t.task_collection, coalesce(t.src_database, ''), t.src_schema, t.src_table, t.id limit %s")


def _engine():
    import importlib.util
    import sys
    from pathlib import Path
    engine_dir = Path(__file__).resolve().parents[2] / "src" / "lakefed_ingest"
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    spec = importlib.util.spec_from_file_location("engine_state_store", engine_dir / "state_store.py")
    engine = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = engine
    spec.loader.exec_module(engine)
    return engine


def test_the_database_source_in_sql_is_the_engines():
    engine = _engine()
    assert q.database_source_sql("t") == engine.database_source_sql("t")
    for row in ({"src_host": "Srv", "src_port": 1433, "secret_key": "k"}, {"src_connection": "conn"},
                {"src_host": "srv", "src_port": None, "secret_key": ""}):
        assert q.database_source(row) == engine.database_source(row)


def test_the_database_source_copies_agree_for_every_source_type_and_port():
    engine = _engine()
    import source_adapters
    assert q.DEFAULT_PORTS == {t: source_adapters.get_adapter(t).default_port for t in source_adapters.SRC_TYPES}
    mysql = {"src_type": "mysql", "src_host": "Srv", "src_port": None, "secret_key": "k"}
    assert q.database_source(mysql) == "srv|3306|k"
    assert q.database_source({"src_type": "mysql", "src_connection": "conn", "src_port": None, "secret_key": None}) \
        == "conn|0|conn"
    for src_type in (None, "", "SQLServer", "oracle", "postgresql", "mysql", "db2", "\toracle", "oracle\n", " oracle "):
        for host in ("Srv", None, ""):
            for connection in (None, "conn", ""):
                for port in (None, 0, 1433, 1521, 5432, 3306, 3307):
                    for secret in (None, "", "k"):
                        row = {"src_type": src_type, "src_host": host, "src_connection": connection,
                               "src_port": port, "secret_key": secret}
                        assert q.database_source(row) == engine.database_source(row), row
    assert "database_check dc" in q.DATABASE_CHECK_JOIN and "dc.source = " + q.database_source_sql("t") in q.DATABASE_CHECK_JOIN


def test_lists_of_group_runs_leave_out_the_check_jobs_own_records():
    for sql in (q.runs_sql(None)[0], q.runs_sql("c1")[0], q.group_runs_before_sql(),
                q.last_runs_sql(), q.running_progress_sql(), q.collection_run_stats_sql()):
        assert q.not_a_check() in sql or q.not_a_check("x") in sql
    assert q.not_a_check() not in q.group_runs_sql()      # job runs place the checks themselves


def test_a_jobs_table_lists_count_the_database_check_stamp_in_each_tables_last_success():
    for sql in (q.job_tables_sql(), q.job_target_sources_sql()):
        assert f"max({q.LAST_SUCCESS}) as last_success_at" in sql and "cp.phase, cp.last_success_at" not in sql
        assert "database_check dc" in sql


def test_run_time_medians_and_percentiles_leave_out_cancelled_runs():
    """A cancelled run didn't finish, so how long it ran is no run time for the baselines."""
    for sql in (q.running_progress_sql(), q.running_runs_sql(), q.collection_run_stats_sql()):
        assert "status not in ('skipped', 'cancelled')" in sql


def test_cancel_requested_runs_are_read_by_run_id_and_the_request_column():
    sql = q.cancel_requested_runs_sql()
    assert "run_id = any(%s)" in sql and "cancel_requested_at is not null" in sql
