# IBM Db2

## IBM Db2 LUW (including BM Db2 on Cloud)

Lakehouse Federation does not have a native Db2 connector, so Db2 LUW is supported via a JDBC connection with `use_remote_query = true`. This means **no foreign catalog is required** — only a Unity Catalog connection (`src_connection`). All operations (schema inference, data copy, watermark queries, partition bounds) are pushed to Db2 via `remote_query()`. Leave `src_catalog` NULL in the control table for Db2 tasks.

The following steps are required before running ingestion jobs.

### Step 1 — Enable required Databricks preview features

The following previews must be enabled on your workspace before using Db2 via JDBC:

- **remote_query table-valued function** — allows pushdown of native SQL queries to the remote source
- **Join Pushdown for Federated Queries** — pushes join operations down to the remote database
- **Custom JDBC on UC Compute** — enables JDBC-based connections on Unity Catalog compute

Contact your workspace admin to enable these features.

### Step 2 — Choose and upload a JDBC driver JAR to a UC Volume

Two drivers are available depending on your Db2 environment:

| Driver | JAR | When to use |
|--------|-----|-------------|
| JT400 | `jt400.jar` | Connecting strictly to IBM i (AS/400). Free, no license required, optimized for that OS. |
| JCC | `db2jcc4.jar` | Connecting to multiple Db2 types (LUW, z/OS, and i) from a single driver. Requires Db2 Connect licenses. |

Upload the chosen JAR to a Unity Catalog Volume, for example:

```text
/Volumes/<catalog>/<schema>/jdbc_jars/
```

### Step 3 — Create a JDBC Connection

Create a Unity Catalog connection pointing at your Db2 instance. Example for Db2 on IBM Cloud:

```sql
CREATE CONNECTION db2_cloud_connection
TYPE JDBC
ENVIRONMENT (
  java_dependencies '["/Volumes/<catalog>/<schema>/jdbc_jars/db2jcc4.jar"]'
)
OPTIONS (
  url 'jdbc:db2://<host>:<port>/<database>:sslConnection=true;',
  user '<username>',
  password '<password>',
  externalOptionsAllowList 'dbtable,query,partitionColumn,lowerBound,upperBound,numPartitions'
);
```

Replace `java_dependencies` with the path to whichever JAR you uploaded. See the [Databricks JDBC connection docs](https://docs.databricks.com/aws/en/connect/jdbc-connection) for all supported options.

### Step 4 — Collect table statistics

For partitioned ingestion, the framework uses `SYSCAT.TABLES.FPAGES` to estimate table size and calculate the number of partitions. `FPAGES` is only populated after `RUNSTATS` has been run on each table. Run this for each table you plan to ingest:

```sql
CALL SYSPROC.ADMIN_CMD('RUNSTATS ON TABLE <schema>.<table> WITH DISTRIBUTION AND INDEXES ALL');
```

If `RUNSTATS` has not been run, `FPAGES` will be `-1` and the framework will default to 2 partitions regardless of table size.

## IBM Db2 z/OS and IBM Db2 for i (AS/400)

Not yet supported.
