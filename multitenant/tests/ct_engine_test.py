"""Unit tests for the pure query/type builders in ct_engine.py.

These cover the string- and type-building logic that assembles the CHANGETABLE read and the
idempotent PK MERGE — the part that must be exactly right — without a SQL Server or Spark
(ct_engine's pytds/pyspark/databricks imports are lazy, so the module imports bare). The
DB-touching functions (current_version, min_valid_version, read_and_merge_ct, seed,
ensure_sink_table) are exercised end-to-end in the Tier-3 validation, not here.
"""
import ct_engine as ce


def test_ct_projection_pk_from_ct_nonpk_from_base():
    out = ce.ct_projection("order_id, amount, status", "order_id")
    assert "ct.[order_id] AS [order_id]" in out      # PK from change table (delete-safe)
    assert "t.[amount] AS [amount]" in out            # non-PK from base table
    assert "t.[status] AS [status]" in out
    assert out.strip().endswith("ct.SYS_CHANGE_OPERATION AS op")


def test_ct_projection_composite_pk():
    out = ce.ct_projection("ord, line_no, qty", "ord, line_no")
    parts = [p.strip() for p in out.split(",")]   # exact-match to avoid 'ct.[ord]' ⊃ 't.[ord]'
    assert "ct.[ord] AS [ord]" in parts and "ct.[line_no] AS [line_no]" in parts
    assert "t.[qty] AS [qty]" in parts
    # PK never taken from the base table (would be t.[ord]/t.[line_no]):
    assert "t.[ord] AS [ord]" not in parts and "t.[line_no] AS [line_no]" not in parts


def test_ct_join_and_merge_on():
    assert ce.ct_join("ord, line_no") == "t.[ord] = ct.[ord] AND t.[line_no] = ct.[line_no]"
    assert ce.merge_on("order_id") == "tgt.`order_id` = s.`order_id`"


def test_ct_read_query_shape():
    cfg = {"src_schema": "dbo", "src_table": "orders",
           "select_list": "order_id, amount", "primary_key": "order_id"}
    q = ce.ct_read_query(cfg, 8)
    assert "CHANGETABLE(CHANGES [dbo].[orders], 8) ct" in q
    assert "LEFT JOIN [dbo].[orders] t ON t.[order_id] = ct.[order_id]" in q


def test_merge_set_excludes_pk():
    assert (ce.merge_set("order_id, amount, status", "order_id")
            == "tgt.`amount` = s.`amount`, tgt.`status` = s.`status`")


def test_merge_set_all_pk_is_noop_safe():
    assert ce.merge_set("a, b", "a, b") == "tgt.`a` = s.`a`, tgt.`b` = s.`b`"


def test_merge_insert_cols_and_vals():
    assert ce.merge_insert_cols("order_id, amount") == "`order_id`, `amount`"
    assert ce.merge_insert_vals("order_id, amount") == "s.`order_id`, s.`amount`"


def test_split_handles_blanks_and_none():
    assert ce._split("a, b ,, c ") == ["a", "b", "c"]
    assert ce._split(None) == []
