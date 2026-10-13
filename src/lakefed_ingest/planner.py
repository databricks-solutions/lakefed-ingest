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

from datetime import datetime, timezone
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


def sink_key(cfg: dict) -> tuple:
    return tuple(str(cfg.get(k) or "").lower() for k in ("sink_catalog", "sink_schema", "sink_table"))


IDENTITY_REASONS = (("source_identity", "source_changed"), ("sink_identity", "sink_changed"),
                    ("merge_key", "primary_key_changed"))


def identity_change(cfg: dict, cp: Optional[dict]) -> Optional[str]:
    """Reseed reason when the control row now points at a different source, sink (or client slice),
    or merge key than its checkpoint was written for — e.g. an edit made with SQL. Continuing would
    merge only new changes into the new place (a silent gap) or match on the wrong key. A checkpoint
    without a recorded value (written before this check existed) is never reseeded for it; the
    value is recorded on its next advance."""
    if not cp:
        return None
    current = dict(zip(("source_identity", "sink_identity", "merge_key"), ss.identity_values(cfg)))
    for col, reason in IDENTITY_REASONS:
        recorded = cp.get(col)
        if recorded is not None and recorded != current[col]:
            return reason
    return None


def sink_missing(cfg: dict, existing_sinks: Optional[set]) -> Optional[str]:
    """'sink_missing' when an incremental table's sink table doesn't exist (dropped, renamed, or the
    row was pointed at a new sink). Continuing would create an empty sink and merge only new changes
    into it — a silent gap — so the table reseeds. existing_sinks None = unknown (check skipped)."""
    if existing_sinks is None:
        return None
    return None if ct_engine.sink_lookup_key(cfg) in existing_sinks else "sink_missing"


def config_error(cfg: dict, mixed_sinks=frozenset(), snapshot_isolation: bool = False) -> Optional[str]:
    """Sink-ownership problems that would let one row overwrite another's data — checked per table
    before any work: an invalid client_id, a consolidated row whose select_list already has a
    __client_id__ column, or a sink shared by a dedicated row and consolidated rows. Also a missing
    primary key for a strategy that MERGEs by it (the database CHECK constraints enforce this too;
    this gives a clear per-table error if a row ever gets past them), an invalid write_mode or
    client_attrs, column names that the sink adds itself (client_attrs, append metadata)
    clashing with select_list, and a read through a connection without one, or with
    ``snapshot_isolation`` on for its change reads (which need a JDBC session statement for it)."""
    try:
        if ls.get_strategy(cfg.get("load_type")).requires_pk and not ct_engine._split(cfg.get("primary_key")):
            return f"load_type {cfg.get('load_type') or 'ct'} needs a primary_key (it MERGEs by it)"
    except ValueError:
        pass   # an unknown load_type is reported by unsupported_reason
    if ct_engine.uses_remote_query(cfg):
        if not cfg.get("src_connection"):
            return "use_remote_query reads through the Unity Catalog connection in src_connection, which is not set"
        if snapshot_isolation and ss.strategy_name(cfg) == ls.CT:
            return ("snapshot_isolation is on, but this row reads through a connection, which runs no session "
                    "statement to set it: turn snapshot_isolation off (READ_COMMITTED_SNAPSHOT ON on the database "
                    "makes each read consistent on its own) or read the row over JDBC (use_remote_query false)")
    try:
        cid = ct_engine.client_id(cfg)
        append = ct_engine.is_append(cfg)
        attrs = [name.lower() for name, _ in ct_engine.client_attrs(cfg)]
    except ValueError as e:
        return str(e)
    selected = {c.lower() for c in ct_engine._split(cfg.get("select_list"))}
    if cid is not None and ct_engine.CLIENT_COLUMN in selected:
        return (f"select_list includes '{ct_engine.CLIENT_COLUMN}', which a consolidated sink (client_id set) "
                "adds itself; remove it from select_list")
    if append and ss.strategy_name(cfg) not in (ls.CT, ls.FULL):
        return f"write_mode append supports load_type ct and full, not {ss.strategy_name(cfg)}"
    clash = [c for c in attrs + (list(ct_engine.APPEND_COLUMNS) if append else []) if c in selected]
    if clash:
        return (f"select_list includes {', '.join(clash)}, which the sink adds itself (client_attrs / "
                "append metadata); rename the client_attrs column or remove it from select_list")
    if mixed_sinks and sink_key(cfg) in mixed_sinks:
        return ("sink {}.{}.{} is claimed by a dedicated row (client_id NULL) and by other rows; a sink is "
                "either dedicated to one source table or shared by client_id".format(*sink_key(cfg)))
    return None


def route_tables(configs: List[dict], checkpoints: Dict[int, dict], mixed_sinks=frozenset(),
                 reseed_on_new_columns: bool = True, snapshot_isolation: bool = False,
                 existing_sinks: Optional[set] = None):
    """Returns ``(plans, candidates, legacy)``.

    plans      — unsupported (pair not in the support matrix, or a sink-ownership problem — see
                 ``config_error``; carries ``error``), full (reload),
                 seed (no CT checkpoint / phase seeding) and schema_reseed (select_list gained
                 columns vs the recorded one) routes, plus cursor_seed / cursor (carries the
                 decoded ``position``) for load_type cursor, and pending (an append write whose
                 commit is unknown: the worker checks the sink), ready for the worker;
    candidates — [(cfg, cp)] CT tables in the incremental phase to run through change detection;
    legacy     — [(cfg, cp)] cdc tables with no recorded select_list (caller checks the sink once).

    ``reseed_on_new_columns`` False: a table whose select_list gained columns carries on from its
    checkpoint instead of reseeding (the writes add the columns; rows written before stay NULL in
    them), e.g. an append sink whose history needn't be backfilled. ``snapshot_isolation``: the job
    parameter, which a CT row read through a connection can't honour (``config_error``).
    """
    plans, candidates, legacy = [], [], []
    for cfg in configs:
        cp = checkpoints.get(int(cfg["id"]))
        reason = (config_error(cfg, mixed_sinks, snapshot_isolation)
                  or ls.unsupported_reason(cfg.get("src_type"), cfg.get("load_type")))
        if reason:
            plans.append({"cfg": cfg, "route": "unsupported", "cp": cp, "error": reason})
            continue
        if ss.strategy_name(cfg) == ls.FULL:
            plans.append({"cfg": cfg, "route": "full", "cp": cp})
            continue
        if ss.strategy_name(cfg) == ls.CURSOR:
            plans.append(_route_cursor(cfg, cp, reseed_on_new_columns, existing_sinks))
            continue
        # CT. A run stopped between an append and its checkpoint advance: find out first whether
        # the append committed (never bulk-advanced or re-read blind).
        if cp is not None and cp.get("pending_run_id") is not None and ct_engine.is_append(cfg):
            plans.append({"cfg": cfg, "route": "pending", "cp": cp})
            continue
        # A checkpoint written by another strategy (load_type changed) can't be resumed: seed.
        if cp is None or cp.get("phase") != ss.INCREMENTAL or (cp.get("strategy") or ls.CT) != ls.CT:
            plans.append({"cfg": cfg, "route": "seed", "cp": cp})
            continue
        moved = identity_change(cfg, cp) or sink_missing(cfg, existing_sinks)
        if moved:
            plans.append({"cfg": cfg, "route": "reseed", "cp": cp, "reason": moved})
            continue
        added = ss.select_list_added_columns(cfg["select_list"], cp.get("select_list"))
        if added is None:
            legacy.append((cfg, cp))
        elif added and reseed_on_new_columns:
            plans.append({"cfg": cfg, "route": "schema_reseed", "cp": cp, "added": added})
        else:
            candidates.append((cfg, cp))
    return plans, candidates, legacy


def _route_cursor(cfg: dict, cp: Optional[dict], reseed_on_new_columns: bool = True,
                  existing_sinks: Optional[set] = None) -> dict:
    """Cursor tables: seed when there's no usable cursor checkpoint (none, phase seeding, another
    strategy's, or recorded for a different cursor_column); reseed when select_list gained columns
    (unless ``reseed_on_new_columns`` is off); else a per-table high-watermark increment."""
    pos = cursor_engine.decode_position(cp.get("position")) if cp else None
    if (cp is None or cp.get("phase") != ss.INCREMENTAL or (cp.get("strategy") or ls.CT) != ls.CURSOR
            or pos is None or pos["col"].lower() != str(cfg.get("cursor_column") or "").lower()):
        return {"cfg": cfg, "route": "cursor_seed", "cp": cp}
    moved = identity_change(cfg, cp) or sink_missing(cfg, existing_sinks)
    if moved:
        return {"cfg": cfg, "route": "reseed", "cp": cp, "reason": moved}
    added = ss.select_list_added_columns(cfg["select_list"], cp.get("select_list"))
    if added and reseed_on_new_columns:
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


def detect_group(members, current_version: Optional[Callable], detect: Callable, record_sl=frozenset(),
                 ct_status: Optional[Callable] = None, database_state: Optional[Callable] = None) -> dict:
    """Plan ONE source database.

    ``database_state(cfg) -> (cur, status)`` answers ``current_version`` and ``ct_status`` in one
    query (``ct_engine.database_ct_state``); when given, those two aren't called.
    ``current_version(cfg) -> int``; ``detect(cfg, items) -> (results, failed_ids, queries)`` where
    items = [(control_id, schema, table, checkpoint)] and results {control_id: (mv, changed)}.
    ``record_sl`` = control ids whose select_list should be recorded on the bulk advance (legacy);
    a select_list that differs from the checkpoint's recorded one is recorded too.
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
    status = None
    try:
        if database_state is not None:
            cur, status = database_state(cfg0)
        else:
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
    if status is None and ct_status is not None:
        try:
            status = ct_status(cfg0)
            out["queries"] += 1
        except Exception as e:  # can't confirm per-table CT state: don't risk a silent fast exit
            err = f"change detection: CT table status query failed: {str(e).splitlines()[0][:400] if str(e) else e!r}"
            out["failed"].extend((c, cp, err) for c, cp in members)
            return out
    if status is not None:
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

    def unchanged_row(c, cp):
        # select_list is recorded for legacy rows, and when it differs from the recorded one (a table
        # that carried on with new columns, reseed_on_new_columns off), so the next cycle compares
        # with what is written now.
        cid = int(c["id"])
        record = cid in record_sl or cp.get("select_list") not in (None, c["select_list"])
        return (cid, cur, c["select_list"] if record else None, begin.get(cid), ss.identity_values(c))

    if ct_engine.all_current([ct_position(cp) for _, cp in members], cur):
        out["fast_exit"] = True
        out["unchanged"] = [unchanged_row(c, cp) for c, cp in members]
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
            out["unchanged"].append(unchanged_row(c, cp))
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


# ---- table retries ------------------------------------------------------------------------------------
def retry_plan(plan: dict, cp: Optional[dict], run_id) -> dict:
    """The next attempt of a table whose attempt failed in run ``run_id`` (``table_retry``), from its
    checkpoint ``cp`` as it is now.

    * Merge mode: the same route on the fresh checkpoint. Every write is idempotent (MERGE by key,
      replace) and the checkpoint advances only after the commit, so doing it again is harmless.
    * Append mode, CT with a pending append: route ``pending``, which checks whether that append
      committed before anything else, as after a stopped run.
    * Any other append: the same route with ``verify_run_id``, so the worker first looks for this
      run's rows of the table in the sink and appends again only when there are none. This covers a
      full load (it records no pending append) and a CT table whose checkpoint advance already ran
      before the attempt failed (the advance clears the pending append)."""
    cfg = plan["cfg"]
    if not ct_engine.is_append(cfg):
        return dict(plan, cp=cp)
    if ss.strategy_name(cfg) == ls.CT and cp is not None and cp.get("pending_run_id") is not None:
        return {"cfg": cfg, "route": "pending", "cp": cp}
    return dict(plan, cp=cp, verify_run_id=run_id)


# ---- scale controls -----------------------------------------------------------------------------------
SEED_ROUTES = ("seed", "cursor_seed")


def apply_seed_budget(plans: List[dict], max_seeds: int) -> Tuple[List[dict], List[dict]]:
    """(kept, deferred): at most ``max_seeds`` seeds (routes seed, cursor_seed) run in this run; the
    rest wait, untouched, for a later run (their checkpoints stay seeding). Onboarding many tables at
    once then takes several bounded runs, and the incremental work of tables already seeded keeps
    running in each. Seeds whose last attempt failed go last, so a few broken tables can't use up the
    budget every run. ``max_seeds`` 0 = no limit."""
    seeds = [p for p in plans if p["route"] in SEED_ROUTES]
    if not max_seeds or max_seeds <= 0 or len(seeds) <= max_seeds:
        return list(plans), []
    ordered = sorted(seeds, key=lambda p: (p.get("cp") or {}).get("status") == "failed")   # stable
    run = {id(p) for p in ordered[:max_seeds]}
    return ([p for p in plans if p["route"] not in SEED_ROUTES or id(p) in run],
            [p for p in seeds if id(p) not in run])


def bulk_rows_to_write(unchanged: list, checkpoints: Dict[int, dict], interval_s: float, now=None) -> list:
    """The unchanged rows the bulk advance has to write. With ``interval_s`` > 0, a row is left out
    when writing it would only refresh ``last_success_at`` and that stamp is under ``interval_s``
    old: the checkpoint is already at the captured version, ok, and has nothing to record (select_list
    or begin version). At scale nearly every table is quiet nearly every run, so this removes most of
    a run's Lakebase writes; ``last_success_at`` of a quiet table is then at most ``interval_s``
    stale. ``interval_s`` 0 = write every row (stamped every run)."""
    if not interval_s or interval_s <= 0:
        return list(unchanged)
    now = now or datetime.now(timezone.utc)
    out = []
    for row in unchanged:
        cid, cur, select_list, begin = (tuple(row) + (None,))[:4]
        cp = checkpoints.get(int(cid)) or {}
        stamp = cp.get("last_success_at")
        quiet = (ct_position(cp) >= int(cur) and select_list is None and cp.get("status") == "ok"
                 and (begin is None or cp.get("ct_begin_version") is not None)
                 and stamp is not None and (now - stamp).total_seconds() < interval_s)
        if not quiet:
            out.append(row)
    return out
