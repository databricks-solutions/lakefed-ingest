"""Unit tests for size-bounded partition planning (partitions.py).

Mirrors the expectations of tests/udtf_test.py (the production GeneratePartitionList UDTF), which
implements the same Spark-JDBC-derived stride; here columns are bracket-quoted for SQL Server.
"""
import re
from datetime import date, datetime
from decimal import Decimal

import pytest

import partitions as p


# ---- num_partitions_for: size-bounded, floor of 2 -------------------------------------------
def test_num_partitions_is_size_over_partition_size():
    assert p.num_partitions_for(10_240, 256) == 40


def test_num_partitions_floor_is_two():
    assert p.num_partitions_for(10, 256) == 2
    assert p.num_partitions_for(None, 256) == 2


def test_num_partitions_accepts_decimal_size():
    assert p.num_partitions_for(Decimal("1024.75"), 512) == 2
    assert p.num_partitions_for(Decimal("5120.00"), 512) == 10


def test_num_partitions_rejects_non_positive_partition_size():
    with pytest.raises(ValueError):
        p.num_partitions_for(1024, 0)


def test_partition_count_is_not_tied_to_cores():
    # The whole point: a big table yields many partitions (run N at a time), not ~#cores.
    assert p.num_partitions_for(1_000_000, 64) == 15_625


# ---- get_partition_list: same results as the production UDTF (bracket-quoted) ----------------
def test_int_partitions_match_udtf():
    got = p.get_partition_list("customer_id", 1, 1000, 5)
    assert [x["where_clause"] for x in got] == [
        "[customer_id] < 200 or [customer_id] is null",
        "[customer_id] >= 200 and [customer_id] < 399",
        "[customer_id] >= 399 and [customer_id] < 598",
        "[customer_id] >= 598 and [customer_id] < 797",
        "[customer_id] >= 797",
    ]
    assert [x["id"] for x in got] == [0, 1, 2, 3, 4]


def test_datetime_partitions_match_udtf():
    got = p.get_partition_list("dt_col", datetime(2024, 1, 1), datetime(2024, 3, 31, 23, 55, 59), 5)
    assert [x["where_clause"] for x in got] == [
        "[dt_col] < '2024-01-19 04:47:11' or [dt_col] is null",
        "[dt_col] >= '2024-01-19 04:47:11' and [dt_col] < '2024-02-06 09:34:22'",
        "[dt_col] >= '2024-02-06 09:34:22' and [dt_col] < '2024-02-24 14:21:33'",
        "[dt_col] >= '2024-02-24 14:21:33' and [dt_col] < '2024-03-13 19:08:44'",
        "[dt_col] >= '2024-03-13 19:08:44'",
    ]


def test_date_partitions_match_udtf():
    got = p.get_partition_list("date_col", date(2024, 1, 1), date(2024, 3, 31), 5)
    assert [x["where_clause"] for x in got] == [
        "[date_col] < '2024-01-19' or [date_col] is null",
        "[date_col] >= '2024-01-19' and [date_col] < '2024-02-06'",
        "[date_col] >= '2024-02-06' and [date_col] < '2024-02-24'",
        "[date_col] >= '2024-02-24' and [date_col] < '2024-03-13'",
        "[date_col] >= '2024-03-13'",
    ]


def _matches(clause, v):
    """Evaluate a generated integer WHERE clause against a value (None = NULL)."""
    expr = re.sub(r"\[\w+\]", "x", clause).replace(" is null", " is None").replace(" or ", " or ").replace(" and ", " and ")
    return bool(eval(expr, {}, {"x": v})) if v is not None else "is None" in expr


@pytest.mark.parametrize("lb,ub,n", [(1, 1000, 5), (0, 9_999_999, 37), (-500, 500, 9), (17, 3_000_016, 50)])
def test_every_value_lands_in_exactly_one_partition(lb, ub, n):
    clauses = [x["where_clause"] for x in p.get_partition_list("pk", lb, ub, n)]
    samples = list(range(lb, ub + 1, max(1, (ub - lb) // 997))) + [lb, ub, lb - 10, ub + 10]
    for v in samples:
        assert sum(_matches(c, v) for c in clauses) == 1, v
    assert sum(_matches(c, None) for c in clauses) == 1   # NULLs only in the first partition


def test_single_partition_covers_everything():
    assert p.get_partition_list("pk", 1, 10, 1) == [{"id": 0, "where_clause": "1=1"}]


def test_mismatched_bound_types_raise():
    with pytest.raises(TypeError):
        p.get_partition_list("pk", 1, datetime(2024, 1, 1), 4)


def test_unsupported_bound_type_raises():
    with pytest.raises(ValueError):
        p.get_partition_list("pk", "a", "z", 4)


def test_effective_partitions_clamped_to_range():
    assert p.effective_num_partitions(1, 5, 40) == 4      # only 4 integer steps
    assert p.effective_num_partitions(7, 7, 40) == 1      # single value
    assert p.effective_num_partitions(1, 10_000, 40) == 40


# ---- metadata queries ------------------------------------------------------------------------
def test_table_size_query_targets_catalog_and_filters():
    q = p.table_size_mb_query("dbo", "orders")
    for frag in ("sys.allocation_units", "total_pages", "table_size_mb",
                 "s.name = 'dbo'", "t.name = 'orders'"):
        assert frag in q


def test_table_size_query_escapes_quotes():
    assert "t.name = 'o''brien'" in p.table_size_mb_query("dbo", "o'brien")


def test_bounds_query_brackets_identifiers():
    assert p.bounds_query("dbo", "orders", "order_id") == \
        "SELECT MIN([order_id]) AS lb, MAX([order_id]) AS ub FROM [dbo].[orders]"
