"""A job made from a discovery (New ingestion job, Add tables, re-discovery) loads a large table partitioned, as Bulk
import's review recommends: a table of at least queries.DEFAULT_PARTITION_THRESHOLD_MB with a partition candidate
gets load_partitioned on that column in partitions of DEFAULT_PARTITION_SIZE_MB. The rows reach new_job.plan as the
discovery CSV (DISCOVERY_COLUMNS); queries.source_rows_sql fills the partition columns (partition_recommendation_pg_test)."""
import new_job as nj
import queries as q

PLAIN = {"sink_catalog": "main", "sink_schema": "s", "sink_table": "{database}_{table}"}


def _discovered(table, partitioned=False, column="id"):
    fields = {"src_connection": "sql_a", "src_database": "sales", "src_schema": "dbo", "src_table": table,
              "select_list": "id, amount", "primary_key": "id", "load_type": "full"}
    if partitioned:
        fields.update(load_partitioned="true", partition_col=column, partition_size_mb=str(q.DEFAULT_PARTITION_SIZE_MB))
    return fields


def _rows(plan):
    return {line["row"]["src_table"]: (line["status"], line["row"].get("load_partitioned"), line["row"].get("partition_col"),
                                       line["row"].get("partition_size_mb")) for line in plan["lines"]}


def test_a_discovered_large_table_is_planned_partitioned_and_the_others_are_not():
    text = nj.csv_of(list(nj.DISCOVERY_COLUMNS), [_discovered("big", True, "order_id"), _discovered("small")])
    plan = nj.plan("Sales", text, PLAIN, 1)
    assert plan["errors"] == []
    assert _rows(plan) == {"big": ("valid", True, "order_id", q.DEFAULT_PARTITION_SIZE_MB),
                           "small": ("valid", None, None, None)}


def test_a_plain_csv_without_the_partition_columns_plans_no_partitioning():
    text = nj.csv_of(["src_connection", "src_database", "src_schema", "src_table", "select_list", "primary_key"],
                     [_discovered("t1")])
    assert _rows(nj.plan("Sales", text, PLAIN, 1)) == {"t1": ("valid", None, None, None)}


def test_re_discovery_and_add_tables_plan_the_same_partitioning_into_an_existing_job():
    job_rows = [{"id": 1, "task_collection": "sales_c01", "src_type": "sqlserver", "src_connection": "sql_a",
                 "use_remote_query": True, "src_database": "sales", "src_schema": "dbo", "src_table": "old",
                 "select_list": "id", "primary_key": "id", "load_type": "full", "write_mode": "merge",
                 "sink_catalog": "main", "sink_schema": "s", "sink_table": "sales_old", "client_id": None,
                 "client_attrs": None, "freshness_sla_minutes": None, "jdbc_options": None}]
    text = nj.csv_of(list(nj.DISCOVERY_COLUMNS), [_discovered("big", True), _discovered("small")])
    plan = nj.plan_for_job("Sales", text, "full", job_rows)
    rows = _rows(plan)
    assert rows["big"][1:] == (True, "id", q.DEFAULT_PARTITION_SIZE_MB) and rows["small"][1:] == (None, None, None)


def test_a_partitioned_recommendation_without_a_column_is_refused_by_the_row_check():
    broken = dict(_discovered("big", True), partition_col="")
    plan = nj.plan("Sales", nj.csv_of(list(nj.DISCOVERY_COLUMNS), [broken]), PLAIN, 1)
    [line] = plan["lines"]
    assert line["status"] == "invalid" and "partitioned loads need partition_col and partition_size_mb" in line["errors"]


def test_line_columns_numbers_lines_as_the_import_does_and_keeps_extra_cells():
    text = "a,load_type,partition_col,b\n1,ct,id,2\n,,,\n3,,,4,extra\n"
    plain, values = nj._line_columns(text, ("load_type", "partition_col"))
    assert plain == "a,b\n1,2\n,\n3,4,extra\n"           # the blank line stays blank; the extra cell stays
    assert values == {2: {"load_type": "ct", "partition_col": "id"}}   # line 3 (the third with a value) has none
    assert nj._line_load_types(text)[1] == {2: "ct"}


def test_the_defaults_partition_only_large_tables_in_large_partitions():
    assert q.DEFAULT_PARTITION_THRESHOLD_MB == 10240 and q.DEFAULT_PARTITION_SIZE_MB == 2048
    assert q.recommended_partitioning(10240, "id", q.DEFAULT_PARTITION_THRESHOLD_MB, q.DEFAULT_PARTITION_SIZE_MB) == (
        True, "id", 2048)
    assert q.recommended_partitioning(5000, "id", q.DEFAULT_PARTITION_THRESHOLD_MB, q.DEFAULT_PARTITION_SIZE_MB)[0] is False


def test_a_tables_csv_upload_takes_the_partition_columns_per_line():
    text = ("src_connection,src_database,src_schema,src_table,select_list,primary_key,load_partitioned,partition_col,"
            "partition_size_mb\n"
            'sql_a,sales,dbo,big,"id, amount",id,true,id,4096\n'
            'sql_a,sales,dbo,small,"id, amount",id,,,\n')
    read = nj.read_upload(text, "sql_a")
    assert read["errors"] == []
    fields = {line["src_table"]: line["fields"] for line in read["lines"]}
    assert (fields["big"]["load_partitioned"], fields["big"]["partition_col"], fields["big"]["partition_size_mb"]) == (
        "true", "id", "4096")
    plan = nj.plan("Sales", nj.csv_of(read["columns"], [line["fields"] for line in read["lines"]]), PLAIN, 1)
    assert _rows(plan) == {"big": ("valid", True, "id", 4096), "small": ("valid", None, None, None)}
