# Databricks notebook source
# MAGIC %md
# MAGIC # Build a synthetic property-management test database (1,800 CT-enabled tables)
# MAGIC
# MAGIC Creates the tables from `synth_catalog.py`, loads deterministic data with set-based T-SQL
# MAGIC (generated inside SQL Server — nothing is pushed row by row), then enables Change Tracking at the
# MAGIC database and table level **after** the bulk load. Idempotent: tables that already exist with the
# MAGIC expected row count are skipped, so a failed run can simply be re-run.
# MAGIC
# MAGIC Runs on a cluster; talks to SQL Server through the JDBC driver bundled with Databricks Runtime
# MAGIC (py4j `DriverManager`), one connection per worker thread. Credentials come from a secret holding
# MAGIC JSON `{"user": ..., "password": ...}` — supply host/database/scope/key as widget values.

# COMMAND ----------

import json, os, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
nb_dir = os.path.dirname(ctx.notebookPath().get())
if ("/Workspace" + nb_dir) not in sys.path:
    sys.path.append("/Workspace" + nb_dir)
import synth_catalog as pc
import synth_sql as ps

for name, default in [("host", ""), ("database", ""), ("secret_scope", ""), ("secret_key", ""),
                      ("scale_factor", "0.1"), ("threads", "12"), ("seed", "42"),
                      ("batch_rows", "1000000"), ("ct_retention_days", "3")]:
    dbutils.widgets.text(name, default)
W = {k: dbutils.widgets.get(k) for k in ["host", "database", "secret_scope", "secret_key", "scale_factor",
                                         "threads", "seed", "batch_rows", "ct_retention_days"]}
assert W["host"] and W["database"] and W["secret_scope"] and W["secret_key"], "host/database/secret_* required"
SF, THREADS, SEED, BATCH = float(W["scale_factor"]), int(W["threads"]), int(W["seed"]), int(W["batch_rows"])

_creds = json.loads(dbutils.secrets.get(W["secret_scope"], W["secret_key"]))
_USER = next(_creds[k] for k in ("user", "username", "login") if k in _creds)
_PWD = next(_creds[k] for k in ("password", "pwd") if k in _creds)
URL = (f"jdbc:sqlserver://{W['host']}:1433;databaseName={W['database']};encrypt=true;"
       "trustServerCertificate=false;hostNameInCertificate=*.database.windows.net;loginTimeout=60")
JVM = spark._sc._gateway.jvm
TRANSIENT = ("40613", "40197", "40501", "10928", "10929", "49918", "49919", "49920", "4060",
             "connection", "Connection reset", "timed out")

# COMMAND ----------

def connect(retries=12):
    for i in range(retries):
        try:
            c = JVM.java.sql.DriverManager.getConnection(URL, _USER, _PWD)
            c.setAutoCommit(True)
            return c
        except Exception as e:                       # serverless resume / transient
            if i == retries - 1:
                raise
            print(f"connect retry {i + 1}: {str(e)[:120]}")
            time.sleep(15)


_local = threading.local()


def conn():
    c = getattr(_local, "c", None)
    if c is None or c.isClosed():
        _local.c = c = connect()
    return c


def execute(sql, retries=5):
    for i in range(retries):
        try:
            st = conn().createStatement()
            st.setQueryTimeout(0)
            st.execute(sql)
            st.close()
            return
        except Exception as e:
            msg = str(e)
            if "2627" in msg or "Violation of PRIMARY KEY" in msg:
                return                                   # batch already committed before a drop
            if i < retries - 1 and any(t in msg for t in TRANSIENT):
                _local.c = None
                time.sleep(10 * (i + 1))
                continue
            raise


def query(sql):
    st = conn().createStatement()
    rs = st.executeQuery(sql)
    md = rs.getMetaData()
    n = md.getColumnCount()
    rows = []
    while rs.next():
        rows.append({md.getColumnLabel(i): rs.getString(i) for i in range(1, n + 1)})
    st.close()
    return rows

# COMMAND ----------

t0 = time.time()
query("SELECT 1 AS warm")                               # wakes a paused serverless database
compat = int(query("SELECT compatibility_level AS c FROM sys.databases WHERE name = DB_NAME()")[0]["c"])
use_gs = compat >= 160
catalog = pc.build_catalog(SEED)
existing = {r["table_name"]: int(r["row_count"]) for r in query(ps.ROW_COUNTS_SQL)}
print(f"compat={compat} generate_series={use_gs} existing_tables={len(existing)} scale={SF}")

# COMMAND ----------

def build_table(t):
    expected = t.rows(SF)
    have = existing.get(t.name)
    if have == expected:
        return t.name, "skipped", expected
    if have is not None:                                 # partial / mismatched: rebuild cleanly
        execute(f"DROP TABLE {ps.qname(t)}")
    execute(ps.create_table_sql(t))
    for start, end in ps.batches(t, SF, BATCH):
        execute(ps.insert_batch_sql(t, start, end, use_gs))
    return t.name, "built", expected


# Largest first, so the long tables start immediately and the small ones fill in around them.
order = sorted(catalog, key=lambda t: -t.est_mb(SF))
results, failures = [], []
with ThreadPoolExecutor(max_workers=THREADS) as ex:
    futs = {ex.submit(build_table, t): t for t in order}
    for i, f in enumerate(as_completed(futs), 1):
        try:
            results.append(f.result())
        except Exception as e:
            failures.append({"table": futs[f].name, "error": str(e)[:300]})
        if i % 100 == 0:
            print(f"{i}/{len(order)} tables done, {len(failures)} failed, {time.time() - t0:.0f}s")
t_load = time.time() - t0

# COMMAND ----------

# Change Tracking AFTER the bulk load (enabling it first would track every generated row).
ct_db = int(query("SELECT COUNT(*) AS n FROM sys.change_tracking_databases WHERE database_id = DB_ID()")[0]["n"])
if not ct_db:
    execute(ps.enable_ct_database_sql(W["database"], int(W["ct_retention_days"])))
ct_have = {r["table_name"] for r in query(ps.CT_TABLES_SQL)}
todo = [t for t in catalog if t.name not in ct_have and t.name not in {x["table"] for x in failures}]
with ThreadPoolExecutor(max_workers=THREADS) as ex:
    for f in as_completed([ex.submit(execute, ps.enable_ct_table_sql(t)) for t in todo]):
        f.result()

# COMMAND ----------

counts = {r["table_name"]: int(r["row_count"]) for r in query(ps.ROW_COUNTS_SQL)}
ct_tables = {r["table_name"] for r in query(ps.CT_TABLES_SQL)}
mismatch = [t.name for t in catalog if counts.get(t.name) != t.rows(SF)]
by_arch = {}
for t in catalog:
    a = by_arch.setdefault(t.archetype, {"tables": 0, "rows": 0})
    a["tables"] += 1
    a["rows"] += counts.get(t.name, 0)
summary = {
    "scale_factor": SF, "seed": SEED, "compat_level": compat,
    "tables_in_catalog": len(catalog), "tables_present": sum(1 for t in catalog if t.name in counts),
    "built": sum(1 for r in results if r[1] == "built"), "skipped": sum(1 for r in results if r[1] == "skipped"),
    "failed": failures[:20], "row_count_mismatches": mismatch[:20], "mismatch_count": len(mismatch),
    "ct_tables": len(ct_tables & {t.name for t in catalog}), "rows_by_archetype": by_arch,
    "total_rows": sum(counts.get(t.name, 0) for t in catalog),
    "db_size_mb": float(query(ps.DB_SIZE_MB_SQL)[0]["size_mb"]),
    "load_seconds": round(t_load), "total_seconds": round(time.time() - t0),
}
print(json.dumps(summary, indent=1))
dbutils.notebook.exit(json.dumps(summary))
