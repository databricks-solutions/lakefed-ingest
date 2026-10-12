"""Skip-no-change planning for the consolidated sweep — pure, unit-testable (no Spark, no DB).

The sweep calls these with injected functions for the source queries, so the routing and
bookkeeping rules can be tested with fakes:

* ``route_tables`` — from the bulk-loaded checkpoints, route each control row to ``seed`` /
  ``schema_reseed`` / legacy check / detection candidate.
* ``group_by_source`` — candidates grouped by source database (CT versions are per database and
  are NOT comparable across databases).
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

from typing import Callable, Dict, List, Tuple

import ct_engine
import state_store as ss


def route_tables(configs: List[dict], checkpoints: Dict[int, dict]):
    """Returns ``(plans, candidates, legacy)``.

    plans      — seed (no checkpoint / phase seeding) and schema_reseed (select_list gained columns
                 vs the recorded one) routes, ready for the per-table worker;
    candidates — [(cfg, cp)] cdc tables to run through change detection;
    legacy     — [(cfg, cp)] cdc tables with no recorded select_list (caller checks the sink once).
    """
    plans, candidates, legacy = [], [], []
    for cfg in configs:
        cp = checkpoints.get(int(cfg["id"]))
        if cp is None or cp.get("phase") != ss.CDC:
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


def group_by_source(candidates) -> Dict[tuple, list]:
    groups: Dict[tuple, list] = {}
    for cfg, cp in candidates:
        groups.setdefault(ct_engine.source_group_key(cfg), []).append((cfg, cp))
    return groups


def detect_group(members, current_version: Callable, detect: Callable, record_sl=frozenset()) -> dict:
    """Plan ONE source database.

    ``current_version(cfg) -> int``; ``detect(cfg, items) -> (results, failed_ids, queries)`` where
    items = [(control_id, schema, table, checkpoint)] and results {control_id: (mv, changed)}.
    ``record_sl`` = control ids whose select_list should be recorded on the bulk advance (legacy).

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
    reset = [(c, cp) for c, cp in members if cp["ct_version"] is not None and int(cp["ct_version"]) > cur]
    for c, cp in reset:
        out["plans"].append({"cfg": c, "route": "retention_reseed", "cp": cp, "reason": "ct_version_reset"})
    if reset:
        reset_ids = {int(c["id"]) for c, _ in reset}
        members = [(c, cp) for c, cp in members if int(c["id"]) not in reset_ids]
        if not members:
            return out

    def unchanged_row(c):
        cid = int(c["id"])
        return (cid, cur, c["select_list"] if cid in record_sl else None)

    if ct_engine.all_current([cp["ct_version"] for _, cp in members], cur):
        out["fast_exit"] = True
        out["unchanged"] = [unchanged_row(c) for c, _ in members]
        return out

    items = [(int(c["id"]), c["src_schema"], c["src_table"], cp["ct_version"]) for c, cp in members]
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
        kind = ct_engine.classify(cp["ct_version"], *res[cid])
        if kind == "reseed":
            out["plans"].append({"cfg": c, "route": "retention_reseed", "cp": cp})
        elif kind == "changed":
            out["plans"].append({"cfg": c, "route": "merge", "cp": cp, "cur": cur})
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
