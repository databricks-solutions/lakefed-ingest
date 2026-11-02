"""The check: which databases of a group of collections need an ingest, and the work list of each."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import ct_engine
import initial_check as ic
import planner
import state_store as ss

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
SOURCE = "conn|0|conn"   # state_store.database_source of _cfg: connection-backed, so its blank port stays 0


def _cfg(cid, db, table, collection="c1"):
    return {"id": cid, "task_collection": collection, "src_connection": "conn", "use_remote_query": True,
            "src_database": db, "src_schema": "dbo", "src_table": table, "select_list": "id, v",
            "primary_key": "id", "src_type": "sqlserver", "load_type": "ct",
            "sink_catalog": "main", "sink_schema": "bronze", "sink_table": f"{db}_{table}"}


def _sinks(configs):
    """The sink tables that exist for these rows: every one of them, unless a test drops one."""
    return {ct_engine.sink_lookup_key(c) for c in configs}


def _cp(v, phase=ss.INCREMENTAL, **kw):
    return {"position": None if v is None else str(v), "phase": phase, "status": "ok", "select_list": "id, v",
            "strategy": "ct", "ct_begin_version": 7, "last_success_at": NOW - timedelta(hours=1), **kw}


def _recorded(configs, checkpoints):
    """The checkpoints with each row's identity recorded, as a run under the identity backstop leaves them."""
    by_id = {c["id"]: c for c in configs}
    names = ("source_identity", "sink_identity", "merge_key")
    return {cid: {**cp, **dict(zip(names, ss.identity_values(by_id[cid])))} for cid, cp in checkpoints.items()}


def _detector(versions, changed=frozenset(), calls=None):
    """detect_one for the fakes: each database's CT version, and the tables with a change."""
    def detect_one(members):
        db = members[0][0]["src_database"]
        if calls is not None:
            calls.append(db)
        cur = versions[db]
        state = lambda c: (cur, {("dbo", m["src_table"]): (1, 7) for m, _ in members})
        detect = lambda c, items: ({cid: (1, int(cid in changed)) for cid, *_ in items}, [], 1)
        return planner.detect_group(members, None, detect, database_state=state)
    return detect_one


def test_only_databases_that_changed_or_were_never_loaded_need_an_ingest():
    configs = [_cfg(1, "quiet", "a"), _cfg(2, "quiet", "b"),
               _cfg(3, "busy", "a"), _cfg(4, "busy", "b"),
               _cfg(5, "new", "a"), _cfg(6, "new", "b"),
               _cfg(7, "moved", "a"),
               _cfg(8, "down", "a")]
    cps = _recorded(configs, {1: _cp(10), 2: _cp(10), 3: _cp(20), 4: _cp(20), 5: _cp(30), 7: _cp(40), 8: _cp(50)})
    calls = []
    detect = _detector({"quiet": 10, "busy": 21, "moved": 41, "down": None}, changed={3}, calls=calls)
    out = ic.check_collection(configs, cps, detect, existing_sinks=_sinks(configs), now=NOW)
    # quiet: fast exit. busy: table 3 changed. new: table 6 was never loaded (no detection asked).
    # moved: the version moved but none of our tables changed. down: no CT version, the ingest reports it.
    assert out["work"] == [[SOURCE, "busy"], [SOURCE, "down"], [SOURCE, "new"]]
    assert sorted(calls) == ["busy", "down", "moved", "quiet"]
    assert out["unchanged_databases"] == 2 and out["unchanged_tables"] == 3
    # moved advances its table to 41 (a position move needs its own row); quiet gets one stamp
    assert [(r[0], r[1]) for r in out["rows"]] == [(7, 41)]
    assert [s[1:] for s in out["database_stamps"]] == [("quiet", 10, 2)]   # moved: every row writes itself
    assert out["work_tables"] == 5


def test_forced_databases_skip_the_check_and_go_to_the_ingest():
    configs = [_cfg(1, "quiet", "a"), _cfg(2, "other", "a")]
    calls = []
    out = ic.check_collection(configs, _recorded(configs, {1: _cp(10), 2: _cp(10)}),
                              _detector({"quiet": 10, "other": 10}, calls=calls),
                              existing_sinks=_sinks(configs), force={"quiet"}, now=NOW)
    assert out["work"] == [[SOURCE, "quiet"]] and calls == ["other"]
    assert [s[1] for s in out["database_stamps"]] == ["other"]


def test_a_dropped_sink_sends_its_database_to_the_ingest_not_the_stamp():
    configs = [_cfg(1, "quiet", "a"), _cfg(2, "quiet", "b")]
    calls = []
    present = _sinks(configs) - {ct_engine.sink_lookup_key(configs[0])}     # quiet.a's sink was dropped
    out = ic.check_collection(configs, _recorded(configs, {1: _cp(10), 2: _cp(10)}), _detector({"quiet": 10}, calls=calls),
                              existing_sinks=present, now=NOW)
    assert out["work"] == [[SOURCE, "quiet"]] and calls == []
    assert out["rows"] == [] and out["database_stamps"] == [] and out["unchanged_databases"] == 0


def test_unknown_sinks_send_every_database_with_a_candidate_to_the_ingest():
    configs = [_cfg(1, "quiet", "a")]
    out = ic.check_collection(configs, _recorded(configs, {1: _cp(10)}), _detector({"quiet": 10}),
                              existing_sinks=None, now=NOW)
    assert out["work"] == [[SOURCE, "quiet"]] and out["database_stamps"] == []


def test_the_check_needs_the_existing_sinks_it_is_given():
    configs = [_cfg(1, "quiet", "a")]
    try:
        ic.check_collection(configs, _recorded(configs, {1: _cp(10)}), _detector({"quiet": 10}), now=NOW)
        raise AssertionError("a check without sink information must not stamp anything")
    except TypeError:
        pass


def test_a_run_check_sends_a_database_with_a_dropped_sink_to_the_ingest():
    configs = [_cfg(1, "quiet", "a", "c1")]
    store = _Store(configs, _recorded(configs, {1: _cp(10)}))
    summary = ic.run_check(store, store, ["c1"], _detector({"quiet": 10}), existing_sinks_of=lambda cfgs: set())
    assert summary["per_collection"][0]["work"] == [[SOURCE, "quiet"]]
    assert store.stamped == {"c1": []}


def test_freshness_skips_a_fresh_database_stamp():
    configs = [_cfg(1, "quiet", "a")]
    stamps = {(ss.database_source(configs[0]), "quiet"): {"version": 10, "checked_at": NOW - timedelta(minutes=5)}}
    out = ic.check_collection(configs, _recorded(configs, {1: _cp(10)}), _detector({"quiet": 10}),
                              existing_sinks=_sinks(configs), stamps=stamps, interval_s=3600, now=NOW)
    assert out["work"] == [] and out["rows"] == [] and out["database_stamps"] == []
    assert out["unchanged_databases"] == 1














class _Store:
    """The Lakebase calls the check makes, recorded; one collection's rows per name."""

    def __init__(self, configs, checkpoints, held=()):
        self.configs, self.checkpoints, self.held = configs, checkpoints, set(held)
        self.runs, self.finished, self.advanced, self.stamped, self.unlocked = [], {}, [], {}, []
        self.closed, self.work_ids, self.rolled = [], [], {}
        self.alive = True

    def mixed_sinks(self):
        return set()

    def try_lock_collection(self, collection):
        return collection not in self.held

    def unlock_collection(self, collection):
        self.unlocked.append(collection)

    def start_run(self, collection, job_run_id, cluster_id, parallelism, work_id=None):
        self.runs.append((collection, job_run_id, cluster_id, parallelism))
        self.work_ids.append(work_id)
        return len(self.runs)

    def list_task_configs(self, collection):
        return [c for c in self.configs if c["task_collection"] == collection]

    def get_checkpoints(self, ids):
        return {i: self.checkpoints[i] for i in ids if i in self.checkpoints}

    def get_database_checks(self, collection):
        return {}

    def bulk_advance_unchanged(self, rows):
        self.advanced.extend(rows)

    def stamp_databases(self, collection, stamps, run_id):
        self.stamped[collection] = [s[1] for s in stamps]

    def rewrite_rollups(self, collection, src_databases):
        self.rolled.setdefault(collection, set()).update(src_databases)

    def finish_run(self, run_id, **kw):
        self.finished[run_id] = kw

    def close_abandoned_runs(self, collection, run_id):
        self.closed.append((collection, run_id))
        return (0, 0)

    def is_alive(self):
        return self.alive


def test_the_check_recomputes_the_rollups_of_the_databases_it_stamped():
    configs = [_cfg(1, "quiet", "a", "c1"), _cfg(2, "quiet", "b", "c1"),
               _cfg(3, "new", "a", "c2"), _cfg(4, "old", "a", "c2")]
    store = _Store(configs, _recorded(configs, {1: _cp(10), 2: _cp(10), 4: _cp(10)}))
    ic.run_check(store, store, ["c1", "c2"], _detector({"quiet": 10, "old": 10}),
                 existing_sinks_of=_sinks, job_run_id="77", cluster_id="serverless", parallelism=4)
    assert store.rolled == {"c1": {"quiet"}, "c2": {"old"}}


def test_the_check_stamps_the_quiet_databases_and_lists_each_collections_work():
    configs = [_cfg(1, "quiet", "a", "c1"), _cfg(2, "quiet", "b", "c1"),
               _cfg(3, "new", "a", "c2"), _cfg(4, "old", "a", "c2")]
    store = _Store(configs, _recorded(configs, {1: _cp(10), 2: _cp(10), 4: _cp(10)}))
    summary = ic.run_check(store, store, ["c1", "c2"], _detector({"quiet": 10, "old": 10}),
                           existing_sinks_of=_sinks, job_run_id="77", cluster_id="serverless", parallelism=4)
    assert [(r["collection"], r["work"]) for r in summary["per_collection"]] == [("c1", []), ("c2", [[SOURCE, "new"]])]
    assert store.stamped == {"c1": ["quiet"], "c2": ["old"]}
    assert sorted(store.unlocked) == ["c1", "c2"]
    assert [r[0] for r in store.runs] == ["c1", "c2"] and {r[1:] for r in store.runs} == {("77", "serverless", 4)}
    detail = json.loads(store.finished[2]["detail"])["check"]
    assert detail["work"] == [[SOURCE, "new"]] and store.finished[2]["status"] == "completed"
    assert summary["collections_with_work"] == 1 and summary["databases_with_work"] == 1 and summary["busy"] == []
    assert summary["unchanged_databases"] == 2 and summary["database_stamps"] == 2
    # The check's own record is marked from the start, so the app tells it from a group run while it is running.
    assert store.work_ids == ["check-77", "check-77"]


def test_the_check_leaves_a_collection_being_swept_to_that_sweep():
    configs = [_cfg(1, "new", "a", "c1")]
    store = _Store(configs, {}, held={"c1"})
    summary = ic.run_check(store, store, ["c1"], _detector({}), existing_sinks_of=_sinks)
    assert summary["busy"] == ["c1"] and summary["per_collection"] == [] and store.runs == [] and store.unlocked == []


def test_the_check_releases_a_collections_lock_when_its_check_fails():
    def broken(members):
        raise RuntimeError("source unreachable")

    configs = [_cfg(1, "quiet", "a", "c1")]
    store = _Store(configs, _recorded(configs, {1: _cp(10)}))
    summary = ic.run_check(store, store, ["c1"], broken, existing_sinks_of=_sinks)
    assert summary["failed"] == [{"collection": "c1", "error": "RuntimeError: source unreachable"}]
    assert summary["per_collection"] == [] and store.unlocked == ["c1"]
    assert store.finished[1]["status"] == "failed" and "source unreachable" in store.finished[1]["detail"]
    assert store.closed == [("c1", 1)]
    assert "c1: RuntimeError: source unreachable" in ic.failure_message(summary["failed"])


def test_one_collection_whose_check_fails_does_not_stop_the_others():
    def detect(members):
        if members[0][0]["src_database"] == "bad":
            raise ValueError("malformed control row")
        return _detector({"quiet": 10})(members)

    configs = [_cfg(1, "bad", "a", "c1"), _cfg(2, "quiet", "a", "c2"), _cfg(3, "new", "a", "c3")]
    store = _Store(configs, _recorded(configs, {1: _cp(10), 2: _cp(10)}))
    summary = ic.run_check(store, store, ["c1", "c2", "c3"], detect, existing_sinks_of=_sinks)
    assert [f["collection"] for f in summary["failed"]] == ["c1"]
    assert [(r["collection"], r["work"]) for r in summary["per_collection"]] == [("c2", []), ("c3", [[SOURCE, "new"]])]
    assert store.stamped["c2"] == ["quiet"] and sorted(store.unlocked) == ["c1", "c2", "c3"]


def test_a_collection_with_work_keeps_its_lock_until_its_run_is_started():
    configs = [_cfg(1, "quiet", "a", "c1"), _cfg(2, "new", "a", "c2")]
    store = _Store(configs, _recorded(configs, {1: _cp(10)}))
    summary = ic.run_check(store, store, ["c1", "c2"], _detector({"quiet": 10}), existing_sinks_of=_sinks,
                           hold_work_locks=True)
    # c2 has work: its lock stays held, so a second check of c2 meanwhile finds it busy and starts nothing.
    assert summary["held"] == ["c2"] and store.unlocked == ["c1"]
    store.held.add("c2")
    again = ic.run_check(store, store, ["c2"], _detector({}), existing_sinks_of=_sinks, hold_work_locks=True)
    assert again["busy"] == ["c2"] and again["per_collection"] == []
    ic.release_locks(store, summary["held"])
    assert store.unlocked == ["c1", "c2"]


def test_the_checks_source_queries_take_their_share_of_the_per_server_cap():
    import threading
    import time as clock

    joined, in_flight, most = [], [0], [0]
    mu = threading.Lock()

    def detect(members):
        with mu:
            in_flight[0] += 1
            most[0] = max(most[0], in_flight[0])
        clock.sleep(0.02)
        with mu:
            in_flight[0] -= 1
        return _detector({c["src_database"]: 10 for c, _ in members})(members)

    # Two sessions read the server (an ingest run and this check): a cap of 4 gives the check 2 at a time.
    cap = ic.SourceCap(4, join=lambda sources: joined.extend(sources) or {s: 11 for s in sources},
                       runs=lambda keys: {11: 2}, source_of=lambda cfg: "connection:sql_a")
    configs = [_cfg(i, f"db{i}", "a", "c1") for i in range(1, 9)]
    store = _Store(configs, _recorded(configs, {i: _cp(10) for i in range(1, 9)}))
    with ThreadPoolExecutor(max_workers=8) as pool:
        summary = ic.run_check(store, store, ["c1"], detect, existing_sinks_of=_sinks, map_fn=pool.map,
                               source_cap=cap)
    assert joined == ["connection:sql_a"] and cap.limiter.limit("connection:sql_a") == 2
    assert most[0] == 2 and summary["unchanged_databases"] == 8


def test_without_a_cap_the_check_leaves_detection_as_it_is():
    cap = ic.SourceCap(0, join=None, runs=None, source_of=None)
    detect = _detector({})
    assert cap.wrap([], detect) is detect


def test_when_the_sessions_cant_be_counted_the_check_takes_the_whole_cap():
    def refuse(keys):
        raise RuntimeError("pg_locks unavailable")

    cap = ic.SourceCap(6, join=lambda sources: {s: 1 for s in sources}, runs=refuse, source_of=lambda cfg: "host:h:1433")
    cap.wrap([_cfg(1, "db1", "a")], _detector({}))
    assert cap.limiter.limit("host:h:1433") == 6


def test_a_check_closes_the_runs_a_dead_sweep_left_open_once_it_holds_the_lock():
    configs = [_cfg(1, "quiet", "a", "c1")]
    store = _Store(configs, _recorded(configs, {1: _cp(10)}))
    summary = ic.run_check(store, store, ["c1"], _detector({"quiet": 10}), existing_sinks_of=_sinks)
    assert store.closed == [("c1", 1)] and summary["per_collection"][0]["run_id"] == 1


def test_a_check_whose_lock_connection_dropped_writes_nothing_and_fails():
    configs = [_cfg(1, "quiet", "a", "c1")]
    store = _Store(configs, _recorded(configs, {1: _cp(10)}))
    store.alive = False                       # the session lock went with the dropped connection
    summary = ic.run_check(store, store, ["c1"], _detector({"quiet": 10}), existing_sinks_of=_sinks)
    assert [f["collection"] for f in summary["failed"]] == ["c1"] and "lock" in summary["failed"][0]["error"]
    assert summary["per_collection"] == [] and store.advanced == [] and store.stamped == {}
    assert store.finished[1]["status"] == "failed" and store.unlocked == ["c1"]


def test_a_failed_unlock_is_reported_and_does_not_hide_the_check_result():
    configs = [_cfg(1, "quiet", "a", "c1")]
    store = _Store(configs, _recorded(configs, {1: _cp(10)}))

    def dead_unlock(collection):
        raise RuntimeError("connection closed")

    store.unlock_collection = dead_unlock
    summary = ic.run_check(store, store, ["c1"], _detector({"quiet": 10}), existing_sinks_of=_sinks)
    assert summary["per_collection"][0]["work"] == [] and store.finished[1]["status"] == "completed"

    def broken(members):
        raise RuntimeError("source unreachable")

    summary = ic.run_check(store, store, ["c1"], broken, existing_sinks_of=_sinks)
    assert "source unreachable" in summary["failed"][0]["error"]


def test_a_failing_close_of_abandoned_runs_does_not_stop_the_check():
    configs = [_cfg(1, "quiet", "a", "c1")]
    store = _Store(configs, _recorded(configs, {1: _cp(10)}))

    def broken_close(collection, run_id):
        raise RuntimeError("telemetry down")

    store.close_abandoned_runs = broken_close
    summary = ic.run_check(store, store, ["c1"], _detector({"quiet": 10}), existing_sinks_of=_sinks)
    assert summary["per_collection"][0]["work"] == [] and store.finished[1]["status"] == "completed"


def test_a_database_check_stamp_names_the_check_run_that_wrote_it():
    configs = [_cfg(1, "quiet", "a", "c1")]
    store = _Store(configs, _recorded(configs, {1: _cp(10)}))
    stamped_by = []
    store.stamp_databases = lambda collection, stamps, run_id: stamped_by.append(run_id)
    ic.run_check(store, store, ["c1"], _detector({"quiet": 10}), existing_sinks_of=_sinks)
    assert stamped_by == [1]        # the check's own ingest_run row, so database_check.run_id is filled in


def test_weights_count_the_changed_tables_and_the_first_loads_of_each_database():
    configs = [_cfg(1, "busy", "a"), _cfg(2, "busy", "b"), _cfg(3, "busy", "c"),
               _cfg(4, "new", "a"), _cfg(5, "new", "b"),
               _cfg(6, "forced", "a"), _cfg(7, "forced", "b"), _cfg(8, "quiet", "a")]
    cps = _recorded(configs, {1: _cp(20), 2: _cp(20), 3: _cp(20), 6: _cp(5), 7: _cp(5), 8: _cp(10)})
    detect = _detector({"busy": 21, "quiet": 10, "forced": 5}, changed={1, 3})
    result = ic.check_collection(configs, cps, detect, existing_sinks=_sinks(configs), force={"forced"}, now=NOW)
    # busy: 2 of its 3 tables changed; new: both never loaded; forced: sent whole, every table counts.
    assert result["weights"] == [[SOURCE, "busy", 2, 0], [SOURCE, "forced", 2, 0], [SOURCE, "new", 2, 2]]


def _work(collection, *databases):
    """A collection's check result with these (database, changed tables[, first loads]) as its work."""
    return {"collection": collection,
            "weights": [[SOURCE, name, tables, rest[0] if rest else 0] for name, tables, *rest in databases]}


def _placed(runs):
    return [tuple(d) for run in runs for d in run["databases"]]


def test_changes_in_one_group_are_shared_over_as_many_runs_as_their_tables_need():
    work = [_work("g1", *[(f"db{i:02d}", 250) for i in range(12)])]           # 3,000 changed tables, one group
    runs = ic.plan_cycle(work, group_size=1000)
    assert [run["tables"] for run in runs] == [1000, 1000, 1000]
    assert all(run["collections"] == ["g1"] and not run["first_loads"] for run in runs)
    placed = _placed(runs)
    assert len(placed) == 12 and len(set(placed)) == 12                       # each database whole, on one run


def test_small_changes_in_many_groups_share_one_run():
    work = [_work(f"g{n}", (f"db{n}", 5)) for n in range(1, 9)]
    runs = ic.plan_cycle(work, group_size=12500)
    assert len(runs) == 1 and runs[0]["tables"] == 40
    assert runs[0]["collections"] == [f"g{n}" for n in range(1, 9)]


def test_a_group_with_a_first_load_keeps_a_run_of_its_own():
    work = [_work("g1", ("new_db", 1000, 1000), ("db_b", 3)), _work("g2", ("db_c", 4)), _work("g3", ("db_d", 2))]
    runs = ic.plan_cycle(work, group_size=1000)
    assert runs[0] == {"databases": [["g1", SOURCE, "new_db"], ["g1", SOURCE, "db_b"]], "collections": ["g1"],
                       "tables": 1003, "first_loads": True}
    assert runs[1:] == [{"databases": [["g2", SOURCE, "db_c"], ["g3", SOURCE, "db_d"]], "collections": ["g2", "g3"],
                         "tables": 6, "first_loads": False}]


def test_a_database_bigger_than_a_share_runs_alone_so_a_cycle_can_take_one_run_more():
    work = [_work("g1", ("huge", 1500), ("a", 200), ("b", 200), ("c", 100))]     # 2,000 tables: two runs by size
    runs = ic.plan_cycle(work, group_size=1000)
    assert [[d[2] for d in run["databases"]] for run in runs] == [["huge"], ["a", "b", "c"]]
    work = [_work("g1", ("huge", 1800), ("big", 1100), ("a", 50), ("b", 50))]      # 3,000 tables, share 1,000
    runs = ic.plan_cycle(work, group_size=1000)
    assert [[d[2] for d in run["databases"]] for run in runs] == [["huge"], ["big"], ["a", "b"]]


def test_the_same_work_always_makes_the_same_runs():
    work = [_work("g2", ("x", 7), ("y", 7)), _work("g1", ("z", 7), ("w", 7))]
    assert ic.plan_cycle(work, 14) == ic.plan_cycle(list(reversed(work)), 14)
    assert ic.plan_cycle([], 14) == [] and ic.plan_cycle([_work("g1")], 14) == []


def test_a_jobs_group_size_is_its_tables_over_its_groups():
    assert ic.group_size_of({"g1": 12500, "g2": 12400, "g3": 100}) == 8334
    assert ic.group_size_of({}) == 1 and ic.group_size_of({"g1": 0}) == 1


class _SharedStore(_Store):
    """The check's Lakebase calls with the shared and per-database locks of groups B: ``busy`` are the databases a
    run of an earlier cycle holds, ``others`` the collections another session holds shared."""

    def __init__(self, configs, checkpoints, busy=(), others=()):
        super().__init__(configs, checkpoints)
        self.busy, self.others, self.calls = set(busy), set(others), []

    def try_lock_collection_shared(self, collection):
        self.calls.append(("shared", collection))
        return True

    def unlock_collection_shared(self, collection):
        self.calls.append(("unshared", collection))

    def alone_on_collection(self, collection):
        return collection not in self.others

    def try_lock_databases(self, keys):
        return {k for k in keys if k.rsplit("|", 1)[1] not in self.busy}

    def unlock_databases(self, keys):
        self.calls.append(("unlock", tuple(sorted(keys))))


def test_with_shared_locks_the_check_leaves_out_the_databases_a_run_holds_and_lets_every_lock_go():
    configs = [_cfg(1, "quiet", "a"), _cfg(2, "busy", "a"), _cfg(3, "new", "a")]
    store = _SharedStore(configs, _recorded(configs, {1: _cp(10), 2: _cp(10)}), busy={"busy"}, others={"c1"})
    summary = ic.run_check(store, store, ["c1"], _detector({"quiet": 10, "busy": 11}), existing_sinks_of=_sinks,
                           job_run_id="77", share_locks=True, hold_work_locks=True)
    result = summary["per_collection"][0]
    assert result["work"] == [[SOURCE, "new"]] and result["busy_databases"] == 1 and result["tables"] == 3
    assert store.stamped == {"c1": ["quiet"]}                         # busy is neither checked nor stamped
    assert store.closed == []                                         # another run holds c1: no abandoned-run close
    assert store.calls[0] == ("shared", "c1") and store.calls[-1] == ("unshared", "c1")
    assert ("unlock", tuple(sorted(ss.database_lock_key("c1", SOURCE, d) for d in ("new", "quiet")))) in store.calls
    assert summary["held"] == [] and store.unlocked == []              # nothing held for the runs: they lock their own


def test_a_run_of_a_share_takes_its_collections_in_turn_and_fails_only_for_a_part_that_failed():
    assert planner.cycle_parts({("g2", "s", "a"), ("g1", "s", "b"), ("g2", "s", "c")}) == ["g1", "g2"]
    summary = planner.cycle_summary("w1", {"g1": {"run_id": 5, "total": 3, "ok": 2, "failed": 1},
                                           "g2": {"run_id": 6, "status": "skipped"},
                                           "g3": {"status": "failed", "error": "boom"}})
    assert summary["failed"] == ["g3"] and summary["skipped"] == ["g2"]   # g1's failed table isn't a failed part
    assert summary["tables"] == 3 and summary["ok"] == 2 and summary["run_ids"] == {"g1": 5, "g2": 6, "g3": None}
