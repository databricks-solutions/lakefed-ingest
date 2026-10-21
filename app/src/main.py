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
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import jobs
import lakebase as db
import queries as q
import schedules as sch

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
    active, recent = jobs.runs(jid, active_only=True, limit=50), jobs.runs(jid, active_only=False, limit=25)
    # A run a wrapper job started (trigger RUN_JOB_TASK) came from a schedule of its collection.
    scheduled = _scheduled_collections()
    for r in active + recent:
        r["schedule"] = ", ".join(scheduled.get(r["task_collection"], [])) if r["trigger"] == "RUN_JOB_TASK" else None
    return {"job": jobs.job_summary(jid), "active": active, "recent": recent}


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


# ---- schedules ----------------------------------------------------------------------------------

class ScheduleBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    cadence_minutes: int
    timezone: str = "UTC"
    parameters: dict = {}
    job_variant: str = "default"
    stagger: bool = True
    paused: bool = False
    notify_emails: Optional[str] = None
    members: List[str] = Field(min_length=1)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return (v or "").strip() or "UTC"      # stored as given: a blank one would break every page


def _ingest_job_id(variant: str) -> Optional[int]:
    jid = jobs.ingest_variants().get(variant or "default")
    return int(jid) if jid else None


def _variant_job_id(variant: str) -> int:
    jid = _ingest_job_id(variant)
    if jid is None:
        raise HTTPException(503, f"no ingestion job configured for variant '{variant}'")
    return jid


def _owner() -> str:
    """The wrapper jobs' owner tag: this app's Lakebase database (other deployments' jobs keep out)."""
    return f"{db.INSTANCE}/{db.DATABASE}"


def _load_schedule(sid: int, cur=None):
    get = (lambda sql, p: db.fetch(cur, sql, p)) if cur is not None else db.query
    rows = get(q.schedule_sql(), (int(sid),))
    if not rows:
        raise HTTPException(404, f"schedule {sid} not found")
    members = get(q.schedule_members_sql(), ([int(sid)],))
    return rows[0], members


def _validate_schedule(body: ScheduleBody):
    known = [r["task_collection"] for r in db.query(q.collections_sql())]
    errs = sch.validate(body.model_dump(), known, jobs.ingest_variants())
    if errs:
        raise HTTPException(422, {"errors": errs})


def _sync(sid: int, user: str) -> List[dict]:
    """Bring the schedule's wrapper jobs in line with Lakebase and record the results. Runs under the
    schedule's sync lock, with the members read after taking it, so concurrent syncs don't both create a
    job. A failed sync never raises once the schedule's change is committed: errors are per member."""
    batch = uuid.uuid4().hex[:12]
    with db.transaction() as cur:
        cur.execute(q.schedule_lock_sql(), (q.SCHEDULE_LOCK_CLASS, q.schedule_lock_key(sid)))
        s, members = _load_schedule(sid, cur)
        active = [m for m in members if m["removed_at"] is None]
        removed = [m for m in members if m["delete_pending"]]
        results = sch.sync(s, active, removed, jobs.schedule_client(), _ingest_job_id(s["job_variant"]),
                           jobs.SCHEDULE_JOB_VIEW_GROUP, _owner())
        for r in results:
            if r.member_id is not None:
                if r.action == "error":
                    cur.execute(q.member_sync_failed_sql(), (r.detail, r.member_id))
                else:
                    cur.execute(q.member_synced_sql(), (r.job_id, r.member_id))
            if r.action in ("created", "adopted", "updated", "deleted"):
                _log(cur, user, "schedule_sync", None, r.task_collection or None, "job_id", None,
                     f"{r.action} job {r.job_id}" + (f" ({r.detail})" if r.detail else ""), batch=batch)
    return [r.__dict__ for r in results]


def _members_view(s: dict, members: List[dict], stats: dict, skipped: dict, now: datetime) -> List[dict]:
    out = []
    for m in members:
        st = stats.get(m["task_collection"], {})
        p95 = st.get("p95_s")
        out.append({
            **m,
            "when": sch.describe(int(s["cadence_minutes"]), int(m["offset_minutes"]), s["timezone"]),
            "next_run": None if s["paused"] else
            sch.next_run(int(s["cadence_minutes"]), int(m["offset_minutes"]), s["timezone"], now).isoformat(),
            "job_url": f"{db.workspace().config.host.rstrip('/')}/jobs/{m['job_id']}" if m.get("job_id") else None,
            **{k: st.get(k) for k in ("last_run_id", "last_status", "last_started", "last_duration_s", "p50_s",
                                      "p95_s", "runs_24h", "failed_24h", "lock_skipped_24h", "min_sla_minutes",
                                      "enabled_tables")},
            "skipped_24h": skipped.get(m["task_collection"]),
            "warnings": sch.warnings(int(s["cadence_minutes"]), p95, st.get("min_sla_minutes"),
                                     skipped.get(m["task_collection"]) or 0),
        })
    return out


_skipped = sch.SkippedRuns()


def _skipped_starts(job_id, since: datetime, now: datetime, client=None) -> Optional[List[int]]:
    """Start times (ms) of a wrapper job's skipped runs since `since`; None if the Jobs API failed."""
    try:
        return _skipped.starts(client or jobs.schedule_client(), job_id, int(since.timestamp() * 1000),
                               int(now.timestamp() * 1000))
    except Exception:
        return None


def _skipped_counts(members: List[dict]) -> dict:
    """Skipped triggers per member over 24 hours, from the wrapper jobs' runs."""
    now = datetime.now(timezone.utc)
    client, out = None, {}
    for m in members:
        if m.get("job_id"):
            client = client or jobs.schedule_client()
            starts = _skipped_starts(m["job_id"], now - timedelta(hours=24), now, client)
            out[m["task_collection"]] = None if starts is None else len(starts)
    return out


@app.get("/api/schedules")
def list_schedules(details: bool = True):
    """Schedules with their members: when each runs next, its wrapper job, recent runs and warnings."""
    rows = db.query(q.schedules_sql())
    members = db.query(q.schedule_members_sql(), ([r["id"] for r in rows],)) if rows else []
    active = [m for m in members if m["removed_at"] is None]
    colls = sorted({m["task_collection"] for m in active})
    stats = {r["task_collection"]: r for r in
             (db.query(q.collection_run_stats_sql(), (colls, colls, colls, colls)) if colls else [])}
    skipped = _skipped_counts(active) if details else {}
    now = datetime.now(timezone.utc)
    return [{**s, "description": sch.describe(int(s["cadence_minutes"]), 0, s["timezone"]),
             "members": _members_view(s, [m for m in active if m["schedule_id"] == s["id"]], stats, skipped, now)}
            for s in rows]


def _scheduled_collections() -> dict:
    """{collection: [names of the active schedules it's in]}."""
    scheduled = {}
    rows = db.query(q.schedules_sql())
    if rows:
        names = {r["id"]: r["name"] for r in rows}
        for m in db.query(q.schedule_members_sql(), (list(names),)):
            if m["removed_at"] is None:
                scheduled.setdefault(m["task_collection"], []).append(names[m["schedule_id"]])
    return scheduled


@app.get("/api/schedules/options")
def schedule_options():
    scheduled = _scheduled_collections()
    try:     # the ingestion job's own defaults, shown as the editor's placeholders
        defaults = {k: v for k, v in jobs.job_summary(jobs.INGEST_JOB_ID)["parameters"].items() if k in sch.PARAMETERS}
    except Exception:
        defaults = {}
    return {"cadences": sch.CADENCES, "parameters": list(sch.PARAMETERS), "flags": sch.FLAGS, "defaults": defaults,
            "variants": sorted(jobs.ingest_variants()),
            "collections": [r["task_collection"] for r in db.query(q.collections_sql())], "scheduled": scheduled,
            "view_group": jobs.SCHEDULE_JOB_VIEW_GROUP}


@app.post("/api/schedules")
def create_schedule(body: ScheduleBody, request: Request):
    import psycopg
    _validate_schedule(body)
    user = _user(request)
    offs = sch.offsets(body.members, body.cadence_minutes, body.stagger)
    batch = uuid.uuid4().hex[:12]
    try:
        with db.transaction() as cur:
            cur.execute(q.insert_schedule_sql(), (body.name.strip(), body.cadence_minutes, body.timezone,
                                                  json.dumps(body.parameters), body.job_variant or "default",
                                                  body.stagger, body.paused, body.notify_emails or None, user or None))
            sid = cur.fetchone()[0]
            for c in sorted(body.members):
                cur.execute(q.insert_member_sql(), (sid, c, offs[c]))
                _log(cur, user, "schedule_create", None, c, "schedule", None,
                     f"{body.name} ({sch.describe(body.cadence_minutes, offs[c], body.timezone)})", batch=batch)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, f"a schedule named '{body.name}' already exists")
    return {"id": sid, "sync": _sync(sid, user)}


@app.post("/api/schedules/{sid}")
def update_schedule(sid: int, body: ScheduleBody, request: Request):
    """Edit a schedule: fields, and members added or removed. Offsets are recomputed; jobs are synced."""
    import psycopg
    _validate_schedule(body)
    user = _user(request)
    s, members = _load_schedule(sid)
    if s["deleted_at"] is not None:
        raise HTTPException(409, "schedule was deleted")
    new = body.model_dump()
    new["notify_emails"] = new["notify_emails"] or None
    new["job_variant"] = new["job_variant"] or "default"
    changed = [f for f in q.SCHEDULE_FIELDS if (s[f] or None) != (new[f] or None) and not
               (f == "parameters" and (s[f] or {}) == (new[f] or {}))]
    active = {m["task_collection"]: m for m in members if m["removed_at"] is None}
    added = sorted(set(body.members) - set(active))
    gone = sorted(set(active) - set(body.members))
    offs = sch.offsets(body.members, body.cadence_minutes, body.stagger)
    batch = uuid.uuid4().hex[:12]
    try:
        with db.transaction() as cur:
            if changed:
                vals = [json.dumps(new[f]) if f == "parameters" else new[f] for f in changed]
                cur.execute(q.update_schedule_sql(changed), vals + [int(sid)])
                for f in changed:
                    _log(cur, user, "schedule_edit", None, None, f"{s['name']}.{f}", s[f], new[f], batch=batch)
            if gone:
                cur.execute(q.remove_members_sql(), ([active[c]["id"] for c in gone],))
                for c in gone:
                    _log(cur, user, "schedule_edit", None, c, f"{s['name']}.members", c, None, batch=batch)
            for c in added:
                cur.execute(q.insert_member_sql(), (int(sid), c, offs[c]))
                _log(cur, user, "schedule_edit", None, c, f"{s['name']}.members", None, c, batch=batch)
            for c, m in active.items():
                if c in offs and int(m["offset_minutes"]) != offs[c]:
                    cur.execute(q.update_member_offset_sql(), (offs[c], m["id"]))
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, f"a schedule named '{body.name}' already exists")
    return {"id": sid, "changed": changed, "added": added, "removed": gone, "sync": _sync(sid, user)}


def _set_paused(sid: int, paused: bool, request: Request):
    user = _user(request)
    s, _ = _load_schedule(sid)
    if s["deleted_at"] is not None:
        raise HTTPException(409, "schedule was deleted")
    if bool(s["paused"]) != paused:
        with db.transaction() as cur:
            cur.execute(q.update_schedule_sql(["paused"]), (paused, int(sid)))
            _log(cur, user, "schedule_pause" if paused else "schedule_resume", None, None, f"{s['name']}.paused",
                 s["paused"], paused)
    return {"id": sid, "paused": paused, "sync": _sync(sid, user)}


@app.post("/api/schedules/{sid}/pause")
def pause_schedule(sid: int, request: Request):
    return _set_paused(sid, True, request)


@app.post("/api/schedules/{sid}/resume")
def resume_schedule(sid: int, request: Request):
    return _set_paused(sid, False, request)


@app.post("/api/schedules/{sid}/delete")
def delete_schedule(sid: int, request: Request):
    """Delete a schedule and its wrapper jobs (the schedule row is kept, marked deleted)."""
    user = _user(request)
    s, members = _load_schedule(sid)
    active = [m["id"] for m in members if m["removed_at"] is None]
    with db.transaction() as cur:
        cur.execute(q.delete_schedule_sql(), (int(sid),))
        if active:
            cur.execute(q.remove_members_sql(), (active,))   # their jobs are deleted by the sync below
        _log(cur, user, "schedule_delete", None, None, s["name"], None, None)
    return {"id": sid, "deleted": True, "sync": _sync(sid, user)}


@app.post("/api/schedules/{sid}/sync")
def sync_schedule(sid: int, request: Request):
    """Re-sync the wrapper jobs (recreates missing ones and undoes edits made outside the app)."""
    return {"id": sid, "sync": _sync(sid, _user(request))}


@app.get("/api/schedules/{sid}/check")
def check_schedule(sid: int):
    """Drift report for the schedule's wrapper jobs; changes nothing."""
    s, members = _load_schedule(sid)
    active = [m for m in members if m["removed_at"] is None]
    return sch.check(s, active, jobs.schedule_client(), _variant_job_id(s["job_variant"]), _owner())


@app.get("/api/timeline")
def timeline(hours: int = 24):
    """Runs per collection in the last `hours`, planned triggers of scheduled collections, and skipped
    triggers (from the wrapper jobs' runs)."""
    hours = max(1, min(int(hours), 168))
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    runs = db.query(q.timeline_runs_sql(), (end, start))
    scheds = db.query(q.schedules_sql())
    members = db.query(q.schedule_members_sql(), ([r["id"] for r in scheds],)) if scheds else []
    by_id = {r["id"]: r for r in scheds}
    rows = {}
    for r in runs:
        rows.setdefault(r["task_collection"], {"task_collection": r["task_collection"], "runs": [], "planned": [],
                                               "skipped": [], "schedules": []})["runs"].append(r)
    client = None
    for m in members:
        if m["removed_at"] is not None:
            continue
        s = by_id[m["schedule_id"]]
        row = rows.setdefault(m["task_collection"], {"task_collection": m["task_collection"], "runs": [],
                                                     "planned": [], "skipped": [], "schedules": []})
        row["schedules"].append(f"{s['name']} ({sch.describe(int(s['cadence_minutes']), int(m['offset_minutes']), s['timezone'])})")
        if not s["paused"]:
            # The current schedule's triggers, from when the collection joined or the schedule last changed.
            since = max([start] + [datetime.fromisoformat(v) for v in (m.get("added_at"), s.get("updated_at")) if v])
            row["planned"] += [t.isoformat() for t in
                               sch.fire_times(int(s["cadence_minutes"]), int(m["offset_minutes"]), s["timezone"], since, end)]
        if m.get("job_id"):
            client = client or jobs.schedule_client()
            row["skipped"] += [jobs.ts(t) for t in (_skipped_starts(m["job_id"], start, end, client) or [])]
    return {"start": start.isoformat(), "end": end.isoformat(),
            "collections": [rows[k] for k in sorted(rows)]}


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
