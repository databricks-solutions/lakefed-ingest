"""Skip-no-change planner: routing, per-database versions, and per-database failure isolation."""
import planner
import state_store as ss


def _cfg(cid, db, table, host="h1", sl="id, v"):
    return {"id": cid, "src_host": host, "src_port": 1433, "src_database": db, "secret_key": f"k-{db}",
            "src_schema": "dbo", "src_table": table, "select_list": sl}


def _cp(v, phase=ss.CDC, sl="id, v", strategy="ct", begin=None):
    return {"position": None if v is None else str(v), "phase": phase, "status": "ok",
            "select_list": sl, "strategy": strategy, "ct_begin_version": begin}


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
    assert sorted(agg["unchanged"]) == [(2, 150, None, None), (12, 9, None, None)]
    merges = {p["cfg"]["id"]: p["cur"] for p in agg["plans"] if p["route"] == "merge"}
    assert merges == {1: 150, 11: 9}
    assert agg["queries"] == 4 and agg["failed"] == []


def test_fast_exit_skips_detection():
    g1, _ = _two_groups()
    called = []
    out = planner.detect_group(g1, lambda c: 100, lambda c, i: called.append(1))
    assert out["fast_exit"] and called == []
    assert out["unchanged"] == [(1, 100, None, None), (2, 100, None, None)] and out["queries"] == 1


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
    assert sorted(agg["unchanged"]) == [(1, 150, None, None), (2, 150, None, None)]
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
    assert out["unchanged"] == [(2, 150, "id, v", None)]


# ---- review fixes: CT disabled at the database; CT versions reset ---------------------------------

def test_ct_disabled_database_fails_loudly_never_unchanged():
    # A NULL CHANGE_TRACKING_CURRENT_VERSION() must NOT look like version 0 ("everything current").
    g1, _ = _two_groups()
    called = []
    out = planner.detect_group(g1, lambda c: None, lambda c, i: called.append(1))
    assert out["unchanged"] == [] and out["plans"] == []
    assert len(out["failed"]) == 2 and all("not enabled" in err for _, _, err in out["failed"])
    assert called == []                       # no detection attempted


def test_version_reset_routes_ahead_checkpoints_to_reseed():
    # checkpoint 100 > current version 40 => CT versions were reset: reseed, never "unchanged".
    g = [(_cfg(1, "d1", "a"), _cp(100)), (_cfg(2, "d1", "b"), _cp(30))]
    out = planner.detect_group(g, lambda c: 40, lambda c, i: ({2: (0, 0)}, [], 1))
    reseeds = [p for p in out["plans"] if p["route"] == "retention_reseed"]
    assert [p["cfg"]["id"] for p in reseeds] == [1] and reseeds[0]["reason"] == "ct_version_reset"
    assert [u[0] for u in out["unchanged"]] == [2]          # the other table still classified normally


def test_all_checkpoints_reset_skips_detection():
    g = [(_cfg(1, "d1", "a"), _cp(100))]
    called = []
    out = planner.detect_group(g, lambda c: 5, lambda c, i: called.append(1))
    assert [p["reason"] for p in out["plans"]] == ["ct_version_reset"] and called == []


# ---- strategies: support matrix enforced per table; full reloads; groups per (db, load_type) ------

def test_route_tables_mixed_strategies_and_sources():
    cfgs = [
        _cfg(1, "d1", "a"),                                                   # ct, no checkpoint
        dict(_cfg(2, "d1", "b"), load_type="full"),                            # sqlserver full
        dict(_cfg(3, "svc", "EMP"), src_type="oracle", load_type="full"),     # oracle full
        dict(_cfg(4, "d1", "c"), src_type="postgresql", load_type="ct"),      # ct on postgres: no
        dict(_cfg(5, "d1", "d"), src_type="mysql", load_type="cursor"),       # placeholder (mysql)
        dict(_cfg(6, "d1", "e"), load_type="cdc"),                             # placeholder
        dict(_cfg(7, "d1", "f"), src_type="db2", load_type="full"),            # unknown adapter
        _cfg(8, "d1", "g"),                                                   # ct, cdc phase
    ]
    cps = {2: _cp(None, phase=ss.LOADED, strategy="full"), 8: _cp(5)}
    plans, candidates, legacy = planner.route_tables(cfgs, cps)
    routes = {p["cfg"]["id"]: p["route"] for p in plans}
    assert routes == {1: "seed", 2: "full", 3: "full", 4: "unsupported", 5: "unsupported",
                      6: "unsupported", 7: "unsupported"}
    errors = {p["cfg"]["id"]: p["error"] for p in plans if p["route"] == "unsupported"}
    assert "not supported for src_type 'postgresql'" in errors[4]
    assert "'cursor' is not implemented" in errors[5] and "'cdc' is not implemented" in errors[6]
    assert "unsupported src_type 'db2'" in errors[7]
    assert [c["id"] for c, _ in candidates] == [8] and legacy == []


def test_full_routes_every_cycle_regardless_of_checkpoint():
    cfg = dict(_cfg(1, "d1", "a"), load_type="full")
    for cp in ({}, {1: _cp(None, phase=ss.LOADED, strategy="full")}, {1: _cp(5)}):
        plans, candidates, _ = planner.route_tables([cfg], cp)
        assert [p["route"] for p in plans] == ["full"] and candidates == []


def test_ct_checkpoint_from_another_strategy_reseeds():
    # load_type switched full -> ct: the 'loaded' full checkpoint can't be resumed as CT
    plans, candidates, _ = planner.route_tables([_cfg(1, "d1", "a")],
                                                {1: _cp(None, phase=ss.LOADED, strategy="full")})
    assert [p["route"] for p in plans] == ["seed"] and candidates == []
    # a cdc-phase row whose recorded strategy is not ct also seeds
    plans, _, _ = planner.route_tables([_cfg(1, "d1", "a")], {1: _cp(5, strategy="cursor")})
    assert [p["route"] for p in plans] == ["seed"]


# ---- cursor (high-watermark) routing ---------------------------------------------------------------

def _cur_cfg(cid=1, col="updated_at", sl="id, v"):
    return dict(_cfg(cid, "d1", "t", sl=sl), load_type="cursor", cursor_column=col, primary_key="id")


def _cur_cp(col="updated_at", value="2026-01-01T00:00:00", sl="id, v", phase=ss.CDC, strategy="cursor"):
    import cursor_engine
    return {"position": cursor_engine.encode_position(col, "datetime2", value), "phase": phase,
            "status": "ok", "select_list": sl, "strategy": strategy, "ct_begin_version": None}


def test_cursor_routes_seed_then_increment_never_ct_detection():
    plans, candidates, legacy = planner.route_tables([_cur_cfg()], {})
    assert [p["route"] for p in plans] == ["cursor_seed"] and candidates == [] and legacy == []
    plans, candidates, _ = planner.route_tables([_cur_cfg()], {1: _cur_cp()})
    assert [p["route"] for p in plans] == ["cursor"] and candidates == []
    assert plans[0]["position"] == {"col": "updated_at", "type": "datetime2", "value": "2026-01-01T00:00:00"}


def test_cursor_reseeds_on_unusable_or_foreign_checkpoint():
    for cp in (_cur_cp(phase=ss.SEEDING),                        # reseed queued
               _cur_cp(strategy="ct"),                           # load_type switched
               dict(_cur_cp(), position="12345"),                # a CT position
               _cur_cp(col="other_col")):                        # cursor_column changed
        plans, _, _ = planner.route_tables([_cur_cfg()], {1: cp})
        assert [p["route"] for p in plans] == ["cursor_seed"], cp
    plans, _, _ = planner.route_tables([_cur_cfg(col="Updated_At")], {1: _cur_cp()})   # case-insensitive
    assert [p["route"] for p in plans] == ["cursor"]


def test_cursor_select_list_growth_routes_to_schema_reseed():
    plans, _, _ = planner.route_tables([_cur_cfg(sl="id, v, w")], {1: _cur_cp()})
    assert [(p["route"], p["added"]) for p in plans] == [("schema_reseed", ["w"])]


def test_group_by_source_keys_on_database_and_load_type():
    a = (_cfg(1, "d1", "a"), _cp(5))
    b = (dict(_cfg(2, "d1", "b"), load_type="cdc"), _cp(5))
    groups = planner.group_by_source([a, b])
    assert len(groups) == 2 and {k[1] for k in groups} == {"ct", "cdc"}


def test_ct_position_decodes_text_and_null():
    assert planner.ct_position({"position": "287493"}) == 287493
    assert planner.ct_position({"position": None}) == 0


# ---- per-table CT status before the fast exit ------------------------------------------------------

def test_table_with_ct_disabled_reseeds_even_on_a_quiet_database():
    # DB version unchanged (fast-exit territory), but one table lost CT: route it to reseed (its seed
    # then refuses to run until CT is back), never unchanged.
    g = [(_cfg(1, "d1", "a"), _cp(100)), (_cfg(2, "d1", "b"), _cp(100))]
    status = {("dbo", "b"): (0, 0)}                  # table "a" missing => CT disabled on it
    out = planner.detect_group(g, lambda c: 100, lambda c, i: ({}, [], 0), ct_status=lambda c: status)
    assert [(p["cfg"]["id"], p["route"], p["reason"]) for p in out["plans"]] == \
        [(1, "retention_reseed", "ct_disabled")]
    assert out["failed"] == [] and [u[0] for u in out["unchanged"]] == [2] and out["fast_exit"] is True


def test_ct_reenabled_or_retention_lapsed_routes_to_reseed_on_quiet_database():
    g = [(_cfg(1, "d1", "a"), _cp(100)), (_cfg(2, "d1", "b"), _cp(100))]
    status = {("dbo", "a"): (150, 0), ("dbo", "b"): (50, 0)}   # a: min valid passed its checkpoint
    out = planner.detect_group(g, lambda c: 100, lambda c, i: ({}, [], 0), ct_status=lambda c: status)
    assert [(p["cfg"]["id"], p["reason"]) for p in out["plans"]] == [(1, "retention_miss")]
    assert [u[0] for u in out["unchanged"]] == [2]


def test_ct_status_query_failure_fails_group_instead_of_fast_exit():
    g = [(_cfg(1, "d1", "a"), _cp(100))]

    def boom(c):
        raise RuntimeError("permission denied")
    out = planner.detect_group(g, lambda c: 100, lambda c, i: ({}, [], 0), ct_status=boom)
    assert out["unchanged"] == [] and len(out["failed"]) == 1 and "status query failed" in out["failed"][0][2]


def test_ct_status_matches_names_case_insensitively():
    g = [(_cfg(1, "d1", "MixedCase"), _cp(100))]
    out = planner.detect_group(g, lambda c: 100, lambda c, i: ({}, [], 0),
                               ct_status=lambda c: {("dbo", "mixedcase"): (0, 0)})
    assert out["failed"] == [] and [u[0] for u in out["unchanged"]] == [1]


def test_ct_reenabled_between_cycles_reseeds_when_begin_version_moved():
    # CT turned off and on with no tracked commits: min_valid == begin == checkpoint, DB version
    # unchanged. Only the begin_version recorded at seed time reveals it.
    g = [(_cfg(1, "d1", "a"), _cp(495, begin=0)), (_cfg(2, "d1", "b"), _cp(495, begin=0))]
    status = {("dbo", "a"): (495, 495), ("dbo", "b"): (0, 0)}
    out = planner.detect_group(g, lambda c: 495, lambda c, i: ({}, [], 0), ct_status=lambda c: status)
    assert [(p["cfg"]["id"], p["reason"]) for p in out["plans"]] == [(1, "ct_reenabled")]
    assert [u[0] for u in out["unchanged"]] == [2]


def test_legacy_checkpoint_records_current_begin_version_on_advance():
    g = [(_cfg(1, "d1", "a"), _cp(100))]            # no recorded begin_version yet
    out = planner.detect_group(g, lambda c: 100, lambda c, i: ({}, [], 0),
                               ct_status=lambda c: {("dbo", "a"): (0, 7)})
    assert out["unchanged"] == [(1, 100, None, 7)]


def test_merge_plan_carries_begin_version():
    g = [(_cfg(1, "d1", "a"), _cp(100))]
    out = planner.detect_group(g, lambda c: 150, lambda c, i: ({1: (0, 1)}, [], 1),
                               ct_status=lambda c: {("dbo", "a"): (0, 7)})
    assert [(p["route"], p["ct_begin"]) for p in out["plans"]] == [("merge", 7)]


def test_position_from_never_decodes_another_strategys_checkpoint():
    import cursor_engine
    cur_cp = {"position": cursor_engine.encode_position("rv", "timestamp", "9"), "phase": ss.CDC,
              "strategy": "cursor"}
    # load_type switched cursor -> ct: the seed must not int() the cursor JSON
    assert planner.position_from({"route": "seed", "cp": cur_cp}) is None
    assert planner.position_from({"route": "schema_reseed", "cp": cur_cp}) is None
    assert planner.position_from({"route": "cursor", "cp": cur_cp,
                                  "position": cursor_engine.decode_position(cur_cp["position"])}) == "9"
    assert planner.position_from({"route": "cursor_seed", "cp": cur_cp}) is None
    assert planner.position_from({"route": "full", "cp": _cp(5)}) is None
    assert planner.position_from({"route": "merge", "cp": _cp(5)}) == 5
    assert planner.position_from({"route": "seed", "cp": None}) == 0


# ---- consolidated sinks: sink-ownership checks before any work -------------------------------------

def _sink(cfg, table="orders", client=None):
    return dict(cfg, sink_catalog="Main", sink_schema="Bronze", sink_table=table, client_id=client)


def test_mixed_sink_rows_fail_at_plan_others_proceed():
    cfgs = [_sink(_cfg(1, "d1", "a"), client="c1"), _sink(_cfg(2, "d2", "a"), table="other")]
    plans, candidates, _ = planner.route_tables(cfgs, {}, mixed_sinks={("main", "bronze", "orders")})
    routes = {p["cfg"]["id"]: p["route"] for p in plans}
    assert routes == {1: "unsupported", 2: "seed"}
    assert "claimed by a dedicated row" in plans[0]["error"]


def test_consolidated_row_must_not_select_client_id_and_must_be_valid():
    bad_sl = _sink(_cfg(1, "d1", "a", sl="id, __Client_ID__, v"), client="c1")
    bad_id = _sink(_cfg(2, "d1", "b"), client="c 1; drop")
    ok = _sink(_cfg(3, "d1", "c", sl="id, client_id, v"))         # dedicated: a native client_id is fine
    ok_cons = _sink(_cfg(4, "d1", "d", sl="id, client_id, v"), client="c1")   # ...and when consolidated
    plans, _, _ = planner.route_tables([bad_sl, bad_id, ok, ok_cons], {})
    errs = {p["cfg"]["id"]: p.get("error") for p in plans}
    assert "remove it from select_list" in errs[1] and "invalid client_id" in errs[2]
    assert errs[3] is None and errs[4] is None
