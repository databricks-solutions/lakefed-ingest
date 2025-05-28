# Databricks notebook source
# MAGIC %md
# MAGIC ## Write to External Database
# MAGIC This notebook can be used to write data to an external database for testing the bulk ingest solution. See the [write data with JDBC](https://learn.microsoft.com/en-us/azure/databricks/connect/external-systems/jdbc#write-data-with-jdbc) doc for more details.
# MAGIC
# MAGIC Databricks recommends using [secrets](https://learn.microsoft.com/en-us/azure/databricks/security/secrets/) to store your database credentials.

# COMMAND ----------

dbutils.widgets.text('catalog', 'lakefed_bulk_ingest_src', '01 Catalog')
dbutils.widgets.text('schema', 'default', '02 Schema')
dbutils.widgets.text('src_table', 'lakefed_src', '03 Source Table')
dbutils.widgets.text('host', '', '04 Database Server Host')
dbutils.widgets.text('secret_scope_name', '', '06 Secret Scope Name')

# COMMAND ----------

# DBTITLE 1,Config
catalog = dbutils.widgets.get('catalog')
schema = dbutils.widgets.get('schema')
src_table = dbutils.widgets.get('src_table')
host = dbutils.widgets.get('host')
secret_scope_name = dbutils.widgets.get('secret_scope_name')

jdbc_url = f'jdbc:postgresql://{host}:5432/postgres?sslmode=require'

username = dbutils.secrets.get(scope = secret_scope_name, key = "jdbc_user")
password = dbutils.secrets.get(scope = secret_scope_name, key = "jdbc_pwd")

# COMMAND ----------

# DBTITLE 1,Get Data
df = spark.read.table(f'{catalog}.{schema}.{src_table}')

# COMMAND ----------

# DBTITLE 1,Write using JDBC
(
    df.write.format("jdbc")
    .option("url", jdbc_url)
    .option("dbtable", "lakefed_src")
    .option("user", username)
    .option("password", password)
    .mode("overwrite")
    .save()
)

# COMMAND ----------
import subprocess

# Create a view in PostgreSQL that returns the size of a table
env = {'PGPASSWORD': password}
qry = f"""\
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
"""
cmd = f'--command={qry}'
subprocess.run(['psql', f'--host={host}', '--port=5432', '--dbname=postgres', f'--username={username}', '--set=sslmode=require', cmd], env=env, capture_output=True)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Connect to External Database using Lakehouse Federation
# MAGIC Follow the [documentation](https://learn.microsoft.com/en-us/azure/databricks/query-federation/) to create a connection and foreign catalog.