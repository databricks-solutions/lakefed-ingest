"""lakefed_ingest operations app (optional): FastAPI backend + React frontend (built into ./static).

JSON API under /api, used by the React app; everything else serves the single-page app. Data access
runs as the app's service principal: Lakebase through lakebase.py, Jobs API through jobs.py. All SQL
is in queries.py (parameterized, unit-tested).

Run locally: `python main.py` (DATABRICKS_CONFIG_PROFILE, LAKEBASE_INSTANCE, LAKEBASE_DATABASE,
INGEST_JOB_ID, SETUP_JOB_ID, DISCOVER_JOB_ID), with the frontend built (`npm run build`) or served by
`npm run dev` (proxies /api here).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import jobs
import lakebase as db
import queries as q

app = FastAPI(title="lakefed_ingest operations", docs_url="/api/docs", openapi_url="/api/openapi.json")
STATIC = Path(__file__).parent / "static"


@app.exception_handler(Exception)
async def _errors(request: Request, exc: Exception):
    msg = str(exc)
    hint = None
    if "permission denied" in msg.lower():
        hint = "The app's service principal may lack privileges: run the lakefed_ingest_app_grants job."
    return JSONResponse(status_code=500, content={"detail": msg[:2000], "hint": hint})


def _user(request: Request) -> str:
    return request.headers.get("X-Forwarded-Email") or request.headers.get("X-Forwarded-User") or ""


def _lock_collections(cur, collections):
    """Take each collection's run lock for this transaction, or refuse: changing tables while their
    collection is running could race the run's own checkpoint writes."""
    for c in sorted({c for c in collections if c}):
        cur.execute(q.collection_lock_sql(), (q.collection_lock_key(c),))
        if not cur.fetchone()[0]:
            raise HTTPException(409, f"collection '{c}' is running; try again after the run finishes")


def _log(cur, user, action, control_id, collection, field=None, old=None, new=None, reseed=None, batch=None):
    as_text = lambda v: None if v is None else str(v)
    cur.execute(q.log_change_sql(), (user or None, action, control_id, collection, field, as_text(old),
                                     as_text(new), reseed, batch))


def _locked_rows(cur, ids):
    cur.execute(q.control_rows_for_update_sql(), (list(ids),))
    cols = [d.name for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    missing = set(ids) - {r["id"] for r in rows}
    if missing:
        raise HTTPException(404, f"control rows not found: {sorted(missing)}")
    return rows


# ---- context / overview -----------------------------------------------------------------------------

@app.get("/api/config")
def config(request: Request):
    return {"lakebase_instance": db.INSTANCE, "lakebase_database": db.DATABASE, "user": _user(request),
            "jobs": {"ingest": jobs.INGEST_JOB_ID, "setup": jobs.SETUP_JOB_ID, "discover": jobs.DISCOVER_JOB_ID},
            "defaults": {"partition_threshold_mb": q.DEFAULT_PARTITION_THRESHOLD_MB,
                         "partition_size_mb": q.DEFAULT_PARTITION_SIZE_MB}}


@app.get("/api/overview")
def overview():
    return {"collections": db.query(q.collections_overview_sql()),
            "running": db.query(q.running_runs_sql())}


@app.get("/api/errors")
def errors(hours: int = 24, limit: int = 200):
    return db.query(q.recent_errors_sql(), (int(hours), int(limit)))


@app.get("/api/collections")
def collections():
    return [r["task_collection"] for r in db.query(q.collections_sql())]


# ---- runs ---------------------------------------------------------------------------------------

@app.get("/api/runs")
def runs(collection: Optional[str] = None, limit: int = 200):
    sql, params = q.runs_sql(collection or None, limit=limit)
    return db.query(sql, params)


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: int):
    return db.query(q.run_events_sql(), (int(run_id),))


# ---- tables -------------------------------------------------------------------------------------

@app.get("/api/tables")
def tables(collection: Optional[str] = None, search: Optional[str] = None, status: str = "all",
           limit: int = 1000):
    if status not in q.TABLE_FILTERS:
        raise HTTPException(400, f"status must be one of {q.TABLE_FILTERS}")
    sql, params = q.tables_sql(collection or None, search or None, status, limit)
    return db.query(sql, params)


class Ids(BaseModel):
    ids: List[int] = Field(min_length=1)


class EnableRequest(Ids):
    enabled: bool


@app.post("/api/tables/enable")
def set_enabled(body: EnableRequest, request: Request):
    batch = uuid.uuid4().hex[:12]
    with db.transaction() as cur:
        rows = _locked_rows(cur, body.ids)
        _lock_collections(cur, [r["task_collection"] for r in rows])
        cur.execute(q.set_enabled_sql(), (body.enabled, body.ids))
        for r in rows:
            if bool(r["task_enabled"]) != body.enabled:
                _log(cur, _user(request), "enable" if body.enabled else "disable", r["id"], r["task_collection"],
                     "task_enabled", r["task_enabled"], body.enabled, batch=batch)
    return {"updated": len(rows)}


@app.post("/api/tables/reseed")
def reseed(body: Ids, request: Request):
    batch = uuid.uuid4().hex[:12]
    with db.transaction() as cur:
        rows = _locked_rows(cur, body.ids)
        _lock_collections(cur, [r["task_collection"] for r in rows])
        cur.execute(q.force_reseed_sql(), (body.ids,))
        n = cur.rowcount
        cur.execute(q.log_reseed_sql(), ("manual", body.ids))
        for r in rows:
            _log(cur, _user(request), "reseed", r["id"], r["task_collection"], reseed="manual", batch=batch)
    return {"updated": n}


@app.post("/api/tables")
def add_table(row: dict, request: Request):
    errs = q.validate_control_row(row)
    if errs:
        raise HTTPException(422, {"errors": errs})
    with db.transaction() as cur:
        cur.execute(q.insert_control_sql(), q.control_params(row))
        new_id = cur.fetchone()[0]
        _log(cur, _user(request), "add", new_id, row.get("task_collection"))
    return {"id": new_id}


@app.get("/api/tables/{control_id}")
def table_detail(control_id: int):
    rows = db.query(q.control_row_sql(), (int(control_id),))
    if not rows:
        raise HTTPException(404, f"control row {control_id} not found")
    cp = db.query(q.checkpoint_for_sql(), (int(control_id),))
    csql, cparams = q.changes_sql(int(control_id), 100)
    return {"control": rows[0], "checkpoint": cp[0] if cp else None, "changes": db.query(csql, cparams)}


class EditRequest(BaseModel):
    ids: List[int] = Field(min_length=1)
    changes: dict = Field(min_length=1)
    dry_run: bool = True


@app.post("/api/tables/edit")
def edit_tables(body: EditRequest, request: Request):
    """Preview (dry_run, the default) or apply an edit to one or many control rows. Applying is all
    or nothing, in one transaction: it refuses while an affected collection is running, flips tables
    whose source, sink or key changed to seeding, checks sink ownership, and logs every change."""
    import psycopg
    bulk = len(body.ids) > 1
    batch = uuid.uuid4().hex[:12]
    try:
        with db.transaction() as cur:
            rows = _locked_rows(cur, body.ids)
            plans = q.plan_edit(rows, body.changes, bulk=bulk)
            summary = {"rows": len(plans), "changed": sum(1 for p in plans if p["changes"]),
                       "reseed": sum(1 for p in plans if p["reseed"]),
                       "errors": sum(1 for p in plans if p["errors"]), "applied": False}
            if body.dry_run or summary["errors"] or not summary["changed"]:
                cur.connection.rollback()
                return {"summary": summary, "plans": plans}
            new_coll = body.changes.get("task_collection")
            _lock_collections(cur, [r["task_collection"] for r in rows] + ([new_coll] if new_coll else []))
            by_id = {r["id"]: r for r in rows}
            user = _user(request)
            for p in plans:
                if not p["changes"]:
                    continue
                fields = list(p["changes"])
                cur.execute(q.update_control_sql(fields), [p["changes"][f][1] for f in fields] + [p["id"]])
                coll = p["changes"].get("task_collection", [None, by_id[p["id"]]["task_collection"]])[1]
                for f in fields:
                    _log(cur, user, "edit", p["id"], coll, f, p["changes"][f][0], p["changes"][f][1],
                         reseed=p["reseed"], batch=batch)
                if p["reseed"]:
                    cur.execute(q.flip_to_seeding_sql(), (f"reseed after edit: {p['reseed']}", [p["id"]]))
                    cur.execute(q.log_reseed_sql(), (f"edit:{p['reseed']}", [p["id"]]))
            sinks = sorted({".".join(str(r[k]).lower() for k in ("sink_catalog", "sink_schema", "sink_table"))
                            for r in (dict(by_id[p["id"]], **{f: v[1] for f, v in p["changes"].items()}) for p in plans)})
            cur.execute(q.mixed_sinks_for_sql(), (sinks,))
            mixed = [r[0] for r in cur.fetchall()]
            if mixed:
                raise HTTPException(409, f"sink {mixed[0]} would be claimed by a dedicated row and by other rows; "
                                         "a sink is either dedicated to one source table or shared by client_id")
            summary["applied"] = True
            return {"summary": summary, "plans": plans}
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "another control row already writes that sink (or that sink and client_id); "
                                 "nothing was changed")


@app.get("/api/changes")
def changes(control_id: Optional[int] = None, limit: int = 200):
    sql, params = q.changes_sql(control_id, limit)
    return db.query(sql, params)


# ---- jobs ---------------------------------------------------------------------------------------

def _require(job_id, name):
    if not job_id:
        raise HTTPException(503, f"{name} job id isn't configured for this app")
    return job_id


@app.get("/api/jobs/ingest")
def ingest_job():
    jid = _require(jobs.INGEST_JOB_ID, "ingestion")
    return {"job": jobs.job_summary(jid), "active": jobs.runs(jid, active_only=True, limit=50),
            "recent": jobs.runs(jid, active_only=False, limit=25)}


class RunRequest(BaseModel):
    task_collection: str = Field(min_length=1)
    parallelism: int = Field(default=32, ge=1, le=256)


@app.post("/api/jobs/ingest/run")
def run_ingest(body: RunRequest):
    rid = jobs.run_now(_require(jobs.INGEST_JOB_ID, "ingestion"),
                       {"task_collection": body.task_collection, "parallelism": str(body.parallelism)})
    return {"run_id": rid}


@app.post("/api/jobs/runs/{run_id}/cancel")
def cancel_run(run_id: int):
    jobs.cancel(run_id)
    return {"cancelled": run_id}


@app.post("/api/jobs/setup/run")
def run_setup():
    return {"run_id": jobs.run_now(_require(jobs.SETUP_JOB_ID, "setup"), {})}


# ---- bulk import --------------------------------------------------------------------------------

class DiscoverRequest(BaseModel):
    src_host: str = Field(min_length=1)
    src_port: Optional[int] = None
    databases: str = Field(min_length=1, description="comma- or newline-separated")
    secret_key_template: str = Field(default="{database}", min_length=1)


@app.post("/api/discoveries")
def start_discovery(body: DiscoverRequest, request: Request):
    names = q.parse_databases(body.databases)
    if not names:
        raise HTTPException(422, "at least one database is required")
    did = uuid.uuid4().hex[:12]
    spec = {"src_type": "sqlserver", "src_host": body.src_host.strip(), "src_port": body.src_port or None,
            "databases": names, "secret_key_template": body.secret_key_template.strip(),
            "requested_by": _user(request)}
    rid = jobs.run_now(_require(jobs.DISCOVER_JOB_ID, "discovery"), {"discovery_id": did, "spec": json.dumps(spec)})
    return {"discovery_id": did, "run_id": rid, "databases": len(names)}


@app.get("/api/discoveries")
def discoveries(limit: int = 50):
    return db.query(q.discoveries_sql(), (int(limit),))


@app.get("/api/discoveries/{discovery_id}/databases")
def discovery_databases(discovery_id: str):
    return db.query(q.discovery_databases_sql(), (discovery_id,))


class Shared(BaseModel):
    sink_catalog: str = "main"
    sink_schema: str = "bronze_{database}"
    sink_table: str = "{table}"
    client_id: str = ""
    freshness_sla_minutes: Optional[int] = Field(default=None, gt=0)
    partition_threshold_mb: float = q.DEFAULT_PARTITION_THRESHOLD_MB
    partition_size_mb: int = q.DEFAULT_PARTITION_SIZE_MB
    include_default: bool = True


class ReviewRequest(BaseModel):
    shared: Shared = Shared()


@app.post("/api/discoveries/{discovery_id}/review")
def review(discovery_id: str, body: ReviewRequest):
    """Review grid rows computed from the discovery and the shared fields."""
    discovered = db.query(q.discovered_tables_sql(), (discovery_id,))
    return [q.public_review_row(r) for r in q.review_rows(discovered, body.shared.model_dump())]


class RegisterRequest(BaseModel):
    task_collection: str = Field(min_length=1)
    shared: Shared = Shared()
    edits: List[dict] = []
    dry_run: bool = False


@app.post("/api/discoveries/{discovery_id}/register")
def register(discovery_id: str, body: RegisterRequest, request: Request):
    """Validate (dry_run) or register the included tables. Rows are rebuilt from the stored discovery
    and only the reviewer's editable fields are applied (queries.merge_review_edits)."""
    discovered = db.query(q.discovered_tables_sql(), (discovery_id,))
    rows = q.merge_review_edits(q.review_rows(discovered, body.shared.model_dump()), body.edits)
    chosen = [r for r in rows if r.get("include")]
    results, valid = [], []
    for r in chosen:
        row = q.control_row_from_review(r, body.task_collection)
        errs = q.validate_control_row(row)
        name = ".".join(q.review_key(r))
        if errs:
            results.append({"table": name, "status": "invalid", "errors": errs})
        else:
            valid.append((name, row))
    if not body.dry_run and valid:
        batch = uuid.uuid4().hex[:12]
        with db.transaction() as cur:
            for name, row in valid:
                cur.execute(q.insert_control_if_absent_sql(), q.control_params(row))
                res = cur.fetchone()
                if res:
                    _log(cur, _user(request), "register", res[0], body.task_collection, batch=batch)
                results.append({"table": name, "status": "registered" if res else "skipped",
                                "id": res[0] if res else None,
                                "errors": [] if res else ["sink already used by another control row"]})
    else:
        results += [{"table": name, "status": "valid", "errors": []} for name, _ in valid]
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"selected": len(chosen), "counts": counts, "results": results}


# ---- single-page app ----------------------------------------------------------------------------

if (STATIC / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    target = STATIC / path
    if path and target.is_file():
        return FileResponse(target)
    index = STATIC / "index.html"
    if not index.is_file():
        return JSONResponse(status_code=503, content={"detail": "frontend not built (npm run build)"})
    return FileResponse(index)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("DATABRICKS_APP_PORT", "8000")))
