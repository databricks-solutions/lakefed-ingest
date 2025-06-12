from pyspark.testing import assertDataFrameEqual
from jsonschema import exceptions
import pytest
import textwrap
from lakefed_ingest.main import *

# Partition lists scoped to module for multiple tests
partition_list_expected_int = [
    {'id': 0, 'where_clause': 'customer_id < 200 or customer_id is null'},
    {'id': 1, 'where_clause': 'customer_id >= 200 and customer_id < 399'},
    {'id': 2, 'where_clause': 'customer_id >= 399 and customer_id < 598'},
    {'id': 3, 'where_clause': 'customer_id >= 598 and customer_id < 797'},
    {'id': 4, 'where_clause': 'customer_id >= 797'}
]

partition_list_expected_dt = [
    {'id': 0, 'where_clause': "dt_col < '2024-01-19 04:47:11' or dt_col is null"},
    {'id': 1, 'where_clause': "dt_col >= '2024-01-19 04:47:11' and dt_col < '2024-02-06 09:34:22'"},
    {'id': 2, 'where_clause': "dt_col >= '2024-02-06 09:34:22' and dt_col < '2024-02-24 14:21:33'"},
    {'id': 3, 'where_clause': "dt_col >= '2024-02-24 14:21:33' and dt_col < '2024-03-13 19:08:44'"},
    {'id': 4, 'where_clause': "dt_col >= '2024-03-13 19:08:44'"}
]

partition_list_expected_date = [
    {"id": 0, "where_clause": "date_col < '2024-01-19' or date_col is null"},
    {"id": 1, "where_clause": "date_col >= '2024-01-19' and date_col < '2024-02-06'"},
    {"id": 2, "where_clause": "date_col >= '2024-02-06' and date_col < '2024-02-24'"},
    {"id": 3, "where_clause": "date_col >= '2024-02-24' and date_col < '2024-03-13'"},
    {"id": 4, "where_clause": "date_col >= '2024-03-13'"}
]

@pytest.mark.parametrize(
    "partition_col,lower_bound,upper_bound,num_partitions,expected",
    [
        ('customer_id', 1, 1000, 5, partition_list_expected_int),
        ('dt_col', datetime(2024, 1, 1), datetime(2024, 3, 31, 23, 55, 59, 342380), 5, partition_list_expected_dt),
        ('date_col', datetime(2024, 1, 1).date(), datetime(2024, 3, 31).date(), 5, partition_list_expected_date)
    ]
)
def test_get_partition_list(partition_col,lower_bound,upper_bound,num_partitions,expected):
    assert get_partition_list(partition_col,lower_bound,upper_bound,num_partitions) == expected

def test_get_partition_df() -> None:

    partition_list_w_batch_expected = [
        {'id': 0, 'where_clause': 'customer_id < 200 or customer_id is null', 'batch_id': 1},
        {'id': 1, 'where_clause': 'customer_id >= 200 and customer_id < 399', 'batch_id': 1},
        {'id': 2, 'where_clause': 'customer_id >= 399 and customer_id < 598', 'batch_id': 2},
        {'id': 3, 'where_clause': 'customer_id >= 598 and customer_id < 797', 'batch_id': 2},
        {'id': 4, 'where_clause': 'customer_id >= 797', 'batch_id': 3}
    ]

    partition_df_expected = spark.createDataFrame(
        partition_list_w_batch_expected, # type: ignore
        schema="id int, where_clause string, batch_id int"
    )

    partition_df = get_partition_df(partition_list=partition_list_expected_int, num_partitions=5, batch_size=2)

    assertDataFrameEqual(partition_df, partition_df_expected)

def test_get_jdbc_config_fails() -> None:
    """Test jdbc config with incorrect schema
    
    Incorrect schema should result in jsonschema.exceptions.ValidationError being raised
    """

    root_dir = str(Path(__file__).resolve().parents[1])

    with pytest.raises(exceptions.ValidationError):
        get_jdbc_config(root_dir, file_path='tests/jdbc_config_bad_schema.json')

@pytest.mark.parametrize(
    "bound_input,expected",
    [
        (42, 42),
        (datetime(2022, 12, 28, 23, 55, 59, 342380), 1672271759),
        (datetime(2023, 3, 21), 1679356800),
        pytest.param("42", 42, marks=pytest.mark.xfail),
    ],
)
def test_get_internal_bound_value(bound_input, expected):
    assert get_internal_bound_value(bound_input) == expected

@pytest.mark.parametrize(
    "bound_input,bound_orig,expected",
    [
        (42, 42, '42'),
        (1672271759, datetime(2022, 12, 28, 23, 55, 59, 342380), "'2022-12-28 23:55:59'"),
        (1679356800, datetime(2023, 3, 21).date(), "'2023-03-21'"),
        pytest.param(42, "42", 42, marks=pytest.mark.xfail),
    ],
)
def test_bound_value_to_str(bound_input, bound_orig, expected):
    assert bound_value_to_str(bound_input, bound_orig) == expected