from databricks.connect.session import DatabricksSession
from pyspark.sql import SparkSession
from pyspark.sql import DataFrame
from pyspark.dbutils import DBUtils
from typing import Optional
from pathlib import Path
from jsonschema import validate
from datetime import date, datetime, timezone
import textwrap
import pprint
import pyspark.sql.functions as F
from pyspark.sql import Window
from decimal import Decimal
import math
import json
import os
import re

# Get SparkSession
# https://docs.databricks.com/dev-tools/databricks-connect.html
def get_spark() -> SparkSession:
    try:
        return DatabricksSession.builder.getOrCreate()
    except ImportError:
        return SparkSession.builder.getOrCreate() # type: ignore

spark = get_spark()
dbutils = DBUtils(spark)

def get_partition_boundaries(catalog:str, schema:str, table:str, partition_col:str) -> tuple[int, int]:
    """Get partition boundaries (Min and max values for partition column)
    
    Args:
        catalog (str): Catalog name
        schema (str): Schema name
        table (str): Table name
        partition_col (str): Column used to partition the table
    
    Returns:
        tuple[int, int]: Partition boundaries
    """

    partition_boundaries_qry = f"""\
        select
          min({partition_col}) as lower_bound,
          max({partition_col}) as upper_bound
        from {catalog}.{schema}.{table}
    """
    print(textwrap.dedent(partition_boundaries_qry))
    
    partition_boundaries_df = spark.sql(partition_boundaries_qry)
    lower_bound = partition_boundaries_df.collect()[0]["lower_bound"]
    upper_bound = partition_boundaries_df.collect()[0]["upper_bound"]
    
    return lower_bound, upper_bound

def get_jdbc_config(root_dir:str, file_path:str) -> dict:
    """Get JDBC config from json file and return as dict

    JDBC pushdown is intended to be used only when Lakehouse
    Federation doesn't support functions needed to get the
    size of a table
    
    Args:
        root_dir (str): Root directory for project files
        file_path (str): Path of config file relative to project root (config/postgresql_jdbc.json)
    
    Returns:
        dict: JDBC config
    """

    file_path_full = os.path.join(root_dir, file_path)

    with open(file_path_full) as f:
        config = json.load(f)
    
    # Validate config schema
    config_schema = {
        "type" : "object",
        "properties" : {
            "host" : {"type" : "string"},
            "port" : {"type" : "string"},
            "database" : {"type" : "string"},
            "secret_scope" : {"type" : "string"},
            "secret_key_user" : {"type" : "string"},
            "secret_key_pwd" : {"type" : "string"},
        },
    }
    
    validate(instance=config, schema=config_schema)
    
    print('JDBC config:')
    pprint.pprint(config)

    return config

def _get_table_size_sqlserver(catalog:str, schema:str, table:str) -> int:
    """Get size of SQL Server table in MB
    
    Args:
        catalog (str): Catalog name
        schema (str): Schema name
        table (str): Table name
    
    Returns:
        int: Size of SQL Server table in MB
    """
    
    table_size_qry = f"""\
        SELECT
          s.name as schema, t.name as table, CAST(ROUND((SUM(a.total_pages) * 8) / 1024, 2) AS NUMERIC(36, 2)) AS table_size_mb
        FROM
          sys.tables t
          JOIN sys.indexes i ON t.OBJECT_ID = i.object_id
          JOIN sys.partitions p ON i.object_id = p.OBJECT_ID AND i.index_id = p.index_id
          JOIN sys.allocation_units a ON p.partition_id = a.container_id
          LEFT OUTER JOIN sys.schemas s ON t.schema_id = s.schema_id
        WHERE
          s.Name = '{schema}'
          AND t.name = '{table}'
          AND t.is_ms_shipped = 0
          AND i.object_id > 255
        GROUP BY
          s.Name, t.Name
    """
    print(f'Query used to get table size:\n{textwrap.dedent(table_size_qry)}')
    
    spark.sql(f'use catalog {catalog}')
    table_size_mb = spark.sql(table_size_qry).collect()[0]['table_size_mb']
    
    return table_size_mb

def _get_table_size_oracle(catalog:str, schema:str, table:str) -> int:
    """Get size of Oracle table in MB

    Requires permission to read the sys.dba_segments table
    
    Args:
        catalog (str): Catalog name
        schema (str): Schema name
        table (str): Table name
    
    Returns:
        int: Size of Oracle table in MB
    """
    
    table_size_qry = f"""\
        select
          bytes/1024/1024 as table_size_mb
        from sys.dba_segments
        where owner = '{schema.upper()}' and segment_name='{table.upper()}'
    """
    print(f'Query used to get table size:\n{textwrap.dedent(table_size_qry)}')
    
    spark.sql(f'use catalog {catalog}')
    table_size_mb = spark.sql(table_size_qry).collect()[0]['table_size_mb']
    
    return table_size_mb

def _get_table_size_postgresql(
    catalog: str,
    schema: str,
    table: str,
    root_dir: Optional[str] = None,
    jdbc_config_file: Optional[str] = None,
) -> int:
    """Get PostgreSQL table size

    Getting a table's size in PostgreSQL requires either using a JDBC pushdown query,
    or creating a view in the source database that can then be queried using Lakehouse Federation.
    This is because Lakehouse Federation doesn't currently support PostgreSQL object size functions
    such as pg_table_size()

    To use JDBC pushdown, add a config file that matches the schema of config/postgresql_jdbc.json.
    Then provide the path to the config file when calling this function.
    https://docs.databricks.com/en/connect/external-systems/postgresql.html

    To use Lakehouse Federation, create the view below in the PostgreSQL database and don't
    use the jdbc_config_file argument.
    
    create or replace view public.vw_pg_table_size
     as
     select
      table_schema,
      table_name,
      pg_table_size(quote_ident(table_name)),
      pg_size_pretty(pg_table_size(quote_ident(table_name))) as pg_table_size_pretty
    from information_schema.tables
    where table_schema not in ('pg_catalog', 'information_schema')
    and table_type = 'BASE TABLE';
    
    Args:
        schema (str): Schema name
        table (str): Table name
        jdbc_config_file (str): Path of config file relative to project root (config/postgresql_jdbc.json)
    
    Returns:
        int: Size of PostgreSQL table in MB
    """
    
    table_size_mb = 0

    if jdbc_config_file:
        print('Using JDBC pushdown to get table size')
        config = get_jdbc_config(root_dir, jdbc_config_file) # type: ignore
        
        # Get database user credentials from secrets
        # https://docs.databricks.com/en/security/secrets/index.html
        user = dbutils.secrets.get(scope=config['secret_scope'], key=config['secret_key_user'])
        password = dbutils.secrets.get(scope=config['secret_scope'], key=config['secret_key_pwd'])
        
        # Query to get table size will be pushed down to PostgreSQL database
        table_size_qry = f"(select pg_relation_size('{schema}.{table}') as size_in_bytes) as size_in_bytes"
        
        print(f'Query used to get table size:\n{table_size_qry}')
        
        table_size_in_bytes = (
            spark.read.format("postgresql")
            .option("dbtable", table_size_qry)
            .option("host", config['host'])
            .option("port", config['port'])
            .option("database", config['database'])
            .option("user", user)
            .option("password", password)
            .load()
        ).collect()[0]['size_in_bytes']
    else:
        print('Querying PostgreSQL view to get table size')
        table_size_qry = f"""\
            select pg_table_size
            from public.vw_pg_table_size
            where table_schema = '{schema}'
            and table_name = '{table}'
        """
        
        print(f'Query used to get table size:\n{textwrap.dedent(table_size_qry)}')
        
        spark.sql(f'use catalog {catalog}')
        table_size_in_bytes = spark.sql(table_size_qry).collect()[0]['pg_table_size']
    
    table_size_mb = math.ceil(table_size_in_bytes / 1024 / 1024)
    
    return table_size_mb

def _get_table_size_redshift(catalog:str, schema:str, table:str) -> int:
    """Get Redshift table size
    
    Args:
        catalog (str): Catalog name
        schema (str): Schema name
        table (str): Table name
    
    Returns:
        int: Size of Redshift table in MB
    """
    
    table_size_qry = f"""\
        select size as table_size_mb
        from {catalog}.pg_catalog.svv_table_info
        where schema = '{schema}'
        and table = '{table}'
    """
    print(f'Query used to get table size:\n{textwrap.dedent(table_size_qry)}')
    
    spark.sql(f'use catalog {catalog}')
    table_size_mb = spark.sql(table_size_qry).collect()[0]['table_size_mb']
    
    return table_size_mb

def _get_table_size_synapse(catalog:str, schema:str, table:str) -> int:
    """Get Synapse table size
    
    Args:
        catalog (str): Catalog name
        schema (str): Schema name
        table (str): Table name
    
    Returns:
        int: Size of Synapse table in MB
    """
    
    table_size_qry = f"""\
        with table_size_base as (
          select
            s.name as schema_name,
            t.name as table_name,
            nt.name as node_table_name,
            row_number() over(partition by nt.name order by (select null)) as node_table_name_seq,
            tp.distribution_policy_desc as distribution_policy_name,
            c.name as distribution_column,
            nt.distribution_id as distribution_id,
            i.type as index_type,
            i.type_desc as index_type_desc,
            nt.pdw_node_id as pdw_node_id,
            pn.type as pdw_node_type,
            pn.name as pdw_node_name,
            di.name as dist_name,
            di.position as dist_position,
            nps.partition_number as partition_nmbr,
            (
              (
                nps.in_row_data_page_count + nps.row_overflow_used_page_count + nps.lob_used_page_count
              ) * 8.0
            ) / 1000 as data_space_mb,
            nps.row_count as row_count
          from
            sys.schemas s
            inner join sys.tables t on s.schema_id = t.schema_id
            inner join sys.indexes i on t.object_id = i.object_id
            and i.index_id <= 1
            inner join sys.pdw_table_distribution_properties tp on t.object_id = tp.object_id
            inner join sys.pdw_table_mappings tm on t.object_id = tm.object_id
            inner join sys.pdw_nodes_tables nt on tm.physical_name = nt.name
            inner join sys.dm_pdw_nodes pn on nt.pdw_node_id = pn.pdw_node_id
            inner join sys.pdw_distributions di on nt.distribution_id = di.distribution_id
            inner join sys.dm_pdw_nodes_db_partition_stats nps on nt.object_id = nps.object_id
            and nt.pdw_node_id = nps.pdw_node_id
            and nt.distribution_id = nps.distribution_id
            and i.index_id = nps.index_id
            left outer join (select * from sys.pdw_column_distribution_properties where distribution_ordinal = 1) cdp on t.object_id = cdp.object_id
            left outer join sys.columns c on cdp.object_id = c.object_id
            and cdp.column_id = c.column_id
          where
            pn.type = 'COMPUTE'
            and s.name = '{schema}'
            and t.name = '{table}'
        )
        select
          schema_name,
          table_name,
          sum(row_count) as table_row_count,
          sum(data_space_mb) as table_size_mb
        from table_size_base
        group by all
    """
    print(f'Query used to get table size:\n{textwrap.dedent(table_size_qry)}')
    
    spark.sql(f'use catalog {catalog}')
    table_size_mb = spark.sql(table_size_qry).collect()[0]['table_size_mb']
    
    return table_size_mb

def _get_table_size_delta(catalog:str, schema:str, table:str) -> int:
    """Get Delta Lake table size

    A Delta source is intended only for testing
    
    Args:
        catalog (str): Catalog name
        schema (str): Schema name
        table (str): Table name
    
    Returns:
        int: Size of Delta table in MB
    """
    
    table_size_in_bytes = spark.sql(f'desc detail {catalog}.{schema}.{table}').collect()[0]['sizeInBytes']
    table_size_mb = table_size_in_bytes / 1024 / 1024
    return table_size_mb

def get_table_size(catalog:str, schema:str, table:str, src_type:str, root_dir:Optional[str]=None, jdbc_config_file:Optional[str]=None) -> int:
    """Get source table size
    
    Args:
        catalog (str): Catalog name
        schema (str): Schema name
        table (str): Table name
        src_type (str): Source system type (SQL Server, PostgreSQL, etc.)
        root_dir (str): Directory containing the JDBC config file
        jdbc_config_file (str): File containing JDBC configs (host, database, etc.)
    
    Returns:
        int: Source table size in MB
    """
    
    table_size_mb = 0
    
    if src_type == 'sqlserver':
        table_size_mb = _get_table_size_sqlserver(catalog, schema, table)
    elif src_type == 'oracle':
        table_size_mb = _get_table_size_oracle(catalog, schema, table)
    elif src_type == 'postgresql':
        table_size_mb = _get_table_size_postgresql(catalog, schema, table, root_dir, jdbc_config_file)
    elif src_type == 'redshift':
        table_size_mb = _get_table_size_redshift(catalog, schema, table)
    elif src_type == 'synapse':
        table_size_mb = _get_table_size_synapse(catalog, schema, table)
    elif src_type == 'delta':
        table_size_mb = _get_table_size_delta(catalog, schema, table)
    else:
        raise ValueError(f'Unsupported src_type: {src_type}')
    
    print(f'Table size MB: {table_size_mb}')

    return table_size_mb

def get_internal_bound_value(bound_value) -> int:
    """Get numeric representation of bound value for get_partition_list function
    
    Args:
        bound_value: lower or upper bound value of partition column 
    
    Returns:
        Numeric representation of bound value
    """
    
    if isinstance(bound_value, (int, float, Decimal)):
        bound_value = int(bound_value)
    elif isinstance(bound_value, datetime):
        bound_value = int(bound_value.replace(tzinfo=timezone.utc).timestamp())
    elif isinstance(bound_value, date):
        dt_bound_value = datetime(
            year=bound_value.year,
            month=bound_value.month,
            day=bound_value.day,
        )
        bound_value = int(dt_bound_value.replace(tzinfo=timezone.utc).timestamp())
    else:
        raise ValueError(f'Unsupported data type: {type(bound_value)}. Only numeric, date, and datetime are supported')

    return bound_value

def bound_value_to_str(bound_value:int, bound_value_orig) -> str:
    """Convert bound value to string for SQL where clause
    
    Args:
        bound_value (int): bound value as int
        bound_value_orig: original bound value used to determine the type
    
    Returns:
        String representation of bound value
    """

    if isinstance(bound_value_orig, (int, float, Decimal)):
        bound_value_str = str(bound_value)
    elif isinstance(bound_value_orig, datetime):
        bound_value_str = f"'{datetime.fromtimestamp(bound_value, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}'"
    elif isinstance(bound_value_orig, date):
        bound_value_str = f"'{datetime.fromtimestamp(bound_value, tz=timezone.utc).strftime('%Y-%m-%d')}'"
    else:
        raise ValueError(f'Unsupported data type: {type(bound_value)}. Only int, date, and datetime are supported')
    
    return bound_value_str

def get_partition_list(partition_col:str, lower_bound, upper_bound, num_partitions:int) -> list[dict]:
    """Generate list of queries based on partitioning schematic
    
    Function is derived from the JDBC partitioning code in Spark:
    
    Given a partitioning schematic (a column of integral type, a number of
    partitions, and upper and lower bounds on the column's value), generate
    WHERE clauses for each partition so that each row in the table appears
    exactly once.  The parameters minValue and maxValue are advisory in that
    incorrect values may cause the partitioning to be poor, but no data
    will fail to be represented.
    
    Null value predicate is added to the first partition where clause to include
    the rows with null value for the partitions column.
    
    Spark source code - https://github.com/apache/spark/blob/7bbcbb84c266b6ff418cd2c3361aa7350299d0ae/sql/core/src/main/scala/org/apache/spark/sql/execution/datasources/jdbc/JDBCRelation.scala#L129
    
    Args:
        partition_col (str): Column used to generate partition queries. Using a clustered index column is recommended.
        lower_bound: Lowest partition column value
        upper_bound: Highest partition column value
        num_partitions (str): Number of partition queries
    
    Returns:
        list[dict]: List of dicts containing lower_bound, upper_bound, and src_query
    """
    
    # Bound values are converted to int and then to string. The original
    # bound value is saved first so we know how to convert the int to string.
    lower_bound_type = type(lower_bound)
    upper_bound_type = type(upper_bound)

    if lower_bound_type == upper_bound_type:
        lower_bound_orig = lower_bound
    else:
        raise TypeError(f'Bound values must have the same type. Lower bound: {lower_bound_type}, Upper bound: {upper_bound_type}')
    
    # Get numeric representation of bound values for stride calculation
    lower_bound = get_internal_bound_value(lower_bound)
    upper_bound = get_internal_bound_value(upper_bound)

    partition_list = []
    stride = int(upper_bound / num_partitions - lower_bound / num_partitions)
    
    i = 0
    currentValue = lower_bound
    while (i < num_partitions):
        lBoundValue = bound_value_to_str(currentValue, lower_bound_orig)
        lBound = f'{partition_col} >= {lBoundValue}' if i != 0 else None
        currentValue += stride
        uBoundValue = bound_value_to_str(currentValue, lower_bound_orig)
        uBound = f'{partition_col} < {uBoundValue}' if i != num_partitions - 1 else None
        if uBound == None:
            whereClause = lBound
        elif lBound == None:
            whereClause = f'{uBound} or {partition_col} is null'
        else:
            whereClause = f'{lBound} and {uBound}'
        partition_list.append({'id' : i, 'where_clause' : whereClause})
        i = i + 1
        
    return partition_list

def get_partition_df(partition_list:list[dict], num_partitions:int, batch_size:int = 1000) -> DataFrame:
    """Create dataframe from list of partitions

    Partitions are defined by a where clause that selects a range of data.
    A batch_id is assigned to each partition. This provides a way of
    dividing the full partition table into N size batches.
    
    Batching is used to avoid exceeding the 1000 item limit of the for each task.
    
    Args:
        partition_list (list[dict]): List of dictionaries containing partition ranges
        num_partitions (int): Number of partitions is used to determing the number of batches
        batch_size (int): Batch size. Defaults to 1000.
    """
    
    # Calculate number of batches and round up
    num_batches = math.ceil(num_partitions / batch_size)
    print(f'Number of batches: {num_batches}')
    
    partition_df = spark.createDataFrame(
        partition_list, # type: ignore
        schema="id int, where_clause string, batch_id int"
    )
    window = Window.orderBy("id")
    partition_df = partition_df.withColumn("batch_id", F.ntile(num_batches).over(window))

    return partition_df