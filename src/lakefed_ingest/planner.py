"""Run planning: strategy routing + CT skip-no-change — pure, unit-testable (no Spark, no DB).

The ingestion notebook calls these with injected functions for the source queries, so the routing
and bookkeeping rules can be tested with fakes:

* ``route_tables`` — per control row, by its load strategy (``load_type``) and source adapter
  (``src_type``): an unsupported pair (or a placeholder strategy) -> ``unsupported`` (the row fails
  on its own with a clear message); ``full`` -> ``full`` (reload every cycle); ``cursor`` ->
  ``cursor_seed`` / ``schema_reseed`` / ``cursor`` (per-table high-watermark increment); ``ct`` ->
  from the bulk-loaded checkpoint, ``seed`` / ``schema_reseed`` / legacy check / detection candidate.
* ``group_by_source`` — candidates grouped by (source database, load_type): CT versions are per
  database and are NOT comparable across databases.
* ``detect_group`` — one database: capture ``cur`` ONCE, fast-exit when every checkpoint is
  already >= cur, otherwise batched detection; classify each table as changed / unchanged /
  reseed. Every unchanged row and every merge plan carries THIS group's ``cur``. If the database
  is unreachable (``current_version`` fails), the group's tables are returned as ``failed``
  (stage ``detect``) and the other groups are unaffected.

Correctness invariant: ``cur`` is captured BEFORE detection, so any change with version <= cur is
visible to the EXISTS check; later changes carry higher versions and are picked up next cycle from
the advanced checkpoint (at-least-once; the MERGE is idempotent).
"""

from __future__ import annotations

from typing import Optional, Callable, Dict, List, Tuple

import ct_engine
import cursor_engine
import load_strategies as ls
import state_store as ss

_CT = ls.get_strategy(ls.CT)


def ct_position(cp: dict) -> int:
    """The CT version a CT checkpoint is at (``position`` text; NULL -> 0)."""
    return _CT.decode_position(cp.get("position"))


def position_from(plan: dict):
    """The resume position a worker starts from (telemetry ``position_from``): the cursor value for
    a cursor increment, the CT version for a CT table resuming its own checkpoint, else None (full
    reload, seeds and reseeds of another strategy's checkpoint — e.g. load_type switched cursor ->
    ct — which must never be decoded as a CT version)."""
    route, cp = plan["route"], plan.get("cp")
    if route == "cursor":
        return plan["position"]["value"]
    if route in ("full", "cursor_seed"):
        return None
    if cp is None:
        return 0
    if (cp.get("strategy") or ls.CT) != ls.CT:
        return None
    return ct_position(cp)


def route_tables(configs: List[dict], checkpoints: Dict[int, dict]):
    """Returns ``(plans, candidates, legacy)``.

    plans      — unsupported (pair not in the support matrix; carries ``error``), full (reload),
                 seed (no CT checkpoint / phase seeding) and schema_reseed (select_list gained
                 columns vs the recorded one) routes, plus cursor_seed / cursor (carries the
                 decoded ``position``) for load_type cursor, ready for the worker;
    candidates — [(cfg, cp)] CT tables in the cdc phase to run through change detection;
    legacy     — [(cfg, cp)] cdc tables with no recorded select_list (caller checks the sink once).
    """
    plans, candidates, legacy = [], [], []
    for cfg in configs:
        cp = checkpoints.get(int(cfg["id"]))
        reason = ls.unsupported_reason(cfg.get("src_type"), cfg.get("load_type"))
        if reason:
            plans.append({"cfg": cfg, "route": "unsupported", "cp": cp, "error": reason})
            continue
        if ss.strategy_name(cfg) == ls.FULL:
            plans.append({"cfg": cfg, "route": "full", "cp": cp})
            continue
        if ss.strategy_name(cfg) == ls.CURSOR:
            plans.append(_route_cursor(cfg, cp))
            continue
        # CT. A checkpoint written by another strategy (load_type changed) can't be resumed: seed.
        if cp is None or cp.get("phase") != ss.CDC or (cp.get("strategy") or ls.CT) != ls.CT:
            plans.append({"cfg": cfg, "route": "seed", "cp": cp})
            continue
        added = ss.select_list_added_columns(cfg["select_list"], cp.get("select_list"))
        if added is None:
            legacy.append((cfg, cp))
        elif added:
            plans.append({"cfg": cfg, "route": "schema_reseed", "cp": cp, "added": added})
        else:
            candidates.append((cfg, cp))
    return plans, candidates, legacy


def _route_cursor(cfg: dict, cp: Optional[dict]) -> dict:
    """Cursor tables: seed when there's no usable cursor checkpoint (none, phase seeding, another
    strategy's, or recorded for a different cursor_column); reseed when select_list gained columns;
    else a per-table high-watermark increment."""
    pos = cursor_engine.decode_position(cp.get("position")) if cp else None
    if (cp is None or cp.get("phase") != ss.CDC or (cp.get("strategy") or ls.CT) != ls.CURSOR
            or pos is None or pos["col"].lower() != str(cfg.get("cursor_column") or "").lower()):
        return {"cfg": cfg, "route": "cursor_seed", "cp": cp}
    added = ss.select_list_added_columns(cfg["select_list"], cp.get("select_list"))
    if added:
        return {"cfg": cfg, "route": "schema_reseed", "cp": cp, "added": added}
    return {"cfg": cfg, "route": "cursor", "cp": cp, "position": pos}


def group_by_source(candidates) -> Dict[tuple, list]:
    """{(source database key, load_type): [(cfg, cp)]} — one detection group per source database
    and strategy."""
    groups: Dict[tuple, list] = {}
    for cfg, cp in candidates:
        key = (ct_engine.source_group_key(cfg), ss.strategy_name(cfg))
        groups.setdefault(key, []).append((cfg, cp))
    return groups


def detect_group(members, current_version: Callable, detect: Callable, record_sl=frozenset(),
                 ct_status: Optional[Callable] = None) -> dict:
    """Plan ONE source database.

    ``current_version(cfg) -> int``; ``detect(cfg, items) -> (results, failed_ids, queries)`` where
    items = [(control_id, schema, table, checkpoint)] and results {control_id: (mv, changed)}.
    ``record_sl`` = control ids whose select_list should be recorded on the bulk advance (legacy).
    ``ct_status(cfg) -> {(schema, table): min_valid_version}`` lists the database's CT-enabled tables
    (keys lower-cased). It runs BEFORE the fast exit: disabling or re-enabling CT on a single table
    doesn't move the database version, so without it a quiet database would never notice. A table
    that lost CT is routed to reseed (its seed then fails visibly until CT is back); one whose min valid version passed its checkpoint (CT
    re-enabled, or retention lapsed on a quiet database) is routed to reseed, as is one whose CT
    begin_version differs from the value recorded at seed time (CT turned off and on between cycles).

    Returns {cur, plans, unchanged: [(control_id, cur, select_list|None)], failed: [(cfg, cp, error)],
    queries, fast_exit, fallback}.
    """
    cfg0 = members[0][0]
    out = {"database": f"{cfg0.get('src_host') or cfg0.get('src_connection')}/{cfg0.get('src_database')}",
           "cur": None, "plans": [], "unchanged": [], "failed": [], "queries": 0,
           "fast_exit": False, "fallback": 0}
    try:
        cur = current_version(cfg0)
        out["queries"] += 1
    except Exception as e:  # database unreachable / auth: isolate THIS group, never the sweep
        err = f"change detection: CHANGE_TRACKING_CURRENT_VERSION failed: {str(e).splitlines()[0][:400] if str(e) else e!r}"
        out["failed"] = [(c, cp, err) for c, cp in members]
        return out
    if cur is None:  # CT disabled on the database: fail loudly (a 0 would look "unchanged" forever)
        out["failed"] = [(c, cp, f"change detection: {ct_engine.CT_NOT_ENABLED_MSG}") for c, cp in members]
        return out
    cur = int(cur)
    out["cur"] = cur

    # A checkpoint AHEAD of the database's current version means CT versions were reset (restore
    # from backup, CT disabled and re-enabled): changes since can't be read from the checkpoint.
    reset = [(c, cp) for c, cp in members if ct_position(cp) > cur]
    for c, cp in reset:
        out["plans"].append({"cfg": c, "route": "retention_reseed", "cp": cp, "reason": "ct_version_reset"})
    if reset:
        reset_ids = {int(c["id"]) for c, _ in reset}
        members = [(c, cp) for c, cp in members if int(c["id"]) not in reset_ids]
        if not members:
            return out

    begin = {}  # control_id -> current CT begin_version (recorded when the checkpoint lacks one)
    if ct_status is not None:
        try:
            status = ct_status(cfg0)
            out["queries"] += 1
        except Exception as e:  # can't confirm per-table CT state: don't risk a silent fast exit
            err = f"change detection: CT table status query failed: {str(e).splitlines()[0][:400] if str(e) else e!r}"
            out["failed"].extend((c, cp, err) for c, cp in members)
            return out
        keep = []
        for c, cp in members:
            key = (str(c["src_schema"]).lower(), str(c["src_table"]).lower())
            if key not in status:
                # CT off on this table: re-seed it. Its seed refuses to run while CT is off (visible
                # failure every cycle) and succeeds once CT is back — changes made meanwhile included.
                out["plans"].append({"cfg": c, "route": "retention_reseed", "cp": cp, "reason": "ct_disabled"})
                continue
            mv, bv = status[key]
            recorded = cp.get("ct_begin_version")
            if mv is None or int(mv) > ct_position(cp):
                out["plans"].append({"cfg": c, "route": "retention_reseed", "cp": cp, "reason": "retention_miss"})
            elif recorded is not None and bv is not None and int(bv) != int(recorded):
                out["plans"].append({"cfg": c, "route": "retention_reseed", "cp": cp, "reason": "ct_reenabled"})
            else:
                begin[int(c["id"])] = bv
                keep.append((c, cp))
        members = keep
        if not members:
            return out

    def unchanged_row(c):
        cid = int(c["id"])
        return (cid, cur, c["select_list"] if cid in record_sl else None, begin.get(cid))

    if ct_engine.all_current([ct_position(cp) for _, cp in members], cur):
        out["fast_exit"] = True
        out["unchanged"] = [unchanged_row(c) for c, _ in members]
        return out

    items = [(int(c["id"]), c["src_schema"], c["src_table"], ct_position(cp)) for c, cp in members]
    try:
        res, failed_ids, q = detect(cfg0, items)
    except Exception as e:  # whole detection failed for this database
        err = f"change detection failed: {str(e).splitlines()[0][:400] if str(e) else e!r}"
        out["failed"] = [(c, cp, err) for c, cp in members]
        return out
    out["queries"] += q
    failed_ids = set(failed_ids)
    for c, cp in members:
        cid = int(c["id"])
        if cid in failed_ids or cid not in res:   # never classify a table we got no answer for
            out["plans"].append({"cfg": c, "route": "fallback", "cp": cp})
            out["fallback"] += 1
            continue
        kind = ct_engine.classify(ct_position(cp), *res[cid])
        if kind == "reseed":
            out["plans"].append({"cfg": c, "route": "retention_reseed", "cp": cp})
        elif kind == "changed":
            out["plans"].append({"cfg": c, "route": "merge", "cp": cp, "cur": cur, "ct_begin": begin.get(cid)})
        else:
            out["unchanged"].append(unchanged_row(c))
    return out


def merge_group_results(group_outputs: List[dict]) -> dict:
    """Combine detect_group outputs: plans, unchanged rows, failed tables, stats."""
    agg = {"plans": [], "unchanged": [], "failed": [], "queries": 0, "fast_exits": 0,
           "fallback_tables": 0, "failed_databases": 0, "group_versions": []}
    for g in group_outputs:
        agg["plans"].extend(g["plans"])
        agg["unchanged"].extend(g["unchanged"])
        agg["failed"].extend(g["failed"])
        agg["queries"] += g["queries"]
        agg["fast_exits"] += int(g["fast_exit"])
        agg["fallback_tables"] += g["fallback"]
        agg["failed_databases"] += int(bool(g["failed"]))
        agg["group_versions"].append({"database": g.get("database"), "cur": g["cur"],
                                      "tables": len(g["unchanged"]) + len(g["plans"]) + len(g["failed"]),
                                      "fast_exit": g["fast_exit"]})
    return agg
