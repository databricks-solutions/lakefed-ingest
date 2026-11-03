"""The app spreads a database too big for one group over several groups by itself (placement.py), and keeps every
database that fits whole, as before. Its tables are cut, in name order, into parts that differ by at most one table,
each part into one of the groups with the fewest tables; the same tables always land the same way. Databases added to
a job later do the same over their server's lightest groups, and a regroup moves a split database part by part.
Plain: no database."""
import pytest

import by_database
import new_job as nj
import placement

HEAD = "src_connection,src_database,src_schema,src_table,select_list,primary_key\n"
SETTINGS = {"fan_in": True, "sink_catalog": "main", "sink_schema": "s", "sink_table": "{table}"}


def _file(*databases):
    """A file of one server's databases, each (name, tables), its tables t0001, t0002 and so on."""
    return HEAD + "".join(f'sql_a,{name},dbo,t{t:04d},"id",id\n' for name, tables in databases
                          for t in range(1, tables + 1))


def _groups_of(plan):
    """{database: {group: its tables there}} of a plan's lines."""
    out = {}
    for line in plan["lines"]:
        groups = out.setdefault(line["row"]["src_database"], {})
        groups[line["group"]] = groups.get(line["group"], 0) + 1
    return out


def test_the_size_a_database_is_measured_against_is_the_jobs_and_a_number_of_groups_keeps_it_whole():
    assert placement.split_size(placement.TablesPerGroup(500)) == 500
    assert placement.split_size(None) == placement.TABLES_PER_GROUP
    assert placement.split_size(3) is None


def test_a_database_needs_as_many_groups_as_its_tables_with_no_group_for_a_small_remainder():
    assert placement.parts_for(10_000, 10_000, 5) == 1
    assert placement.parts_for(11_000, 10_000, 5) == 1            # a tenth over stays whole, as a server's groups do
    assert placement.parts_for(11_001, 10_000, 5) == 2
    assert placement.parts_for(25_000, 10_000, 5) == 3
    assert placement.parts_for(25_000, 10_000, 2) == 2            # never more than the server has groups
    assert placement.parts_for(25_000, None, 5) == 1              # sized by a number of groups: whole


def test_a_database_is_cut_into_parts_that_differ_by_at_most_one_table():
    assert placement.part_sizes(10, 3) == [4, 3, 3]
    assert placement.part_sizes(25_001, 3) == [8_334, 8_334, 8_333]
    assert [placement.part_of(n, 10, 3) for n in range(10)] == [0, 0, 0, 0, 1, 1, 1, 2, 2, 2]


def test_databases_that_fit_are_placed_exactly_as_before():
    rows = [(f"eng_{i:03d}", (i * 37) % 900 + 1) for i in range(60)]
    assert placement.even(rows, 4, per_group=10_000) == placement.even(rows, 4)


def test_a_database_bigger_than_a_group_is_spread_over_the_lightest_groups_and_the_rest_stay_whole():
    spread = placement.even([("huge", 250), ("a", 60), ("b", 40), ("c", 30)], 4, per_group=100)
    assert spread["huge"] == (0, 1, 2)                       # 250 tables at 100 a group: three parts of 84, 83, 83
    assert {spread[name] for name in ("a", "b", "c")} <= {0, 1, 2, 3}
    assert all(isinstance(spread[name], int) for name in ("a", "b", "c"))
    assert spread["a"] == 3                                  # the empty group takes the next biggest whole
    assert placement.indexes(spread["huge"]) == (0, 1, 2) and placement.indexes(spread["a"]) == (3,)


def test_a_new_jobs_big_database_has_each_table_in_one_group_and_its_groups_even():
    plan = nj.plan("Job", _file(("huge", 250), ("small", 20)), SETTINGS, placement.TablesPerGroup(100))
    assert plan["errors"] == []
    groups = _groups_of(plan)
    assert sorted(groups["huge"].values()) == [83, 83, 84] and len(groups["small"]) == 1
    assert sum(groups["huge"].values()) == 250 and len(plan["lines"]) == 270
    assert sorted(plan["groups"]) == ["job_sql_a_c01", "job_sql_a_c02", "job_sql_a_c03"]


def test_a_big_databases_tables_go_to_its_groups_in_name_order_whatever_the_file_order():
    forward = nj.plan("Job", _file(("huge", 250)), SETTINGS, placement.TablesPerGroup(100))
    lines = forward["lines"]
    backward = nj.plan("Job", HEAD + "".join(reversed(_file(("huge", 250))[len(HEAD):].splitlines(True))), SETTINGS,
                       placement.TablesPerGroup(100))
    assert {l["row"]["src_table"]: l["group"] for l in lines} == {l["row"]["src_table"]: l["group"] for l in backward["lines"]}
    first = [l for l in lines if l["row"]["src_table"] == "t0001"][0]["group"]
    last = [l for l in lines if l["row"]["src_table"] == "t0250"][0]["group"]
    assert (first, last) == ("job_sql_a_c01", "job_sql_a_c03")


def test_a_job_sized_by_a_number_of_groups_keeps_its_big_database_whole():
    plan = nj.plan("Job", _file(("huge", 250), ("small", 20)), SETTINGS, 3)
    assert {name: len(groups) for name, groups in _groups_of(plan).items()} == {"huge": 1, "small": 1}


def test_the_by_database_create_gives_a_big_database_its_groups():
    placements = placement.server_placements([("sql_a", "huge", 250), ("sql_a", "small", 20)], {"sql_a": 3}, 100)
    group_of = by_database.group_of_placement("Job", placements)
    assert group_of("sql_a", "HUGE") == ["job_sql_a_c01", "job_sql_a_c02", "job_sql_a_c03"]
    assert isinstance(group_of("sql_a", "small"), str)


def test_a_big_database_added_later_spreads_over_its_servers_lightest_groups_and_nothing_placed_moves():
    join = placement.Lightest({"sql_a": {"g1": 50, "g2": 10, "g3": 20}}, per_group=100)
    assert join.parts("sql_a", 90) == 1 and join.parts("sql_a", 250) == 3
    assert join.spread("sql_a", "huge", 250) == ["g1", "g2", "g3"]
    assert join.spread("sql_a", "HUGE", 250) == ["g1", "g2", "g3"]          # spread already: it keeps its groups
    join = placement.Lightest({"sql_a": {"g1": 50, "g2": 10, "g3": 20}}, per_group=100)
    assert join.spread("sql_a", "big", 150) == ["g2", "g3"]                  # two parts, into the two lightest
    assert join.place("sql_a", "next", 1) == "g1"                            # 50 against 10 + 75 and 20 + 75
    assert placement.Lightest({"sql_a": {"g1": 1}}).spread("sql_b", "x", 5) is None


def test_tables_added_to_a_job_put_a_big_new_database_over_its_lightest_groups():
    job_rows = [{"id": n, "task_collection": f"job_sql_a_c0{1 + n % 3}", "src_connection": "sql_a",
                 "use_remote_query": True, "src_host": None, "src_port": None, "src_type": "sqlserver",
                 "src_database": f"old_{n % 3}", "src_schema": "dbo", "src_table": f"t{n:04d}", "select_list": "id",
                 "primary_key": "id", "load_type": "ct", "write_mode": "merge", "sink_catalog": "main",
                 "sink_schema": "s", "sink_table": f"t{n:04d}", "client_id": f"old_{n % 3}", "client_attrs": None,
                 "freshness_sla_minutes": None} for n in range(30)]
    plan = nj.plan_for_job("Job", _file(("huge", 250)), "ct", job_rows, placement.TablesPerGroup(100))
    assert plan["errors"] == [] and plan["new_groups"] == []
    assert sorted(_groups_of(plan)["huge"].values()) == [83, 83, 84]


@pytest.fixture
def main():
    pytest.importorskip("fastapi")
    import main as app
    return app


def test_a_regroup_moves_a_split_database_part_by_part_and_never_gathers_it(main):
    rows, next_id = [], 1
    for database, group, tables in (("huge", 1, 30), ("huge", 2, 30), ("a", 1, 10), ("b", 2, 10)):
        for t in range(tables):
            rows.append({"id": next_id, "task_collection": f"job_sql_a_c{group:02d}", "src_connection": "sql_a",
                         "use_remote_query": True, "src_host": None, "src_port": None, "src_database": database,
                         "src_table": f"t{group}{t:03d}"})
            next_id += 1
    plan = main._regroup_plan("job", rows, 4)
    after = {}
    for r in rows:
        group = next((a for i, _, a, _ in plan["moves"] if i == r["id"]), r["task_collection"])
        after.setdefault((r["src_database"], r["task_collection"]), set()).add(group)
    assert all(len(groups) == 1 for groups in after.values())                 # each part moves whole
    assert after[("huge", "job_sql_a_c01")] != after[("huge", "job_sql_a_c02")]   # and the parts stay apart
    assert plan["servers"][0]["databases"] == 3
