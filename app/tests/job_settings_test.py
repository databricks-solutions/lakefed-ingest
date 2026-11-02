"""An ingestion job's settings (job_settings.py): read from all of its rows, edited for all of them at once, with
target names and identity values written as templates over {server}, {database}, {schema} and {table}."""
import json

import job_settings as js


def row(id=1, database="db_000101", schema="dbo", table="OrderLine", **over):
    base = dict(id=id, task_collection="cust_c01", src_type="sqlserver", src_host=None, src_port=None,
                secret_key=None, src_connection="sql_01", use_remote_query=True, src_database=database,
                src_schema=schema, src_table=table, select_list="id,qty", primary_key="id", cursor_column=None,
                load_type="ct", write_mode="append", sink_catalog="bronze", sink_schema="extraction",
                sink_table=table.lower(), client_id=database,
                client_attrs=json.dumps({"source_server": "sql_01", "source_database": database}),
                freshness_sla_minutes=60, jdbc_options=None, task_enabled=True)
    return {**base, **over}


FLEET = [row(1), row(2, table="Invoice"), row(3, database="db_000102"), row(4, database="db_000102", table="Invoice"),
         row(5, schema="E1", table="Child_C_View", load_type="full")]


def test_the_server_is_named_as_the_tree_names_it_the_connection_or_host_and_port():
    assert js.server_of(row(src_connection="SQL_01")) == "sql_01"
    assert js.server_of(row(src_connection=None, use_remote_query=False, src_host="SQL-1.corp", src_port=None)) == "sql-1.corp:1433"
    assert js.server_of(row(src_connection=None, use_remote_query=False, src_host="sql-1.corp", src_port=14330)) == "sql-1.corp:14330"


def test_a_template_fills_each_tag_and_target_names_are_lower_case_identifiers():
    r = row()
    assert js.render("{server}/{database}", r) == "sql_01/db_000101"
    assert js.render("{schema}.{table}", r) == "dbo.OrderLine"
    assert js.render("{schema}_{table}", r, target=True) == "dbo_orderline"
    assert js.render("{table}", row(table="Order-Line"), target=True) == "order_line"


def test_an_existing_jobs_names_come_out_as_they_always_have_and_new_jobs_follow_unity_catalog():
    tables = ("Order-Lines", "Sales (2024)", "a--b")
    assert [js.render("{table}", row(table=t), target=True) for t in tables] == ["order_lines", "sales_2024", "a_b"]
    assert [js.render("{table}", row(table=t), target=True, unity_catalog=True) for t in tables] == [
        "order-lines", "sales_(2024)", "a--b"]


def test_a_jobs_naming_rule_is_an_option_and_the_read_reports_it():
    assert js.unity_catalog_of({js.TARGET_NAMES_OPTION: js.UNITY_CATALOG_NAMES}) is True
    assert js.unity_catalog_of({}) is False and js.unity_catalog_of(None) is False
    assert js.read([row()], unity_catalog=True)["unity_catalog"] == {"value": True}


def test_two_tables_on_one_target_are_refused_under_the_rule_the_job_uses():
    rows = [row(1, table="Order-Lines", client_id=None), row(2, table="order_lines", client_id=None)]
    assert js.check(rows, {"sink_table": "{table}"}, unity_catalog=True) == []
    (error,) = js.check(rows, {"sink_table": "{table}"})
    assert error.startswith("1 targets would be written by more than one table (e.g. bronze.extraction.order_lines)")


def test_an_existing_jobs_added_databases_take_its_names_and_a_unity_catalog_job_its_own():
    entries = [{"line": 2, "server": "sql_01", "database": "db_000199", "enabled": True}]
    older = [row(1, table="Order-Lines", sink_table="order_lines"),
             row(2, table="Sales (2024)", sink_table="sales_2024"), row(3, table="a--b", sink_table="a_b")]
    plan = js.plan_databases(older, entries, "Job", 4)
    assert plan["errors"] == []
    assert sorted(r["sink_table"] for r in plan["new"]) == ["a_b", "order_lines", "sales_2024"]
    named = [row(1, table="Order-Lines", sink_table="order-lines"),
             row(2, table="Sales (2024)", sink_table="sales_(2024)"), row(3, table="a--b", sink_table="a--b")]
    plan = js.plan_databases(named, entries, "Job", 4, unity_catalog=True)
    assert plan["errors"] == []
    assert sorted(r["sink_table"] for r in plan["new"]) == ["a--b", "order-lines", "sales_(2024)"]


def test_typed_text_in_a_target_name_must_not_hold_a_character_unity_catalog_refuses():
    base = {"sink_catalog": "bronze", "sink_schema": "extraction"}
    refused = ("target table {!r} has a character Unity Catalog refuses (a space, period, slash or control character): "
               "take it out")
    assert js.typed_name_errors({**base, "sink_table": "{table}"}, unity_catalog=True) == []
    assert js.typed_name_errors({**base, "sink_table": "Sales 2024"}, unity_catalog=True) == [
        refused.format("Sales 2024")]
    assert js.typed_name_errors({**base, "sink_table": "{table}.raw"}, unity_catalog=True) == [
        refused.format("{table}.raw")]
    assert js.typed_name_errors({**base, "sink_table": "Sales 2024"}, unity_catalog=False) == []


def test_an_older_job_with_only_safe_names_adds_databases_under_the_older_rule():
    older = [row(1, table="Order-Lines", sink_table="order_lines")]
    entries = [{"line": 2, "server": "sql_01", "database": "db_000199", "enabled": True}]
    plan = js.plan_databases(older, entries, "Job", 4, unity_catalog=js.unity_catalog_of({}))
    assert plan["errors"] == [] and [r["sink_table"] for r in plan["new"]] == ["order_lines"]


def test_unknown_tags_are_named_and_fan_in_refuses_server_and_database_in_target_names():
    assert js.template_errors("{server}/{db}", "identity column source_key") == [
        "identity column source_key: unknown tag {db}; valid tags are {server}, {database}, {schema}, {table}"]
    assert js.template_errors("{database}_{table}", "target table", fan_in=True) == [
        "target table: {database} gives every database its own table, so fan-in can't use it"]
    assert js.template_errors("{database}_{table}", "target table", fan_in=False) == []


def test_a_jobs_settings_are_what_its_rows_share_with_write_modes_apart_for_change_tracked_and_full_tables():
    s = js.read(FLEET)
    assert s["fan_in"] == {"value": True}
    assert s["write_mode_changes"] == {"value": "append"} and s["write_mode_full"] == {"value": "append"}
    assert s["freshness_sla_minutes"] == {"value": 60}
    assert (s["sink_catalog"], s["sink_schema"], s["sink_table"]) == (
        {"value": "bronze"}, {"value": "extraction"}, {"value": "{table}"})
    assert s["client_id"] == {"value": "{database}"}
    assert s["identity"] == {"value": {"source_server": "{server}", "source_database": "{database}"}}


def test_a_new_jobs_key_reads_back_as_its_server_and_database():
    keyed = [row(1, client_id="sql_01_db_000101"), row(2, table="Invoice", client_id="sql_01_db_000101"),
             row(3, database="db_000102", client_id="sql_01_db_000102")]
    assert js.read(keyed)["client_id"] == {"value": "{server}_{database}"}


def test_a_setting_the_rows_differ_on_is_mixed_with_its_counts():
    s = js.read(FLEET[:4] + [row(6, freshness_sla_minutes=240)])
    assert s["freshness_sla_minutes"] == {"mixed": {"60": 4, "240": 1}}


def test_a_jobs_settings_with_no_rows_are_empty():
    s = js.read([])
    assert s["fan_in"] == {"value": None} and s["identity"] == {"value": {}}


def test_an_edit_plans_each_rows_new_values_from_the_templates():
    changes = js.plan(FLEET, {"sink_table": "{schema}_{table}", "identity": {"source_key": "{server}/{database}"}})
    assert changes[1] == {"sink_table": "dbo_orderline", "client_attrs": json.dumps({"source_key": "sql_01/db_000101"})}
    assert changes[5]["sink_table"] == "e1_child_c_view"


def test_write_modes_go_to_their_own_tables_and_a_cursor_table_always_merges():
    rows = [row(1), row(2, load_type="full", table="V"), row(3, load_type="cursor", table="C", write_mode="merge")]
    changes = js.plan(rows, {"write_mode_changes": "merge", "write_mode_full": "merge"})
    assert changes[1] == {"write_mode": "merge"} and changes[2] == {"write_mode": "merge"} and 3 not in changes


def test_turning_fan_in_off_clears_client_id_and_on_fills_it_from_its_template():
    off = js.plan(FLEET[:1], {"fan_in": False})
    assert off[1]["client_id"] is None
    own = [row(1, client_id=None, client_attrs=None)]
    assert js.plan(own, {"fan_in": True, "client_id": "{server}:{database}"})[1]["client_id"] == "sql_01:db_000101"


def test_turning_fan_in_on_with_no_client_id_takes_the_server_and_database_key_new_jobs_get():
    a = row(1, client_id=None, client_attrs=None, src_connection="sql_a", database="sales", table="Orders")
    b = row(2, client_id=None, client_attrs=None, src_connection="sql_b", database="sales", table="Orders")
    planned = js.plan([a, b], {"fan_in": True})
    assert planned[1]["client_id"] == "sql_a_sales" and planned[2]["client_id"] == "sql_b_sales"
    assert js.default_client_id(False) == js.SHARED_CLIENT_ID


def test_edit_offers_the_server_and_database_key_when_the_job_has_none():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "src" / "frontend" / "src" / "pages" / "JobEdit.tsx").read_text()
    assert f'client_id: value(s.client_id) ?? "{js.SHARED_CLIENT_ID}"' in page


def test_a_job_already_sharing_its_tables_keeps_its_database_key():
    assert js.default_client_id(True) == js.LEGACY_CLIENT_ID
    assert js.plan(FLEET[:1], {"fan_in": True}) == {}


def test_the_sql_edit_turning_fan_in_on_fills_the_server_and_database_key():
    import job_settings_sql as jss
    p = jss.Params()
    off = {"fan_in": {"value": False}, "client_id": {"value": None}}
    sql = jss._new_values({"fan_in": True}, off, p)["client_id"]
    assert jss.TAG_SQL["{server}"] in sql and jss.TAG_SQL["{database}"] in sql and "_" in p.values()
    p = jss.Params()
    on = {"fan_in": {"value": True}, "client_id": {"value": None}}
    assert jss.TAG_SQL["{server}"] not in jss._new_values({"fan_in": True}, on, p)["client_id"]


def test_an_unchanged_setting_plans_nothing():
    assert js.plan(FLEET, {"sink_table": "{table}", "freshness_sla_minutes": 60}) == {}


def test_two_tables_on_one_target_without_fan_in_is_an_error_naming_the_fix():
    errors = js.check(FLEET[:4], {"fan_in": False})
    assert errors == ["2 targets would be written by more than one table (e.g. bronze.extraction.orderline): "
                      "add {database} to the target names, or turn fan-in on"]


def test_check_reports_template_errors_by_setting():
    errors = js.check(FLEET, {"sink_schema": "bronze_{database}", "identity": {"src": "{host}"}})
    assert errors == ["target schema: {database} gives every database its own table, so fan-in can't use it",
                      "identity column src: unknown tag {host}; valid tags are {server}, {database}, {schema}, {table}"]


CUSTOMER_CSV = ("listID,serverName,databaseName,displayName,isActive\n"
            "000103,sql_01,db_000103,Fabrikam,True\n"
            "000104,sql_01,db_000104,Tailspin,False\n"
            "000101,sql_01,db_000101,Northwind,True\n")
CUSTOMER_COLUMNS = {"server": "serverName", "database": "databaseName", "enabled": "isActive"}


def test_a_databases_file_is_read_through_its_column_match_and_extra_columns_are_ignored():
    entries, errors = js.read_databases(CUSTOMER_CSV, CUSTOMER_COLUMNS)
    assert errors == [] and entries[0] == {"line": 2, "server": "sql_01", "database": "db_000103", "enabled": True}
    assert entries[1]["enabled"] is False


def test_a_databases_file_without_its_matched_columns_is_refused_by_name():
    _, errors = js.read_databases("server,db\nx,y\n", {"server": "server", "database": "databaseName"})
    assert errors == ["the file has no column databaseName (matched to database)"]


def test_each_new_database_gets_every_table_of_the_job_with_its_settings_filled_in():
    plan = js.plan_databases(FLEET, [{"line": 2, "server": "sql_01", "database": "db_000103", "enabled": True}],
                             job_name="Customer", groups_per_server=1)
    assert plan["errors"] == [] and len(plan["new"]) == 3      # the job's 3 tables: OrderLine, Invoice, the view
    new = {r["src_table"]: r for r in plan["new"]}
    order = new["OrderLine"]
    assert (order["src_connection"], order["use_remote_query"], order["src_database"]) == ("sql_01", True, "db_000103")
    assert (order["sink_table"], order["client_id"], order["write_mode"], order["load_type"]) == (
        "orderline", "db_000103", "append", "ct")
    assert json.loads(order["client_attrs"]) == {"source_server": "sql_01", "source_database": "db_000103"}
    assert new["Child_C_View"]["load_type"] == "full"
    assert order["task_collection"] == "cust_c01"           # the server's existing group


def test_a_dotless_host_the_job_reads_is_that_host_not_a_new_connection():
    hosted = [row(i, src_connection=None, use_remote_query=False, src_host="sqlprod01", src_port=None,
                  secret_key="creds-db_000101", **({"table": t} if t else {})) for i, t in ((1, None), (2, "Invoice"))]
    plan = js.plan_databases(hosted, [{"line": 2, "server": "sqlprod01", "database": "newdb", "enabled": True}],
                             job_name="Customer", groups_per_server=1)
    assert plan["errors"] == [] and plan["new_groups"] == []
    new = plan["new"][0]
    assert (new["src_host"], new["use_remote_query"], new["src_connection"]) == ("sqlprod01", False, None)
    assert new["secret_key"] == "creds-newdb" and new["task_collection"] == "cust_c01"


def test_a_dotless_name_the_job_does_not_read_as_a_host_is_still_a_connection_name():
    assert js._server_key("sql_02", "sqlserver", {"sql_01": {}}) == "sql_02"
    assert js._server_key("sqlprod01", "sqlserver", {"sqlprod01": {}, "sqlprod01:1433": {}}) == "sqlprod01"
    assert js._server_key("sqlprod01", "sqlserver", {"sqlprod01:1433": {}}) == "sqlprod01:1433"
    assert js._server_key("sql.corp", "sqlserver") == "sql.corp:1433"


def test_a_new_server_gets_its_own_groups_its_databases_spread_evenly():
    entries = [{"line": i, "server": "sql_02", "database": f"db_9{i:02d}", "enabled": True} for i in range(20)]
    plan = js.plan_databases(FLEET, entries, job_name="Customer", groups_per_server=4)
    groups = {r["task_collection"] for r in plan["new"]}
    assert groups == {"customer_sql_02_c01", "customer_sql_02_c02", "customer_sql_02_c03", "customer_sql_02_c04"}
    assert plan["new_groups"] == sorted(groups)
    per_group = {}
    for r in plan["new"]:
        per_group.setdefault(r["task_collection"], set()).add(r["src_database"])
    assert sorted(len(databases) for databases in per_group.values()) == [5, 5, 5, 5]
    again = js.plan_databases(FLEET, entries[:5], job_name="Customer", groups_per_server=4)
    first = {r["src_database"]: r["task_collection"] for r in plan["new"]}
    assert all(first[r["src_database"]] == r["task_collection"] for r in again["new"])


def test_a_database_already_in_the_job_is_left_and_its_enabled_flag_follows_the_file():
    entries = [{"line": 2, "server": "sql_01", "database": "db_000102", "enabled": False}]
    plan = js.plan_databases(FLEET, entries, job_name="Customer", groups_per_server=1)
    assert plan["new"] == [] and plan["disable"] == [3, 4] and plan["enable"] == []


def test_with_the_full_list_a_database_missing_from_the_file_is_disabled_never_deleted():
    entries = [{"line": 2, "server": "sql_01", "database": "db_000101", "enabled": True}]
    plan = js.plan_databases(FLEET, entries, job_name="Customer", groups_per_server=1, full_list=True)
    assert plan["disable"] == [3, 4]


def test_a_new_jdbc_server_without_a_known_secret_is_an_error_for_its_line():
    plan = js.plan_databases(FLEET, [{"line": 7, "server": "sql-9.corp:1433", "database": "D", "enabled": True}],
                             job_name="Customer", groups_per_server=1)
    assert plan["errors"] == ["line 7: server sql-9.corp:1433 is new to this job and is not a UC connection name; "
                              "add one of its databases by Import CSV first, with its secret"]


def test_a_job_without_tables_cannot_take_databases_yet():
    plan = js.plan_databases([], [{"line": 2, "server": "s", "database": "d", "enabled": True}], job_name="J",
                             groups_per_server=1)
    assert plan["errors"] == ["the job has no tables yet: import one database's tables first (Import CSV)"]


def _jdbc(id, database, **over):
    return row(id, database=database, **{"src_connection": None, "use_remote_query": False, "src_host": "sql-1.corp",
                                         "src_port": 1433, "secret_key": f"kv_{database}", **over})


def test_a_new_database_on_a_jdbc_server_follows_the_servers_secret_names():
    rows = [_jdbc(1, "Sales"), _jdbc(2, "Stock")]
    plan = js.plan_databases(rows, [{"line": 2, "server": "sql-1.corp", "database": "Hr", "enabled": True}],
                             job_name="J", groups_per_server=1)
    assert plan["errors"] == [] and plan["new"][0]["secret_key"] == "kv_Hr"
    as_shown = js.plan_databases(rows, [{"line": 2, "server": "SQL-1.corp:1433", "database": "Hr", "enabled": True}],
                                 job_name="J", groups_per_server=1)
    assert as_shown["errors"] == [] and len(as_shown["new"]) == 1
    shared = [_jdbc(1, "Sales", secret_key="sql1"), _jdbc(2, "Stock", secret_key="sql1")]
    plan = js.plan_databases(shared, [{"line": 2, "server": "sql-1.corp", "database": "Hr", "enabled": True}],
                             job_name="J", groups_per_server=1)
    assert plan["new"][0]["secret_key"] == "sql1"


def test_a_database_listed_twice_is_added_once_and_listed_twice_differently_is_an_error():
    twice = [{"line": 2, "server": "sql_01", "database": "db_9", "enabled": True},
             {"line": 3, "server": "SQL_01", "database": "db_9", "enabled": True}]
    assert len(js.plan_databases(FLEET, twice, job_name="K", groups_per_server=1)["new"]) == 3
    twice[1]["enabled"] = False
    assert js.plan_databases(FLEET, twice, job_name="K", groups_per_server=1)["errors"] == [
        "line 3: sql_01 / db_9 is listed again with another enabled value"]


def test_a_new_row_that_would_break_a_control_row_rule_is_an_error_for_its_line():
    plan = js.plan_databases(FLEET, [{"line": 4, "server": "sql_01", "database": "New DB", "enabled": True}],
                             job_name="K", groups_per_server=1)
    assert plan["errors"] and plan["errors"][0].startswith("line 4: New DB: ")


def test_an_identity_column_whose_values_follow_no_template_is_named_so_the_page_can_show_its_row():
    # Every table has invoice_id, with values no template matches: one mixed entry naming it, covering every table.
    assert js.differing_identity_columns({"mixed": {"invoice_id": 100000}}, 100000) == ["invoice_id"]
    # Tables whose identity columns themselves differ: one entry per set of names, none covering every table.
    assert js.differing_identity_columns({"mixed": {"a, b": 60, "a": 40}}, 100) == []
    # The same names in another order collapse to one entry, which is still a set of names, not a column.
    assert js.differing_identity_columns({"mixed": {"a, b": 100}}, 100) == []
    assert js.differing_identity_columns({"value": {"a": "{database}"}}, 100) == []


def test_a_new_database_on_a_server_of_the_job_joins_its_lightest_group():
    fleet = FLEET + [row(11, database="db_000201", task_collection="cust_c02"),
                     row(12, database="db_000201", table="Invoice", task_collection="cust_c02")]
    # cust_c01 has 5 tables, cust_c02 2: the new database's 3 tables go to cust_c02, the next database's to cust_c01
    # (now 5 against 5, the first by name).
    entries = [{"line": 2, "server": "sql_01", "database": "db_000301", "enabled": True},
               {"line": 3, "server": "sql_01", "database": "db_000302", "enabled": True}]
    plan = js.plan_databases(fleet, entries, job_name="Customer", groups_per_server=1)
    placed = {r["src_database"]: r["task_collection"] for r in plan["new"]}
    assert placed == {"db_000301": "cust_c02", "db_000302": "cust_c01"}


def test_an_old_job_opens_and_saves_with_its_names_unchanged_whether_they_were_lower_case_or_not():
    for sink_schema, sink_table in (("bronze", "orderline"), ("Bronze", "Order-Line")):
        rows = [row(sink_schema=sink_schema, sink_table=sink_table)]
        opened = js.read(rows)
        assert "lowercase" not in opened
        saved = {name: opened[name]["value"] for name in ("sink_catalog", "sink_schema", "sink_table")}
        assert js.plan(rows, saved) == {}
        assert js.plan(rows, {**saved, "lowercase": False}) == {}
        assert js.plan(rows, {**saved, "lowercase": True}) == {}
