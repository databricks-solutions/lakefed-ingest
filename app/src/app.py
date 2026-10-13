"""lakefed_ingest operations app (optional): observe runs and tables, manage control rows, run jobs.

Pages: Overview, Runs, Tables, Jobs, Add table, Bulk import (discover source tables, review, register). Reads Lakebase directly (lakebase.py) and the Jobs
API as the app's service principal (jobs.py). Every write asks for confirmation.
"""

from __future__ import annotations

import json
import uuid

import streamlit as st

import jobs
import lakebase as db
import queries as q

st.set_page_config(page_title="lakefed_ingest", layout="wide")


@st.cache_data(ttl=30, show_spinner=False)
def read(sql: str, params: tuple = ()):
    return db.query(sql, params)


@st.cache_data(ttl=30, show_spinner=False)
def read_job(job_id):
    return jobs.job_summary(job_id)


@st.cache_data(ttl=15, show_spinner=False)
def read_runs(job_id, active_only: bool, limit: int):
    return jobs.runs(job_id, active_only=active_only, limit=limit)


def refresh():
    st.cache_data.clear()


def show_error(e: Exception):
    msg = str(e)
    st.error(msg[:1000])
    if "permission denied" in msg.lower():
        st.info("The app's service principal may not have table privileges yet. Run the "
                "`lakefed_ingest_app_grants` job (see app/README.md).")


def collections() -> list:
    try:
        return read(q.collections_sql())["task_collection"].tolist()
    except Exception:
        return []


# ---- sidebar ------------------------------------------------------------------------------------

st.sidebar.title("lakefed_ingest")
page = st.sidebar.radio("Page", ["Overview", "Runs", "Tables", "Jobs", "Add table", "Bulk import"],
                        label_visibility="collapsed")
stale_minutes = st.sidebar.number_input("Stale after (minutes)", min_value=5, value=60, step=5,
                                        help="A table counts as stale when its last successful ingest is older than this.")
if st.sidebar.button("Refresh", width="stretch"):
    refresh()
st.sidebar.caption(f"Lakebase: `{db.INSTANCE}` / `{db.DATABASE}`")


# ---- Overview -----------------------------------------------------------------------------------

if page == "Overview":
    st.header("Overview")
    try:
        ov = read(q.collections_overview_sql(), (int(stale_minutes),))
        running = read(q.running_runs_sql())
        c = st.columns(5)
        c[0].metric("Collections", len(ov))
        c[1].metric("Enabled tables", int(ov["enabled_tables"].sum()) if len(ov) else 0)
        c[2].metric("Failing tables", int(ov["failing_tables"].sum()) if len(ov) else 0)
        c[3].metric(f"Stale (> {stale_minutes} min)", int(ov["stale_tables"].sum()) if len(ov) else 0)
        c[4].metric("Runs in progress", len(running))
        st.subheader("Collections")
        st.dataframe(ov, hide_index=True, width="stretch", column_config={
            "last_success_at": st.column_config.DatetimeColumn("last success"),
            "last_run_started": st.column_config.DatetimeColumn("last run started"),
            "last_run_finished": st.column_config.DatetimeColumn("last run finished"),
        })
        if len(running):
            st.subheader("Runs in progress")
            st.dataframe(running, hide_index=True, width="stretch")
        st.subheader("Errors in the last 24 hours")
        errs = read(q.recent_errors_sql(), (24, 200))
        if len(errs):
            st.dataframe(errs, hide_index=True, width="stretch")
        else:
            st.success("No failed tables in the last 24 hours.")
    except Exception as e:
        show_error(e)


# ---- Runs ---------------------------------------------------------------------------------------

elif page == "Runs":
    st.header("Runs")
    coll = st.selectbox("Collection", ["(all)"] + collections())
    try:
        sql, params = q.runs_sql(None if coll == "(all)" else coll, limit=200)
        runs = read(sql, params)
        st.caption("Select a run to see its tables.")
        sel = st.dataframe(runs, hide_index=True, width="stretch", on_select="rerun",
                           selection_mode="single-row", key="runs")
        rows = sel.selection.rows if sel and sel.selection else []
        if rows:
            run_id = int(runs.iloc[rows[0]]["run_id"])
            events = read(q.run_events_sql(), (run_id,))
            failed = events[events["status"] == "failed"]
            st.subheader(f"Run {run_id}: {len(events)} table events, {len(failed)} failed")
            st.caption("Tables found unchanged by change detection are counted in the run's `skipped`, "
                       "without an event.")
            st.dataframe(events, hide_index=True, width="stretch")
    except Exception as e:
        show_error(e)


# ---- Tables -------------------------------------------------------------------------------------

elif page == "Tables":
    st.header("Tables")
    f = st.columns([2, 3, 2, 1])
    coll = f[0].selectbox("Collection", ["(all)"] + collections())
    search = f[1].text_input("Search", placeholder="database, table, sink or client_id")
    status = f[2].selectbox("Show", q.TABLE_FILTERS)
    limit = f[3].number_input("Max rows", min_value=50, max_value=20000, value=1000, step=50)
    try:
        sql, params = q.tables_sql(None if coll == "(all)" else coll, search, status, int(stale_minutes), int(limit))
        tables = read(sql, params)
        st.caption(f"{len(tables)} rows. Select rows to act on them.")
        sel = st.dataframe(tables, hide_index=True, width="stretch", on_select="rerun",
                           selection_mode="multi-row", key="tables", column_config={
                               "last_success_at": st.column_config.DatetimeColumn("last success")})
        rows = sel.selection.rows if sel and sel.selection else []
        ids = [int(tables.iloc[i]["id"]) for i in rows]
        if ids:
            st.subheader(f"{len(ids)} selected")
            confirm = st.checkbox(f"I want to change these {len(ids)} tables")
            a = st.columns(3)
            if a[0].button("Enable", disabled=not confirm):
                n = db.execute(q.set_enabled_sql(), (True, ids)); refresh(); st.success(f"Enabled {n}.")
            if a[1].button("Disable", disabled=not confirm):
                n = db.execute(q.set_enabled_sql(), (False, ids)); refresh(); st.success(f"Disabled {n}.")
            if a[2].button("Force reseed", disabled=not confirm,
                           help="Sets the checkpoint back to seeding; the next run copies these tables again."):
                n = db.execute(q.force_reseed_sql(), (ids,))
                db.execute(q.log_reseed_sql(), ("manual", ids))
                refresh(); st.success(f"{n} tables will reseed on their next run.")
    except Exception as e:
        show_error(e)


# ---- Jobs ---------------------------------------------------------------------------------------

elif page == "Jobs":
    st.header("Jobs")
    if not jobs.INGEST_JOB_ID:
        st.warning("INGEST_JOB_ID isn't configured for this app.")
    else:
        try:
            j = read_job(jobs.INGEST_JOB_ID)
            st.subheader(j["name"])
            c = st.columns(3)
            c[0].metric("Schedule", j["schedule"] or "none")
            c[1].metric("Schedule status", j["schedule_status"] or "-")
            c[2].metric("Scheduled collection", j["default_task_collection"] or "-")
            st.caption("A schedule only runs the job's default task_collection. "
                       f"[Open the job]({j['url']})")

            with st.form("run_now"):
                st.markdown("**Start a run**")
                r = st.columns(3)
                coll = r[0].selectbox("Collection", collections())
                par = r[1].number_input("Parallelism", min_value=1, max_value=256,
                                        value=int(j["parameters"].get("parallelism") or 32))
                ok = r[2].checkbox("Confirm")
                if st.form_submit_button("Run now") and ok and coll:
                    rid = jobs.run_now(jobs.INGEST_JOB_ID, {"task_collection": coll, "parallelism": str(par)})
                    refresh(); st.success(f"Started run {rid} for {coll}.")

            active = read_runs(jobs.INGEST_JOB_ID, True, 50)
            st.subheader(f"Active runs ({len(active)})")
            if len(active):
                st.dataframe(active, hide_index=True, width="stretch",
                             column_config={"url": st.column_config.LinkColumn("link", display_text="open")})
                cr = st.columns([2, 1])
                to_cancel = cr[0].selectbox("Cancel run", active["run_id"].tolist())
                if cr[1].button("Cancel run") and to_cancel:
                    jobs.cancel(to_cancel); refresh(); st.success(f"Cancel requested for {to_cancel}.")
            st.subheader("Recent runs")
            st.dataframe(read_runs(jobs.INGEST_JOB_ID, False, 25), hide_index=True, width="stretch",
                         column_config={"url": st.column_config.LinkColumn("link", display_text="open")})
        except Exception as e:
            show_error(e)
    if jobs.SETUP_JOB_ID:
        st.divider()
        st.subheader("Schema setup")
        st.caption("Applies / migrates the Lakebase schema. Safe to re-run; needed after upgrades.")
        if st.checkbox("Confirm schema setup") and st.button("Run setup job"):
            try:
                rid = jobs.run_now(jobs.SETUP_JOB_ID, {})
                st.success(f"Started setup run {rid}.")
            except Exception as e:
                show_error(e)


# ---- Add table ----------------------------------------------------------------------------------

elif page == "Add table":
    st.header("Add a table")
    st.caption("Adds one control row. The next run of its collection seeds it. Bulk loads: "
               "notebooks/load_control_example.ipynb.")
    with st.form("add"):
        a = st.columns(3)
        row = {
            "task_collection": a[0].text_input("task_collection*"),
            "src_type": a[1].selectbox("src_type", q.SRC_TYPES),
            "load_type": a[2].selectbox("load_type", q.LOAD_TYPES),
        }
        b = st.columns(4)
        row["src_host"] = b[0].text_input("src_host*")
        row["src_port"] = b[1].number_input("src_port (0 = default)", min_value=0, value=0) or None
        row["src_database"] = b[2].text_input("src_database*")
        row["secret_key"] = b[3].text_input("secret_key*", help="Secret holding JSON {user, password}")
        c = st.columns(3)
        row["src_schema"] = c[0].text_input("src_schema*", value="dbo")
        row["src_table"] = c[1].text_input("src_table*")
        row["primary_key"] = c[2].text_input("primary_key", help="Comma-separated; required for ct and cursor")
        row["select_list"] = st.text_input("select_list*", help="Explicit, comma-separated columns")
        d = st.columns(4)
        row["sink_catalog"] = d[0].text_input("sink_catalog*")
        row["sink_schema"] = d[1].text_input("sink_schema*")
        row["sink_table"] = d[2].text_input("sink_table*")
        row["client_id"] = d[3].text_input("client_id", help="Set to share one sink across databases")
        e = st.columns(4)
        row["cursor_column"] = e[0].text_input("cursor_column", help="cursor only (rowversion recommended)")
        row["load_partitioned"] = e[1].checkbox("load_partitioned")
        row["partition_col"] = e[2].text_input("partition_col")
        row["partition_size_mb"] = e[3].number_input("partition_size_mb", min_value=0, value=0) or None
        if st.form_submit_button("Add table"):
            errs = q.validate_control_row(row)
            if errs:
                for m in errs:
                    st.error(m)
            else:
                try:
                    new_id = db.execute_returning(q.insert_control_sql(), q.control_params(row))[0]
                    refresh(); st.success(f"Added control row {new_id}.")
                except Exception as ex:
                    show_error(ex)


# ---- Bulk import --------------------------------------------------------------------------------

elif page == "Bulk import":
    st.header("Bulk import")
    st.caption("Discover a server's tables, review the recommended settings, and register the ones you want. "
               "Discovery runs as a job on the ingestion network path and reads catalog metadata only.")
    if not jobs.DISCOVER_JOB_ID:
        st.warning("DISCOVER_JOB_ID isn't configured for this app.")
        st.stop()

    with st.expander("1. Discover tables", expanded=not st.session_state.get("discovery_id")):
        with st.form("discover"):
            a = st.columns(3)
            host = a[0].text_input("src_host*")
            port = a[1].number_input("src_port (0 = default)", min_value=0, value=0)
            a[2].selectbox("src_type", ["sqlserver"], help="Discovery supports SQL Server for now.")
            dbs = st.text_area("Databases* (comma- or newline-separated)")
            b = st.columns(2)
            key = b[0].text_input("Secret key template*", value="{database}",
                                  help="Secret holding JSON {user, password}; {database} is replaced per database.")
            ok = b[1].checkbox("Confirm: start a discovery job")
            if st.form_submit_button("Discover") and ok:
                names = q.parse_databases(dbs)
                if not host or not names or not key:
                    st.error("src_host, at least one database and the secret key template are required.")
                else:
                    did = uuid.uuid4().hex[:12]
                    spec = {"src_type": "sqlserver", "src_host": host.strip(), "src_port": int(port) or None,
                            "databases": names, "secret_key_template": key.strip(),
                            "requested_by": st.context.headers.get("X-Forwarded-Email", "")}
                    rid = jobs.run_now(jobs.DISCOVER_JOB_ID, {"discovery_id": did, "spec": json.dumps(spec)})
                    st.session_state["discovery_id"] = did
                    refresh()
                    st.success(f"Discovery {did} started for {len(names)} databases (job run {rid}). "
                               "Results appear below in a few minutes.")

    st.subheader("2. Review")
    try:
        recent = read(q.discoveries_sql(), (50,))
    except Exception as e:
        show_error(e); st.stop()
    if not len(recent):
        st.info("No discoveries yet."); st.stop()
    ids = recent["discovery_id"].tolist()
    default = ids.index(st.session_state["discovery_id"]) if st.session_state.get("discovery_id") in ids else 0
    did = st.selectbox("Discovery", ids, index=default, format_func=lambda i: (
        lambda r: f"{i}: {r['src_host']}, {r['databases']} databases, {r['tables']} tables, started {r['started_at']:%Y-%m-%d %H:%M}"
    )(recent[recent["discovery_id"] == i].iloc[0]))
    dbstat = read(q.discovery_databases_sql(), (did,))
    if (dbstat["status"] == "running").any():
        st.info(f"{int((dbstat['status'] == 'running').sum())} databases still running. Press Refresh in a moment.")
    if (dbstat["status"] == "failed").any():
        st.error("Some databases failed:")
        st.dataframe(dbstat[dbstat["status"] == "failed"][["src_database", "error"]], hide_index=True, width="stretch")
    discovered = read(q.discovered_tables_sql(), (did,))
    if not len(discovered):
        st.stop()

    st.markdown("**Shared fields.** Templates can use `{database}`, `{schema}` and `{table}`. "
                "Changing these resets edits in the grid below, so set them first.")
    f = st.columns(4)
    coll = f[0].text_input("task_collection*")
    sink_catalog = f[1].text_input("sink_catalog*", value="main")
    sink_schema = f[2].text_input("sink_schema*", value="bronze_{database}")
    sink_table = f[3].text_input("sink_table*", value="{table}")
    g = st.columns(4)
    client_id = g[0].text_input("client_id (consolidate)", value="",
                                help="e.g. {database} with a shared sink_schema to consolidate databases into one table each")
    threshold = g[1].number_input("Partition tables larger than (MB)", min_value=1,
                                  value=q.DEFAULT_PARTITION_THRESHOLD_MB, step=256)
    psize = g[2].number_input("partition_size_mb", min_value=1, value=q.DEFAULT_PARTITION_SIZE_MB, step=64)
    include_default = g[3].checkbox("Include new tables by default", value=True)
    shared = dict(sink_catalog=sink_catalog, sink_schema=sink_schema, sink_table=sink_table, client_id=client_id,
                  partition_threshold_mb=threshold, partition_size_mb=psize, include_default=include_default)

    h = st.columns([3, 1, 1])
    search = h[0].text_input("Filter tables", placeholder="database, schema or table")
    hide_registered = h[1].checkbox("Hide registered", value=True)
    min_mb = h[2].number_input("Min size (MB)", min_value=0.0, value=0.0)
    rows = q.review_rows(discovered.to_dict("records"), shared)
    if search:
        rows = [r for r in rows if search.lower() in f"{r['src_database']}.{r['src_schema']}.{r['src_table']}".lower()]
    if hide_registered:
        rows = [r for r in rows if not r["registered_id"]]
    rows = [r for r in rows if r["size_mb"] >= min_mb]
    import pandas as pd
    grid = pd.DataFrame(rows)
    if not len(grid):
        st.info("No tables match the filters."); st.stop()
    shown = [c for c in grid.columns if not c.startswith("_")]
    edit_key = "review_" + did + "_" + str(abs(hash(json.dumps(shared, sort_keys=True, default=str) + search + str(hide_registered) + str(min_mb))))
    edited = st.data_editor(
        grid[shown], key=edit_key, hide_index=True, width="stretch", num_rows="fixed",
        disabled=["src_database", "src_schema", "src_table", "rows", "size_mb", "ct_enabled", "registered_id", "notes"],
        column_config={
            "include": st.column_config.CheckboxColumn("include"),
            "load_type": st.column_config.SelectboxColumn("load_type", options=list(q.LOAD_TYPES)),
            "load_partitioned": st.column_config.CheckboxColumn("partitioned"),
            "size_mb": st.column_config.NumberColumn("size_mb", format="%.1f"),
        })
    chosen = [dict(grid.iloc[i].to_dict(), **edited.iloc[i].to_dict()) for i in range(len(edited)) if edited.iloc[i]["include"]]
    st.caption(f"{len(chosen)} of {len(edited)} shown tables selected "
               f"({sum(1 for r in chosen if r['load_partitioned'])} partitioned).")

    st.subheader("3. Register")
    to_insert, problems = [], []
    for r in chosen:
        row = q.control_row_from_review(r, coll)
        errs = q.validate_control_row(row)
        (problems.append({"table": f"{r['src_database']}.{r['src_schema']}.{r['src_table']}", "problems": "; ".join(errs)})
         if errs else to_insert.append(row))
    if problems:
        st.warning(f"{len(problems)} selected tables need fixes before they can be registered:")
        st.dataframe(pd.DataFrame(problems), hide_index=True, width="stretch")
    confirm = st.checkbox(f"Register {len(to_insert)} tables in collection '{coll or '?'}'")
    if st.button("Register tables", disabled=not (confirm and to_insert and coll)):
        added = skipped = 0
        try:
            for row in to_insert:
                res = db.execute_returning(q.insert_control_if_absent_sql(), q.control_params(row))
                added, skipped = (added + 1, skipped) if res else (added, skipped + 1)
            refresh()
            st.success(f"Registered {added} tables in '{coll}'." + (
                f" Skipped {skipped} whose sink is already used by another control row." if skipped else "")
                + " Their first run seeds them.")
        except Exception as e:
            show_error(e)
            if added:
                st.info(f"{added} tables were registered before the error.")
