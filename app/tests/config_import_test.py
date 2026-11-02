"""The configuration import (config_import.py): a CSV of what to ingest into one ingestion group, whose settings come
from the group's rows (decision A: no table of groups)."""
import json

import config_import as ci

SETTINGS = dict(src_type="sqlserver", use_remote_query=True, load_type="ct", write_mode="merge", fan_in=False,
                sink_catalog="main", sink_schema="bronze_{database}", sink_table="{schema}_{table}",
                freshness_sla_minutes=60, jdbc_options=None)
CSV = ("src_connection,src_database,src_schema,src_table,select_list,primary_key\n"
       "sql_a,Sales,dbo,Orders,\"id,amount\",id\n"
       "sql_a,Sales,dbo,Lines,\"id,qty\",id\n")


def row(**over):
    base = dict(id=1, task_collection="g1", src_type="sqlserver", src_host=None, src_port=None, secret_key=None,
                src_connection="sql_a", use_remote_query=True, src_database="Sales", src_schema="dbo",
                src_table="Orders", select_list="id,amount", primary_key="id", cursor_column=None, load_type="ct",
                write_mode="merge", sink_catalog="main", sink_schema="bronze_sales", sink_table="dbo_orders",
                client_id=None, client_attrs=None, freshness_sla_minutes=60, jdbc_options=None, task_enabled=True)
    return {**base, **over}


def test_a_typed_target_name_keeps_its_text_and_is_refused_under_unity_catalog():
    typed = dict(SETTINGS, sink_table="Sales 2024")
    refused = ci.plan_import(CSV, "g1", typed, group_rows=[], unity_catalog=True)
    assert refused["lines"] == [] and "Unity Catalog refuses" in refused["errors"][0]
    kept = ci.plan_import(CSV, "g1", typed, group_rows=[])
    assert kept["errors"] == [] and kept["lines"][0]["row"]["sink_table"] == "Sales 2024"


def test_each_line_and_the_groups_settings_make_one_control_row():
    found = ci.plan_import(CSV, "g1", SETTINGS, group_rows=[])
    assert found["errors"] == [] and [line["status"] for line in found["lines"]] == ["valid", "valid"]
    first = found["lines"][0]["row"]
    assert (first["task_collection"], first["src_connection"], first["use_remote_query"], first["load_type"]) == (
        "g1", "sql_a", True, "ct")
    assert (first["sink_catalog"], first["sink_schema"], first["sink_table"]) == ("main", "bronze_sales", "dbo_orders")
    assert first["freshness_sla_minutes"] == 60 and first["client_id"] is None


def test_an_unknown_column_refuses_the_whole_file_by_name():
    found = ci.plan_import("src_connection,src_database,src_schema,src_table,load_type\nsql_a,S,dbo,T,full\n", "g1",
                           SETTINGS, group_rows=[])
    assert found["lines"] == [] and found["errors"] == ["unknown column load_type: a group setting, set at import"]


def test_a_line_is_checked_as_add_table_checks_a_row():
    text = "src_connection,src_database,src_schema,src_table,select_list\nsql_a,Sales,dbo,Orders,\"id,amount\"\n"
    line = ci.plan_import(text, "g1", SETTINGS, group_rows=[])["lines"][0]
    assert line["status"] == "invalid" and "primary_key is required for load_type ct" in line["errors"]


def test_the_row_checks_can_be_left_to_the_caller_and_are_on_by_default():
    text = "src_connection,src_database,src_schema,src_table,select_list,primary_key\nsql_a,Sales,dbo,,\"id\",id\n"
    checked = ci.plan_import(text, "g1", SETTINGS, group_rows=[])["lines"][0]
    unchecked = ci.plan_import(text, "g1", SETTINGS, group_rows=[], check_rows=False)["lines"][0]
    assert checked["errors"] == ["src_table is required"] and checked["status"] == "invalid"
    assert unchecked["errors"] == [] and unchecked["status"] == "valid"


def test_a_line_for_another_group_and_a_duplicate_sink_are_refused():
    text = CSV + "sql_a,Sales,dbo,Orders,\"id,amount\",id\n"
    lines = ci.plan_import(text, "g1", SETTINGS, group_rows=[])["lines"]
    assert lines[2]["errors"] == ["line 2 already writes main.bronze_sales.dbo_orders"]
    other = "task_collection," + CSV.splitlines()[0] + "\ng2," + CSV.splitlines()[1] + "\n"
    assert ci.plan_import(other, "g1", SETTINGS, group_rows=[])["lines"][0]["errors"] == [
        "task_collection is g2, but this import is into g1"]


def test_a_fan_in_group_takes_each_lines_client_id_and_identity_columns():
    fan_in = dict(SETTINGS, fan_in=True, sink_schema="bronze", sink_table="{table}")
    text = ("src_connection,src_database,src_schema,src_table,select_list,primary_key,client_id,attr:business_unit\n"
            "sql_a,Unit1,dbo,Orders,\"id,amount\",id,u1,North\n"
            "sql_a,Unit2,dbo,Orders,\"id,amount\",id,u2,South\n"
            "sql_a,Unit3,dbo,Orders,\"id,amount\",id,,West\n")
    lines = ci.plan_import(text, "g1", fan_in, group_rows=[])["lines"]
    assert [(line["row"]["sink_table"], line["row"]["client_id"]) for line in lines[:2]] == [("orders", "u1"),
                                                                                              ("orders", "u2")]
    assert json.loads(lines[0]["row"]["client_attrs"]) == {"business_unit": "North"}
    assert lines[2]["errors"] == ["client_id is required: this group fans in to shared tables"]
    plain = ci.plan_import(text, "g1", SETTINGS, group_rows=[])["lines"][0]
    assert "client_id must be blank: this group does not fan in" in plain["errors"]


def test_a_groups_settings_are_the_values_its_rows_share_or_mixed():
    rows = [row(), row(id=2, src_table="Lines", sink_table="dbo_lines", freshness_sla_minutes=30),
            row(id=3, src_database="Hr", src_table="People", sink_schema="bronze_hr", sink_table="dbo_people")]
    found = ci.group_settings(rows)
    assert found["load_type"] == {"value": "ct"} and found["sink_schema"] == {"value": "bronze_{database}"}
    assert found["sink_table"] == {"value": "{schema}_{table}"} and found["fan_in"] == {"value": False}
    assert found["freshness_sla_minutes"] == {"mixed": {"60": 2, "30": 1}}
    assert ci.group_settings([]) == {name: {"value": None} for name in ci.SETTINGS}


def test_importing_into_a_group_takes_its_shared_settings_and_names_mixed_ones_not_given():
    rows = [row(), row(id=2, src_table="Lines", sink_table="dbo_lines", freshness_sla_minutes=30)]
    found = ci.plan_import(CSV, "g1", {}, group_rows=rows)
    assert found["errors"] == ["freshness_sla_minutes differs across the group's rows (60, 30): set it for this import"]
    taken = ci.plan_import(CSV, "g1", {"freshness_sla_minutes": 45}, group_rows=rows)
    assert taken["errors"] == [] and taken["lines"][0]["row"]["freshness_sla_minutes"] == 45
    assert taken["lines"][0]["row"]["sink_schema"] == "bronze_sales"
    new = ci.plan_import(CSV, "g9", {}, group_rows=[])
    assert "set load_type for this new group" in new["errors"]


def test_a_large_file_is_refused_with_its_limit():
    text = CSV.splitlines()[0] + "\n" + "".join(f"sql_a,S,dbo,T{i},id,id\n" for i in range(ci.MAX_LINES + 1))
    assert ci.plan_import(text, "g1", SETTINGS, group_rows=[])["errors"] == [
        f"more than {ci.MAX_LINES:,} lines: at most {ci.MAX_LINES:,} in one import"]


def test_the_cap_is_checked_as_each_line_is_read_so_the_rest_of_the_file_is_not_read():
    read = []

    def rows():
        for number in range(1_000_000):
            read.append(number)
            yield {"src_table": f"T{number}"}

    assert ci._lines_within(rows(), 5) is None
    assert len(read) == 6
    assert ci._lines_within([{"src_table": ""}, {"src_table": "a"}], 1) == [{"src_table": "a"}]
    assert len(ci._lines_within(({"src_table": f"T{n}"} for n in range(5)), 5)) == 5


def test_an_exported_group_imports_back_into_the_same_rows():
    rows = [row(client_id="u1", client_attrs=json.dumps({"business_unit": "North"}), sink_schema="bronze",
                sink_table="orders"),
            row(id=2, src_database="Unit2", client_id="u2", client_attrs=json.dumps({"business_unit": "South"}),
                sink_schema="bronze", sink_table="orders")]
    text = ci.export_csv(rows)
    assert text.splitlines()[0] == ("src_connection,src_database,src_schema,src_table,select_list,primary_key,"
                                    "client_id,attr:business_unit")
    found = ci.plan_import(text, "g1", {}, group_rows=rows)
    assert found["errors"] == []
    keys = ("src_connection", "src_database", "src_schema", "src_table", "select_list", "primary_key", "load_type",
            "write_mode", "sink_catalog", "sink_schema", "sink_table", "client_id", "freshness_sla_minutes")
    assert [{k: line["row"][k] for k in keys} for line in found["lines"]] == [{k: r[k] for k in keys} for r in rows]
    assert [json.loads(line["row"]["client_attrs"]) for line in found["lines"]] == [{"business_unit": "North"},
                                                                                     {"business_unit": "South"}]


def test_a_retired_lowercase_setting_is_ignored_on_import_and_an_exported_old_job_imports_back_with_its_names():
    for sink_schema, sink_table in (("bronze", "orders"), ("Bronze", "Order-Line")):
        rows = [row(sink_schema=sink_schema, sink_table=sink_table)]
        text = ci.export_csv(rows)
        assert "lowercase" not in text.splitlines()[0]
        plain = ci.plan_import(text, "g1", {}, group_rows=rows)
        retired = ci.plan_import(text, "g1", {"lowercase": "no"}, group_rows=rows)
        assert retired["errors"] == [] and retired["lines"] == plain["lines"]
        assert [(line["row"]["sink_schema"], line["row"]["sink_table"]) for line in retired["lines"]] == [
            (sink_schema, sink_table)]


def test_a_sink_template_is_found_when_a_database_name_is_part_of_the_schema_name():
    rows = [row(src_database="Sales", src_schema="Sales_DB", sink_schema="sales_sales_db"),
            row(id=2, src_database="HR", src_schema="HR_DB", sink_schema="hr_hr_db")]
    assert ci.group_settings(rows)["sink_schema"] == {"value": "{database}_{schema}"}


def test_a_line_read_through_the_connection_gives_no_host_port_or_secret():
    text = ("src_connection,src_host,src_port,secret_key,src_database,src_schema,src_table,select_list,primary_key\n"
            "sql_a,db.example,1433,scope-key,Sales,dbo,Orders,id,id\n")
    errors = ci.plan_import(text, "g1", SETTINGS, group_rows=[])["lines"][0]["errors"]
    assert [e for e in errors if "must be blank" in e] == [
        f"{c} must be blank: this group reads through the UC connection" for c in ("src_host", "src_port", "secret_key")]


def test_a_port_must_be_a_number_and_cells_must_fit_the_header():
    jdbc = dict(SETTINGS, use_remote_query=False)
    text = ("src_host,src_port,secret_key,src_database,src_schema,src_table,select_list,primary_key\n"
            "db.example,abc,k,Sales,dbo,Orders,id,id\n"
            "db.example,1433,k,Sales,dbo,Lines,id,id,surplus\n")
    lines = ci.plan_import(text, "g1", jdbc, group_rows=[])["lines"]
    assert "src_port must be a whole number from 1 to 65535" in lines[0]["errors"]
    assert lines[1]["errors"] == ["this line has more cells than the header has columns"]
    twice = "src_connection,src_table,src_database,src_schema,src_table\nsql_a,A,S,dbo,B\n"
    assert ci.plan_import(twice, "g1", SETTINGS, group_rows=[])["errors"] == ["column src_table is given twice"]


def test_identity_cells_are_kept_as_written_and_a_blank_one_is_null():
    fan_in = dict(SETTINGS, fan_in=True, sink_schema="bronze", sink_table="{table}")
    text = ("src_connection,src_database,src_schema,src_table,select_list,primary_key,client_id,attr:label,attr:tenant\n"
            "sql_a,Unit1,dbo,Orders,id,id,u1, ACME ,\n")
    found = ci.plan_import(text, "g1", fan_in, group_rows=[])["lines"][0]["row"]
    assert json.loads(found["client_attrs"]) == {"label": " ACME ", "tenant": None}
    exported = ci.export_csv([dict(row(), client_id="u1", client_attrs=found["client_attrs"])])
    assert exported.splitlines()[1].endswith(",u1, ACME ,")
