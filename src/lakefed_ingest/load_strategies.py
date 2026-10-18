"""Load strategies: HOW a table is kept in sync (``control.load_type``), independent of the source
engine (``control.src_type`` -> ``source_adapters``).

Each strategy owns the meaning of the generic checkpoint row (``lakefed_ingest.checkpoint``):
``strategy`` names it and ``position`` (text) is its opaque resume position, encoded and decoded
only by the strategy that wrote it.

=========  ============================================================  ==========================
load_type  behaviour                                                     position
=========  ============================================================  ==========================
ct         SQL Server Change Tracking: seed, then CHANGETABLE increments  CT version (decimal text)
           MERGEd by PK; skip-no-change per source database
full       Reload the whole table every cycle (data-only atomic replace;  NULL (not resumable)
           partitioned tables use the staged swap). No PK required
cursor     High-watermark column (SQL Server; placeholder elsewhere): seed,  JSON {col, type, value}
           then (position, hi] reads upsert-MERGEd by PK; no deletes
cdc        PLACEHOLDER — log-based CDC (e.g. SQL Server CDC, Postgres     engine LSN / SCN (text)
           logical decoding, Oracle LogMiner, MySQL binlog)
=========  ============================================================  ==========================

``supported(src_type, load_type)`` is the support matrix the planner enforces: a control row whose
pair isn't supported fails on its own (clear message, stage ``plan``) without affecting the run.

Pure Python — unit-tested in tests/load_strategies_test.py.
"""

from __future__ import annotations

from typing import Optional

import source_adapters as sa

CT, FULL, CURSOR, CDC = "ct", "full", "cursor", "cdc"
LOAD_TYPES = (CT, CDC, CURSOR, FULL)


class LoadStrategy:
    name: str = ""
    requires_pk: bool = False
    implemented: bool = False

    def encode_position(self, value) -> Optional[str]:
        return None if value is None else str(value)

    def decode_position(self, text: Optional[str]):
        return text


class CtStrategy(LoadStrategy):
    """SQL Server Change Tracking. The engine lives in ``ct_engine`` (seed, batched detection,
    CHANGETABLE read + MERGE, retention guard); the sweep routes each table per cycle via
    ``planner.detect_group``. Position = the database CT version the sink is consistent with."""

    name = CT
    requires_pk = True
    implemented = True

    def encode_position(self, value):
        return None if value is None else str(int(value))

    def decode_position(self, text):
        return 0 if text is None or str(text).strip() == "" else int(text)


class FullStrategy(LoadStrategy):
    """Full reload every cycle: one JDBC read streams into a data-only ``INSERT OVERWRITE`` (or the
    staged, atomic partitioned swap when ``load_partitioned``). The table definition (id, grants,
    clustering, properties) is preserved. No PK and no position. With ``write_mode`` append, each
    cycle's read is appended as a snapshot instead (operation 'S', written by ingest run ``run_id``)."""

    name = FULL
    implemented = True

    def load(self, spark, cfg: dict, sink_fqn: str, slots=None, executor=None, guard=None, run_id=None):
        import ct_engine
        ct_engine.ensure_sink_table(spark, cfg, sink_fqn, slots=slots)
        return ct_engine.seed(spark, cfg, sink_fqn, slots=slots, executor=executor, guard=guard,
                              run_id=run_id)


class CursorStrategy(LoadStrategy):
    """Cursor (high-watermark) increments — implemented for SQL Server in ``cursor_engine``; other
    adapters are placeholders (they need their own type lookup / high-watermark / literal SQL).

    * control: ``cursor_column`` (monotonic: rowversion, identity, or an application-maintained
      updated_at) and ``primary_key`` (rows are MERGEd by PK; a DB constraint enforces both);
    * first cycle: capture ``hi`` BEFORE a full seed, checkpoint position = hi;
    * each cycle: ``hi'``; equal -> unchanged, else read ``(position, hi']`` (one source read),
      upsert-MERGE by PK, then advance to hi' (at-least-once; the MERGE is idempotent);
    * position = JSON ``{col, type, value}`` (``cursor_engine.encode_position``) — a changed
      ``cursor_column`` re-seeds;
    * deletes are NOT captured (use ``ct``, or a periodic ``full``).
    """

    name = CURSOR
    requires_pk = True
    implemented = True

    def encode_position(self, value):
        return value                      # already encoded by cursor_engine.encode_position

    def decode_position(self, text):
        import cursor_engine
        return cursor_engine.decode_position(text)


class CdcStrategy(LoadStrategy):
    """PLACEHOLDER — log-based CDC (inserts, updates AND deletes from the engine's change log).
    Contract for the implementation:

    * control: ``cdc_capture_instance`` (SQL Server CDC capture instance / Postgres slot+publication /
      Oracle or MySQL equivalent) and ``primary_key``;
    * seed at a consistent log position (captured BEFORE the seed), checkpoint position = that LSN/SCN
      encoded as text;
    * each cycle: read changes in (position, current] — one source read — MERGE I/U/D by PK keeping
      the LAST change per key, advance position only after the MERGE commits;
    * a position older than the log's retention -> reseed (same contract as CT's retention guard).
    """

    name = CDC
    requires_pk = True

    def load(self, *_, **__):
        raise NotImplementedError("load_type 'cdc' is not implemented yet")


_STRATEGIES = {s.name: s for s in (CtStrategy(), FullStrategy(), CursorStrategy(), CdcStrategy())}

# (src_type, load_type) pairs that run today. CT is SQL Server-only by definition; FULL works for
# every adapter; CURSOR is SQL Server-only for now. PLACEHOLDER pairs are planned (clear "not
# implemented yet" error); anything else is "not supported". CDC is a placeholder everywhere.
SUPPORT = {
    (sa.SQLSERVER, CT), (sa.SQLSERVER, FULL), (sa.SQLSERVER, CURSOR),
    (sa.ORACLE, FULL), (sa.POSTGRESQL, FULL), (sa.MYSQL, FULL),
}
PLACEHOLDER = {(sa.ORACLE, CURSOR), (sa.POSTGRESQL, CURSOR), (sa.MYSQL, CURSOR)} | {
    (st, CDC) for st in sa.SRC_TYPES}


def get_strategy(load_type: Optional[str]) -> LoadStrategy:
    """Strategy for ``control.load_type`` (NULL/empty -> ct, the column default)."""
    key = (load_type or CT).strip().lower()
    try:
        return _STRATEGIES[key]
    except KeyError:
        raise ValueError(
            f"unsupported load_type {load_type!r}; supported: {', '.join(LOAD_TYPES)}") from None


def supported(src_type: Optional[str], load_type: Optional[str]) -> bool:
    return unsupported_reason(src_type, load_type) is None


def unsupported_reason(src_type: Optional[str], load_type: Optional[str]) -> Optional[str]:
    """None when the pair runs today, else a clear, per-table error message."""
    try:
        adapter = sa.get_adapter(src_type)
        strategy = get_strategy(load_type)
    except ValueError as e:
        return str(e)
    if (adapter.src_type, strategy.name) in SUPPORT:
        return None
    if not strategy.implemented or (adapter.src_type, strategy.name) in PLACEHOLDER:
        return f"load_type '{strategy.name}' is not implemented yet (src_type '{adapter.src_type}')"
    ok = sorted(lt for st, lt in SUPPORT if st == adapter.src_type)
    return (f"load_type '{strategy.name}' is not supported for src_type '{adapter.src_type}' "
            f"(supported: {', '.join(ok)})")


def support_matrix() -> dict:
    """{src_type: {load_type: 'yes' | 'placeholder' | 'no'}} — for docs and tests."""
    out = {}
    for st in sa.SRC_TYPES:
        out[st] = {}
        for lt in LOAD_TYPES:
            if (st, lt) in SUPPORT:
                out[st][lt] = "yes"
            elif (st, lt) in PLACEHOLDER or not _STRATEGIES[lt].implemented:
                out[st][lt] = "placeholder"
            else:
                out[st][lt] = "no"
    return out
