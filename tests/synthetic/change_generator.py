# Databricks notebook source
# MAGIC %md
# MAGIC # Change generator for the synthetic test database
# MAGIC
# MAGIC Applies inserts, updates and deletes to a deterministic (seeded) fraction of the NON-EMPTY tables
# MAGIC of the synthetic catalog (`synth_catalog.py`), so the sweep's change detection and CT increments can
# MAGIC be measured under realistic churn. Writes a manifest (one row per touched table with the applied
# MAGIC counts) to `<manifest_table>` for `reconcile.py`. Supply host/database/secret as widget values.

# COMMAND ----------

import json, os, sys, time, threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
nb_dir = os.path.dirname(ctx.notebookPath().get())
if ("/Workspace" + nb_dir) not in sys.path:
    sys.path.append("/Workspace" + nb_dir)
import synth_catalog as sc
import synth_changes as ch

for name, default in [("host", ""), ("database", ""), ("secret_scope", ""), ("secret_key", ""),
                      ("scale_factor", "0.1"), ("seed", "42"), ("fraction", "0.05"), ("change_seed", "1"),
                      ("inserts", "10"), ("updates", "10"), ("deletes", "2"), ("threads", "8"),
                      ("run_label", ""), ("manifest_table", "")]:
    dbutils.widgets.text(name, default)
W = {k: dbutils.widgets.get(k) for k in ["host", "database", "secret_scope", "secret_key", "scale_factor",
                                         "seed", "fraction", "change_seed", "inserts", "updates", "deletes",
                                         "threads", "run_label", "manifest_table"]}
assert W["host"] and W["database"] and W["secret_scope"] and W["secret_key"] and W["manifest_table"], \
    "host/database/secret_*/manifest_table required"
RUN = W["run_label"] or datetime.now(timezone.utc).strftime("chg-%Y%m%dT%H%M%S")
_creds = json.loads(dbutils.secrets.get(W["secret_scope"], W["secret_key"]))
_USER = next(_creds[k] for k in ("user", "username", "login") if k in _creds)
_PWD = next(_creds[k] for k in ("password", "pwd") if k in _creds)
URL = (f"jdbc:sqlserver://{W['host']}:1433;databaseName={W['database']};encrypt=true;"
       "trustServerCertificate=false;hostNameInCertificate=*.database.windows.net;loginTimeout=60")
JVM = spark._sc._gateway.jvm
_local = threading.local()


def conn():
    c = getattr(_local, "c", None)
    if c is None or c.isClosed():
        for i in range(12):
            try:
                c = JVM.java.sql.DriverManager.getConnection(URL, _USER, _PWD)
                c.setAutoCommit(True)
                break
            except Exception as e:                    # serverless resume / transient
                if i == 11:
                    raise
                print(f"connect retry {i + 1}: {str(e)[:120]}")
                time.sleep(15)
        _local.c = c
    return c

# COMMAND ----------

SF = float(W["scale_factor"])
catalog = sc.build_catalog(int(W["seed"]))
picked = ch.pick_tables(catalog, SF, float(W["fraction"]), int(W["change_seed"]))
st0 = conn().createStatement()
rs = st0.executeQuery("SELECT compatibility_level AS c FROM sys.databases WHERE name = DB_NAME()")
rs.next(); use_gs = int(rs.getString(1)) >= 160; st0.close()
print(f"run {RUN}: {len(picked)} of {len(catalog)} tables (fraction={W['fraction']})")


def apply(t):
    counts = {"insert": 0, "update": 0, "delete": 0}
    for kind, sql in ch.change_statements(t, int(W["inserts"]), int(W["updates"]), int(W["deletes"]), use_gs):
        st = conn().createStatement()
        counts[kind] = int(st.executeUpdate(sql))
        st.close()
    return {"run_label": RUN, "table_name": t.name, "inserted": counts["insert"], "updated": counts["update"],
            "deleted": counts["delete"], "applied_at": datetime.now(timezone.utc)}


t0 = time.time()
with ThreadPoolExecutor(max_workers=int(W["threads"])) as ex:
    rows = list(ex.map(apply, picked))
elapsed = round(time.time() - t0, 1)
spark.createDataFrame(rows).write.mode("append").saveAsTable(W["manifest_table"])
summary = {"run_label": RUN, "tables": len(rows), "inserted": sum(r["inserted"] for r in rows),
           "updated": sum(r["updated"] for r in rows), "deleted": sum(r["deleted"] for r in rows),
           "elapsed_s": elapsed}
print(json.dumps(summary))
dbutils.notebook.exit(json.dumps(summary))
