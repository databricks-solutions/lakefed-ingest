"""Unit tests for the CT query-string builder UDFs in copy_data_ct.ipynb.

Single source of truth: the Python bodies are extracted verbatim from the notebook's
`CREATE ... FUNCTION ... LANGUAGE PYTHON AS $$ ... $$` blocks and executed in-process
(same idiom as tests/udtf_test.py for the partition UDTF) — so these tests exercise the
exact code that runs on the warehouse, with no cluster needed.
"""
import json
import re
import textwrap
from pathlib import Path

_NB = Path(__file__).parent.parent / "src" / "multitenant" / "copy_data_ct.ipynb"
_PAT = re.compile(
    r"CREATE OR REPLACE TEMPORARY FUNCTION\s+(\w+)\s*\((.*?)\)\s*"
    r"RETURNS\s+STRING\s+LANGUAGE\s+PYTHON\s+AS\s+\$\$(.*?)\$\$",
    re.DOTALL | re.IGNORECASE,
)


def _load_udfs():
    cells = json.loads(_NB.read_text())["cells"]
    sql = "\n".join(
        ("".join(c["source"]) if isinstance(c["source"], list) else c["source"])
        for c in cells if c["cell_type"] == "code"
    )
    ns = {}
    for name, args, body in _PAT.findall(sql):
        argnames = [a.strip().split()[0] for a in args.split(",") if a.strip()]
        fn = (f"def {name}({', '.join(argnames)}):\n"
              + textwrap.indent(textwrap.dedent(body).strip("\n"), "    ") + "\n")
        exec(fn, ns)  # noqa: S102 — trusted repo content
    return ns


UDF = _load_udfs()


def test_all_builders_extracted():
    for n in ("ct_projection", "ct_join", "merge_on", "merge_set",
              "merge_insert_cols", "merge_insert_vals"):
        assert n in UDF, f"missing builder {n}"


def test_ct_projection_pk_from_ct_nonpk_from_t():
    out = UDF["ct_projection"]("order_id, amount, status", "order_id")
    assert "ct.[order_id] AS [order_id]" in out            # PK from ct (delete-safe)
    assert "t.[amount] AS [amount]" in out                  # non-PK from t
    assert "t.[status] AS [status]" in out
    assert out.strip().endswith("ct.SYS_CHANGE_OPERATION AS sys_change_operation")


def test_ct_projection_composite_pk():
    out = UDF["ct_projection"]("ord, line_no, qty", "ord, line_no")
    assert "ct.[ord] AS [ord]" in out and "ct.[line_no] AS [line_no]" in out
    assert "t.[qty] AS [qty]" in out


def test_ct_join_and_merge_on_clauses():
    assert UDF["ct_join"]("ord, line_no") == "t.[ord] = ct.[ord] AND t.[line_no] = ct.[line_no]"
    assert UDF["merge_on"]("order_id") == "tgt.`order_id` = s.`order_id`"


def test_merge_set_excludes_pk():
    assert (UDF["merge_set"]("order_id, amount, status", "order_id")
            == "tgt.`amount` = s.`amount`, tgt.`status` = s.`status`")


def test_merge_set_all_pk_table_is_noop_safe():
    # degenerate all-PK table: SET pk=pk (harmless no-op) rather than empty
    assert UDF["merge_set"]("a, b", "a, b") == "tgt.`a` = s.`a`, tgt.`b` = s.`b`"


def test_merge_insert_cols_and_vals_cover_all_columns():
    assert UDF["merge_insert_cols"]("order_id, amount") == "`order_id`, `amount`"
    assert UDF["merge_insert_vals"]("order_id, amount") == "s.`order_id`, s.`amount`"
