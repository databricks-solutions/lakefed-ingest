"""Skip-no-change planner: routing, per-database versions, and per-database failure isolation."""
import planner
import state_store as ss


def _cfg(cid, db, table, host="h1", sl="id, v"):
    return {"id": cid, "src_host": host, "src_port": 1433, "src_database": db, "secret_key": f"k-{db}",
            "src_schema": "dbo", "src_table": table, "select_list": sl}


def _cp(v, phase=ss.CDC, sl="id, v"):
    return {"ct_version": v, "phase": phase, "status": "ok", "select_list": sl}


def test_route_tables():
    cfgs = [_cfg(1, "d1", "a"), _cfg(2, "d1", "b"), _cfg(3, "d1", "c", sl="id, v, w"),
            _cfg(4, "d1", "d"), _cfg(5, "d1", "e")]
    cps = {2: _cp(5, phase=ss.SEEDING), 3: _cp(5), 4: _cp(5, sl=None), 5: _cp(5)}
    plans, candidates, legacy = planner.route_tables(cfgs, cps)
    routes = {p["cfg"]["id"]: p["route"] for p in plans}
    assert routes == {1: "seed", 2: "seed", 3: "schema_reseed"}
    assert [c["id"] for c, _ in legacy] == [4]
    assert [c["id"] for c, _ in candidates] == [5]


def _two_groups():
    g1 = [(_cfg(1, "d1", "a"), _cp(100)), (_cfg(2, "d1", "b"), _cp(100))]
    g2 = [(_cfg(11, "d2", "a", host="h2"), _cp(7)), (_cfg(12, "d2", "b", host="h2"), _cp(7))]
    return g1, g2


def test_group_by_source_splits_databases():
    g1, g2 = _two_groups()
    groups = planner.group_by_source(g1 + g2)
    assert len(groups) == 2
    assert sorted(len(v) for v in groups.values()) == [2, 2]


def test_each_group_uses_its_own_version():
    g1, g2 = _two_groups()
    versions = {"d1": 150, "d2": 9}

    def current_version(cfg):
        return versions[cfg["src_database"]]

    def detect(cfg, items):   # table 'a' changed in each db, 'b' unchanged
        return ({cid: (1, 1 if t == "a" else 0) for cid, _, t, _ in items}, [], 1)

    outs = [planner.detect_group(m, current_version, detect) for m in (g1, g2)]
    agg = planner.merge_group_results(outs)
    # unchanged rows carry THEIR database's version — never one value across databases
    assert sorted(agg["unchanged"]) == [(2, 150, None), (12, 9, None)]
    merges = {p["cfg"]["id"]: p["cur"] for p in agg["plans"] if p["route"] == "merge"}
    assert merges == {1: 150, 11: 9}
    assert agg["queries"] == 4 and agg["failed"] == []


def test_fast_exit_skips_detection():
    g1, _ = _two_groups()
    called = []
    out = planner.detect_group(g1, lambda c: 100, lambda c, i: called.append(1))
    assert out["fast_exit"] and called == []
    assert out["unchanged"] == [(1, 100, None), (2, 100, None)] and out["queries"] == 1


def test_failing_database_is_isolated():
    g1, g2 = _two_groups()

    def current_version(cfg):
        if cfg["src_database"] == "d2":
            raise RuntimeError("Login failed for user")
        return 150

    detect = lambda cfg, items: ({cid: (1, 0) for cid, *_ in items}, [], 1)
    outs = [planner.detect_group(m, current_version, detect) for m in (g1, g2)]
    agg = planner.merge_group_results(outs)
    assert [c["id"] for c, _, _ in agg["failed"]] == [11, 12]
    assert all("Login failed" in err for _, _, err in agg["failed"])
    # the healthy database is classified and bookkept exactly as if alone
    assert sorted(agg["unchanged"]) == [(1, 150, None), (2, 150, None)]
    assert agg["failed_databases"] == 1


def test_detection_failure_for_whole_group_is_isolated():
    g1, _ = _two_groups()

    def boom(cfg, items):
        raise RuntimeError("connection reset")
    out = planner.detect_group(g1, lambda c: 150, boom)
    assert [c["id"] for c, _, _ in out["failed"]] == [1, 2] and out["unchanged"] == []


def test_chunk_failure_and_missing_answers_fall_back_never_guess():
    g1, _ = _two_groups()
    # table 1: its chunk failed; table 2: no row returned -> both per-table fallback
    out = planner.detect_group(g1, lambda c: 150, lambda c, i: ({}, [1], 1))
    assert sorted(p["cfg"]["id"] for p in out["plans"] if p["route"] == "fallback") == [1, 2]
    assert out["unchanged"] == [] and out["fallback"] == 2


def test_reseed_classification_and_legacy_select_list_recording():
    g1, _ = _two_groups()
    # table 1 retention lapsed (checkpoint 100 < mv 120); table 2 unchanged and legacy -> record sl
    out = planner.detect_group(g1, lambda c: 150, lambda c, i: ({1: (120, None), 2: (1, 0)}, [], 1),
                               record_sl={2})
    assert [p["route"] for p in out["plans"]] == ["retention_reseed"]
    assert out["unchanged"] == [(2, 150, "id, v")]
