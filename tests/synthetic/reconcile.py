# Databricks notebook source
# MAGIC %md
# MAGIC # Reconcile the synthetic source with bronze
# MAGIC
# MAGIC For the tables of a change-generator run (`run_label` in the manifest) — or every table when
# MAGIC `run_label` is blank — computes the same aggregates on the SQL Server source and the Delta sink
# MAGIC (row count, sum of the leading PK column, and a type-appropriate aggregate of the updated column)
# MAGIC and reports mismatches. Supply host/database/secret and the bronze catalog.schema as widgets.

# COMMAND ----------

import json, os, sys, threading
from concurrent.futures import ThreadPoolExecutor

ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
nb_dir = os.path.dirname(ctx.notebookPath().get())
if ("/Workspace" + nb_dir) not in sys.path:
    sys.path.append("/Workspace" + nb_dir)
import synth_catalog as sc
import synth_changes as ch
import synth_sql as ps

for name, default in [("host", ""), ("port", "1433"), ("trust_server_certificate", "false"),
                      ("database", ""), ("secret_scope", ""), ("secret_key", ""),
                      ("seed", "42"), ("bronze_schema", ""), ("manifest_table", ""), ("run_label", ""),
                      ("threads", "8")]:
    dbutils.widgets.text(name, default)
W = {k: dbutils.widgets.get(k) for k in ["host", "port", "trust_server_certificate", "database",
                                         "secret_scope", "secret_key", "seed",
                                         "bronze_schema", "manifest_table", "run_label", "threads"]}
assert W["host"] and W["database"] and W["secret_scope"] and W["secret_key"] and W["bronze_schema"], \
    "host/database/secret_*/bronze_schema required"
_creds = json.loads(dbutils.secrets.get(W["secret_scope"], W["secret_key"]))
_USER = next(_creds[k] for k in ("user", "username", "login") if k in _creds)
_PWD = next(_creds[k] for k in ("password", "pwd") if k in _creds)
URL = ps.jdbc_url(W["host"], W["database"], int(W["port"] or 1433),
                  W["trust_server_certificate"].strip().lower() == "true")
JVM = spark._sc._gateway.jvm
_local = threading.local()


def src_row(sql):
    c = getattr(_local, "c", None)
    if c is None or c.isClosed():
        _local.c = c = JVM.java.sql.DriverManager.getConnection(URL, _USER, _PWD)
    st = c.createStatement()
    rs = st.executeQuery(sql); rs.next()
    out = {k: rs.getString(k) for k in ("n", "pk_sum", "col_agg")}
    st.close()
    return out

# COMMAND ----------

catalog = {t.name: t for t in sc.build_catalog(int(W["seed"]))}
if W["run_label"]:
    names = [r["table_name"] for r in spark.table(W["manifest_table"])
             .where(f"run_label = '{W['run_label']}'").select("table_name").distinct().collect()]
else:
    names = sorted(catalog)


def check(name):
    t = catalog[name]
    src = src_row(ch.source_check_sql(t))
    bz = spark.sql(ch.bronze_check_sql(t, f"{W['bronze_schema']}.{name}")).first().asDict()
    diff = ch.compare_checks(src, bz)
    return {"table": name, "diff": diff, "source": src, "bronze": {k: str(v) for k, v in bz.items()}} if diff else None


with ThreadPoolExecutor(max_workers=int(W["threads"])) as ex:
    mismatched = [r for r in ex.map(check, names) if r]
summary = {"run_label": W["run_label"] or "(all)", "tables_checked": len(names),
           "matched": len(names) - len(mismatched), "mismatched": mismatched[:50]}
print(json.dumps(summary, default=str))
dbutils.notebook.exit(json.dumps(summary, default=str))
