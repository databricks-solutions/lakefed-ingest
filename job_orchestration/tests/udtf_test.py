import re
import pytest
from pathlib import Path


def _load_udtf_class():
    """Extract and load GeneratePartitionList from create_partition_udtf.sql.

    The UDTF is defined as a Python class embedded inside a SQL string delimited
    by $$ markers (Databricks LANGUAGE PYTHON syntax). Rather than deploying the
    UDTF to a warehouse and calling it remotely, we pull the class out of the SQL
    file with a regex and load it into the local Python namespace via exec(). This
    lets pytest test the partition logic directly — no Databricks cluster needed.
    """
    sql_path = Path(__file__).parent.parent / "src/lakefed_ingest/create_partition_udtf.sql"
    sql = sql_path.read_text()
    # re.DOTALL makes . match newlines so the pattern spans the entire class body
    match = re.search(r'\$\$(.*?)\$\$', sql, re.DOTALL)
    if match is None:
        raise ValueError(f"No python code block found in {sql_path}")
    code = match.group(1).strip()
    if not code:
        raise ValueError(f"Empty python code block in {sql_path}")
    ns = {}
    exec(code, ns)
    return ns['GeneratePartitionList']


# Load once at module import time; all test functions share this class reference.
GeneratePartitionList = _load_udtf_class()


# ---- _to_int ----------------------------------------------------------------
# _to_int(val, col_type) converts a string-encoded bound value to an
# integer so that stride arithmetic can be done uniformly across all types.
# Numeric types are cast directly; DATE and TIMESTAMP are converted to seconds
# since the Unix epoch (UTC midnight for dates, truncating sub-seconds for timestamps).

@pytest.mark.parametrize("val,col_type,expected", [
    ("42",                         "INT",       42),
    ("42",                         "BIGINT",    42),
    ("3.14",                       "FLOAT",      3),           # float() then int() truncates
    ("2022-12-28 23:55:59",        "TIMESTAMP", 1672271759),
    ("2022-12-28 23:55:59.342380", "TIMESTAMP", 1672271759),   # sub-seconds are stripped before parsing
    ("2023-03-21",                 "DATE",      1679356800),   # UTC midnight of that date
])
def test_to_int(val, col_type, expected):
    assert GeneratePartitionList()._to_int(val, col_type) == expected


@pytest.mark.xfail(raises=ValueError, strict=True)
def test_to_int_unsupported_type():
    # Passing an unrecognised col_type must raise ValueError, not silently return wrong data.
    GeneratePartitionList()._to_int("42", "UNKNOWN")


# ---- _to_str ----------------------------------------------------------------
# _to_str(int_val, col_type) is the inverse of _to_int: it converts an
# integer back to the SQL literal string that will appear in a WHERE clause.
# Numeric types render as plain numbers; DATE and TIMESTAMP render as quoted
# strings (e.g. "'2023-03-21'") so the clause is valid SQL when interpolated.

@pytest.mark.parametrize("int_val,col_type,expected", [
    (42,         "INT",       "42"),
    (1672271759, "TIMESTAMP", "'2022-12-28 23:55:59'"),   # single-quoted for SQL
    (1679356800, "DATE",      "'2023-03-21'"),             # single-quoted for SQL
])
def test_to_str(int_val, col_type, expected):
    assert GeneratePartitionList()._to_str(int_val, col_type) == expected


# ---- eval -------------------------------------------------------------------
# eval() is the main entry point of the UDTF. Given a partition column name,
# lower/upper bound strings, the column type, and a partition count N, it yields
# N (id, where_clause) tuples that together cover every row in the table.
#
# The algorithm follows Spark's JDBC partitioning logic:
#   stride = int(upper_bound / N - lower_bound / N)
#
# Partition 0 always appends "or <col> is null" so NULL rows aren't skipped.
# Partition N-1 has no upper bound so rows beyond the computed maximum are included.
#
# Example — INT, bounds 1 to 1000, 5 partitions:
#   stride = int(1000/5 - 1/5) = int(199.8) = 199
#   boundaries: 200, 399, 598, 797
#   → "customer_id < 200 or customer_id is null"
#   → "customer_id >= 200 and customer_id < 399"  ... etc.

partition_list_expected_int = [
    (0, 'customer_id < 200 or customer_id is null'),
    (1, 'customer_id >= 200 and customer_id < 399'),
    (2, 'customer_id >= 399 and customer_id < 598'),
    (3, 'customer_id >= 598 and customer_id < 797'),
    (4, 'customer_id >= 797'),
]

partition_list_expected_dt = [
    (0, "dt_col < '2024-01-19 04:47:11' or dt_col is null"),
    (1, "dt_col >= '2024-01-19 04:47:11' and dt_col < '2024-02-06 09:34:22'"),
    (2, "dt_col >= '2024-02-06 09:34:22' and dt_col < '2024-02-24 14:21:33'"),
    (3, "dt_col >= '2024-02-24 14:21:33' and dt_col < '2024-03-13 19:08:44'"),
    (4, "dt_col >= '2024-03-13 19:08:44'"),
]

partition_list_expected_date = [
    (0, "date_col < '2024-01-19' or date_col is null"),
    (1, "date_col >= '2024-01-19' and date_col < '2024-02-06'"),
    (2, "date_col >= '2024-02-06' and date_col < '2024-02-24'"),
    (3, "date_col >= '2024-02-24' and date_col < '2024-03-13'"),
    (4, "date_col >= '2024-03-13'"),
]


@pytest.mark.parametrize("partition_col,lower_bound,upper_bound,col_type,num_partitions,expected", [
    # INT: stride arithmetic on plain integers
    ('customer_id', '1',                   '1000',                'INT',       5, partition_list_expected_int),
    # TIMESTAMP: bounds converted to/from UTC epoch seconds; sub-seconds ignored
    ('dt_col',      '2024-01-01 00:00:00', '2024-03-31 23:55:59', 'TIMESTAMP', 5, partition_list_expected_dt),
    # DATE: bounds converted to/from UTC midnight epoch seconds
    ('date_col',    '2024-01-01',          '2024-03-31',          'DATE',      5, partition_list_expected_date),
])
def test_eval(partition_col, lower_bound, upper_bound, col_type, num_partitions, expected):
    result = list(GeneratePartitionList().eval(partition_col, lower_bound, upper_bound, col_type, num_partitions))
    assert result == expected


def test_eval_null_bounds():
    # When MIN/MAX of the partition column returns NULL (empty table), eval()
    # yields a single "1=1" clause so the copy task still runs and produces an
    # empty sink table rather than failing.
    result = list(GeneratePartitionList().eval('col', None, None, 'INT', 5))
    assert result == [(0, '1=1')]
