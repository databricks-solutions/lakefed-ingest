"""How a job's databases are placed in its groups (placement.py): as many groups as the tables need with no group for
a small remainder, spread evenly by tables, never splitting a database, and later databases into the lightest group."""
import placement


def test_the_default_number_of_groups_leaves_out_a_small_remainder():
    assert placement.groups_for_tables(100_200) == 10
    assert placement.groups_for_tables(10_000 * 8 + 1_001) == 9
    assert [placement.groups_for_tables(n) for n in (0, 1, 10_000, 11_000, 11_001)] == [1, 1, 1, 1, 2]


def test_a_jobs_tables_per_group_sizes_its_groups_and_a_number_of_groups_still_wins():
    assert placement.group_count(100_200) == 10
    assert placement.group_count(100_200, placement.TablesPerGroup(25_000)) == 4
    assert placement.group_count(100_200, 3) == 3
    assert placement.sizing(None, None) is None
    assert placement.sizing(None, 5_000) == placement.TablesPerGroup(5_000)
    assert placement.sizing(None, None, 20_000) == placement.TablesPerGroup(20_000)      # the job's own
    assert placement.sizing(None, 5_000, 20_000) == placement.TablesPerGroup(5_000)     # the request's wins
    assert placement.sizing(2, 5_000, 20_000) == 2                                      # an older caller's number


def test_databases_are_spread_biggest_first_into_the_lightest_group():
    spread = placement.even([("a", 50), ("b", 40), ("c", 30), ("d", 20), ("e", 10)], 2)
    loads = [sum(t for name, t in (("a", 50), ("b", 40), ("c", 30), ("d", 20), ("e", 10)) if spread[name] == g)
             for g in (0, 1)]
    assert sorted(loads) == [70, 80]


def test_the_same_databases_always_land_the_same_way_whatever_their_order_or_case():
    rows = [("Eng_%03d" % i, (i * 37) % 11 + 1) for i in range(50)]
    assert placement.even(rows, 4) == placement.even(list(reversed([(n.lower(), t) for n, t in rows])), 4)


def test_a_database_listed_twice_is_one_database_in_one_group():
    spread = placement.even([("a", 3), ("A", 4), ("b", 5)], 2)
    assert set(spread) == {"a", "b"} and spread["a"] != spread["b"]


def test_later_databases_join_the_lightest_group_and_nothing_moves():
    join = placement.Lightest({"sql_a": {"g1": 10, "g2": 4}})
    assert join.place("sql_a", "new_1", 5) == "g2"            # 4 + 5 = 9
    assert join.place("sql_a", "new_2", 5) == "g2"            # 9 is still lighter than 10
    assert join.place("sql_a", "new_3", 1) == "g1"            # 10 against 14
    assert join.place("sql_a", "NEW_1") == "g2"               # placed already: it keeps its group
    assert join.place("sql_b", "x") is None and not join.knows("sql_b")


def test_a_new_number_of_groups_spreads_the_databases_again_moving_as_few_as_it_can():
    now = {f"db{i}": ("job_s_c01" if i < 6 else "job_s_c02", 10) for i in range(12)}
    plan = placement.regroup("job_s", now, 3)
    assert plan["groups"] == ["job_s_c01", "job_s_c02", "job_s_c03"] and plan["new"] == ["job_s_c03"]
    per_group = {}
    for name, group in plan["group_of"].items():
        per_group[group] = per_group.get(group, 0) + 1
    assert sorted(per_group.values()) == [4, 4, 4]
    assert len(plan["moves"]) == 4 and plan["emptied"] == []   # only the four the new group takes move


def test_fewer_groups_empty_the_extra_ones_and_keep_the_names_with_the_most_tables():
    now = {"a": ("job_s_c01", 50), "b": ("job_s_c02", 40), "c": ("job_s_c03", 5), "d": ("job_s_c04", 5)}
    plan = placement.regroup("job_s", now, 2)
    assert plan["groups"] == ["job_s_c01", "job_s_c02"] and plan["emptied"] == ["job_s_c03", "job_s_c04"]
    assert plan["group_of"]["a"] == "job_s_c01" and plan["group_of"]["b"] == "job_s_c02"
    assert set(plan["moves"]) == {"c", "d"}


def test_the_remainder_example_in_the_comment_holds():
    assert placement.groups_for_tables(100_200) == 10
    assert placement.groups_for_tables(101_500) == 11
