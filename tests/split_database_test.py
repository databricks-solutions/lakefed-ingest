"""One source database whose tables sit in two ingestion groups, run through the real check, cycle plan and ingest
planning, cycle after cycle, against a fake change-tracked source and an in-memory Lakebase.

The question these tests answer is whether a split database loads correctly: each group checks the database for its
own tables only, keeps its own check stamp, loads only its own tables, and between them nothing is loaded twice or
missed. They also count the source queries a split costs, which is what the warning in the app tells the user about.

Plain: no Spark and no Lakebase. The planning functions are the ones ``check_launch.ipynb`` and ``ingest.ipynb`` call;
only the source queries and the Lakebase reads and writes are stand-ins, and a load is the checkpoint move a real
load ends with (``StateStore.advance`` after the Delta commit)."""
from datetime import datetime, timedelta, timezone

import ct_engine
import initial_check as ic
import planner
import state_store as ss

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
BEGIN_VERSION = 7          # the change tracking begin version every table reports, as recorded at its seed
GROUP_OF = {"t1": "g1", "t2": "g1", "t3": "g2", "t4": "g2"}     # one database, sales, in two groups
SOURCE = "sql1|1433|k1"    # state_store.database_source of the rows below


def _row(control_id, table, collection, database="sales"):
    return {"id": control_id, "task_collection": collection, "src_type": "sqlserver", "load_type": "ct",
            "src_host": "sql1", "src_port": 1433, "secret_key": "k1", "src_connection": None,
            "use_remote_query": False, "src_database": database, "src_schema": "dbo", "src_table": table,
            "select_list": "id, v", "primary_key": "id", "sink_catalog": "main", "sink_schema": "bronze",
            "sink_table": f"{database}_{table}"}


class Source:
    """A SQL Server database with change tracking: a version that moves with every change, and the version at which
    each table last changed. It counts the queries it is asked, as the source's own load would count them."""

    def __init__(self, tables):
        self.version = 10
        self.changed_at = {table: 0 for table in tables}
        self.queries = 0

    def change(self, *tables):
        for table in tables:
            self.version += 1
            self.changed_at[table] = self.version

    def detect_one(self, members):
        """planner.detect_group for one detection group (one database, the rows of one collection)."""
        def database_state(cfg):
            self.queries += 1
            return self.version, {("dbo", row["src_table"].lower()): (1, BEGIN_VERSION) for row, _ in members}

        def detect(cfg, items):
            self.queries += 1
            return ({control_id: (1, int(self.changed_at[table] > position))
                     for control_id, _, table, position in items}, [], 1)
        return planner.detect_group(members, None, detect, database_state=database_state)


class Lakebase:
    """The control rows, checkpoints and check stamps, kept as the real tables key them: a checkpoint per control row,
    a stamp per (collection, source, database)."""

    def __init__(self, rows):
        self.rows = rows
        self.checkpoints = {}
        self.stamps = {}

    def configs(self, collection):
        return [row for row in self.rows if row["task_collection"] == collection]

    def checkpoints_of(self, configs):
        return {int(c["id"]): self.checkpoints[int(c["id"])] for c in configs if int(c["id"]) in self.checkpoints}

    def stamps_of(self, collection):
        return {(source, database): stamp for (c, source, database), stamp in self.stamps.items() if c == collection}

    def advance_unchanged(self, rows):
        """StateStore.bulk_advance_unchanged: each row's position moves to the version it was checked at."""
        for control_id, version, *_ in rows:
            self.checkpoints[int(control_id)] = dict(self.checkpoints[int(control_id)], position=str(version))

    def stamp(self, collection, stamps):
        for source, database, version, _ in stamps:
            self.stamps[(collection, source, database)] = {"version": version, "checked_at": NOW}

    def loaded(self, control_id, position):
        """StateStore.advance after a seed or a merge committed: the table is incremental at ``position``."""
        row = next(r for r in self.rows if int(r["id"]) == int(control_id))
        names = ("source_identity", "sink_identity", "merge_key")
        self.checkpoints[int(control_id)] = {"position": str(position), "phase": ss.INCREMENTAL, "status": "ok",
                                             "strategy": "ct", "select_list": row["select_list"],
                                             "ct_begin_version": BEGIN_VERSION,
                                             "last_success_at": NOW - timedelta(minutes=5),
                                             **dict(zip(names, ss.identity_values(row)))}


def _sinks(rows):
    return {ct_engine.sink_lookup_key(row) for row in rows}


def _check(lakebase, source, collection):
    """check_launch.ipynb for one collection: the check, then its unchanged rows and stamps written."""
    configs = lakebase.configs(collection)
    result = ic.check_collection(configs, lakebase.checkpoints_of(configs), source.detect_one,
                                 existing_sinks=_sinks(configs), stamps=lakebase.stamps_of(collection), now=NOW)
    lakebase.advance_unchanged(result["rows"])
    lakebase.stamp(collection, result["database_stamps"])
    return dict(result, collection=collection)


def _ingest(lakebase, source, collection, work):
    """ingest.ipynb's planning for one collection's work list, then each planned load. Returns what it loaded, as
    (collection, table, position from, position to)."""
    configs = planner.select_work(lakebase.configs(collection), work)
    checkpoints = lakebase.checkpoints_of(configs)
    plans, candidates, _ = planner.route_tables(configs, checkpoints, existing_sinks=_sinks(configs))
    elsewhere = planner.database_keys(p["cfg"] for p in plans)
    members = list(planner.group_by_source(candidates).values())
    outputs = [source.detect_one(group) for group in members]
    plans += planner.merge_group_results(outputs)["plans"]
    rows, stamps = planner.unchanged_writes(members, outputs, checkpoints, 0, stamps=lakebase.stamps_of(collection),
                                            elsewhere=elsewhere, now=NOW)
    lakebase.advance_unchanged(rows)
    lakebase.stamp(collection, stamps)
    loads = []
    for plan in plans:
        assert plan["route"] in ("seed", "merge"), plan
        start = planner.position_from(plan)
        to = source.version if plan["route"] == "seed" else plan["cur"]
        loads.append((collection, plan["cfg"]["src_table"], start, to))
        lakebase.loaded(plan["cfg"]["id"], to)
    return loads


def _cycle(lakebase, source, collections=("g1", "g2"), group_size=2):
    """One cycle of the check job: check each collection, share its work out over runs (plan_cycle), then each run
    takes its collections in turn (planner.cycle_parts), each part ingesting only its own work list."""
    results = [_check(lakebase, source, collection) for collection in collections]
    runs = ic.plan_cycle(results, group_size)
    loads = []
    for run in runs:
        for collection in planner.cycle_parts(run["databases"]):
            work = {(source_key, database) for c, source_key, database in run["databases"] if c == collection}
            loads += _ingest(lakebase, source, collection, work)
    return {"results": {r["collection"]: r for r in results}, "runs": runs, "loads": sorted(loads)}


def _split_world():
    rows = [_row(n, table, GROUP_OF[table]) for n, table in enumerate(sorted(GROUP_OF), start=1)]
    return Lakebase(rows), Source(GROUP_OF)


def _loaded_tables(cycle):
    return sorted((collection, table) for collection, table, *_ in cycle["loads"])


def test_a_first_load_seeds_each_groups_own_tables_on_a_run_of_its_own():
    lakebase, source = _split_world()
    cycle = _cycle(lakebase, source)
    assert _loaded_tables(cycle) == [("g1", "t1"), ("g1", "t2"), ("g2", "t3"), ("g2", "t4")]
    assert [run["collections"] for run in cycle["runs"]] == [["g1"], ["g2"]]
    assert all(run["first_loads"] for run in cycle["runs"])


def test_a_quiet_split_database_gets_one_stamp_per_group_each_for_its_own_tables():
    lakebase, source = _split_world()
    _cycle(lakebase, source)
    cycle = _cycle(lakebase, source)
    assert cycle["loads"] == [] and cycle["runs"] == []
    assert cycle["results"]["g1"]["database_stamps"] == [(SOURCE, "sales", source.version, 2)]
    assert cycle["results"]["g2"]["database_stamps"] == [(SOURCE, "sales", source.version, 2)]
    assert sorted(lakebase.stamps) == [("g1", SOURCE, "sales"), ("g2", SOURCE, "sales")]
    for collection, control_ids in (("g1", (1, 2)), ("g2", (3, 4))):
        stamp = lakebase.stamps_of(collection)[(SOURCE, "sales")]
        assert all(planner.stamp_vouches(lakebase.checkpoints[cid], stamp) for cid in control_ids)


def test_a_change_in_one_groups_table_is_loaded_by_that_group_only():
    lakebase, source = _split_world()
    _cycle(lakebase, source)
    _cycle(lakebase, source)
    before = source.version
    source.change("t3")
    cycle = _cycle(lakebase, source)
    assert cycle["loads"] == [("g2", "t3", before, source.version)]
    assert cycle["results"]["g1"]["work"] == [] and cycle["results"]["g2"]["work"] == [[SOURCE, "sales"]]
    # g1 asked the database too (its version moved), found none of its tables changed, and moved them on.
    assert all(int(lakebase.checkpoints[cid]["position"]) == source.version for cid in (1, 2))
    assert int(lakebase.checkpoints[4]["position"]) == source.version


def test_changes_in_both_groups_are_each_loaded_once_by_their_own_group():
    lakebase, source = _split_world()
    _cycle(lakebase, source)
    source.change("t1", "t4")
    cycle = _cycle(lakebase, source)
    assert _loaded_tables(cycle) == [("g1", "t1"), ("g2", "t4")]
    tables = [table for _, table, *_ in cycle["loads"]]
    assert len(tables) == len(set(tables))


def test_over_many_cycles_every_change_is_loaded_exactly_once_and_by_the_group_holding_its_table():
    lakebase, source = _split_world()
    _cycle(lakebase, source)
    changes = [("t1",), (), ("t3", "t4"), ("t2", "t3"), (), ("t1", "t2", "t3", "t4"), ("t4",)]
    for tables in changes:
        source.change(*tables)
        cycle = _cycle(lakebase, source)
        assert _loaded_tables(cycle) == sorted((GROUP_OF[t], t) for t in set(tables))
    # Every table ends at the database's current version: nothing missed, nothing left behind.
    assert {cid: int(cp["position"]) for cid, cp in lakebase.checkpoints.items()} == \
        {cid: source.version for cid in (1, 2, 3, 4)}


def test_the_two_groups_parts_can_go_to_different_runs_and_each_part_runs_once():
    lakebase, source = _split_world()
    _cycle(lakebase, source)
    source.change("t1", "t2", "t3", "t4")
    cycle = _cycle(lakebase, source, group_size=2)
    parts = [tuple(part) for run in cycle["runs"] for part in run["databases"]]
    assert sorted(parts) == [("g1", SOURCE, "sales"), ("g2", SOURCE, "sales")]
    assert len(cycle["runs"]) == 2


def test_the_two_groups_lock_the_split_database_under_different_keys():
    """A run's database lock is per group (state_store.database_lock_key), so the two groups' runs never wait for each
    other or skip each other's part of the database."""
    g1 = planner.work_database_keys("g1", {(SOURCE, "sales")})
    g2 = planner.work_database_keys("g2", {(SOURCE, "sales")})
    assert set(g1).isdisjoint(g2)
    assert list(g1.values()) == list(g2.values()) == [(SOURCE, "sales")]


def test_what_a_split_costs_is_one_more_change_query_per_extra_group_each_cycle():
    """The source queries of one cycle, whole against split. Quiet: one version query per group. A change in the
    other group's tables: the group with nothing to load also runs its batched detection query."""
    def queries(group_of, changed):
        rows = [_row(n, table, group_of[table]) for n, table in enumerate(sorted(group_of), start=1)]
        lakebase, source = Lakebase(rows), Source(group_of)
        collections = sorted(set(group_of.values()))
        _cycle(lakebase, source, collections)
        source.change(*changed)
        source.queries = 0
        _cycle(lakebase, source, collections)
        return source.queries
    whole = {table: "g1" for table in GROUP_OF}
    assert queries(whole, ()) == 1 and queries(GROUP_OF, ()) == 2
    # The check asks the version and runs detection; the run asks both again for its own tables.
    assert queries(whole, ("t3",)) == 4 and queries(GROUP_OF, ("t3",)) == 6
