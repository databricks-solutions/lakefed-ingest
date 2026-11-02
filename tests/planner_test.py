"""Skip-no-change planner: routing, per-database versions, and per-database failure isolation."""
import pytest

import planner
import state_store as ss


def _cfg(cid, db, table, host="h1", sl="id, v"):
    return {"id": cid, "src_host": host, "src_port": 1433, "src_database": db, "secret_key": f"k-{db}",
            "src_schema": "dbo", "src_table": table, "select_list": sl, "primary_key": "id"}


def _cp(v, phase=ss.INCREMENTAL, sl="id, v", strategy="ct", begin=None):
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
    assert sorted(u[:4] for u in agg["unchanged"]) == [(2, 150, None, None), (12, 9, None, None)]
    merges = {p["cfg"]["id"]: p["cur"] for p in agg["plans"] if p["route"] == "merge"}
    assert merges == {1: 150, 11: 9}
    assert agg["queries"] == 4 and agg["failed"] == []


def test_fast_exit_skips_detection():
    g1, _ = _two_groups()
    called = []
    out = planner.detect_group(g1, lambda c: 100, lambda c, i: called.append(1))
    assert out["fast_exit"] and called == []
    assert [u[:4] for u in out["unchanged"]] == [(1, 100, None, None), (2, 100, None, None)] and out["queries"] == 1


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
    assert sorted(u[:4] for u in agg["unchanged"]) == [(1, 150, None, None), (2, 150, None, None)]
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
    assert [u[:4] for u in out["unchanged"]] == [(2, 150, "id, v", None)]


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
        _cfg(8, "d1", "g"),                                                   # ct, incremental phase
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


def _cur_cp(col="updated_at", value="2026-01-01T00:00:00", sl="id, v", phase=ss.INCREMENTAL, strategy="cursor"):
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
    assert [u[:4] for u in out["unchanged"]] == [(1, 100, None, 7)]


def test_merge_plan_carries_begin_version():
    g = [(_cfg(1, "d1", "a"), _cp(100))]
    out = planner.detect_group(g, lambda c: 150, lambda c, i: ({1: (0, 1)}, [], 1),
                               ct_status=lambda c: {("dbo", "a"): (0, 7)})
    assert [(p["route"], p["ct_begin"]) for p in out["plans"]] == [("merge", 7)]


def test_position_from_never_decodes_another_strategys_checkpoint():
    import cursor_engine
    cur_cp = {"position": cursor_engine.encode_position("rv", "timestamp", "9"), "phase": ss.INCREMENTAL,
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


def test_missing_primary_key_fails_at_plan_for_merge_strategies_only():
    ct = dict(_cfg(1, "d1", "a"), primary_key="")
    cursor = dict(_cfg(2, "d1", "b"), load_type="cursor", cursor_column="v", primary_key=None)
    full = dict(_cfg(3, "d1", "c"), load_type="full", primary_key=None)
    assert "needs a primary_key" in planner.config_error(ct)
    assert "needs a primary_key" in planner.config_error(cursor)
    assert planner.config_error(full) is None
    plans, _, _ = planner.route_tables([ct, full], {})
    assert {p["cfg"]["id"]: p["route"] for p in plans} == {1: "unsupported", 3: "full"}


# ---- append write mode -----------------------------------------------------------------------------

def test_pending_append_is_verified_before_anything_else():
    cfg = dict(_cfg(1, "d1", "a"), write_mode="append")
    for cp in (dict(_cp(100), pending_position="150", pending_run_id=9),                  # change append
               dict(_cp(None, phase=ss.SEEDING), pending_position="80", pending_run_id=9)):  # seed append
        plans, candidates, _ = planner.route_tables([cfg], {1: cp})
        assert [p["route"] for p in plans] == ["pending"] and candidates == []
    # merge mode never records one; a stray value is ignored (the next checkpoint advance clears it)
    plans, candidates, _ = planner.route_tables([_cfg(1, "d1", "a")], {1: dict(_cp(100), pending_run_id=9)})
    assert plans == [] and [c["id"] for c, _ in candidates] == [1]


def test_new_columns_carry_on_when_reseed_on_new_columns_is_off():
    cfgs = [_cfg(1, "d1", "a", sl="id, v, w"), _cur_cfg(cid=2, sl="id, v, w")]
    cps = {1: _cp(5), 2: _cur_cp()}
    plans, candidates, _ = planner.route_tables(cfgs, cps, reseed_on_new_columns=False)
    assert [p["route"] for p in plans] == ["cursor"] and [c["id"] for c, _ in candidates] == [1]
    plans, _, _ = planner.route_tables(cfgs, cps)
    assert [p["route"] for p in plans] == ["schema_reseed", "schema_reseed"]


def test_unchanged_rows_record_a_select_list_that_differs_from_the_recorded_one():
    g = [(_cfg(1, "d1", "a", sl="id, v, w"), _cp(100)), (_cfg(2, "d1", "b"), _cp(100))]
    out = planner.detect_group(g, lambda c: 100, lambda c, i: ({}, [], 0))
    assert [u[:4] for u in out["unchanged"]] == [(1, 100, "id, v, w", None), (2, 100, None, None)]


def test_append_config_errors():
    ok = dict(_cfg(1, "d1", "a"), write_mode="append", client_attrs='{"tenant_name": "x"}')
    assert planner.config_error(ok) is None and planner.config_error(dict(ok, load_type="full")) is None
    cursor = dict(ok, load_type="cursor", cursor_column="v")
    assert "supports load_type ct and full" in planner.config_error(cursor)
    assert "invalid write_mode" in planner.config_error(dict(ok, write_mode="upsert"))
    assert "invalid client_attrs column" in planner.config_error(dict(ok, client_attrs='{"__x": 1}'))
    clash = planner.config_error(dict(ok, select_list="id, Tenant_Name"))
    assert "tenant_name" in clash and "rename" in clash
    assert "__change_op" in planner.config_error(dict(ok, select_list="id, __change_op"))
    assert planner.config_error(dict(ok, write_mode="merge", select_list="id, __change_op")) is None


# ---- scale controls ------------------------------------------------------------------------------------

def test_database_state_answers_version_and_table_status_in_one_query():
    g = [(_cfg(1, "d1", "a"), _cp(100)), (_cfg(2, "d1", "b"), _cp(100))]
    calls = []

    def state(c):
        calls.append(c["src_database"])
        return 100, {("dbo", "b"): (0, 0)}                # table "a" lost CT
    out = planner.detect_group(g, None, lambda c, i: ({}, [], 0), database_state=state)
    assert calls == ["d1"] and out["queries"] == 1 and out["fast_exit"] is True
    assert [(p["cfg"]["id"], p["reason"]) for p in out["plans"]] == [(1, "ct_disabled")]
    assert [u[0] for u in out["unchanged"]] == [2]


def test_database_state_failure_or_ct_disabled_fails_the_group():
    g = [(_cfg(1, "d1", "a"), _cp(100))]

    def down(c):
        raise RuntimeError("Login failed for user")
    out = planner.detect_group(g, None, lambda c, i: ({}, [], 0), database_state=down)
    assert "Login failed" in out["failed"][0][2] and out["unchanged"] == []
    out = planner.detect_group(g, None, lambda c, i: ({}, [], 0), database_state=lambda c: (None, {}))
    assert "not enabled" in out["failed"][0][2]


def test_seed_budget_defers_the_rest_and_puts_failed_seeds_last():
    plans = [{"cfg": _cfg(i, "d1", "t"), "route": r, "cp": cp} for i, r, cp in (
        (1, "seed", {"status": "failed"}), (2, "merge", {}), (3, "seed", None), (4, "cursor_seed", None),
        (5, "seed", None))]
    kept, deferred = planner.apply_seed_budget(plans, 2)
    assert [p["cfg"]["id"] for p in kept] == [2, 3, 4] and [p["cfg"]["id"] for p in deferred] == [1, 5]
    assert planner.apply_seed_budget(plans, 0) == (plans, []) and planner.apply_seed_budget(plans, 4) == (plans, [])


def test_freshness_interval_skips_quiet_rows_whose_stamp_is_recent():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    recent, old = now - timedelta(minutes=10), now - timedelta(hours=3)
    cps = {1: dict(_cp(100, begin=7), last_success_at=recent),                    # quiet, stamped recently: skip
           2: dict(_cp(100, begin=7), last_success_at=old),                       # stamp too old
           3: dict(_cp(90, begin=7), last_success_at=recent),                     # checkpoint moves
           4: dict(_cp(100, begin=7), last_success_at=recent, status="failed"),   # status to clear
           5: dict(_cp(100, begin=7), last_success_at=recent),                    # records a select_list
           6: dict(_cp(100), last_success_at=recent)}                             # records its begin version
    rows = [(1, 100, None, 7), (2, 100, None, 7), (3, 100, None, 7), (4, 100, None, 7), (5, 100, "id, v", 7),
            (6, 100, None, 7)]
    assert [r[0] for r in planner.bulk_rows_to_write(rows, cps, 3600, now=now)] == [2, 3, 4, 5, 6]
    assert planner.bulk_rows_to_write(rows, cps, 0) == rows


def test_a_read_through_a_connection_needs_the_connection_and_no_snapshot_isolation():
    remote = dict(_cfg(1, "d1", "a"), use_remote_query=True, src_connection="c1", secret_key=None)
    assert planner.config_error(remote) is None
    assert "src_connection" in planner.config_error(dict(remote, src_connection=None))
    assert "snapshot_isolation is on" in planner.config_error(remote, snapshot_isolation=True)
    assert planner.config_error(dict(remote, load_type="full"), snapshot_isolation=True) is None   # no change reads
    assert planner.config_error(dict(remote, use_remote_query=False), snapshot_isolation=True) is None
    plans, candidates, _ = planner.route_tables([remote], {1: _cp(5)}, snapshot_isolation=True)
    assert [p["route"] for p in plans] == ["unsupported"] and candidates == []


# ---- identity backstop: the control row now points elsewhere than its checkpoint ------------------

def _ident_cfg(**kw):
    base = dict(_cfg(1, "d1", "a"), src_type="sqlserver", src_host="Srv1", src_port=None,
                sink_catalog="main", sink_schema="bronze", sink_table="a", primary_key="id")
    base.update(kw)
    return base


def _ident_cp(cfg, **kw):
    src, sink, key = ss.identity_values(cfg)
    return dict(_cp(5), source_identity=src, sink_identity=sink, merge_key=key, **kw)


@pytest.mark.parametrize("change,reason", [
    ({"sink_table": "a2"}, "sink_changed"),
    ({"client_id": "c9"}, "sink_changed"),
    ({"src_host": "srv2"}, "source_changed"),
    ({"src_port": 1544}, "source_changed"),
    ({"src_table": "other"}, "source_changed"),
    ({"primary_key": "id, v"}, "primary_key_changed"),
])
def test_edited_control_row_routes_to_reseed(change, reason):
    cfg = _ident_cfg()
    plans, candidates, _ = planner.route_tables([dict(cfg, **change)], {1: _ident_cp(cfg)})
    assert [(p["route"], p["reason"]) for p in plans] == [("reseed", reason)] and candidates == []


def test_moved_table_with_a_pending_append_reseeds_instead_of_settling_it():
    # The append in flight belongs to the old source or sink: settling it would continue from the old
    # position under the new identity. This holds however the row was edited (app, SQL, notebook).
    cfg = _ident_cfg(write_mode="append")
    cp = _ident_cp(cfg, pending_position="150", pending_run_id=9)
    for change, reason in (({"src_host": "srv2"}, "source_changed"), ({"sink_table": "a2"}, "sink_changed")):
        plans, _, _ = planner.route_tables([dict(cfg, **change)], {1: cp})
        assert [(p["route"], p["reason"]) for p in plans] == [("reseed", reason)]
    plans, _, _ = planner.route_tables([cfg], {1: cp}, existing_sinks=set())             # sink dropped
    assert [(p["route"], p["reason"]) for p in plans] == [("reseed", "sink_missing")]
    plans, _, _ = planner.route_tables([cfg], {1: cp}, existing_sinks={"main.bronze.a"})  # unchanged
    assert [p["route"] for p in plans] == ["pending"]


def test_cosmetic_edits_do_not_reseed():
    cfg = _ident_cfg()
    for change in ({"src_host": "SRV1"}, {"src_port": 1433}, {"primary_key": " ID "}, {"sink_schema": "BRONZE"},
                   {"task_collection": "moved"}, {"secret_key": "new-creds"}):
        plans, candidates, _ = planner.route_tables([dict(cfg, **change)], {1: _ident_cp(cfg)})
        assert plans == [] and len(candidates) == 1, change


def test_unrecorded_identity_never_reseeds_and_is_recorded_on_advance():
    cfg = _ident_cfg()
    plans, candidates, _ = planner.route_tables([dict(cfg, sink_table="zz")], {1: _cp(5)})   # legacy cp
    assert plans == [] and len(candidates) == 1
    out = planner.detect_group([(cfg, _cp(100))], lambda c: 100, lambda c, i: ({}, [], 0))
    assert out["unchanged"][0][4] == ss.identity_values(cfg)


def test_cursor_tables_get_the_same_backstop():
    import cursor_engine
    cfg = _ident_cfg(load_type="cursor", cursor_column="rv")
    cp = _ident_cp(cfg)
    cp.update(position=cursor_engine.encode_position("rv", "timestamp", "9"), strategy="cursor")
    plans, _, _ = planner.route_tables([dict(cfg, sink_schema="silver")], {1: cp})
    assert [(p["route"], p["reason"]) for p in plans] == [("reseed", "sink_changed")]
    assert planner.position_from(plans[0]) is None


def test_missing_sink_reseeds_incremental_tables_only():
    cfg = _ident_cfg()
    plans, candidates, _ = planner.route_tables([cfg], {1: _ident_cp(cfg)}, existing_sinks=set())
    assert [(p["route"], p["reason"]) for p in plans] == [("reseed", "sink_missing")] and candidates == []
    plans, candidates, _ = planner.route_tables([cfg], {1: _ident_cp(cfg)}, existing_sinks={"main.bronze.a"})
    assert plans == [] and len(candidates) == 1
    plans, candidates, _ = planner.route_tables([cfg], {1: _ident_cp(cfg)}, existing_sinks=None)   # unknown: skip
    assert plans == [] and len(candidates) == 1
    full = dict(cfg, load_type="full")
    plans, _, _ = planner.route_tables([full], {}, existing_sinks=set())
    assert [p["route"] for p in plans] == ["full"]
    seed, _, _ = planner.route_tables([cfg], {}, existing_sinks=set())                     # no checkpoint: seed
    assert [p["route"] for p in seed] == ["seed"]


def test_legacy_checkpoint_pointed_at_a_new_sink_is_caught_by_missing_sink():
    cfg = _ident_cfg(sink_table="moved")
    plans, _, _ = planner.route_tables([cfg], {1: _cp(5)}, existing_sinks={"main.bronze.a"})
    assert [(p["route"], p["reason"]) for p in plans] == [("reseed", "sink_missing")]


# ---- per-database check stamps ------------------------------------------------------------------
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

_NOW = _dt(2026, 10, 22, 12, 0, tzinfo=_tz.utc)


def _stamped(v, minutes_ago, **kw):
    """A quiet table's checkpoint at version v, its own stamp ``minutes_ago`` old, its identity recorded."""
    return {**_cp(v, begin=7), "last_success_at": _NOW - _td(minutes=minutes_ago), "source_identity": "src",
            "sink_identity": "sink", "merge_key": "id", **kw}


def _db(*tables, db="d1", host="h1"):
    return [_cfg(cid, db, name, host=host) for cid, name in tables]


def _state(cur, cfgs):
    """database_state for these tables: CT on for each, min valid 1, begin version 7."""
    return lambda c: (cur, {("dbo", c2["src_table"]): (1, 7) for c2 in cfgs})


def test_a_database_stamp_vouches_only_for_ct_tables_at_or_past_its_version():
    stamp = {"version": 100, "checked_at": _NOW}
    assert planner.stamp_vouches(_cp(100), stamp) and planner.stamp_vouches(_cp(130), stamp)
    assert not planner.stamp_vouches(_cp(99), stamp)                      # behind the checked version
    assert not planner.stamp_vouches(_cp(None), stamp)
    assert not planner.stamp_vouches(_cp(100, phase=ss.SEEDING), stamp)   # reseed queued since
    assert not planner.stamp_vouches(_cp(100, strategy="cursor"), stamp)
    assert not planner.stamp_vouches(_cp(100), None)
    own = _NOW - _td(hours=3)
    assert planner.last_success(dict(_cp(100), last_success_at=own), stamp) == _NOW
    assert planner.last_success(dict(_cp(99), last_success_at=own), stamp) == own
    assert planner.last_success(dict(_cp(100), last_success_at=None), None) is None
    assert planner.last_success(dict(_cp(100), last_success_at=_NOW), {"version": 1, "checked_at": own}) == _NOW


def test_a_whole_unchanged_database_writes_one_stamp_instead_of_a_row_per_table():
    cfgs = _db((1, "a"), (2, "b"), (3, "c"))
    cps = {1: _stamped(100, 5), 2: _stamped(100, 5), 3: _stamped(100, 5, status="failed")}
    members = [(c, cps[c["id"]]) for c in cfgs]
    out = planner.detect_group(members, None, None, database_state=_state(100, cfgs))
    assert out["fast_exit"]
    rows, stamps = planner.unchanged_writes([members], [out], cps, 0, now=_NOW)
    assert [r[0] for r in rows] == [3]            # only the failed table has something of its own to record
    assert stamps == [("h1|1433|k-d1", "d1", 100, 3)]
    # freshness 3600: a fresh stamp at or below the version vouches for the database, so nothing is written
    fresh = {("h1|1433|k-d1", "d1"): {"version": 100, "checked_at": _NOW - _td(minutes=10)}}
    assert planner.unchanged_writes([members], [out], cps, 3600, stamps=fresh, now=_NOW) == (rows, [])
    stale = {("h1|1433|k-d1", "d1"): {"version": 100, "checked_at": _NOW - _td(hours=2)}}
    assert planner.unchanged_writes([members], [out], cps, 3600, stamps=stale, now=_NOW)[1] == stamps
    # a stamp from before a change-tracking reset (higher version than now) vouches for nothing
    reset = {("h1|1433|k-d1", "d1"): {"version": 900, "checked_at": _NOW - _td(minutes=10)}}
    assert planner.unchanged_writes([members], [out], cps, 3600, stamps=reset, now=_NOW)[1] == stamps


def test_a_table_whose_identity_is_not_recorded_yet_is_written_even_in_a_stamped_database():
    # A checkpoint written before the identity backstop has no source, sink or key recorded. The bulk
    # advance records them; a database stamp standing in for the row would leave the backstop off.
    cfgs = _db((1, "a"), (2, "b"))
    cps = {1: _stamped(100, 5), 2: _stamped(100, 5, source_identity=None, sink_identity=None, merge_key=None)}
    members = [(c, cps[c["id"]]) for c in cfgs]
    out = planner.detect_group(members, None, None, database_state=_state(100, cfgs))
    rows, stamps = planner.unchanged_writes([members], [out], cps, 0, now=_NOW)
    assert [r[0] for r in rows] == [2]
    assert stamps == [("h1|1433|k-d1", "d1", 100, 2)]


def test_a_database_that_changed_after_its_stamp_is_read_and_its_quiet_tables_advance():
    # Stamped at version 100 ten minutes ago; since then table a changed (version 105).
    cfgs = _db((1, "a"), (2, "b"), (3, "c"))
    cps = {1: _stamped(100, 180), 2: _stamped(100, 180), 3: _stamped(105, 180)}   # c re-seeded at 105
    stamps = {("h1|1433|k-d1", "d1"): {"version": 100, "checked_at": _NOW - _td(minutes=10)}}
    members = [(c, cps[c["id"]]) for c in cfgs]
    out = planner.detect_group(members, None, lambda c, items: ({1: (1, 1), 2: (1, 0), 3: (1, 0)}, [], 1),
                               database_state=_state(105, cfgs))
    assert not out["fast_exit"]
    assert [(p["cfg"]["id"], p["route"], p["cur"]) for p in out["plans"]] == [(1, "merge", 105)]
    rows, new_stamps = planner.unchanged_writes([members], [out], cps, 3600, stamps=stamps, now=_NOW)
    # b must move to 105 whatever the stamp says; c is at 105 already and the fresh stamp vouches for it
    assert [(r[0], r[1]) for r in rows] == [(2, 105)]
    assert new_stamps == []                      # a database with a change is not stamped as a whole
    # without the database stamp, c's own 3-hour-old stamp is due
    assert [r[0] for r in planner.unchanged_writes([members], [out], cps, 3600, now=_NOW)[0]] == [2, 3]


def test_a_database_with_work_elsewhere_or_a_failure_is_never_stamped_as_a_whole():
    cfgs = _db((1, "a"), (2, "b"))
    cps = {1: _stamped(100, 5), 2: _stamped(100, 5)}
    members = [(c, cps[c["id"]]) for c in cfgs]
    out = planner.detect_group(members, None, None, database_state=_state(100, cfgs))
    # another row of this database is seeding, pending or reseeding: per table, as before
    elsewhere = planner.database_keys([_cfg(9, "d1", "z")])
    rows, stamps = planner.unchanged_writes([members], [out], cps, 0, elsewhere=elsewhere, now=_NOW)
    assert [r[0] for r in rows] == [1, 2] and stamps == []
    down = planner.detect_group(members, None, None, database_state=lambda c: (None, {}))
    assert planner.unchanged_writes([members], [down], cps, 0, now=_NOW) == ([], [])


def test_one_stamp_per_source_identity_in_a_detection_group():
    a, b = _cfg(1, "d1", "a"), dict(_cfg(2, "d1", "b"), src_port=None)   # one group: a port and a blank port
    cps = {1: _stamped(100, 5), 2: _stamped(100, 5)}
    members = [(a, cps[1]), (b, cps[2])]
    assert len(planner.group_by_source(members)) == 1
    out = planner.detect_group(members, None, None, database_state=_state(100, [a, b]))
    assert sorted(planner.unchanged_writes([members], [out], cps, 0, now=_NOW)[1]) == [
        ("h1|1433|k-d1", "d1", 100, 2)]   # a blank port is the default port, so the two share one stamp


def test_bulk_rows_to_write_reads_a_vouching_database_stamp_as_the_last_success():
    cps = {1: _stamped(100, 180), 2: _stamped(99, 180)}
    stamp = {"version": 100, "checked_at": _NOW - _td(minutes=10)}
    rows = [(1, 100, None, 7), (2, 100, None, 7)]
    assert [r[0] for r in planner.bulk_rows_to_write(rows, cps, 3600, now=_NOW, stamp_of=lambda cid: stamp)] == [2]
    assert [r[0] for r in planner.bulk_rows_to_write(rows, cps, 3600, now=_NOW)] == [1, 2]


def test_a_run_given_a_work_list_keeps_only_those_databases_of_its_collection():
    cfgs = [_cfg(1, "d1", "a"), _cfg(2, "d2", "a"), _cfg(3, "d2", "b", host="h2")]
    work = {planner.database_key(cfgs[1])}
    assert [c["id"] for c in planner.select_work(cfgs, work)] == [2]   # same name on another server: not listed
    assert planner.select_work(cfgs, None) == cfgs                      # no work list: the whole collection


def test_a_work_list_that_matches_no_enabled_row_is_refused_not_ingested_as_nothing():
    cfgs = [_cfg(1, "d1", "a"), _cfg(2, "d2", "a")]
    gone = {("h9|1433|k-d9", "d9")}
    with pytest.raises(ValueError, match="d9"):
        planner.select_work(cfgs, gone)
    with pytest.raises(ValueError):
        planner.select_work(cfgs, set())
    partly = {planner.database_key(cfgs[0]), ("h9|1433|k-d9", "d9")}
    assert [c["id"] for c in planner.select_work(cfgs, partly)] == [1]
    assert planner.work_without_rows(cfgs, partly) == [("h9|1433|k-d9", "d9")]


def test_a_run_for_a_work_list_that_finds_its_collection_locked_fails_instead_of_skipping():
    assert planner.locked_run_status("") == "skipped"
    assert planner.locked_run_status("launch-1:c1") == "failed"


def test_the_sink_lookup_covers_the_incremental_rows_only():
    """The sweep and the check list the sinks of ct and cursor rows (the ones a missing sink reseeds);
    full reloads have no checkpoint to protect."""
    ct = _cfg(1, "d1", "a")
    cursor = dict(_cfg(2, "d1", "b"), load_type="cursor")
    full = dict(_cfg(3, "d1", "c"), load_type="full")
    assert [c["id"] for c in planner.sink_checked_configs([ct, cursor, full])] == [1, 2]


def _failed_row(database, table, error):
    return ({"src_database": database, "src_schema": "dbo", "src_table": table}, error)


def test_a_failure_message_names_up_to_five_tables_with_their_errors_and_the_job_page():
    failed = [_failed_row("db", f"t{i}", f"boom {i}") for i in range(7)]
    message = planner.failure_message(42, "cohort", failed, link="https://host/jobs/1/runs/2")
    assert message.splitlines() == [
        "7 table(s) failed in run 42 of cohort:",
        "db.dbo.t0: boom 0", "db.dbo.t1: boom 1", "db.dbo.t2: boom 2", "db.dbo.t3: boom 3", "db.dbo.t4: boom 4",
        "and 2 more",
        "Job page: https://host/jobs/1/runs/2",
    ]


def test_a_failure_message_cuts_a_long_error_and_says_where_to_look_without_a_link():
    message = planner.failure_message(7, "cohort", [_failed_row("db", "orders", "x" * 900)])
    assert message.splitlines()[1] == "db.dbo.orders: " + "x" * 200
    assert message.splitlines()[-1] == "See the run in the operations app or lakefed_ingest.ingest_event."
