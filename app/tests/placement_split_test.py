"""The app spreads a database too big for one group over several groups by itself (placement.py), and keeps every
database that fits whole, as before. Its tables are cut, in name order, into parts sized to the room in the groups with
the fewest tables, so the groups end level; the same tables always land the same way. Databases added to
a job later do the same over their server's lightest groups, and a regroup moves a split database part by part.
Plain: no database."""
from typing import Dict

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


def test_a_database_is_cut_into_parts_sized_to_the_room_in_its_groups():
    assert placement.room_shares([0, 0, 0], 10) == [4, 3, 3]               # equal groups: parts differ by one table
    assert placement.room_shares([0, 0, 0], 25_001) == [8_334, 8_334, 8_333]
    assert placement.room_shares([0, 7_500], 12_000) == [9_750, 2_250]      # the emptier group takes more
    assert placement.room_shares([20, 6, 4], 30) == [0, 14, 16]            # a group above the level takes none
    assert placement.room_shares([5, 0], 3) == [0, 3]
    assert placement.room_shares([3, 1], 3) == [0, 3]                      # the level is 3.5: the lighter takes the odd one
    assert placement.room_shares([4], 7) == [7]


def test_a_table_goes_to_the_part_its_place_in_name_order_falls_in():
    spread = placement.Spread(groups=("a", "b", "c"), shares=(4, 3, 3))
    assert [spread.group_for(n, 10) for n in range(10)] == list("aaaabbbccc")
    unequal = placement.Spread(groups=("a", "b"), shares=(2_250, 9_750))
    assert [unequal.group_for(n, 12_000) for n in (0, 2_249, 2_250, 11_999)] == ["a", "a", "b", "b"]


def test_a_database_with_other_tables_than_it_was_placed_for_is_cut_in_the_same_proportions():
    spread = placement.Spread(groups=("a", "b"), shares=(1, 3))
    assert [spread.group_for(n, 8) for n in range(8)] == list("aabbbbbb")
    assert [spread.group_for(n, 2) for n in range(2)] == ["a", "b"]
    assert placement.Spread(groups=("only",), shares=(5,)).group_for(3, 99) == "only"


def test_databases_that_fit_are_placed_exactly_as_before():
    rows = [(f"eng_{i:03d}", (i * 37) % 900 + 1) for i in range(60)]
    assert placement.even(rows, 4, per_group=10_000) == placement.even(rows, 4)


def test_a_database_bigger_than_a_group_is_spread_over_the_lightest_groups_and_the_rest_stay_whole():
    spread = placement.even([("huge", 250), ("a", 60), ("b", 40), ("c", 30)], 4, per_group=100)
    assert spread["huge"] == placement.Spread(groups=(0, 1, 2), shares=(84, 83, 83))   # 250 tables at 100 a group
    assert {spread[name] for name in ("a", "b", "c")} <= {0, 1, 2, 3}
    assert all(isinstance(spread[name], int) for name in ("a", "b", "c"))
    assert spread["a"] == 3                                  # the empty group takes the next biggest whole
    assert placement.indexes(spread["huge"]) == (0, 1, 2) and placement.indexes(spread["a"]) == (3,)


def _loads(placements: Dict[str, object], tables: Dict[str, int], groups: int):
    """What each group holds when every database goes where ``placements`` (placement.even) says."""
    loads = [0] * groups
    for name, place in placements.items():
        if isinstance(place, placement.Spread):
            for index, share in zip(place.groups, place.shares):
                loads[index] += share
        else:
            loads[place] += tables[name]
    return loads


def test_a_server_of_30500_tables_ends_with_its_groups_level_and_not_at_13500_8000_9000():
    tables = {"sales": 15_000, "orders": 12_000, "billing": 3_000, "hr": 500}
    spread = placement.even(list(tables.items()), 3, per_group=10_000)
    assert spread["sales"] == placement.Spread(groups=(0, 1), shares=(7_500, 7_500))
    assert spread["orders"] == placement.Spread(groups=(0, 2), shares=(2_250, 9_750))     # the room left, not 6,000 each
    assert (spread["billing"], spread["hr"]) == (1, 0)
    assert _loads(spread, tables, 3) == [10_250, 10_500, 9_750]


def test_the_groups_end_level_to_within_the_smallest_database_left_whole():
    tables = {f"db{n}": size for n, size in enumerate((25_000, 18_000, 700, 300))}
    loads = _loads(placement.even(list(tables.items()), 5, per_group=10_000), tables, 5)
    assert sum(loads) == sum(tables.values()) and max(loads) - min(loads) <= 700


def test_a_database_that_fits_still_goes_whole_into_the_group_with_the_fewest_tables():
    spread = placement.even([("huge", 250), ("a", 60), ("b", 40), ("c", 30)], 4, per_group=100)
    assert (spread["a"], spread["b"], spread["c"]) == (3, 3, 1)       # after the huge one: 84, 83, 83 and 0


def test_a_new_jobs_big_database_has_each_table_in_one_group_and_its_groups_even():
    plan = nj.plan("Job", _file(("huge", 250), ("small", 20)), SETTINGS, placement.TablesPerGroup(100))
    assert plan["errors"] == []
    groups = _groups_of(plan)
    assert sorted(groups["huge"].values()) == [83, 83, 84] and len(groups["small"]) == 1
    assert sum(groups["huge"].values()) == 250 and len(plan["lines"]) == 270
    assert sorted(plan["groups"]) == ["job_sql_a_c01", "job_sql_a_c02", "job_sql_a_c03"]


def test_a_new_jobs_groups_end_level_and_each_part_holds_the_tables_its_group_has_room_for():
    plan = nj.plan("Job", _file(("sales", 150), ("orders", 120), ("billing", 30), ("hr", 5)), SETTINGS,
                   placement.TablesPerGroup(100))
    assert plan["errors"] == []
    totals = {}
    for line in plan["lines"]:
        totals[line["group"]] = totals.get(line["group"], 0) + 1
    assert totals == {"job_sql_a_c01": 102, "job_sql_a_c02": 105, "job_sql_a_c03": 98}
    orders = {line["row"]["src_table"]: line["group"] for line in plan["lines"] if line["row"]["src_database"] == "orders"}
    assert [orders[f"t{n:04d}"] for n in (1, 22, 23, 120)] == ["job_sql_a_c01", "job_sql_a_c01", "job_sql_a_c03", "job_sql_a_c03"]


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
    assert group_of("sql_a", "HUGE") == placement.Spread(groups=("job_sql_a_c01", "job_sql_a_c02", "job_sql_a_c03"),
                                                         shares=(84, 83, 83))
    assert isinstance(group_of("sql_a", "small"), str)


def test_a_big_database_added_later_spreads_over_its_servers_lightest_groups_and_nothing_placed_moves():
    join = placement.Lightest({"sql_a": {"g1": 50, "g2": 10, "g3": 20}}, per_group=100)
    assert join.parts("sql_a", 90) == 1 and join.parts("sql_a", 250) == 3
    huge = placement.Spread(groups=("g1", "g2", "g3"), shares=(60, 100, 90))      # all three end at 110
    assert join.spread("sql_a", "huge", 250) == huge
    assert join.spread("sql_a", "HUGE", 250) == huge                              # spread already: it keeps its groups
    join = placement.Lightest({"sql_a": {"g1": 50, "g2": 10, "g3": 20}}, per_group=100)
    assert join.spread("sql_a", "big", 150) == placement.Spread(groups=("g2", "g3"), shares=(80, 70))   # the two lightest
    assert join.place("sql_a", "next", 1) == "g1"                            # 50 against 90 and 90
    assert placement.Lightest({"sql_a": {"g1": 1}}).spread("sql_b", "x", 5) is None


def test_a_tie_between_groups_goes_to_the_first_by_name_whatever_order_the_groups_are_listed_in():
    """Review of PR 41: spread() kept the order the groups were listed in on equal loads, place() takes the first by
    name, so a database that fits could land in another group than before."""
    tied = {"sql_a": {"g2": 5, "g1": 5}}
    assert placement.Lightest(tied, per_group=100).place("sql_a", "small") == "g1"
    assert placement.Lightest(tied, per_group=100).spread("sql_a", "small", 3) == placement.Spread(groups=("g1",), shares=(3,))
    level = placement.Lightest({"sql_a": {"g3": 0, "g1": 0, "g2": 0}}, per_group=100)
    assert level.spread("sql_a", "big", 150) == placement.Spread(groups=("g1", "g2"), shares=(75, 75))


def test_a_group_already_above_the_level_takes_no_part_of_a_database_added_later():
    join = placement.Lightest({"sql_a": {"g1": 500, "g2": 0, "g3": 0}}, per_group=100)
    assert join.spread("sql_a", "huge", 250) == placement.Spread(groups=("g2", "g3"), shares=(125, 125))
    join = placement.Lightest({"sql_a": {"g1": 500, "g2": 0}}, per_group=100)
    assert join.spread("sql_a", "huge", 150) == placement.Spread(groups=("g2",), shares=(150,))    # all in the one that has room


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


def test_tables_added_to_a_job_whose_groups_differ_are_sized_to_the_room_in_each():
    held = {"job_sql_a_c01": 60, "job_sql_a_c02": 10, "job_sql_a_c03": 20}
    job_rows = [{"id": n, "task_collection": group, "src_connection": "sql_a", "use_remote_query": True,
                 "src_host": None, "src_port": None, "src_type": "sqlserver", "src_database": "old",
                 "src_schema": "dbo", "src_table": f"{group}_{n:03d}", "select_list": "id", "primary_key": "id",
                 "load_type": "ct", "write_mode": "merge", "sink_catalog": "main", "sink_schema": "s",
                 "sink_table": f"{group}_{n:03d}", "client_id": "old", "client_attrs": None,
                 "freshness_sla_minutes": None}
                for group, tables in held.items() for n in range(tables)]
    plan = nj.plan_for_job("Job", _file(("huge", 250)), "ct", job_rows, placement.TablesPerGroup(100))
    assert plan["errors"] == []
    assert _groups_of(plan)["huge"] == {"job_sql_a_c01": 53, "job_sql_a_c02": 104, "job_sql_a_c03": 93}     # all three end at 113 or 114


def _held_rows(held):
    """The rows of a job holding database "huge" split over groups: held is {group: its tables}, named x<group><n>."""
    return [{"id": at, "task_collection": group, "src_connection": "sql_a", "use_remote_query": True, "src_host": None,
             "src_port": None, "src_type": "sqlserver", "src_database": "huge", "src_schema": "dbo",
             "src_table": f"x{group}{n:04d}", "select_list": "id", "primary_key": "id", "load_type": "ct",
             "write_mode": "merge", "sink_catalog": "main", "sink_schema": "s", "sink_table": f"x{group}{n:04d}",
             "client_id": "huge", "client_attrs": None, "freshness_sla_minutes": None}
            for at, (group, n) in enumerate((g, n) for g, tables in held.items() for n in range(tables))]


def test_tables_added_to_a_database_split_over_groups_go_to_the_lightest_of_its_own_groups():
    """Review of PR 41: only the group of the database's first row was known, so every new table piled onto it."""
    rows = _held_rows({"job_sql_a_c01": 100, "job_sql_a_c02": 80})
    plan = nj.plan_for_job("Job", _file(("huge", 60)), "ct", rows, placement.TablesPerGroup(1000))
    assert plan["errors"] == [] and plan["new_groups"] == []
    assert _groups_of(plan)["huge"] == {"job_sql_a_c01": 20, "job_sql_a_c02": 40}      # both end at 120


def test_a_table_of_a_split_database_the_job_holds_already_keeps_its_group_and_adds_no_load():
    rows = _held_rows({"job_sql_a_c01": 100, "job_sql_a_c02": 80})
    again = HEAD + "".join(f'sql_a,huge,dbo,xjob_sql_a_c01{n:04d},"id",id\n' for n in range(5))
    plan = nj.plan_for_job("Job", again + _file(("huge", 60))[len(HEAD):], "ct", rows, placement.TablesPerGroup(1000))
    held = [line for line in plan["lines"] if line["status"] == "in_job"]
    assert len(held) == 5 and {line["group"] for line in held} == {"job_sql_a_c01"}
    assert _groups_of(plan)["huge"] == {"job_sql_a_c01": 25, "job_sql_a_c02": 40}      # the five held count for nothing


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
