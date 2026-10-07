"""Change-Tracking engine — PLACEHOLDERS.

Owned by Alex — workstreams A/B/F. These are the only functions the consolidated sweep
orchestrator (copy_data_sweep.ipynb) leaves unimplemented: every source-touching / CT
operation lives here so the orchestration (threading, FAIR pools, batch iteration, Lakebase
checkpointing, failure isolation) can be built and tested independently.

Each function currently raises NotImplementedError. The docstrings are the contract.

The reference implementation to adapt is the precursor sketch
``lakefed_ingest_cluster_orchestration/distributed_ct_read_sketch.py`` (CHANGETABLE + LEFT JOIN
the base table on the PK; idempotent MERGE by PK with ``WHEN MATCHED AND op='D' THEN DELETE``;
one connection per database; version-before-read). Transport is open: either the governed
``remote_query`` path on a SQL warehouse (v1) or Spark JDBC / pytds on the cluster (expansion,
ADR-0001). Whichever is used, the MUST-FIX from the demo reconciliation applies.

MUST-FIX (remote_query): append a per-query uniqueness comment ``/* <uuid> */`` to EVERY
remote_query string — especially the version queries whose text is otherwise identical every
cycle — because remote_query caches by identical query text and would otherwise return a stale
version/changeset.

``cfg`` is a control-row dict keyed by state_store.CONTROL_COLUMNS; the keys used below are
noted per function.
"""

from __future__ import annotations


def current_version(cfg: dict) -> int:
    """(B) Return the source database's current Change-Tracking version.

    SELECT CHANGE_TRACKING_CURRENT_VERSION() against cfg['src_database'] via cfg['src_connection']
    (governed remote_query) or a direct cluster connection. Database-wide (not per-table).

    Captured (a) BEFORE the seed, and (b) as the upper bound BEFORE each incremental read; the
    checkpoint only advances to it AFTER the bronze MERGE commits (at-least-once).

    Uses: src_connection, src_database.  Returns: int (CT version).  MUST-FIX: /* uuid */.
    """
    raise NotImplementedError("Alex (workstream B): capture CHANGE_TRACKING_CURRENT_VERSION()")


def min_valid_version(cfg: dict) -> int:
    """(F) Return CHANGE_TRACKING_MIN_VALID_VERSION(OBJECT_ID('<schema>.<table>')) for the table.

    The sweep compares this to the checkpoint via state_store.is_reseed_needed / decide_action:
    if the checkpoint has fallen below it, CT rows were purged and the table must be reseeded.

    Uses: src_connection, src_database, src_schema, src_table.  Returns: int (or a sentinel the
    caller treats as 'reseed' when CT is not enabled).  MUST-FIX: /* uuid */.
    """
    raise NotImplementedError("Alex (workstream F): CHANGE_TRACKING_MIN_VALID_VERSION check")


def read_and_merge_ct(spark, cfg: dict, sink_fqn: str, from_version: int) -> int:
    """(A) Read CT deltas since ``from_version`` and MERGE them into the Delta bronze sink.

    Shape (see distributed_ct_read_sketch.py): project PK columns from the change table (they
    survive deletes) and the remaining select_list columns from the base table via LEFT JOIN on
    the PK (NULL for deletes); carry SYS_CHANGE_OPERATION as ``op``:

        SELECT <pk from ct>, <non-pk from base t>, ct.SYS_CHANGE_OPERATION AS op
        FROM CHANGETABLE(CHANGES <schema.table>, <from_version>) ct
        LEFT JOIN <schema.table> t ON <pk join>   /* <uuid> */

    Then an idempotent MERGE keyed on the PK (composite-PK supported):
        WHEN MATCHED AND op='D' THEN DELETE
        WHEN MATCHED            THEN UPDATE SET *
        WHEN NOT MATCHED AND op<>'D' THEN INSERT *

    Uses: src_connection, src_database, src_schema, src_table, primary_key, select_list.
    Returns: int rows merged (0 if no changes).  MUST-FIX: /* uuid */ on the read.
    """
    raise NotImplementedError("Alex (workstream A): CHANGETABLE read + idempotent PK MERGE")


def seed(spark, cfg: dict, sink_fqn: str) -> int:
    """(B seed) Full copy of the current table state into the Delta bronze sink.

    May reuse the production warehouse copy (src/lakefed_ingest/copy_data.ipynb) via remote_query,
    or a Spark JDBC / pytds read on the cluster — transport decided with Chris. Runs AFTER
    current_version() has captured V0 so changes during the seed are reconciled on the first cdc
    cycle (idempotent PK MERGE => no gap, no dupe).

    Uses: src_* and sink_* and select_list.  Returns: int rows seeded.
    """
    raise NotImplementedError("Alex (workstream B): full seed copy into bronze")


def ensure_sink_table(spark, cfg: dict, sink_fqn: str) -> None:
    """Create the Delta bronze sink if absent (schema inferred from the source).

    May reuse the production prelude: src/lakefed_ingest/get_src_tbl_metadata.sql (DESCRIBE ... AS
    JSON) + create_sink_table.ipynb (column mapping, optional clustering / Iceberg). Idempotent.

    Uses: src_* (for schema inference), sink_* , select_list, enable_iceberg_reads,
    sink_cluster_cols.  Returns: None.
    """
    raise NotImplementedError("Alex/Chris: ensure Delta bronze sink exists (reuse create_sink_table)")
