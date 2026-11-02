"""The synthetic SQL Server instance (tests/synthetic/server_catalog.py): names, shapes and determinism, and that discovery
reads its rows the way it reads a real catalog."""
import collections
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "synthetic"))

import discovery as d  # noqa: E402
import server_catalog as sc  # noqa: E402


def test_names_follow_the_family_shares_and_round_trip():
    names = sc.ServerCatalog(databases=22_000).database_names()
    assert len(names) == len(set(names)) == 22_000
    counts = collections.Counter(n[:-5] for n in names)
    assert counts == {"lfc_eng_": 13_200, "lfc_test_": 4_400, "lfc_heavy_": 2_200, "ops_": 2_200}
    assert all(sc.database_name(sc.database_index(n)) == n for n in names)


def test_name_filters_select_the_families():
    names = sc.ServerCatalog(databases=40).database_names()
    assert sum(n.startswith("lfc_eng_") for n in names) == 24
    assert sum(n.startswith("lfc_heavy_") for n in names) == 4


def test_the_catalog_is_deterministic_per_seed_and_database():
    a, b = sc.ServerCatalog(databases=20, seed=7), sc.ServerCatalog(databases=20, seed=7)
    assert a.catalog(5) == b.catalog(5)
    assert sc.ServerCatalog(databases=20, seed=8).catalog(5) != a.catalog(5)
    assert a.catalog(5) != a.catalog(6)


def test_each_database_has_its_tables_and_object_ids_are_unique_across_the_server():
    server = sc.ServerCatalog(databases=30, tables_per_database=240)
    oids = set()
    for index in range(30):
        tables, _, _ = server.catalog(index)
        assert len(tables) == 240
        oids.update(t["oid"] for t in tables)
    assert len(oids) == 30 * 240


def test_load_type_shares_are_about_right():
    server = sc.ServerCatalog(databases=40)
    tables = [t for i in range(40) for t in server.catalog(i)[0]]
    assert 0.82 < sum(t["ct_enabled"] for t in tables) / len(tables) < 0.88


@pytest.mark.parametrize("index", [0, 1, 2, 3])
def test_discovery_recommends_what_the_catalog_says(index):
    tables, columns, keys = sc.ServerCatalog(databases=40).catalog(index)
    found = {(r["src_schema"], r["src_table"]): r for r in d.assemble(tables, columns, keys)}
    for t in tables:
        rec = found[(t["sch"], t["tbl"])]
        has_key = any(k["oid"] == t["oid"] for k in keys)
        if t["ct_enabled"]:
            assert rec["load_type"] == "ct" and has_key
        elif any(c["oid"] == t["oid"] and c["base_type"] == "timestamp" for c in columns):
            assert rec["load_type"] == "cursor" and rec["cursor_column"] == "rv"
        else:
            assert rec["load_type"] == "full" and not has_key


def test_geography_columns_are_left_out_of_the_select_list():
    server = sc.ServerCatalog(databases=200)
    for index in range(200):
        tables, columns, keys = server.catalog(index)
        geo_oids = {c["oid"] for c in columns if c["base_type"] == "geography"}
        for r in d.assemble(tables, columns, keys):
            if any(t["oid"] in geo_oids and t["sch"] == r["src_schema"] and t["tbl"] == r["src_table"] for t in tables):
                assert "geo" not in r["select_list"].split(", ")
                assert r["excluded_columns"] == "geo"
                return
    pytest.fail("no geography column found in 200 databases")


def test_unopenable_and_paused_databases_are_few_and_distinct():
    server = sc.ServerCatalog(databases=22_000)
    assert len(server.unopenable_names()) == 3 and len(server.paused_names()) == 2
    assert not set(server.unopenable_names()) & set(server.paused_names())
    assert all(server.is_unopenable(sc.database_index(n)) for n in server.unopenable_names())


def test_the_arm_listing_pages_through_every_database():
    server = sc.ServerCatalog(databases=22_000)
    base = "https://management.azure.com/x"
    seen, page, pages = [], 0, 0
    while True:
        body = server.arm_page(page, base)
        seen += [v["name"] for v in body["value"]]
        pages += 1
        if "nextLink" not in body:
            break
        assert body["nextLink"] == f"{base}?page={page + 1}"
        page += 1
    assert pages == server.arm_page_count() == 22_000 // sc.ARM_PAGE_SIZE
    assert seen == [sc.database_name(i) for i in range(22_000)]
    assert {v["properties"]["status"] for v in server.arm_page(0, base)["value"]} == {"Online"}


def test_a_small_server_lists_its_paused_database_as_paused():
    server = sc.ServerCatalog(databases=25)
    statuses = {v["name"]: v["properties"]["status"] for v in server.arm_page(0, "u")["value"]}
    assert [n for n, s in statuses.items() if s == "Paused"] == server.paused_names()


def test_reading_past_the_last_database_is_refused():
    with pytest.raises(KeyError):
        sc.ServerCatalog(databases=3).catalog(3)
