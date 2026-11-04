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
import logging
import os
import re
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

import action_states
import background_reads
import compute
import config_import as ci
import ingestion_jobs as ij
import jdbc_connections as jc
import job_settings as js
import job_settings_sql as jss
import jobs
import by_database
import jdbc_logins as jl
import lakebase as db
import new_job as nj
import placement
import preparing
import queries as q
import rediscovery
import rollup
import run_poller
import table_removal
import schedules as sch
import schema_upgrade
import source_catalog
import warehouse
import warehouse_reads


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """The app's background work while it runs: the run poller, which keeps the databricks_run table current (every
    instance runs one, and only the one holding the poller's lock polls; RUN_POLLER=0 turns it off on this instance),
    the re-discovery scheduler (REDISCOVERY_TICK_S; 0 leaves it off), the background reader, which keeps the reads
    through the SQL warehouse going whether a page is open or not (background_reads; BACKGROUND_READS=0 turns it off on
    this instance, and the pages' steps take the reads instead), the schema upgrade check, once, which starts the
    setup job when the state database is behind (schema_upgrade; SCHEMA_UPGRADE=0 turns it off), and the look for jobs
    whose preparation no thread holds, which starts it again (_prepare_resumer; PREPARE_RESUME_S=0 turns it off)."""
    if os.environ.get("RUN_POLLER", "1") != "0":
        run_poller.POLLER.start(on_change=_forget_shared_reads)
    if os.environ.get("BACKGROUND_READS", "1") != "0" and warehouse.configured():
        READER.start()
    if os.environ.get("SCHEMA_UPGRADE", "1") != "0":
        schema_upgrade.start(db.connect, jobs.SETUP_JOB_ID, lambda job_id: jobs.runs(job_id, active_only=True, limit=1),
                             lambda job_id: jobs.run_now(job_id, {}))
    if REDISCOVERY_TICK_S > 0:
        threading.Thread(target=_rediscovery_scheduler, daemon=True, name="lakefed-app-rediscovery").start()
    if PREPARE_RESUME_S > 0:
        threading.Thread(target=_prepare_resumer, daemon=True, name="lakefed-app-prepare-resume").start()
    yield


app = FastAPI(title="lakefed_ingest operations", docs_url="/api/docs", openapi_url="/api/openapi.json",
              lifespan=_lifespan)
STATIC = Path(__file__).parent / "static"

# The routes that take a CSV of tables, and the most a request to one may carry: a whole server of 120,000 lines
# (new_job.MAX_LINES) of long select lists fits within it, and a larger upload is refused before it is read, not after.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(64 * 1024 * 1024)))
UPLOAD_ROUTES = re.compile(r"^/api/(import|uploads|ingestion-jobs|ingestion-jobs/\d+/(tables|databases))/?$")


class UploadLimit:
    """Refuse (413) a request to an upload route whose body is over MAX_UPLOAD_BYTES: by its Content-Length when it
    gives one, else by counting the body as it arrives. Past the limit the rest is never read: the route sees an
    empty end of body, and its answer is replaced with the refusal (FastAPI would turn a raised error into a 400)."""

    # Starlette builds middleware as cls(app=app, ...) in the versions Databricks Apps installs (0.38) and as
    # cls(app, ...) in later ones, so the first parameter must be named app.
    def __init__(self, app, limit: int):
        self.inner, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or not UPLOAD_ROUTES.match(scope["path"]):
            return await self.inner(scope, receive, send)
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > self.limit:
            return await self._refuse(send)
        state = {"seen": 0, "over": False, "refused": False}

        async def counted():
            if state["over"]:
                return {"type": "http.request", "body": b"", "more_body": False}
            message = await receive()
            state["seen"] += len(message.get("body", b""))
            if state["seen"] > self.limit:
                state["over"] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def answered(message):
            if not state["over"]:
                return await send(message)
            if not state["refused"]:
                state["refused"] = True
                await self._refuse(send)
        await self.inner(scope, counted, answered)

    async def _refuse(self, send):
        body = json.dumps({"detail": f"the upload is over {self.limit // (1024 * 1024)} MB: split the file"}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(UploadLimit, limit=MAX_UPLOAD_BYTES)


@app.exception_handler(Exception)
async def _errors(request: Request, exc: Exception):
    msg = str(exc)
    hint = None
    if "permission denied" in msg.lower():
        hint = "The app's service principal may lack privileges: run the lakefed_ingest_app_grants job."
    return JSONResponse(status_code=500, content={"detail": msg[:2000], "hint": hint})


@app.exception_handler(RequestValidationError)
async def _invalid_input(request: Request, exc: RequestValidationError):
    """FastAPI's 422 echoes each failing field's input, which on the JDBC login route is a password: that route keeps
    only where and why a field failed. Every other route keeps FastAPI's own 422."""
    if request.url.path != jl.ROUTE:
        return await request_validation_exception_handler(request, exc)
    return JSONResponse(status_code=422, content={"detail": jl.safe_validation_errors(exc.errors())})


def _user(request: Request) -> str:
    return (request.headers.get("X-Forwarded-Email") or request.headers.get("X-Forwarded-User") or "").strip()


@app.middleware("http")
async def _writes_need_a_user(request: Request, call_next):
    """Every change through the API needs to know who makes it: a write with no user from the Apps proxy is refused,
    so no permission check or change log ever runs for an unknown caller. Reads stay open to anyone the proxy admits."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/") and not _user(request):
        return JSONResponse(status_code=403, content={"detail": NO_IDENTITY})
    return await call_next(request)


# The reason a refusal's detail carries when a run of a group holds its lock, so a caller (re-discovery's "deferred")
# reads it from the detail's fields, never from its text, which also names jobs.
RUNNING_REASON = "running"


def _running_refusal(group: str) -> HTTPException:
    """The 409 for a group whose run holds its lock: its message, the reason RUNNING_REASON and the group."""
    return HTTPException(409, {"message": f"its ingestion job is running (group '{group}'); try again after the run "
                                          "finishes", "reason": RUNNING_REASON, "group": group})


def _refused_while_running(error: HTTPException) -> bool:
    """Whether a refusal is the one a running group gives (_running_refusal), by its reason field."""
    return (error.status_code == 409 and isinstance(error.detail, dict)
            and error.detail.get("reason") == RUNNING_REASON)


def _lock_collections(cur, collections):
    """Take each collection's run lock for this transaction, or refuse: changing tables while their
    collection is running could race the run's own checkpoint writes."""
    for c in sorted({c for c in collections if c}):
        cur.execute(q.collection_lock_sql(), (q.collection_lock_key(c),))
        if not cur.fetchone()[0]:
            raise _running_refusal(c)


def _lock_collections_together(cur, collections):
    """_lock_collections for many collections in one statement: each one's lock, or a 409 for the first a run holds."""
    collections = sorted({c for c in collections if c})
    if not collections:
        return
    cur.execute(q.collection_locks_sql(), ([q.collection_lock_key(c) for c in collections],))
    got = {key: taken for key, taken in cur.fetchall()}
    for c in collections:
        if not got.get(q.collection_lock_key(c)):
            raise _running_refusal(c)


def _refuse_mixed_sinks(cur, rows):
    """After writing control rows, in the same transaction: refuse when a sink they write would be
    claimed by a dedicated row and by other rows (the next run would stop every table on it)."""
    sinks = sorted({tuple(str(r.get(k)).lower() for k in ("sink_catalog", "sink_schema", "sink_table")) for r in rows})
    if not sinks:
        return
    cur.execute(q.mixed_sinks_for_sql(), tuple([sink[at] for sink in sinks] for at in range(3)))
    mixed = [r[0] for r in cur.fetchall()]
    if mixed:
        raise HTTPException(409, f"sink {mixed[0]} would be claimed by a dedicated row and by other rows; "
                                 "a sink is either dedicated to one source table or shared by client_id")


def _refuse_split_databases(cur, rows):
    """After writing control rows, in the same transaction: refuse when a database they belong to would
    have tables in more than one collection. Its run locks one collection, and the check job reads each
    database once for its collection, so the database's other tables would be checked and ingested apart."""
    keys = sorted({q.database_key(r) for r in rows})
    if not keys:
        return
    cur.execute(q.split_databases_sql(), (keys,))
    split = cur.fetchall()
    if split:
        cur.execute(q.clash_jobs_sql(), (sorted({group for row in split for group in row[2]}),))
        jobs = [{"id": row[0], "name": row[1]} for row in cur.fetchall()]
        raise HTTPException(409, {"message": _split_message(split, [job["name"] for job in jobs]), "jobs": jobs})


def _split_message(split, job_names=()) -> str:
    """The refusal for databases with tables in more than one ingestion group: the one database and its groups, or the
    first three databases and a count for the rest. When live jobs hold some of those groups, it names them too. Only
    names already read are used; the write is rolled back."""
    def first_three(names):
        shown = ", ".join(names[:3])
        return f"{shown} and {len(names) - 3} more" if len(names) > 3 else shown
    databases = [row[1] for row in split]
    groups = first_three(sorted({group for row in split for group in row[2]}))
    holders = ""
    if job_names:
        kind = "job" if len(job_names) == 1 else "jobs"
        holders = f" Some of these groups are in ingestion {kind} {first_three(job_names)}."
    if len(databases) == 1:
        return (f"Database {databases[0]} has tables in more than one ingestion group ({groups}).{holders} "
                "Move all of its tables into one group. Nothing was written.")
    count, names = len(databases), first_three(databases)
    return (f"{count} databases have tables in more than one ingestion group: {names}.{holders} "
            "Move all of each database's tables into one group. Nothing was written.")


def _refuse_deleted_databases(cur, rows):
    """Before writing new control rows, in the same transaction: refuse when a database they read is one a deleted job
    still keeps (not yet purged). Its tables would share the job's groups' state, so the job is restored or purged
    first."""
    keys = sorted({q.database_key(r) for r in rows})
    if not keys:
        return
    cur.execute(q.deleted_database_owners_sql(), (keys,))
    owners = cur.fetchall()
    if owners:
        database, job, _ = owners[0]
        raise HTTPException(409, f"database {database} is in the deleted ingestion job {job}, which is kept until "
                                 f"it is purged: Restore {job}, or Delete now to purge it, then add these tables")


def _log(cur, user, action, control_id, collection, field=None, old=None, new=None, reseed=None, batch=None):
    as_text = lambda v: None if v is None else str(v)
    cur.execute(q.log_change_sql(), (user or None, action, control_id, collection, field, as_text(old),
                                     as_text(new), reseed, batch))


def _log_lines(cur, lines, per_statement=q.CONTROL_BATCH):
    """Many change log lines, each the values _log takes after cur, per_statement to a statement."""
    as_text = lambda v: None if v is None else str(v)
    lines = [(user or None, action, control_id, collection, field, as_text(old), as_text(new), reseed, batch)
             for user, action, control_id, collection, field, old, new, reseed, batch in lines]
    for at in range(0, len(lines), per_statement):
        chunk = lines[at:at + per_statement]
        cur.execute(q.log_changes_sql(len(chunk)), tuple(v for line in chunk for v in line))


def _taken_target(row: dict) -> str:
    """Why a row's target is refused. A consolidated row is named by its database and server in words: its client_id
    is an internal identity key that no one typed, so it stays out of the message."""
    sink = f"{row['sink_catalog']}.{row['sink_schema']}.{row['sink_table']}"
    if not row.get("client_id"):
        return f"another table already writes {sink}"
    server = str(row.get("src_connection") or row.get("src_host") or "").strip()
    return (f"another table already writes {sink} for database {row['src_database']}"
            + (f" on {server}" if server else ""))


def _without_repeated_targets(rows: List[dict], skip_taken: bool) -> List[dict]:
    """Rows that one insert would give the same target (and client_id) never reach it together: Postgres skips the
    second as a conflict with the first (on conflict do nothing), and both would find the first's id, so neither would
    be refused. The second refuses the lot, as a target another table writes does, or with skip_taken is left out."""
    seen, kept = set(), []
    for row in rows:
        key = q.control_sink_key(q.control_params(row))
        if key in seen:
            if skip_taken:
                continue
            raise HTTPException(409, _taken_target(row))
        seen.add(key)
        kept.append(row)
    return kept


def _insert_controls(cur, rows, user, batch, skip_taken=False, progress=None,
                     log_per_statement=q.CONTROL_BATCH) -> List[tuple]:
    """Insert new control rows q.CONTROL_BATCH to a statement, then their "import" change log lines, log_per_statement
    to a statement, inside the caller's transaction. A row whose target another table writes refuses the lot (409), or
    with skip_taken is left out. Returns (row, id) for each row written; progress(n) is told how many rows are done
    after each statement."""
    _refuse_deleted_databases(cur, rows)
    rows = _without_repeated_targets(rows, skip_taken)
    written = []
    for at in range(0, len(rows), q.CONTROL_BATCH):
        chunk = rows[at:at + q.CONTROL_BATCH]
        params = [q.control_params(row) for row in chunk]
        cur.execute(q.insert_controls_sql(len(chunk)), tuple(v for p in params for v in p))
        ids = {tuple(found[1:]): found[0] for found in cur.fetchall()}
        now = []
        for row, p in zip(chunk, params):
            control_id = ids.get(q.control_sink_key(p))
            if control_id is not None:
                now.append((row, control_id))
            elif not skip_taken:
                raise HTTPException(409, _taken_target(row))
        written += now
        if progress:
            progress(at + len(chunk))
    _log_lines(cur, [(user, "import", control_id, row["task_collection"], None, None, None, None, batch)
                     for row, control_id in written], log_per_statement)
    _rewrite_rollups(cur, [(row["task_collection"], row.get("src_database")) for row, _ in written])
    return written


def _touch(cur, user, jobs_changed=(), groups=()):
    """Record who last changed these ingestion jobs (or the jobs holding these groups, whose tables changed), and
    when: the job page and the Ingestion jobs list show it as Last updated."""
    if jobs_changed:
        cur.execute(q.touch_jobs_sql(), (user or None, [int(j) for j in jobs_changed]))
    wanted = sorted({g for g in groups if g})
    if wanted:
        cur.execute(q.touch_group_jobs_sql(), (user or None, wanted))


def _rewrite_rollups(cur, databases) -> None:
    """Recompute the monitoring rollups of the databases a transaction changed, given as (ingestion group, source
    database name) pairs, and of their groups, inside the caller's transaction. Each database is counted from its own
    control rows, so the rows are exact after the edit and need no deltas."""
    by_group = {}
    for group, src_database in databases:
        by_group.setdefault(group, set()).add(src_database or "")
    pairs = [(group, name) for group in sorted(by_group) for name in sorted(by_group[group])]
    for at in range(0, len(pairs), q.ROLLUP_CHUNK):
        chunk = pairs[at:at + q.ROLLUP_CHUNK]
        cur.execute(q.rollup_database_rewrite_sql(), ([group for group, _ in chunk], [name for _, name in chunk]))
    if by_group:
        cur.execute(q.rollup_group_rewrite_sql(), (sorted(by_group),))


def _rewrite_rollups_for_ids(cur, ids) -> None:
    """_rewrite_rollups for the databases of some control rows, by id."""
    if ids:
        cur.execute(q.control_databases_sql("ids"), (list(ids),))
        _rewrite_rollups(cur, cur.fetchall())


def _rewrite_rollups_for_groups(cur, groups) -> None:
    """_rewrite_rollups for every database of some ingestion groups."""
    if groups:
        cur.execute(q.control_databases_sql("groups"), (sorted(groups),))
        _rewrite_rollups(cur, cur.fetchall())


def _apply_plans(cur, rows, plans, user, batch):
    """Write planned edits (queries.plan_edit) inside the caller's transaction: each changed field, its change log
    line, a reseed where the plan needs one, then the mixed-sink and split-database checks over the rows as they
    now are."""
    _refuse_connection_edits(rows, plans, cur=cur, user=user)
    by_id = {r["id"]: r for r in rows}
    before = [(by_id[p["id"]]["task_collection"], by_id[p["id"]].get("src_database")) for p in plans]
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
    edited = [dict(by_id[p["id"]], **{f: v[1] for f, v in p["changes"].items()}) for p in plans]
    _rewrite_rollups(cur, before + [(row["task_collection"], row.get("src_database")) for row in edited])
    _refuse_mixed_sinks(cur, edited)
    _refuse_split_databases(cur, edited)


def _locked_rows(cur, ids):
    cur.execute(q.control_rows_for_update_sql(), (list(ids),))
    cols = [d.name for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    missing = set(ids) - {r["id"] for r in rows}
    if missing:
        raise HTTPException(404, f"control rows not found: {sorted(missing)}")
    return rows


# ---- context / overview -----------------------------------------------------------------------------

@app.get("/api/databricks-runs/{run_id}/open")
def open_databricks_run(run_id: int):
    """Open a Databricks job run's own page (a group run's job_run_id), as its run page link in the Jobs UI does."""
    try:
        url = run_poller.run_state_of(run_id)["url"]
    except Exception as e:
        raise HTTPException(404, f"can't find Databricks job run {run_id}: {str(e).split('. ')[0]}")
    if not url:
        raise HTTPException(404, f"Databricks job run {run_id} has no page")
    return RedirectResponse(url)


@app.get("/api/config")
def config(request: Request):
    return {"lakebase_instance": db.INSTANCE, "lakebase_database": db.DATABASE, "user": _user(request),
            "jobs": {"ingest": jobs.INGEST_JOB_ID, "setup": jobs.SETUP_JOB_ID, "discover": jobs.DISCOVER_JOB_ID},
            "workspace_url": jobs.workspace_url(), "app_principal": jobs.APP_PRINCIPAL,
            "defaults": {"partition_threshold_mb": q.DEFAULT_PARTITION_THRESHOLD_MB,
                         "partition_size_mb": q.DEFAULT_PARTITION_SIZE_MB}}


# Monitoring reads are shared by every viewer and recomputed at most once per MONITOR_CACHE_S seconds,
# so the app's load on Lakebase does not grow with open tabs. 0 turns the cache off.
MONITOR_CACHE_S = max(0, int(os.environ.get("MONITOR_CACHE_S", "15")))
_shared_reads: dict = {}
_shared_lock = threading.Lock()
_shared_computing: dict = {}     # key: the lock its one compute holds, so other requests wait for it
_shared_generation = [0]         # bumped by every committed write: a read begun before it isn't kept


def _shared(key, compute, ttl=None):
    """compute() at most once per `ttl` seconds (MONITOR_CACHE_S by default) for key, shared by every
    request. When it is due, one request computes it and the others asking meanwhile wait for that result
    instead of running the same query. A read that a write committed during is returned but not kept."""
    ttl = MONITOR_CACHE_S if ttl is None else ttl
    if not ttl:
        return compute()

    def fresh():
        hit = _shared_reads.get(key)
        return hit if hit and time.time() - hit[1] < ttl else None

    with _shared_lock:
        hit = fresh()
        if hit:
            return hit[0]
        computing = _shared_computing.setdefault(key, threading.Lock())
    with computing:
        with _shared_lock:
            hit = fresh()
            if hit:
                return hit[0]
            generation = _shared_generation[0]
        value = compute()
        with _shared_lock:
            if generation == _shared_generation[0]:
                _shared_reads[key] = (value, time.time())
    return value


def _forget_shared_reads() -> None:
    """Drop every shared read, so the pages show a change as soon as it is committed rather than up to a minute
    later."""
    with _shared_lock:
        _shared_generation[0] += 1
        _shared_reads.clear()


@contextmanager
def _write():
    """db.transaction, then the shared reads forgotten once it has committed: every write the app makes (tables,
    settings, jobs, schedules, imports) goes through it. The run poller polls again at once, so a new job's runs
    show within a poll."""
    with db.transaction() as cur:
        yield cur
    _forget_shared_reads()
    run_poller.POLLER.wake()


# ---- background writes ---------------------------------------------------------------------------------------------
# A write of more than BACKGROUND_ROWS tables (New's Create, Add tables, a databases file) takes longer than the Apps
# proxy waits (a 100,000-table Create took 7 minutes one row at a time; batched it is under a minute, still too long to
# hold a request on). The request checks what it can, records a task (app_task) and answers 202 at once; a thread
# writes the tables in the one transaction the request would have, and the pages follow the task's progress.
BACKGROUND_ROWS = int(os.environ.get("BACKGROUND_ROWS", "5000"))
TASK_BEAT_S = float(os.environ.get("TASK_BEAT_S", "2"))
# A running task's heartbeat older than this means its thread is gone: the app restarted while it wrote.
TASK_STALE_S = int(os.environ.get("TASK_STALE_S", "120"))
TASK_WRITTEN_NOT_SYNCED = ("The tables were written, but their Databricks jobs were not synced, so the task is not "
                           "done:")
TASK_ROLLED_BACK = ("the app restarted while writing these tables; its transaction was rolled back with it, so none "
                    "of them was written: send it again")
TASK_UNSYNCED = ("the tables were written, but the app restarted before the job's Databricks jobs were synced: "
                 "open the job and save it once to sync them")
TASK_PARTIAL = ("the app restarted while writing the job's databases: the ones written so far are kept, with their "
                "tables; send the same request again to finish the job")


class NotSynced(HTTPException):
    """A write that committed, but a wrapper job it changed did not sync: the write is not done, and the detail says
    what failed. Raised by a write's `after` (in its task, or in the request when the write was small)."""


def _synced(results: List[dict]) -> None:
    """Refuse a write as not done when a Databricks job it changed did not sync (the tables are written already)."""
    failures = action_states.sync_failures(results)
    if failures:
        raise NotSynced(500, f"{TASK_WRITTEN_NOT_SYNCED} {'; '.join(failures)}"[:4000])


def _start_background(work) -> None:
    """Run work on its own thread (tests run it inline instead)."""
    threading.Thread(target=work, daemon=True, name="lakefed-app-task").start()


def _error_text(e: BaseException) -> str:
    """What a failed task says: an HTTPException's detail as the request would have answered it, its errors joined."""
    if isinstance(e, HTTPException):
        detail = e.detail
        if isinstance(detail, dict) and detail.get("errors"):
            return "; ".join(str(x) for x in detail["errors"][:20])[:4000]
        if isinstance(detail, dict) and detail.get("message"):
            return str(detail["message"])[:4000]
        return str(detail)[:4000]
    return (str(e) or type(e).__name__)[:4000]


def _task(task_id: int) -> dict:
    """A task as GET /api/tasks/{id} shows it, a stale one first ended (TASK_STALE_S)."""
    db.execute(q.stale_tasks_sql(), (TASK_UNSYNCED, TASK_ROLLED_BACK, TASK_PARTIAL, TASK_STALE_S, int(task_id), None))
    rows = db.query(q.task_sql(), (int(task_id),))
    if not rows:
        raise HTTPException(404, f"task {task_id} not found")
    return rows[0]


def _task_written(cur, task_id: Optional[int], result: dict, job_id=None, done=0) -> None:
    """The write's last statement, in its transaction: the task's result commits with the tables, so a task whose
    app restarted is known to have written them (a result) or not (none)."""
    if task_id is not None:
        cur.execute(q.task_written_sql(), (json.dumps(result, default=str), job_id, done, task_id))


def _unsynced(e: Exception, result: dict):
    """The result and error of a write whose sync after its commit failed: the tables stay written, the task is not done."""
    error = _error_text(e) if isinstance(e, NotSynced) else f"{TASK_WRITTEN_NOT_SYNCED} {_error_text(e)}"
    return {**result, "after_error": _error_text(e)}, error


def _attempt(task_id: int, write, after):
    """A task's work on the calling thread: write(progress, task_id) in its transaction, then after(result) (syncing the
    Databricks jobs) once it has committed. A beat thread records progress and the heartbeat every TASK_BEAT_S, also
    while one long statement runs. Returns (status, result, error, failure): a write that raised has no result; a sync
    that failed after the commit keeps its result, with what failed."""
    state = {"done": 0}
    stop = threading.Event()

    def beat():
        while not stop.wait(TASK_BEAT_S):
            try:
                db.execute(q.task_progress_sql(), (state["done"], task_id))
            except Exception:
                pass     # a missed beat is caught up by the next; TASK_STALE_S allows for many

    def progress(n: int) -> None:
        state["done"] = n

    threading.Thread(target=beat, daemon=True, name="lakefed-app-task-beat").start()
    result, status, error, failure = None, "failed", None, None
    try:
        result = write(progress, task_id)
        try:
            result = after(result)
            status = "succeeded"
        except Exception as e:
            result, error = _unsynced(e, result)
            failure = e
    except BaseException as e:
        error, failure = _error_text(e), e
    finally:
        stop.set()
    return status, result, error, failure


def _finish(task_id: int, status: str, result, error) -> None:
    db.execute(q.finish_task_sql(), (status, (result or {}).get("id"), json.dumps(result, default=str) if result else None,
                                     error, task_id))


def _run_task(task_id: int, write, after) -> None:
    """A background task's thread: the work (_attempt), then the task ends with what it reached."""
    status, result, error, _ = _attempt(task_id, write, after)
    _finish(task_id, status, result, error)


def _task_answer(task_id: int) -> JSONResponse:
    return JSONResponse(status_code=202, content={"task": _task(task_id), "created": False, "applied": False})


def _run_now(task_id: int, write, after):
    """A small write, in the request. Its task was recorded first, so the same request id finds it. A write that failed
    answers as the request always did; one that committed but whose Databricks sync failed answers 202 with its task,
    not done, so the page can finish it by sending the same request id again (_answer_again)."""
    status, result, error, failure = _attempt(task_id, write, after)
    _finish(task_id, status, result, error)
    if failure is not None and result is None:
        raise failure
    return result if status == "succeeded" else _task_answer(task_id)


def _finish_sync(task_id: int, stored: dict, after) -> None:
    """Sync again a write that committed (its stored result) but whose sync failed: nothing is written again."""
    try:
        status, result, error = "succeeded", after(dict(stored)), None
    except Exception as e:
        status = "failed"
        result, error = _unsynced(e, stored)
    _finish(task_id, status, result, error)


def _answer_again(task_id: int, after) -> JSONResponse:
    """The same request id sent again: the task it made answers, and nothing is written again. A task that committed but
    is not done (its Databricks sync failed, or the app restarted before it) is finished here by syncing again."""
    task = _task(task_id)
    if task["status"] == "failed" and task["result"] is not None and after is not None \
            and db.query(q.resume_synced_task_sql(), (task_id,)):
        _finish_sync(task_id, task["result"], after)
    return _task_answer(task_id)


def _write_now_or_later(kind, request_id, job_id, total, user, write, after, background: bool = False):
    """Every write is a task: recorded first (the request id is its key), then written in the request when it is small
    (_run_now), else on a background thread. Sent again with the same request id, it answers with its task (_answer_again).
    A write that first discovers its tables (background) always runs on the thread, whatever its size."""
    made = db.query(q.insert_task_sql(), (kind, request_id or uuid.uuid4().hex, job_id, int(total), None, user or None))[0]
    if not made["made"]:
        return _answer_again(made["id"], after)
    if total <= BACKGROUND_ROWS and not background:
        return _run_now(made["id"], write, after)
    _start_background(lambda: _run_task(made["id"], write, after))
    return _task_answer(made["id"])


def _made_already(body, after) -> Optional[JSONResponse]:
    """The answer to a write sent again with the request id of one that made a task: that task, before anything is
    planned again (the first write's own tables would now make the plan refuse it)."""
    if body.dry_run or not body.request_id:
        return None
    made = db.query(q.task_of_request_sql(), (body.request_id,))
    return _answer_again(made[0]["id"], after) if made else None


@app.get("/api/tasks/{task_id}")
def task(task_id: int):
    """A background write: its status (running, succeeded, failed), done of total tables, its result (what the
    request would have answered) or error."""
    return _task(task_id)


@app.get("/api/ingestion-jobs/{job_id}/tasks")
def job_tasks(job_id: int):
    """A job's background writes of the last day, newest first, so its page shows one still running."""
    db.execute(q.stale_tasks_sql(), (TASK_UNSYNCED, TASK_ROLLED_BACK, TASK_PARTIAL, TASK_STALE_S, None, int(job_id)))
    return db.query(q.job_tasks_sql(), (int(job_id),))


# ---- uploads, the sources' top level and their pages, and selections by reference -----------------------------------
# A CSV is posted once (POST /api/uploads) and kept in Lakebase; a discovery's tables are there already. New and Add
# tables show a source's servers and databases and page a database's tables from there, and Create, Add tables and
# their previews name what to take (a Selection), which the server reads back from Lakebase and plans as it would the
# same lines sent in the body.

class UploadRequest(BaseModel):
    csv: str
    # The UC connection the file reads through, chosen before the file: the file gives no connection of its own (one
    # that names another connection on any row refuses it).
    connection: str = Field(min_length=1, max_length=255)
    file_name: Optional[str] = Field(None, max_length=255)


@app.post("/api/uploads")
def upload_tables(body: UploadRequest, request: Request):
    """Keep a CSV of tables in Lakebase, one row per line, written q.CONTROL_BATCH lines to a statement, read through the
    connection it was chosen with: its header problems, or a line naming another connection, refuse it (422). A line with
    a database alone is kept to be discovered at Create; a line that can't name a table is kept as invalid, with why."""
    read = nj.read_upload(body.csv, body.connection)
    if read["errors"]:
        raise HTTPException(422, {"errors": read["errors"]})
    upload_id, lines = uuid.uuid4().hex, read["lines"]
    tables = sum(line["status"] == nj.TABLE_LINE for line in lines)
    databases = sum(line["status"] == nj.DATABASE_LINE for line in lines)
    invalid = len(lines) - tables - databases
    with _write() as cur:
        cur.execute(q.insert_upload_sql(), (upload_id, body.file_name, _user(request) or None, read["columns"],
                                            len(lines), tables, invalid))
        for at in range(0, len(lines), q.CONTROL_BATCH):
            chunk = lines[at:at + q.CONTROL_BATCH]
            cur.execute(q.insert_upload_lines_sql(len(chunk)), tuple(
                v for line in chunk for v in (upload_id, line["line"], line["server"], line["src_database"],
                                              line["src_schema"], line["src_table"], line["load_type"],
                                              json.dumps(line["fields"]), line["status"], line["errors"])))
    invalid_lines = [{"line": line["line"], "table": ".".join(line[k] for k in ("src_database", "src_schema", "src_table") if line[k]),
                      "errors": line["errors"]} for line in lines if line["status"] == nj.INVALID_LINE][:20]
    return {"upload_id": upload_id, "file_name": body.file_name, "connection": read["connection"],
            "columns": read["columns"], "lines": len(lines), "tables": tables, "databases": databases,
            "invalid": invalid, "invalid_lines": invalid_lines}


@app.get("/api/uploads/{upload_id}/lines")
def upload_first_lines(upload_id: str, limit: int = 5):
    """An upload's header and its first lines as the file gave them, for the import's preview: the header's columns, and
    each line's number and fields, read from Lakebase. At most 20 lines, however many are asked for."""
    rows = db.query(q.upload_head_sql(), (max(1, min(int(limit), 20)), upload_id))
    if not rows:
        raise HTTPException(404, f"upload {upload_id} not found")
    return {"columns": list(rows[0]["columns"]),
            "lines": [{"line": r["line"], "fields": r["fields"]} for r in rows if r["line"] is not None]}


SOURCE_KINDS = ("upload", "discovery")


def _source_param(kind: str, source_id: str):
    """The parameter queries.source_rows_sql takes for a source: an upload's id (404 when there is none), or the list
    of discovery ids (comma-separated in a path)."""
    if kind == "upload":
        if not db.query(q.upload_sql(), (source_id,)):
            raise HTTPException(404, f"upload {source_id} not found")
        return source_id
    if kind == "discovery":
        return [i.strip() for i in source_id.split(",") if i.strip()]
    raise HTTPException(404, f"no source kind {kind}: {' or '.join(SOURCE_KINDS)}")


@app.get("/api/sources/{kind}/{source_id}")
def source_overview(kind: str, source_id: str, filter: str = "", offset: int = 0, limit: int = 100):
    """The top level of an upload or a discovery: its servers (databases, tables, tables by load type), its totals,
    and one page of its databases matching the filter, never its tables."""
    param = _source_param(kind, source_id)
    limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
    every = db.query(q.source_servers_sql(kind), (param, ""))
    matching = db.query(q.source_servers_sql(kind), (param, filter)) if filter else every
    out = {"kind": kind, "source_id": source_id, "filter": filter, "offset": offset, "limit": limit,
           "servers": matching,
           "totals": {"servers": len(every), "databases": sum(s["databases"] for s in every),
                      "tables": sum(s["tables"] for s in every)},
           "matched": {"databases": sum(s["databases"] for s in matching), "tables": sum(s["tables"] for s in matching)},
           "databases": db.query(q.source_databases_sql(kind), (param, filter, limit, offset))}
    if kind == "upload":
        out["upload"] = db.query(q.upload_sql(), (source_id,))[0]
        out["upload"]["databases_only"] = db.query(q.upload_databases_count_sql(), (source_id,))[0]["n"]
        out["invalid_lines"] = db.query(q.upload_invalid_sql(), (source_id, 20))
    return out


@app.get("/api/sources/{kind}/{source_id}/tables")
def source_tables(kind: str, source_id: str, server: str, database: str, search: str = "", offset: int = 0,
                  limit: int = 50):
    """One page of a database's tables in an upload or a discovery, those matching the search."""
    param = _source_param(kind, source_id)
    limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
    rows = db.query(q.source_tables_sql(kind), (param, server, database, search, limit, offset))
    return {"tables": [{k: v for k, v in r.items() if k != "total"} for r in rows],
            "total": rows[0]["total"] if rows else 0, "offset": offset, "limit": limit}


class SelectedDatabase(BaseModel):
    server: str
    database: str


class SelectedTables(SelectedDatabase):
    tables: List[str] = Field(min_length=1)     # schema.table


class Selection(BaseModel):
    """What to take from an upload or from discoveries: every database of the source matching the filter
    (whole_server), whole databases, and some tables of others."""
    upload_id: Optional[str] = None
    discovery_id: Optional[str] = None
    discovery_ids: List[str] = []
    whole_server: bool = False
    filter: str = ""
    databases: List[SelectedDatabase] = []
    tables: List[SelectedTables] = []

    @model_validator(mode="after")
    def _one_source(self):
        if bool(self.upload_id) == bool(self.discovery_id or self.discovery_ids):
            raise ValueError("name an upload_id or a discovery_id, not both")
        if not (self.whole_server or self.databases or self.tables or self.upload_id):
            raise ValueError("choose the whole server, databases or tables")
        return self

    def source(self):
        """(kind, the parameter source_rows_sql takes, the id as job_selection keeps it)."""
        if self.upload_id:
            return "upload", self.upload_id, self.upload_id
        ids = list(dict.fromkeys(([self.discovery_id] if self.discovery_id else []) + self.discovery_ids))
        return "discovery", ids, ",".join(ids)


def _selection_params(selection: Selection) -> tuple:
    """(kind, the parameter the source's queries take, source id, the parameters the selection queries take after
    it)."""
    kind, param, source_id = selection.source()
    if kind == "upload" and not db.query(q.upload_sql(), (param,)):
        raise HTTPException(404, f"upload {param} not found")
    database_keys = [q.selection_key(d.server, d.database) for d in selection.databases]
    table_keys = [q.selection_key(t.server, t.database, name) for t in selection.tables for name in t.tables]
    return kind, param, source_id, (selection.whole_server, selection.filter, database_keys, table_keys)


def _selection_parts(selection: Selection, whole_servers: List[str]) -> list:
    """What a selection was made from, part by part (job_selection): the whole servers (with the filter), the whole
    databases and the tables it names, leaving out those a whole server covers."""
    covered = lambda database: bool(selection.whole_server) and selection.filter.lower() in database.lower()
    return ([("server", server, None, None, selection.filter or None) for server in whole_servers]
            + [("database", d.server, d.database, None, None) for d in selection.databases if not covered(d.database)]
            + [("tables", t.server, t.database, list(t.tables), None) for t in selection.tables if not covered(t.database)])


SELECTED_PAGE = 1000   # the tables a selection is read from Lakebase by, one keyset page at a time


def _selected_rows(kind: str, param, selection: Selection, whole_databases: Iterable[str] = ()) -> List[dict]:
    """The tables a selection takes, read a page at a time in key order. The read stops, with a refusal, when the rows
    passed BACKGROUND_ROWS: the count before it said fewer, so the selection grew while it was read, and the request
    is sent again to be written in the background. The request holds at most BACKGROUND_ROWS rows, whatever the source
    has. whole_databases are more database keys taken whole, besides the selection's own."""
    database_keys = [q.selection_key(d.server, d.database) for d in selection.databases] + list(whole_databases)
    table_keys = [q.selection_key(t.server, t.database, name) for t in selection.tables for name in t.tables]
    rows, after = [], ("", "", -1, "", "")
    while True:
        page = db.query(q.selected_rows_page_sql(kind), (param, selection.whole_server, selection.filter, database_keys,
                                                         table_keys, *after, SELECTED_PAGE))
        if len(rows) + len(page) > BACKGROUND_ROWS:
            raise HTTPException(422, {"errors": [f"the selection grew past {BACKGROUND_ROWS:,} tables while it was "
                                                 "read: send it again, and it is written in the background"]})
        rows += page
        if len(page) < SELECTED_PAGE:
            return rows
        last = page[-1]
        after = (last["server"], last["k_database"], last["line"], last["k_schema"], last["k_table"])


def _no_tables_refusal(kind: str, param, selection: Selection) -> str:
    """Why a selection takes no tables (new_job.no_tables_refusal): the source's servers are read only here, on the
    refusal, to name its server keys when the selection names servers it doesn't have."""
    named = [d.server for d in selection.databases] + [t.server for t in selection.tables]
    if not named:
        return nj.NO_TABLES
    return nj.no_tables_refusal(named, [s["server"] for s in db.query(q.source_servers_sql(kind), (param, ""))])


def _selected(selection: Selection) -> dict:
    """What a selection takes, read back from Lakebase: {"text": the CSV of its table rows, "rows": those rows (server,
    database, schema, table, line, fields), "columns": the header the CSV is written with, "lines": each row's line in
    the uploaded file (or None), "database_lines": an upload's database-only lines ({line, database, connection}),
    "parts": the job_selection rows to keep with the job, "kind", "source_id"}. 422 when it takes neither tables nor
    database-only lines."""
    kind, param, source_id = selection.source()
    database_lines = []
    if kind == "upload":
        upload = db.query(q.upload_sql(), (param,))
        if not upload:
            raise HTTPException(404, f"upload {param} not found")
        columns = list(upload[0]["columns"])
        database_lines = [{"line": r["line"], "database": r["src_database"],
                           "connection": (r["fields"] or {}).get("src_connection") or ""}
                          for r in db.query(q.upload_database_lines_sql(), (param,))]
    else:
        columns = list(nj.DISCOVERY_COLUMNS)
    # A database-only line takes its database whole, so the file's own table rows in it are taken whatever is picked,
    # and discovery's copies of those tables are left out (_with_databases).
    whole_databases = [q.selection_key(js.server_of({"src_connection": line["connection"], "use_remote_query": True}),
                                       line["database"]) for line in database_lines]
    rows = _selected_rows(kind, param, selection, whole_databases)
    if not rows and not database_lines:
        raise HTTPException(422, {"errors": [_no_tables_refusal(kind, param, selection)]})
    whole = ([s["server"] for s in db.query(q.source_servers_sql(kind), (param, selection.filter))]
             if selection.whole_server else [])
    covered = lambda server, database: (server.lower() in {w.lower() for w in whole}
                                        and selection.filter.lower() in database.lower())
    parts = ([("server", server, None, None, selection.filter or None) for server in whole]
             + [("database", d.server, d.database, None, None) for d in selection.databases if not covered(d.server, d.database)]
             + [("tables", t.server, t.database, list(t.tables), None) for t in selection.tables
                if not covered(t.server, t.database)])
    return {"text": nj.csv_of(columns, [r["fields"] for r in rows]), "rows": rows, "columns": columns,
            "lines": [r["line"] if kind == "upload" else None for r in rows], "database_lines": database_lines,
            "parts": parts, "kind": kind, "source_id": source_id}


def _file_lines(plan: dict, selected: Optional[dict]) -> dict:
    """A plan of an upload's selection, its lines numbered as in the uploaded file (a discovered table's line is its
    database's line), with what the file's database-only lines came to."""
    if selected and selected["kind"] == "upload":
        for line in plan["lines"]:
            line["line"] = selected["lines"][line["line"] - 2]
        plan.update({k: selected[k] for k in ("covered", "discovered", "unreadable") if k in selected})
    return plan


# A file's database-only lines are read by a discovery run through the connection. The previews and the Create share
# that run for this long (per process), so the Create after a preview reads each database once.
DATABASE_READ_MEMO_S = float(os.environ.get("DATABASE_READ_MEMO_S", str(2 * 3600)))
_database_reads: dict = {}
_database_reads_lock = threading.Lock()


def _has_database_lines(selection: "Selection") -> bool:
    """Whether an upload has database-only lines: those are discovered by name, so the table counts don't cover them."""
    kind, param, _ = selection.source()
    counted = db.query(q.upload_databases_count_sql(), (param,))
    return kind == "upload" and bool(counted and counted[0]["n"])


def _resolve_databases(selected: dict, wait: bool, request: Optional[Request] = None) -> Optional[dict]:
    """The tables of a selection's database-only lines, found by name: a discovery run through the connection reads each
    database, and its tables come back as rows, as a discovery's do. None when there are no such lines, else
    {"discovering": bool, "databases": how many, "rows": the tables found ({line, server, src_database, src_schema,
    src_table, fields}), "unreadable": [{line, database, error}]}. Not waiting (a preview), a run still going answers
    discovering and the page asks again; waiting (the background Create or Add), the run is waited for."""
    lines = selected["database_lines"]
    if not lines:
        return None
    names: dict = {}
    for line in lines:
        names.setdefault(line["database"].lower(), line)
    connection = lines[0]["connection"]
    key = (connection.lower(), tuple(sorted(names)))
    read = _database_read(key)
    if read is None:
        read = _start_database_read(key, connection, [line["database"] for line in names.values()], request)
    if read["ended"] is None:
        ended = _wait_for_run(read["run_id"]) if wait else jobs.run_ended(read["run_id"])
        if ended is None:
            return {"discovering": True, "databases": len(names), "rows": [], "unreadable": []}
        read["ended"] = ended
    ended = read["ended"]
    if ended.get("result") != "SUCCESS":
        _forget_database_read(key)
        why = f"its discovery run ended {ended.get('result') or ended.get('state')}"
        if ended.get("message"):
            why += f": {ended['message']}"
        return {"discovering": False, "databases": len(names), "rows": [],
                "unreadable": [_unreadable(line, why) for line in names.values()]}
    ids = [read["discovery_id"]]
    statuses = {(s["src_database"] or "").lower(): s for s in db.query(q.discovery_statuses_sql(), (ids,))}
    unreadable = []
    for lower, line in names.items():
        status = statuses.get(lower)
        if status is None:
            unreadable.append(_unreadable(line, "the connection did not list this database"))
        elif status["status"] != "ok":
            unreadable.append(_unreadable(line, status["error"] or f"it could not be read ({status['status']})"))
    rows = [{"line": names[r["src_database"].lower()]["line"], "server": r["server"], "src_database": r["src_database"],
             "src_schema": r["src_schema"], "src_table": r["src_table"], "fields": r["fields"]}
            for r in db.query(q.source_rows_sql("discovery"), (ids,))
            if r["src_database"].lower() in names and (statuses.get(r["src_database"].lower()) or {}).get("status") == "ok"]
    return {"discovering": False, "databases": len(names), "rows": rows, "unreadable": unreadable}


def _unreadable(line: dict, error: str) -> dict:
    return {"line": line["line"], "database": line["database"], "error": error}


def _database_read(key: tuple):
    with _database_reads_lock:
        read = _database_reads.get(key)
        if read is not None and time.monotonic() - read["started"] > DATABASE_READ_MEMO_S:
            del _database_reads[key]
            read = None
        return read


def _forget_database_read(key: tuple) -> None:
    with _database_reads_lock:
        _database_reads.pop(key, None)


def _start_database_read(key: tuple, connection: str, databases: List[str], request: Optional[Request]) -> dict:
    """Starts a discovery run that reads these databases through the connection, and keeps it under the key."""
    listed = connections()
    if connection not in {c["name"] for c in listed["connections"]}:
        raise HTTPException(422, {"errors": [_connection_unseen(connection, listed.get("error"))]})
    job_id = _require(jobs.DISCOVER_JOB_ID, "discovery")
    if request is not None:
        _refuse_run_of(job_id, request)
    discovery_id = uuid.uuid4().hex[:12]
    spec = {"src_type": "sqlserver", "src_connection": connection, "kind": "tables", "databases": databases,
            "requested_by": _user(request) if request is not None else ""}
    run_id = jobs.run_now(job_id, {"discovery_id": discovery_id, "spec": json.dumps(spec)})
    read = {"run_id": run_id, "discovery_id": discovery_id, "ended": None, "started": time.monotonic()}
    with _database_reads_lock:
        _database_reads[key] = read
    return read


def _with_databases(selected: dict, resolved: Optional[dict]) -> dict:
    """The selection with the tables found for its database-only lines added after its table rows. A table the file
    names in a row of its own is left to that row (nj.discovered_tables), so the explicit row's settings are used."""
    if resolved is None:
        return selected
    kept, covered = nj.discovered_tables(resolved["rows"], {nj.table_key(row) for row in selected["rows"]})
    rows = selected["rows"] + kept
    columns = list(dict.fromkeys(selected["columns"] + list(nj.DISCOVERY_COLUMNS)))
    return {**selected, "columns": columns, "text": nj.csv_of(columns, [row["fields"] for row in rows]),
            "lines": [row["line"] for row in rows], "covered": covered, "discovered": len(kept),
            "unreadable": resolved["unreadable"]}


def _task_stage(task_id: Optional[int], stage: str) -> None:
    """A background write's stage (discovering, then writing), which its task's progress shows."""
    if task_id is not None:
        db.execute(q.task_progress_json_sql(), (json.dumps({"stage": stage}), 0, task_id))


def _keep_selection(cur, job_id: int, selected: Optional[dict], user: str) -> None:
    """In the job's transaction: what it was made from (job_selection), for a later re-discovery to follow. The drafts
    it was chosen from become the job's in the same transaction, so no purge takes them."""
    if (selected or {}).get("kind") == "discovery" and selected.get("source_id"):
        cur.execute(q.draft_claim_sql(), (int(job_id), str(selected["source_id"]).split(",")))
    parts = (selected or {}).get("parts") or []
    for at in range(0, len(parts), q.CONTROL_BATCH):
        chunk = parts[at:at + q.CONTROL_BATCH]
        cur.execute(q.insert_job_selection_sql(len(chunk)), tuple(
            v for scope, server, database, tables, filter_ in chunk
            for v in (int(job_id), scope, server, database, tables, filter_, selected["kind"], selected["source_id"],
                      user or None)))


@app.get("/api/ingestion-jobs/{job_id}/selection")
def job_selection(job_id: int):
    """What a job was made from, part by part: whole servers, whole databases, chosen tables."""
    return db.query(q.job_selection_sql(), (int(job_id),))


# Table states change at run cadence, so the one per-database scan they come from is shared for longer
# than running progress is.
STATES_CACHE_S = max(0, int(os.environ.get("STATES_CACHE_S", "15")))
# How often the group names with a control row are re-read (a scan of the control index), for groups with no rollup yet.
CONTROL_GROUPS_S = 600


def _states() -> List[dict]:
    """The database rows the monitoring views sum: each database's rollup row (queries.rollup_states_sql), read once
    and shared for STATES_CACHE_S. A group with no rollup row yet (its rows predate the rollups and no write or check
    has touched it) is counted from its control rows until one does."""
    return _shared("database_states", _load_states, ttl=STATES_CACHE_S)


def _load_states() -> List[dict]:
    rows = db.query(q.rollup_states_sql())
    written = {r["task_collection"] for r in db.query(q.rollup_group_names_sql())}
    unwritten = sorted(_control_groups() - written)
    if unwritten:
        rows += [rollup.from_scan(r) for r in db.query(q.database_states_for_groups_sql(), (unwritten,))]
    return rows


def _control_groups() -> set:
    """Every ingestion group with a control row, re-read at most every CONTROL_GROUPS_S seconds."""
    return _shared("control_groups",
                   lambda: {r["task_collection"] for r in db.query(q.control_group_names_sql())}, ttl=CONTROL_GROUPS_S)


def _last_runs() -> dict:
    """Each ingestion group's latest run, by group, shared as often as _running, so a run that has finished
    is not still shown running beside an empty running list."""
    return _shared("last_runs", lambda: {r["task_collection"]: r for r in db.query(q.last_runs_sql())})


def _read_at(key) -> str:
    """When the shared read under key was made: now, when it is not cached."""
    hit = _shared_reads.get(key)
    return datetime.fromtimestamp(hit[1] if hit else time.time(), timezone.utc).isoformat()


def _running() -> List[dict]:
    """queries.running_progress_sql, shared: every running group run's progress, read once per
    MONITOR_CACHE_S however many pages ask. Until the run poller's table exists, runs whose Databricks run has
    ended are closed here first (_close_ended_runs); after that the poller closes them."""
    def compute():
        if not run_poller.table_ready():
            try:
                _close_ended_runs()
            except Exception:           # the pages still show what is recorded; the next read tries again
                pass
        return db.query(q.running_progress_sql())
    return _shared("running", compute)


def _close_ended_runs() -> int:
    """Close each run recorded as running whose Databricks run has ended, while the poller's table does not exist yet
    (the poller does it once it does, run_poller.closure). A run with no job run id, or whose state can't be read now,
    or still going in Databricks, is left as it is; nothing is ever cancelled here. Returns how many were closed."""
    closed = 0
    now = datetime.now(timezone.utc)
    for r in db.query(q.open_runs_sql()):
        job_run = str(r.get("job_run_id") or "").strip()
        if not job_run.isdigit():
            continue
        try:
            ended = jobs.run_ended(job_run)
        except Exception:
            continue
        if not ended:
            continue
        state_row = {"life_cycle_state": ended["state"], "result_state": ended.get("result"),
                     "state_message": ended.get("message"), "ended_at": ended.get("ended")}
        close = run_poller.closure(state_row, r.get("started_at"), int(job_run), now)
        if close is None:
            continue
        with _write() as cur:
            run_poller.write_close(cur, int(r["run_id"]), close)
        closed += 1
    return closed


# The most running job runs, and jobs needing attention, the Overview lists; Runs and Ingestion jobs list all.
OVERVIEW_LIST = 5
OVERVIEW_JOBS_PAGE = 25


def _rows_written_24h() -> dict:
    """The rows every group wrote in the last 24 hours ({rows, partial}: partial when some loads recorded no count),
    their table loads' and their batched appends' together; shared like _states. The groups only deleted jobs hold
    (_deleted_groups) are left out, as the Overview counts live jobs only."""
    def compute():
        deleted = _deleted_groups()
        groups = sorted({s["task_collection"] for s in _states()} - deleted)
        if not groups:
            return {"rows": 0, "partial": False}
        loads = next(iter(db.query(q.rows_written_since_sql(), (groups, 24))), {})
        batched = [q.batch_rows(r["detail"]) for r in db.query(q.run_batch_rows_sql(), (24,))
                   if r.get("task_collection") not in deleted]
        return {"rows": int(loads.get("rows_written") or 0) + sum(rows for rows, _ in batched),
                "partial": bool(loads.get("uncounted")) or any(unknown for _, unknown in batched)}
    return _shared("rows_written_24h", compute, ttl=STATES_CACHE_S)


@app.get("/api/overview")
def overview(offset: int = 0):
    """The status summary: the running job runs and the jobs needing attention (OVERVIEW_LIST of each, with their
    totals), the totals over every enabled table with the rows written in 24 hours, and the next scheduled triggers,
    from the shared reads (_jobs_view). The ingestion jobs (a page of OVERVIEW_JOBS_PAGE, failing first) and "Not in
    any job" come too, for the totals' links."""
    view = _jobs_view()
    offset = max(0, int(offset))
    upcoming = sorted((j for j in view["jobs"] if j.get("next_start")), key=lambda j: j["next_start"])
    return {"next_triggers": [{k: j.get(k) for k in ("id", "name", "next_start", "description")}
                              for j in upcoming[:OVERVIEW_LIST]],
            "paused_jobs": sum(1 for j in view["jobs"] if j.get("paused")), "rows_24h": _rows_written_24h(),
            "jobs": view["jobs"][offset:offset + OVERVIEW_JOBS_PAGE], "jobs_total": len(view["jobs"]),
            "offset": offset, "limit": OVERVIEW_JOBS_PAGE, "not_in_job": view["not_in_job"],
            "running_job_runs": view["running_job_runs"][:OVERVIEW_LIST], "running_total": len(view["running_job_runs"]),
            "running_not_in_job": view["running_not_in_job"],
            "attention": view["attention"][:OVERVIEW_LIST], "attention_total": len(view["attention"]),
            "totals": view["totals"], "read_at": _read_at("database_states"), "cache_s": MONITOR_CACHE_S,
            "states_cache_s": STATES_CACHE_S}


# The most failed tables the failure groups read; past it they say how many they show.
ERROR_TABLES_LIMIT = 20000


@app.get("/api/errors/groups")
def error_groups(hours: int = 24):
    """Failed tables in the last `hours`, grouped by cause (queries.group_errors). The newest
    ERROR_TABLES_LIMIT tables at most; `truncated` says when more failed."""
    hours = max(1, min(int(hours), 168))

    def compute():
        rows = db.query(q.failed_tables_sql(), (hours, ERROR_TABLES_LIMIT + 1))
        tables = list(dict.fromkeys(r["control_id"] for r in rows))
        kept = set(tables[:ERROR_TABLES_LIMIT])
        shown = [r for r in rows if r["control_id"] in kept]
        return {"groups": q.group_errors(shown), "tables_shown": len(kept), "truncated": len(tables) > len(kept)}
    return _shared(("errors", hours), compute)


@app.get("/api/collections/{name}/databases")
def collection_databases(name: str):
    """A collection's source databases: state now, this run's progress, rows in first loads and in
    changes (24 h), and batched appends' totals placed on the databases they loaded."""
    def compute():
        rows = {q.server_database_key(r["server"], r["src_database"]): r for r in db.query(q.collection_database_rows_sql(), (name,))}
        running = [r for r in _running() if r["task_collection"] == name]
        batches = []
        for b in db.query(q.collection_batch_runs_sql(), (name, name)):
            total, unknown = q.batch_rows(b["detail"])
            if total or unknown:
                batches.append({"run_id": b["run_id"], "rows": total, "rows_unknown": unknown,
                                "databases": sorted(b["databases"] or []), "keys": sorted(b["keys"] or [])})
        databases = []
        for d in db.query(q.collection_databases_sql(), (name, name)):
            key = q.server_database_key(d["server"], d["src_database"])
            r = rows.get(key, {})
            d.update({k: r.get(k) for k in ("rows_first_loads", "first_loads", "first_loads_uncounted",
                                             "rows_changes_24h", "loaded_this_run", "in_flight", "failed_this_run")})
            d["batches"] = [{"run_id": b["run_id"], "rows": b["rows"]} for b in batches
                            if b["keys"] == [key]]
            databases.append(d)
        schedules, members = _schedules()
        job_names = [{"id": sc["id"], "name": sc["name"]} for sc in schedules
                     if any(m["schedule_id"] == sc["id"] and m["task_collection"] == name for m in _active(members))]
        return {"collection": name, "databases": databases, "running": running[0] if running else None, "jobs": job_names,
                "batches": batches, "read_at": datetime.now(timezone.utc).isoformat(), "cache_s": MONITOR_CACHE_S}
    return _shared(("collection", name), compute)


@app.get("/api/errors")
def errors(hours: int = 24, limit: int = 200):
    return db.query(q.recent_errors_sql(), (int(hours), int(limit)))


@app.get("/api/collections")
def collections():
    return [r["task_collection"] for r in db.query(q.collections_sql())]


# ---- ingestion jobs and job runs ----------------------------------------------------------------

def _schedules():
    """Active schedules, the deleted ones whose Delete is pending, and all their members, removed ones included (a
    member's past runs still belong to the job runs of its time in the schedule), shared like _running. _active keeps
    the current members. A pending deleted job carries its delete state (ingestion_jobs.delete_state)."""
    def compute():
        rows = _with_delete_states(db.query(q.schedules_sql()), datetime.now(timezone.utc))
        return rows, db.query(q.schedule_members_sql(), ([r["id"] for r in rows],)) if rows else []
    return _shared("schedules", compute)


def _with_delete_states(rows: List[dict], now: datetime) -> List[dict]:
    """The rows with each deleted job's delete state (ingestion_jobs.delete_state). One whose purge finished (the delete
    job succeeded and no tables are left) is marked ``delete_finished`` and leaves the list. Its own marking does this
    too; this catches a delete job run from before it did."""
    deleted = [r for r in rows if r.get("deleted_at") is not None]
    if not deleted:
        return rows
    left = {r["schedule_id"] for r in db.query(q.deleted_tables_left_sql(), ([r["id"] for r in deleted],))}
    out = []
    for r in rows:
        if r.get("deleted_at") is None:
            out.append(r)
            continue
        state = ij.delete_state(r, r["id"] in left, _delete_run(r), now, jobs.DELETED_JOB_RETENTION_DAYS)
        if state["state"] == ij.DELETE_GONE:
            with db.transaction() as cur:
                cur.execute(q.set_schedule_flag_sql(), (["delete_finished"], True, int(r["id"])))
            continue
        out.append({**r, "delete": state})
    return out


def _deleted_groups() -> set:
    """The groups of deleted jobs kept for a Restore (queries.deleted_groups_sql), shared like _schedules: they stay
    with their job, under Deleted jobs, until the purge removes them."""
    return _shared("deleted_groups", lambda: {r["task_collection"] for r in db.query(q.deleted_groups_sql())})


def _live_states() -> List[dict]:
    """The shared table states (_states) without the groups only deleted jobs hold (_deleted_groups): the databases
    and tables the pages count, since a deleted job's are shown under Deleted jobs only."""
    deleted = _deleted_groups()
    return [s for s in _states() if s["task_collection"] not in deleted]


def _active(members: List[dict]) -> List[dict]:
    return [m for m in members if m.get("removed_at") is None]


def _group_stats() -> dict:
    """Each ingestion group's run-time percentiles and strictest freshness target, shared like _states."""
    def compute():
        names = sorted({s["task_collection"] for s in _states()})
        rows = db.query(q.collection_run_stats_sql(), (names, names, names, names)) if names else []
        return {r["task_collection"]: r for r in rows}
    return _shared("group_stats", compute, ttl=STATES_CACHE_S)


def _group_runs(groups: List[str], start: datetime, end: datetime) -> List[dict]:
    """The group runs of some groups that started in [start, end), with their work and outcome."""
    if not groups:
        return []
    return _with_cancelling(_with_rows_written(db.query(q.group_runs_sql(), (sorted(groups), start, end))))


def _with_cancelling(runs: List[dict]) -> List[dict]:
    """The running group runs whose Databricks run a Cancel asked to stop, marked cancelling until the poller
    closes them (queries.job_run_outcome reads the mark). Every page that places group runs reads them here."""
    ids = sorted({int(r["job_run_id"]) for r in runs
                  if r.get("status") == "running" and str(r.get("job_run_id") or "").isdigit()})
    if not ids or not run_poller.table_ready():
        return runs
    asked = {str(row["run_id"]) for row in db.query(q.cancel_requested_runs_sql(), (ids,))}
    return [dict(r, cancelling=True) if r.get("status") == "running" and str(r.get("job_run_id") or "") in asked
            else r for r in runs]


def _schedule(job_id: int) -> dict:
    schedules, _ = _schedules()
    found = next((s for s in schedules if s["id"] == int(job_id)), None)
    if not found:
        raise HTTPException(404, f"ingestion job {job_id} not found")
    return found


def _runs_window(job: dict, start: datetime, end: datetime):
    """The window of group runs that job runs with slots in [start, end) are placed from: two slots before `start`
    (ij.place starts a slot early, and an earlier run still going at a trigger makes it skipped), and up to two
    cadences after `end` (a member's offset, then up to its next trigger)."""
    cadence = int(job["cadence_minutes"])
    return start - timedelta(minutes=2 * max(cadence, 60)), end + timedelta(minutes=2 * cadence)


def _job_runs_of(job: dict, start: datetime, end: datetime, now: datetime,
                 runs: Optional[List[dict]] = None) -> List[dict]:
    """A job's job runs whose slot started in [start, end), newest first, as ij.summarize shows them, with
    their placed members, from `runs` when given (read for _runs_window), else read here."""
    _, members = _schedules()
    mine = [m for m in members if m["schedule_id"] == job["id"]]
    cadence = int(job["cadence_minutes"])
    if runs is None:
        runs = _group_runs([m["task_collection"] for m in mine], *_runs_window(job, start, end))
    placed = [jr for jr in ij.place(job, mine, runs, start, end + timedelta(minutes=cadence), now)
              if start <= jr["slot"] < end and jr["slot"] <= now]
    running = {r["run_id"]: r for r in _running()}
    tables = {name: g["enabled_tables"] for name, g in rollup.groups(_states()).items()}
    p50 = {g: st.get("p50_s") for g, st in _group_stats().items()}
    return [dict(ij.summarize(job, jr, running, tables, p50, now), placed=jr) for jr in placed]


def _last_ran(jobs: List[dict], now: datetime) -> dict:
    """Each job's last job run that ran, however long ago, by job id (jobs whose recent triggers did not run): each
    member's newest finished group run while it was the member (one read for every job), placed into their job
    runs, the newest slot summarized (ij.earlier_last) from one more read of the runs around every job's slot. A
    job that never ran has none."""
    _, members = _schedules()
    candidates = {}
    for r in db.query(q.last_ran_group_runs_sql(), (sorted(j["id"] for j in jobs),)) if jobs else []:
        candidates.setdefault(r["schedule_id"], []).append(r)
    newest = {j["id"]: slots[0] for j in jobs for slots in [ij.newest_slots(candidates.get(j["id"]) or [], j, members)]
              if slots}
    windows = [(m["task_collection"], *_runs_window(j, newest[j["id"]], newest[j["id"]] + timedelta(seconds=1)))
               for j in jobs if j["id"] in newest for m in members if m["schedule_id"] == j["id"]]
    groups, starts, ends = (list(column) for column in zip(*windows)) if windows else ([], [], [])
    runs = (_with_cancelling(_with_rows_written(db.query(q.group_runs_in_windows_sql(), (groups, starts, ends))))
            if windows else [])
    found = {}
    for job in jobs:
        if job["id"] not in newest:
            continue
        slot = newest[job["id"]]
        start, end = _runs_window(job, slot, slot + timedelta(seconds=1))
        mine = {m["task_collection"] for m in members if m["schedule_id"] == job["id"]}
        around = [r for r in runs if r["task_collection"] in mine and start <= q._when(r["started_at"]) < end]
        last = ij.earlier_last(_job_runs_of(job, slot, slot + timedelta(seconds=1), now, runs=around))
        if last:
            found[job["id"]] = last
    return found


def _jobs_view() -> dict:
    """ingestion_jobs.jobs_view over the shared reads, with each failing job's causes from its latest job run
    with failed tables; shared like _running."""
    def compute():
        now = datetime.now(timezone.utc)
        schedules, members = _schedules()
        states, running, stats = _states(), _running(), _group_stats()
        groups = sorted({m["task_collection"] for m in members})
        start = min([ij.window(int(s["cadence_minutes"]), ij.RECENT_JOB_RUNS + 1, now) for s in schedules]
                    + [q._when(r["started_at"]) - timedelta(days=1) for r in running if r.get("started_at")], default=now)
        runs = _group_runs(groups, start, now + timedelta(seconds=1))
        deleting = _deleted_groups()
        view = ij.jobs_view(schedules, members, states, running, runs, stats, now, deleting=deleting)
        by_id = {s["id"]: s for s in schedules}
        # A job none of whose recent triggers ran (paused overnight, say): its last job run is found before them.
        earlier = _last_ran([by_id[j["id"]] for j in view["jobs"] if j["id"] is not None and not j["last_job_run"]], now)
        latest_ids = {}
        for job in view["jobs"]:
            last = job["last_job_run"] or earlier.get(job["id"])
            latest = next((jr for jr in (job["running_job_run"], last) if jr and jr["failed_tables"]), None)
            if job["state_failing"] and latest and latest["run_ids"]:
                latest_ids[job["id"]] = set(latest["run_ids"])
        causes = {}
        if latest_ids:      # one read for every failing job's latest job run, split by job
            failures = db.query(q.run_failures_sql(), (sorted(set().union(*latest_ids.values())),))
            causes = {job_id: q.group_errors([f for f in failures if f["run_id"] in ids]) for job_id, ids in latest_ids.items()}
        if causes or earlier:
            view = ij.jobs_view(schedules, members, states, running, runs, stats, now, causes_by_job=causes,
                                earlier_last_by_job=earlier, deleting=deleting)
        return view
    return _shared("jobs_view", compute)


JOB_FILTERS = ("all", "running", "failing", "late", "paused", "attention")
# The sums a group row shows: its databases, enabled tables, late databases, and its tables by state (rollup.STATES).
GROUP_SUMS = ("databases", "enabled_tables", "late_databases") + rollup.STATES


def _job_filter(job: dict, name: str) -> bool:
    return {"all": True, "running": bool(job.get("running_job_run")), "failing": bool(job["state_failing"]),
            "late": bool(job["late_databases"]), "paused": bool(job.get("paused")),
            "attention": ij.needs_attention(job)}[name]


# Columns the Ingestion jobs list sorts by, besides its own order (failing first, then late, then by name).
JOB_SORTS = ("created_at", "updated_at")


def _audit(schedule: dict) -> dict:
    """Who created a job and who last changed it (it or its groups' tables), and when; None where not recorded."""
    return {"created_by": schedule.get("created_by"), "created_at": schedule.get("created_at"),
            "updated_by": sch.updated_by(schedule), "updated_at": schedule.get("updated_at")}


@app.get("/api/ingestion-jobs")
def ingestion_jobs(filter: str = "all", search: str = "", offset: int = 0, limit: int = 25, sort: str = "",
                   descending: bool = True):
    """Every ingestion job, as on the Overview, filtered (each filter with its count) and searched on job and
    group names, a page at a time; "Not in any job" comes apart, under All. ``sort`` (created_at or updated_at)
    orders the whole list by that time, unknown times last."""
    if filter not in JOB_FILTERS:
        raise HTTPException(400, f"filter must be one of {', '.join(JOB_FILTERS)}")
    if sort and sort not in JOB_SORTS:
        raise HTTPException(400, f"sort must be one of {', '.join(JOB_SORTS)}")
    view = _jobs_view()
    audits = {s["id"]: _audit(s) for s in _schedules()[0]}
    needle = search.strip().lower()
    found = [{**j, **audits.get(j["id"], {})} for j in view["jobs"]
             if not needle or needle in str(j["name"]).lower() or any(needle in g.lower() for g in j["groups"])]
    counts = {f: sum(1 for j in found if _job_filter(j, f)) for f in JOB_FILTERS}
    rows = [j for j in found if _job_filter(j, filter)]
    if sort:
        known = sorted((j for j in rows if j.get(sort)), key=lambda j: q._when(j[sort]), reverse=descending)
        rows = known + [j for j in rows if not j.get(sort)]
    not_in_job = view["not_in_job"]
    shown_apart = filter == "all" and not_in_job and (not needle or any(needle in g.lower() for g in not_in_job["groups"]))
    offset, limit = max(0, int(offset)), max(1, min(int(limit), 100))
    return {"jobs": rows[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit,
            "not_in_job": not_in_job if shown_apart else None, "counts": counts, "read_at": _read_at("jobs_view"),
            "cache_s": MONITOR_CACHE_S}


@app.get("/api/ingestion-jobs/not-in-job")
def groups_not_in_job():
    """The groups in no ingestion job, as a page of their own: their tables by state and each group's last run, so a
    deployment with no jobs (a read-only share) still opens its groups and tables from the jobs list."""
    schedules, members = _schedules()
    _, not_in_job = rollup.jobs(_states(), schedules, members, _deleted_groups())
    not_in_job = not_in_job or {"groups": []}
    groups, stats, last_runs = rollup.groups(_states()), _group_stats(), _last_runs()
    rows = []
    for name in sorted(not_in_job["groups"]):
        g, st, last = groups.get(name) or {}, stats.get(name) or {}, last_runs.get(name)
        rows.append({"task_collection": name, **{k: g.get(k) for k in GROUP_SUMS},
                     "last_run": dict(last, outcome=q.run_outcome(last)) if last else None,
                     "p95_s": st.get("p95_s"), "p50_s": st.get("p50_s")})
    return {**not_in_job, "name": "Not in any job", "groups_detail": rows}


@app.get("/api/ingestion-jobs/deletions")
def job_deletions(request: Request, offset: int = 0, limit: int = 0, rows: bool = True):
    """The deleted jobs kept for a Restore (not yet purged), newest deletion first, a page at a time (limit 0:
    DELETIONS_LISTED), and how many there are; with rows false only how many, for the closed list's header. Each row has
    its delete state (kept with the days left, deleting, or failed with the reason), whether it asked to drop its target
    tables, how many of its tables are left, and why it can't be restored or purged now (delete_refusal)."""
    user = _user(request)
    now = datetime.now(timezone.utc)
    limit = min(max(int(limit or DELETIONS_LISTED), 1), DELETIONS_PAGE_MAX)
    offset = max(int(offset), 0)
    total = int(db.query(q.deleted_jobs_count_sql())[0]["total"])
    deleted = []
    for s in _with_delete_states(db.query(q.deleted_jobs_sql(), (limit, offset)), now) if rows else []:
        plan = _delete_plan(s["id"])
        deleted.append({"id": s["id"], "name": s["name"], "deleted_at": s["deleted_at"],
                        "drop_targets": bool(sch.options_of(s).get("delete_drop_targets")), "delete": s["delete"],
                        "tables_left": plan["tables"], "groups_left": len(plan["groups"]),
                        "delete_refusal": _delete_refusal(s, plan["groups"], user)})
    return {"deleted": deleted, "total": total, "offset": offset, "limit": limit,
            "configured": bool(jobs.DELETE_JOB_ID), "retention_days": jobs.DELETED_JOB_RETENTION_DAYS}


@app.get("/api/ingestion-jobs/{job_id}/delete-preview")
def delete_preview(job_id: int):
    """What Delete would remove, for its dialog: the groups it deletes and those it keeps (another job holds them),
    their tables, the target tables only they write, and the target tables another job's rows also write (kept and
    named); and why it can't be deleted now, if so."""
    job, members = _load_schedule(job_id)
    active = [m for m in members if m["removed_at"] is None]
    plan = _delete_plan(job_id)
    kept_groups = sorted({m["task_collection"] for m in active} - set(plan["groups"]))
    blocker = _delete_blocker(job, active, plan["groups"])
    if not blocker and plan["tables"] and not jobs.DELETE_JOB_ID:
        blocker = NO_DELETE_JOB
    return {**plan, "groups_kept": kept_groups, "configured": bool(jobs.DELETE_JOB_ID),
            "retention_days": jobs.DELETED_JOB_RETENTION_DAYS,
            "blocked": blocker.get("message") if isinstance(blocker, dict) else blocker}


@app.get("/api/permissions")
def permissions(request: Request):
    """What the signed-in user may do, from their Databricks permissions, so each page can show a button disabled
    with the reason (every action checks again on its own call): per ingestion job, run, change and delete; per
    ingestion group in a job, changing its tables (groups in no job are open); and running each of the bundle's
    jobs the app starts by hand."""
    user = _user(request)
    schedules = db.query(q.schedules_sql())
    members = db.query(q.schedule_members_sql(), ([s["id"] for s in schedules],)) if schedules else []
    by_job = {s["id"]: _permission_refusals(s, user) for s in schedules}
    groups: dict = {}
    for s in schedules:
        for m in members:
            if m["schedule_id"] == s["id"] and m["removed_at"] is None and by_job[s["id"]]["change"]:
                groups.setdefault(m["task_collection"],
                                  f"these tables are in the ingestion job {s['name']}: {by_job[s['id']]['change']}")
    bundle = {name: (_databricks_job_refusals(job_id, user)["run"] if job_id else f"no {name} job is configured")
              for name, job_id in (("ingestion", jobs.INGEST_JOB_ID), ("discovery", jobs.DISCOVER_JOB_ID))}
    return {"jobs": {str(sid): refusals for sid, refusals in by_job.items()}, "groups": groups, "run": bundle,
            "new_running_job": {variant: _permission_refusals({"job_variant": variant}, user)["run"]
                                for variant in jobs.schedule_variants()},
            "manage": _manage_refusal(user)}


@app.get("/api/ingestion-jobs/{job_id}")
def ingestion_job(job_id: int, request: Request):
    """One ingestion job's details: its schedule and members (wrapper jobs, next start), its tables by
    state, its last job run that ran (however long ago), the median speed over its last 24 job runs, its p95 job
    run time over 7 days, and its groups with their last group run and p95 run time. Job runs are placed once over
    7 days; the speed is over the newest 24 finished ones."""
    job = _schedule(job_id)
    now = datetime.now(timezone.utc)
    _, members = _schedules()
    mine = [m for m in _active(members) if m["schedule_id"] == job["id"]]
    sums = next(j for j in rollup.jobs(_states(), [job], members)[0])
    stats, last_runs = _group_stats(), _last_runs()
    cadence = int(job["cadence_minutes"])
    start = sch.start_minute(job)
    finished = [r for r in _job_runs_of(job, now - timedelta(days=7), now + timedelta(seconds=1), now)
                if r["outcome"]["label"] not in q.RUNNING_OUTCOMES]
    week = [r["took_s"] for r in finished if r["took_s"] is not None]
    groups = rollup.groups(_states())
    group_rows = []
    for m in sorted(mine, key=lambda m: (sch.stagger_of(m["offset_minutes"], cadence, start), m["task_collection"])):
        g, st, last = groups.get(m["task_collection"]) or {}, stats.get(m["task_collection"]) or {}, last_runs.get(m["task_collection"])
        group_rows.append({"task_collection": m["task_collection"], "offset_minutes": m["offset_minutes"],
                           "starts": sch.member_start(cadence, int(m["offset_minutes"])),
                           **{k: g.get(k) for k in GROUP_SUMS},
                           "last_run": dict(last, outcome=q.run_outcome(last)) if last else None,
                           "p95_s": st.get("p95_s"), "p50_s": st.get("p50_s"),
                           "job_url": (f"{db.workspace().config.host.rstrip('/')}/jobs/{m['job_id']}"
                                       if m.get("job_id") else None)})
    speeds = sorted(r["speed_tables_per_min"] for r in finished[:24] if r["speed_tables_per_min"] is not None)
    offsets = [sch.stagger_of(m["offset_minutes"], cadence, start) for m in mine]
    last = ij.last_finished(finished)
    last = {k: v for k, v in last.items() if k != "placed"} if last else _last_ran([job], now).get(job["id"])
    next_starts = [sch.next_run(cadence, int(m["offset_minutes"]), job.get("timezone") or "UTC", now) for m in mine]
    refusals = _permission_refusals(job, _user(request))
    return {**{k: v for k, v in sums.items()},
            "description": sch.describe(cadence, sch.start_minute(job), job.get("timezone") or "UTC"),
            "stagger_from": min(offsets) if offsets else None, "stagger_to": max(offsets) if offsets else None,
            "parameters": job.get("parameters"), "job_variant": job.get("job_variant"), "paused": job.get("paused"),
            "last_job_run": last,
            "next_start": min(next_starts).isoformat() if next_starts and not job.get("paused") else None,
            "median_speed_tables_per_min": speeds[len(speeds) // 2] if speeds else None, "speed_job_runs": len(speeds),
            "p95_took_s": sorted(week)[min(len(week) - 1, int(0.95 * len(week)))] if week else None,
            "groups_detail": group_rows, "read_at": now.isoformat(),
            # For the Run now button: why it can't start now (None when it can), whether a run is going (the button
            # is then Cancel run, which the run permission also gates), and what one run starts.
            "run_now": {"blocked": refusals["run"] or _run_now_blocker(job, mine), "refused": refusals["run"],
                        "groups": len(mine), "check": job.get("job_variant") == sch.CHECK_VARIANT,
                        **_run_phase_of(job, mine, now)},
            "delete_blocked": refusals["delete"], "change_blocked": refusals["change"],
            # Where the job stands until it can run (preparing.summary); its page reads the rest from /prepare.
            "prepare": preparing.summary(sch.options_of(job)),
            "delete": job.get("delete"),
            "delete_refusal": (_delete_refusal(job, _delete_plan(job["id"])["groups"], _user(request))
                               if job.get("delete") else None),
            "rediscovery": _rediscovery_view(job, now), **_audit(job)}


def _job_groups(job_id: int) -> List[str]:
    """An ingestion job's current groups, read fresh (not from the shared schedule cache) since an edit writes
    their rows."""
    if not db.query(q.schedule_sql(), (int(job_id),)):
        raise HTTPException(404, f"ingestion job {job_id} not found")
    return sorted({m["task_collection"] for m in _active(db.query(q.schedule_members_sql(), ([int(job_id)],)))})


def _lock_job(cur, job_id: int) -> List[str]:
    """Inside an applying transaction: wait for the job's schedule lock (held by member changes and syncs too),
    then read its current groups, so a group added or removed meanwhile is never edited from a stale list."""
    cur.execute(q.schedule_lock_sql(), (q.SCHEDULE_LOCK_CLASS, q.schedule_lock_key(job_id)))
    members = db.fetch(cur, q.schedule_members_sql(), ([int(job_id)],))
    return sorted({m["task_collection"] for m in members if m["removed_at"] is None})


def _started_job(job: dict) -> Optional[str]:
    """The Databricks job an ingestion job's runs execute as: the check job on a check schedule, else its variant's
    ingestion job. Every action on the ingestion job follows the signed-in user's permission on it."""
    if (job.get("job_variant") or "default") == sch.CHECK_VARIANT:
        return jobs.CHECK_JOB_ID
    copy = sch.copy_job_id(job)
    return str(copy) if copy else jobs.ingest_variants().get(job.get("job_variant") or "default")


# Who the user is comes from the Apps proxy's X-Forwarded-Email. Without it nothing can be checked, so every
# permission check refuses.
NO_IDENTITY = "the app can't tell who you are (the Databricks Apps proxy sent no user), so it refuses this"


DELETED_JOB = ("this job is deleted: it can't be run or edited until it is restored; Restore brings it back paused, "
               "and Delete now purges it for good")


def _permission_refusals(job: dict, user: str) -> dict:
    """{action: why the user may not take it, or None} for each action on an ingestion job (run, change, delete),
    from their Databricks permission on the job its runs execute as. A deleted job can't be run or changed; its
    delete stays open, for Restore and Delete now."""
    refusals = _job_permission_refusals(job, user)
    if job.get("deleted_at") is None:
        return refusals
    return {**refusals, "run": DELETED_JOB, "change": DELETED_JOB}


def _job_permission_refusals(job: dict, user: str) -> dict:
    if not (user or "").strip():
        return {action: NO_IDENTITY for action in jobs.ACTION_NEEDS}
    started = _started_job(job)
    if not started:
        # An orphaned job (its variant no longer configured) has no Databricks job to ask; admins may clean it up.
        reason = "no Databricks job is configured for this job's runs"
        try:
            admin = "admins" in jobs.user_groups(user)
        except Exception:
            admin = False
        admin_only = None if admin else f"{reason}, so only a workspace admin can change or delete it"
        return {"run": reason, "change": admin_only, "delete": admin_only}
    refusals = _databricks_job_refusals(started, user)
    return {action: refusals[action] for action in jobs.ACTION_NEEDS}


def _databricks_job_refusals(job_id, user: str) -> dict:
    """{action: why the user may not take it on this Databricks job, or None}."""
    if not (user or "").strip():
        return {action: NO_IDENTITY for action in jobs.ACTION_NEEDS}
    label = f"the Databricks job {job_id}"
    try:
        level = jobs.user_job_permission(job_id, user)
    except Exception as e:  # shown on the page and refused; Databricks decides, so no answer means no
        reason = f"can't read your permission on {label}: {str(e).split('. ')[0]}"
        return {action: reason for action in jobs.ACTION_NEEDS}
    return {action: jobs.action_refusal(action, level, label) for action in jobs.ACTION_NEEDS}


def _signed_in_refusal(user: str) -> Optional[str]:
    """Why a call with no signed-in user may not change a JDBC connection or login, or None. Beyond this, the
    checks on the ingestion jobs that read the key decide."""
    return None if (user or "").strip() else NO_IDENTITY


def _manage_refusal(user: str) -> Optional[str]:
    """Why the user may not manage the app (Manage: its version, the export, the Databricks jobs), or None. It is the
    app's own Databricks permission, Can Manage on the app (workspace admins have it), not one of the app's."""
    if not (user or "").strip():
        return NO_IDENTITY
    if not jobs.APP_NAME:
        return "the app's name is not set here (DATABRICKS_APP_NAME), so who manages it can't be read"
    try:
        level = jobs.user_app_permission(user)
    except Exception as e:  # Databricks decides, so no answer means no
        return f"can't read your permission on the app {jobs.APP_NAME}: {str(e).split('. ')[0]}"
    if jobs.PERMISSION_RANK.get(level or "", 0) >= jobs.PERMISSION_RANK["CAN_MANAGE"]:
        return None
    have = f"you have {jobs.PERMISSION_LABEL.get(level, level)}" if level else "you have no access to it"
    return (f"managing the app needs Can Manage on the app {jobs.APP_NAME}, or a place in its managers (the bundle's "
            f"app_managers); {have}")


def _refuse(refusal: Optional[str]) -> None:
    if refusal:
        raise HTTPException(403, refusal)


def _refuse_job(job: dict, request: Request, action: str) -> None:
    """Refuse the call unless the signed-in user may take this action on the ingestion job."""
    _refuse(_permission_refusals(job, _user(request))[action])


def _refuse_run_of(job_id, request: Request) -> None:
    """Refuse starting (or cancelling) a run of one of the bundle's Databricks jobs without Can Manage Run on it."""
    _refuse(_databricks_job_refusals(job_id, _user(request))["run"])


def _groups_refusal(groups: Iterable[str], user: str) -> Optional[str]:
    """Why the user may not change tables of these ingestion groups, or None: every ingestion job holding one of them
    needs their Can Manage. Groups in no job are open, as anyone may set up a new job. Read fresh, not shared."""
    wanted = {g for g in groups if g}
    if not wanted:
        return None
    if not (user or "").strip():
        return NO_IDENTITY
    schedules = db.query(q.schedules_sql())
    members = db.query(q.schedule_members_sql(), ([s["id"] for s in schedules],)) if schedules else []
    holding = {m["schedule_id"] for m in members if m["removed_at"] is None and m["task_collection"] in wanted}
    for s in schedules:
        if s["id"] in holding:
            refusal = _permission_refusals(s, user)["change"]
            if refusal:
                return f"these tables are in the ingestion job {s['name']}: {refusal}"
    return None


def _refuse_groups(groups: Iterable[str], request: Request) -> None:
    _refuse(_groups_refusal(groups, _user(request)))
    _refuse_unready_groups(groups, preparing.tables_refusal)


def _refuse_unready(job: dict, refusal_of) -> None:
    """Refuse (409) what a job may not do until it is Ready, saying where it stands: refusal_of is one of preparing's
    refusals (run_refusal, turn_on_refusal, tables_refusal, settings_refusal). The detail carries the reason
    preparing.NOT_READY_REASON, and the job's state and stage."""
    options = sch.options_of(job)
    why = refusal_of(options)
    if why:
        raise HTTPException(409, {"message": why, "reason": preparing.NOT_READY_REASON,
                                  "state": preparing.state_of(options), "stage": preparing.stage_of(options)})


def _refuse_unready_groups(groups: Iterable[Optional[str]], refusal_of) -> None:
    """_refuse_unready for every job holding one of these groups: a table route that names groups, not a job, never
    reaches a job that isn't Ready (its prepare task writes them). Read fresh, not shared."""
    wanted = {g for g in groups if g}
    if not wanted:
        return
    schedules = db.query(q.schedules_sql())
    members = db.query(q.schedule_members_sql(), ([s["id"] for s in schedules],)) if schedules else []
    holding = {m["schedule_id"] for m in members if m["removed_at"] is None and m["task_collection"] in wanted}
    for s in schedules:
        if s["id"] in holding:
            _refuse_unready(s, refusal_of)


def _refuse_rows_groups(ids: List[int], request: Request, also: Iterable[Optional[str]] = ()) -> set:
    """Refuse a change to these control rows unless the user may change every job running their groups (and the
    groups in `also`). Read and checked before the rows are locked, since the Permissions and SCIM calls can take a
    while and edits of those rows would wait on them; returns the groups checked, for _recheck_locked_rows."""
    groups = {r["task_collection"] for r in db.query(q.control_groups_sql(), (list(ids),))} | {g for g in also if g}
    _refuse_groups(groups, request)
    return groups


def _recheck_locked_rows(rows: List[dict], checked: set, request: Request) -> None:
    """A row moved to another group between the check and its lock is checked again, now under the lock."""
    moved = {r["task_collection"] for r in rows} - checked
    if moved:
        _refuse_groups(moved, request)


def _refuse_job_tables(job: dict, groups: Iterable[str], request: Request) -> None:
    """Refuse a change to a job's tables unless the user may change the job and every other job that runs one of
    the groups it touches: a group can be in more than one job, and its tables change for all of them. Checked
    before any lock is taken, as the Permissions and SCIM calls can be slow. A job that isn't Ready refuses it: its
    prepare task writes its tables."""
    _refuse_unready(job, preparing.tables_refusal)
    _refuse_job(job, request, "change")
    _refuse_groups(groups, request)


OUTSIDE_A_JOB = ("every ingestion group belongs to an ingestion job, and {groups} is in none: add tables through a "
                 "job's Add tables, or make a job for them with New ingestion job")


def _refuse_groups_outside_jobs(groups: Iterable[str]) -> None:
    """Refuse a write that would put tables into a group no ingestion job holds, so no group is ever outside a job.
    New ingestion job makes its groups with the job, in one transaction, and Add tables puts tables only into the
    job's own groups, so neither comes here. Read fresh, not shared."""
    wanted = {g for g in groups if g}
    if not wanted:
        return
    schedules = db.query(q.schedules_sql())
    members = db.query(q.schedule_members_sql(), ([s["id"] for s in schedules],)) if schedules else []
    held = {m["task_collection"] for m in members if m["removed_at"] is None}
    outside = sorted(wanted - held)
    if outside:
        raise HTTPException(422, OUTSIDE_A_JOB.format(groups=", ".join(outside)))


def _run_now_blocker(job: dict, members: List[dict]) -> Optional[str]:
    """Why the job can't be started by hand right now (it isn't Ready, a run of it is going, or its wrapper jobs aren't
    synced). The running runs are read after the app has closed those whose Databricks run ended (_running)."""
    waiting = preparing.run_refusal(sch.options_of(job))
    if waiting:
        return waiting
    if _recorded_running(members):
        return "a run of this job is already running: cancel it, or wait for it to end"
    if not members or any(not m.get("job_id") for m in members):
        return "the job's Databricks jobs are not synced yet"
    return None


def _recorded_running(members: List[dict]) -> bool:
    """Whether Lakebase has a run of one of these groups going, after closing those whose Databricks run ended."""
    groups = {m["task_collection"] for m in members}
    return any(r["task_collection"] in groups for r in _running())


@app.post("/api/ingestion-jobs/{job_id}/run")
def run_ingestion_job(job_id: int, request: Request):
    """Start one run of an ingestion job by hand, as its schedule would: its wrapper jobs (one per group, or a check
    schedule's one wrapper, which starts the check job over every group). Refused while a run of it is going."""
    job, members = _load_schedule(job_id)
    _refuse_job(job, request, "run")
    _refuse_unready(job, preparing.run_refusal)
    members = [m for m in members if m.get("removed_at") is None]
    blocker = _run_now_blocker(job, members)
    if blocker:
        raise HTTPException(409, blocker)
    # Lakebase sees a run only once its cluster is up; the Jobs API sees it at once. Databricks would skip a second
    # run of a wrapper (max_concurrent_runs 1) without a word, so a Run now while one is going is refused here.
    going = _active_wrapper_runs(members)
    if going:
        raise HTTPException(409, {"message": f"a run of this job is already going, started {going[0].get('started') or 'just now'}: "
                                             "Databricks would skip a second one, so wait for it to end",
                                  "run_url": going[0].get("url"), "run_id": going[0].get("run_id")})
    wrappers = sorted({int(m["job_id"]) for m in members})
    host = jobs.workspace_url()
    started = []
    for wrapper in wrappers:
        run_id = jobs.run_now(wrapper, {})
        started.append({"job_id": wrapper, "run_id": run_id, "url": f"{host}/jobs/{wrapper}/runs/{run_id}" if host else None})
    return {"started": started}


def _ingest_target(job: dict) -> Optional[int]:
    """The Databricks ingest job that the group runs of an ingestion job execute as: its own copy, else its variant's.
    A check schedule's wrapper starts the check, so its ingest job is its copy, else the default variant's."""
    if (job.get("job_variant") or "default") == sch.CHECK_VARIANT:
        return sch.copy_job_id(job) or _ingest_job_id("default")
    return _wrapper_target(job)


def _job_active_runs(job: dict, members: List[dict]) -> List[dict]:
    """Every Databricks run of an ingestion job that is still active, from the poller's table: its wrapper runs, its
    check job's run (check kind), and the group runs of its groups. Cancel run cancels all of them, so Cancelling
    waits for all of them to end."""
    groups = {m["task_collection"] for m in members}
    runs = _active_wrapper_runs(members)
    check_kind = (job.get("job_variant") or "default") == sch.CHECK_VARIANT
    if check_kind and jobs.CHECK_JOB_ID:
        runs += [r for r in run_poller.runs_of(jobs.CHECK_JOB_ID, active_only=True, limit=10)
                 if ij.check_run_of(r, job["id"], groups)]
    target = _ingest_target(job)
    if target and groups:
        runs += [r for r in run_poller.runs_of(target, active_only=True, limit=50) if r.get("task_collection") in groups]
    return runs


def _active_wrapper_runs(members: List[dict]) -> List[dict]:
    """The job's wrapper jobs' runs going now (active only), newest first."""
    out = []
    for wrapper in sorted({int(m["job_id"]) for m in members if m.get("job_id")}):
        out += [r for r in run_poller.runs_of(wrapper, active_only=True, limit=5) if ij.is_active(r)]
    return sorted(out, key=lambda r: r.get("started") or "", reverse=True)


def _run_phase_of(job: dict, members: List[dict], now: datetime) -> dict:
    """Where Run now and Cancel run stand for a job (ingestion_jobs.run_phase), from its active runs. ``going`` is
    true while any run is active, so a page that asks what is going reads the same thing as the buttons."""
    phase = ij.run_phase(_job_active_runs(job, members), now)
    return {**phase, "going": phase["phase"] != ij.IDLE}


def _job_databricks_runs(job: dict, members: List[dict]) -> dict:
    """The Databricks runs of one ingestion job, as the Jobs API has them: its wrapper jobs' runs going now, its check
    job's recent runs (check kind) and the ingestion job's recent runs, of every group (main filters to the job's)."""
    groups = sorted({m["task_collection"] for m in members})
    check_kind = (job.get("job_variant") or "default") == sch.CHECK_VARIANT
    checks = [r for r in run_poller.runs_of(jobs.CHECK_JOB_ID, limit=10) if ij.check_run_of(r, job["id"], groups)] \
        if check_kind and jobs.CHECK_JOB_ID else []
    target = (sch.copy_job_id(job) or _ingest_job_id("default")) if check_kind else _wrapper_target(job)
    ingest = run_poller.runs_of(target, limit=max(25, 3 * len(groups))) if target else []
    return {"groups": groups, "wrappers": _active_wrapper_runs(members), "checks": checks, "ingest": ingest}


@app.get("/api/ingestion-jobs/{job_id}/live")
def ingestion_job_live(job_id: int):
    """The job run going now, as its Databricks runs are: the wrapper runs, the check run and what it found (check
    kind), and the group runs it started, with each one's state and, once it has recorded it, its progress. Read
    from the Jobs API, which sees a run from its start, joined with Lakebase. Nothing is going: active is false."""
    job, members = _load_schedule(job_id)
    members = [m for m in members if m.get("removed_at") is None]
    now = datetime.now(timezone.utc)
    found_runs = _job_databricks_runs(job, members)
    groups, wrappers, checks, ingest = (found_runs[k] for k in ("groups", "wrappers", "checks", "ingest"))
    check = max(checks, key=lambda r: r.get("started") or "", default=None)
    work_ids = sorted({(r.get("parameters") or {}).get("work_id") for r in ingest} - {None, ""})
    found = db.query(q.check_work_found_sql(), (work_ids,)) if check and work_ids else []
    stamped = db.query(q.database_checks_since_sql(), (groups, check["started"]))[0]["databases"] \
        if check and check.get("started") and groups else None
    return ij.live_view(wrappers, checks, ingest, groups, found, stamped, _running(), now)


@app.post("/api/ingestion-jobs/{job_id}/stop")
def stop_ingestion_job(job_id: int, request: Request):
    """Stop the job's run going now, as the Databricks job run page's Cancel run does: cancel its wrapper runs, its
    check run and the group runs of its groups (those the Jobs API lists, and any its groups' records name). The
    records are not closed here: the run poller closes each one as cancelled when Databricks ends its run, so the page
    shows Cancelling until then. A run it could neither cancel nor read is left as it is. Needs what Run now needs.
    Nothing going: 409."""
    job, members = _load_schedule(job_id)
    _refuse_job(job, request, "run")
    members = [m for m in members if m.get("removed_at") is None]
    user = _user(request)
    found_runs = _job_databricks_runs(job, members)
    groups = set(found_runs["groups"])
    going = [dict(r, kind="wrapper") for r in found_runs["wrappers"]] + \
        [dict(r, kind="check") for r in found_runs["checks"] if ij.is_active(r)] + \
        [dict(r, kind="group") for r in found_runs["ingest"]
         if ij.is_active(r) and (r.get("parameters") or {}).get("task_collection") in groups]
    recorded = [r for r in db.query(q.open_runs_sql()) if r["task_collection"] in groups]
    if not going and not recorded:
        raise HTTPException(409, "no run of this job is going")
    jobs.forget_run_states()
    errors = []
    listed = {int(r["run_id"]) for r in going}
    for r in recorded:
        job_run = str(r.get("job_run_id") or "").strip()
        if not job_run.isdigit() or int(job_run) in listed:
            continue
        try:        # a run the list missed (an older one on a busy ingestion job) is cancelled too
            if not run_poller.ended_of(job_run):
                going.append({"kind": "group", "run_id": int(job_run),
                              "parameters": {"task_collection": r["task_collection"]}})
                listed.add(int(job_run))
        except Exception as e:
            errors.append(f"run {job_run}: {str(e).split('. ')[0][:200]}")
    cancelled = []
    for r in {int(r["run_id"]): r for r in going}.values():
        try:
            jobs.cancel(r["run_id"])
            cancelled.append({"kind": r["kind"], "run_id": r["run_id"], "job_id": r.get("job_id"), "url": r.get("url"),
                              "task_collection": (r.get("parameters") or {}).get("task_collection")})
        except Exception as e:      # one that ended meanwhile, or a refusal: the others are still stopped
            errors.append(f"run {r['run_id']}: {str(e).split('. ')[0][:200]}")
    batch = uuid.uuid4().hex[:12]
    with _write() as cur:
        for group in sorted({r.get("task_collection") for r in cancelled if r.get("task_collection")}) or [None]:
            _log(cur, user, "job_stop", None, group, "run", None, f"{job['name']}: {len(cancelled)} Databricks runs cancelled",
                 batch=batch)
    jobs.forget_run_states()
    return {"cancelled": cancelled, "errors": errors}


@app.get("/api/ingestion-jobs/{job_id}/compute")
def ingestion_job_compute(job_id: int):
    """What a job's runs execute on, read live from the Jobs API: the check job (on a check schedule) and the
    ingestion job it starts, each task's compute with where every value comes from (compute.describe)."""
    job = _load_schedule(job_id)[0]
    variant = job.get("job_variant") or "default"
    ingestion = sch.copy_job_id(job) or jobs.ingest_variants().get("default" if variant == sch.CHECK_VARIANT else variant)
    started = ([("check", jobs.CHECK_JOB_ID)] if variant == sch.CHECK_VARIANT else []) + [("ingestion", ingestion)]
    out = []
    for role, started_job_id in started:
        entry = {"role": role, "job_id": started_job_id, "name": None, "compute": [], "error": None}
        if not started_job_id:
            entry["error"] = f"no {role} job is configured for this app"
        else:
            try:
                settings = jobs.job_settings(started_job_id)
                entry.update(name=settings.get("name"), compute=compute.describe(
                    settings, own_cluster=role == "ingestion" and bool(sch.copy_job_id(job))))
            except Exception as e:  # shown on the page; the rest of the job's settings still load
                entry["error"] = f"can't read job {started_job_id}: {e}"
        out.append(entry)
    return {"jobs": out}


# Where the cluster copies run has not ended this long after its start, the Edit view says so, and the save is not done.
CLUSTER_LIMIT_S = 30 * 60


@app.get("/api/ingestion-jobs/{job_id}/cluster")
def ingestion_job_cluster(job_id: int, request: Request):
    """A job's own cluster for the Edit view: what it asks for (None = the ingestion job's own), the ingestion job's
    values as the defaults, the workspace's node types, the copy it runs, and the copies job's run applying a change
    (with its outcome: pending, timed out, or not done). Once that run has ended, the job's wrapper jobs are synced to
    start the copy (or the ingestion job again); a failed copy or sync is the error, and the copy is not applied."""
    s = _load_schedule(job_id)[0]
    applying, error = None, None
    run_id = sch.options_of(s).get("cluster_copy_run_id")
    if run_id:
        state = run_poller.run_state_of(run_id)
        outcome = action_states.run_outcome(state, datetime.now(timezone.utc), CLUSTER_LIMIT_S)
        applying = {"run_id": run_id, "state": state["state"], "result": state["result"], "url": state["url"],
                    "outcome": outcome}
        if state["state"] in run_poller.ENDED_STATES or state["state"] == run_poller.NOT_FOUND:
            with _write() as cur:
                cur.execute(q.clear_schedule_option_sql(), ("cluster_copy_run_id", int(job_id)))
            if state["result"] == "SUCCESS":
                problems = action_states.sync_failures(_sync(job_id, _user(request)))
                s = _load_schedule(job_id)[0]
                if problems:
                    error = "the job's Databricks jobs did not sync: " + "; ".join(problems)
            else:
                error = f"the cluster copies run did not apply the cluster: {outcome['reason']}"
            applying = None
    choices = _cluster_choices()
    copy = sch.copy_job_id(s)
    host = db.workspace().config.host.rstrip("/")
    return {**choices, "cluster": sch.own_cluster(s), "copy_job_id": copy, "copy_url": f"{host}/jobs/{copy}" if copy else None,
            "applying": applying, "error": error or choices["error"]}


@app.get("/api/cluster-options")
def cluster_options():
    """What New offers for a job's own cluster before the job exists: the ingestion job's values as the defaults and the
    workspace's node types, as the Edit view's Job cluster shows them."""
    return {**_cluster_choices(), "cluster": None, "copy_job_id": None, "copy_url": None, "applying": None}


def _cluster_choices() -> dict:
    """The ingestion job's cluster values (the defaults a job's own cluster starts from) and the workspace's node types."""
    error = None
    try:
        bundle_cluster = ((jobs.job_settings(jobs.ingest_variants().get("default")).get("job_clusters") or [{}])[0]
                          .get("new_cluster") or {})
        defaults = {k: bundle_cluster.get(k) for k in sch.CLUSTER_KEYS}
    except Exception as e:
        defaults, error = {}, f"can't read the ingestion job's cluster: {str(e).split('. ')[0]}"
    try:
        node_types = jobs.node_type_details()
    except Exception:
        node_types = []
    return {"defaults": defaults, "node_types": node_types, "max_workers": sch.MAX_WORKERS, "error": error,
            "configured": bool(jobs.CLUSTER_COPIES_JOB_ID)}


class JobSettingsRequest(BaseModel):
    settings: dict
    dry_run: bool = True


@app.get("/api/ingestion-jobs/{job_id}/settings")
def ingestion_job_settings(job_id: int):
    """What all of a job's tables share (job_settings_sql.read: one scan, whatever the job's size), each setting
    with its value or its most common values. A job that isn't Ready answers with the settings its record keeps
    (_stored_settings)."""
    groups = _job_groups(job_id)
    job = _load_schedule(job_id)[0]
    if not preparing.is_ready(sch.options_of(job)):
        return _stored_settings(job, groups)
    if not groups:
        return {"job_id": int(job_id), "groups": groups, "tables": 0, "settings": js.read([]), "reads_by_host": False}
    unity_catalog = _unity_catalog(job)
    with _snapshot() as fetch:
        tables, settings = fetch(q.groups_row_count_sql(), (groups,))[0]["n"], jss.read(fetch, groups, unity_catalog)
        reads_by_host = _reads_by_host(fetch, groups)
    return {"job_id": int(job_id), "groups": groups, "tables": tables, "settings": settings,
            "identity_differs": js.differing_identity_columns(settings["identity"], tables),
            "reads_by_host": reads_by_host}


@app.post("/api/ingestion-jobs/{job_id}/settings")
def edit_ingestion_job_settings(job_id: int, body: JobSettingsRequest, request: Request):
    """Preview (dry_run, the default) or apply a settings edit to every table of a job, in set-based SQL
    (job_settings_sql): templates filled per row, a reseed where a change-tracked or cursor table gets a new
    target or client_id, one change-log line per database. Anything it can't take is a 422 naming why. Applying
    is one transaction under the job's lock and every group's run lock. A job that isn't Ready changes the settings its
    record keeps instead, while its tables are still read (_edit_stored_settings)."""
    import psycopg
    groups = _job_groups(job_id)
    job = _load_schedule(job_id)[0]
    if not preparing.is_ready(sch.options_of(job)):
        return _edit_stored_settings(job, body.settings, body.dry_run, request)
    if not groups:
        return {"summary": {"rows": 0, "changed": 0, "reseed": 0, "errors": 0, "applied": False}, "errors": []}
    unity_catalog = _unity_catalog(job)

    def checked(fetch, groups):
        now = jss.read(fetch, groups, unity_catalog)
        found = jss.preview(fetch, groups, body.settings, now)
        if found["errors"]:
            raise HTTPException(422, {"errors": found["errors"], "summary": found["summary"]})
        return now, found

    if body.dry_run:
        with _snapshot() as fetch:
            return checked(fetch, groups)[1]
    _refuse_job_tables(job, groups, request)
    try:
        with _write() as cur:
            groups = _lock_job(cur, job_id)
            _lock_collections(cur, groups)
            fetch = lambda sql, params=(): db.fetch(cur, sql, params)
            now, found = checked(fetch, groups)
            if not found["summary"]["changed"]:
                return found
            result = jss.apply(cur, groups, body.settings, now, _user(request), uuid.uuid4().hex[:12])
            _rewrite_rollups_for_groups(cur, groups)
            _touch(cur, _user(request), [job_id])
            mixed = jss.mixed_sinks(fetch, groups)
            if mixed:
                raise HTTPException(409, f"target {mixed[0]} would be claimed by a dedicated table and by others; "
                                         "a target is either one table's own or shared by client_id. Nothing was changed")
            found["summary"].update(changed=result["changed"], reseed=result["reseed"], applied=True)
            return found
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "another control row already writes one of those targets (or that target and "
                                 "client_id); nothing was changed")
    except psycopg.errors.CheckViolation as e:
        raise HTTPException(422, {"errors": [f"a table can't take this change ({e.diag.constraint_name}); "
                                             "nothing was changed"]})


@contextmanager
def _snapshot():
    """A fetch over one read-only, repeatable-read transaction, so a read of several queries sees one moment."""
    with db.transaction() as cur:
        cur.execute("set transaction isolation level repeatable read, read only")
        yield lambda sql, params=(): db.fetch(cur, sql, params)


def _reads_by_host(fetch, groups: List[str]) -> bool:
    """Whether any enabled table of a job is read by host and password rather than through a connection: the Manage
    export's own question (q.export_reads_jdbc_sql). Edit shows the two settings only such a job uses."""
    return bool(fetch(q.export_reads_jdbc_sql(), (groups,))[0]["jdbc"])


@app.get("/api/ingestion-jobs/{job_id}/tree")
def ingestion_job_tree(job_id: int, server: str = "", database: str = "", search: str = "", offset: int = 0,
                       limit: int = 50):
    """One level of a job's tables: its servers; with server, that server's databases; with database too, that
    database's tables. Searched and paged at each level."""
    groups = _job_groups(job_id)
    offset, limit = max(0, int(offset)), max(1, min(int(limit), 200))
    pattern = f"%{search.strip()}%"
    if not groups:
        rows, level = [], "servers"
    elif not server:
        level, rows = "servers", db.query(q.job_servers_sql(), (groups, pattern, limit, offset))
    elif not database:
        level, rows = "databases", db.query(q.job_databases_sql(), (groups, groups, server, pattern, limit, offset))
    else:
        level, rows = "tables", db.query(q.job_tables_sql(), (groups, server, database, pattern, limit, offset))
        for r in rows:
            r["columns"] = len([c for c in str(r.pop("select_list") or "").split(",") if c.strip()])
    total = rows[0]["total"] if rows else 0
    # The job-level JDBC connection is shown with its servers (the top level); the page polls the job, not the tree.
    return {"level": level, "server": server or None, "database": database or None, "total": total,
            "offset": offset, "limit": limit, "items": [{k: v for k, v in r.items() if k != "total"} for r in rows],
            "jdbc": _job_jdbc(job_id) if not server else None}


@app.get("/api/ingestion-jobs/{job_id}/targets")
def ingestion_job_targets(job_id: int, search: str = "", offset: int = 0, limit: int = 25):
    """A job's target tables: each with the source tables of the job writing it (and how many outside it), their
    states, the rows the run records say were written over 7 days, and a link to the table in Catalog Explorer;
    fan_in counts those written from more than one source table. Searched by name and paged."""
    groups = _job_groups(job_id)
    offset, limit = max(0, int(offset)), max(1, min(int(limit), 200))
    rows = (db.query(q.job_targets_sql(), (groups, groups, groups, f"%{search.strip()}%", limit, offset))
            if groups else [])
    host = db.workspace().config.host.rstrip("/")
    items = [{**{k: v for k, v in r.items() if k not in ("total", "fan_in_total")},
              "url": f"{host}/explore/data/{r['catalog']}/{r['schema']}/{r['table']}"} for r in rows]
    return {"total": rows[0]["total"] if rows else 0, "fan_in": rows[0]["fan_in_total"] if rows else 0,
            "offset": offset, "limit": limit, "items": items}


@app.get("/api/ingestion-jobs/{job_id}/targets/sources")
def ingestion_job_target_sources(job_id: int, target: str, offset: int = 0, limit: int = 25):
    """The job's source tables writing one target (catalog.schema.table), paged."""
    parts = target.split(".")
    if len(parts) != 3 or not all(parts):
        raise HTTPException(400, "give the target as catalog.schema.table")
    groups = _job_groups(job_id)
    offset, limit = max(0, int(offset)), max(1, min(int(limit), 200))
    rows = db.query(q.job_target_sources_sql(), (groups, groups, *parts, limit, offset)) if groups else []
    return {"target": target, "total": rows[0]["total"] if rows else 0, "offset": offset, "limit": limit,
            "items": [{k: v for k, v in r.items() if k != "total"} for r in rows]}


@app.get("/api/ingestion-jobs/{job_id}/failing-tables")
def ingestion_job_failing_tables(job_id: int):
    """The job's failing tables: enabled tables whose checkpoint is failed, each with its latest error, its class and
    kind, when it started failing and how many runs failed it. The job page shows its section only when there are some."""
    job, members = _load_schedule(job_id)
    groups = sorted({m["task_collection"] for m in _active(members)})
    rows = q.failing_table_rows(db.query(q.failing_tables_sql(), (groups,))) if groups else []
    removals = table_removal.entries(sch.options_of(job))
    now = datetime.now(timezone.utc)
    for row in rows:
        entry = removals.get(str(row["control_id"]))
        row["remove"] = table_removal.state_of(entry, _removal_run(entry), now)
    return {"tables": rows}


def _removal_run(entry: Optional[dict]) -> Optional[dict]:
    """The delete job's run a table's removal started, as the Jobs API has it, or None (not started, or unreadable)."""
    if not entry or entry.get("run_id") is None:
        return None
    try:
        return run_poller.run_state_of(entry["run_id"])
    except Exception:
        return None


class RetryDatabaseRequest(BaseModel):
    task_collection: str
    server: str = ""
    database: str = ""


@app.post("/api/ingestion-jobs/{job_id}/failing-tables/retry")
def retry_failing_database(job_id: int, body: RetryDatabaseRequest, request: Request):
    """Start one ingest run for one database, and no other: the job's ingest job, with a work list of just that database
    (the way the check job starts a run for the databases with work). Refused while a run of its group is going, since
    Databricks would skip a second one."""
    job, members = _load_schedule(job_id)
    _refuse_job(job, request, "run")
    _refuse_unready(job, preparing.run_refusal)
    mine = [m for m in _active(members) if m["task_collection"] == body.task_collection]
    if not mine:
        raise HTTPException(404, f"ingestion group {body.task_collection} is not in this job")
    if _job_active_runs(job, mine) or _recorded_running(mine):
        raise HTTPException(409, f"a run of {body.task_collection} is going: wait for it to end, or cancel it")
    target = _ingest_target(job)
    if not target:
        raise HTTPException(409, "the job's ingest job is not synced yet")
    work_id = f"retry-{uuid.uuid4().hex[:12]}:{body.task_collection}"
    with _write() as cur:
        recorded = db.fetch(cur, q.retry_database_sql(), (work_id, body.task_collection, body.server, body.database))
        if not recorded:
            raise HTTPException(409, f"{body.database or 'that database'} has no enabled tables in {body.task_collection} now")
    run_id = jobs.run_now(target, {**sch.run_parameters(job, body.task_collection), "work_id": work_id})
    host = jobs.workspace_url()
    return {"started": {"job_id": int(target), "run_id": run_id, "work_id": work_id, "database": body.database,
                        "url": f"{host}/jobs/{target}/runs/{run_id}" if host else None}}


class RemoveTablesRequest(BaseModel):
    control_ids: List[int] = Field(min_length=1)
    confirmed: bool = False


@app.post("/api/ingestion-jobs/{job_id}/tables/remove")
def remove_job_tables(job_id: int, body: RemoveTablesRequest, request: Request):
    """Remove tables from a live job for good. The app records the request on the job and starts the delete job, which
    deletes each table's control row and its state as the deployer and keeps the target tables. Needs the permission
    Delete needs, and only a request the user confirmed (the page asks first). A group another live job also holds
    can't be removed this way: that would change the other job's tables too."""
    why = table_removal.confirmation_missing(body.confirmed)
    if why:
        raise HTTPException(422, why)
    user = _user(request)
    job, members = _load_schedule(job_id)
    _refuse_job(job, request, "delete")
    _refuse_unready(job, preparing.tables_refusal)
    if not jobs.DELETE_JOB_ID:
        raise HTTPException(409, NO_TABLE_REMOVE_JOB)
    ids = sorted(set(body.control_ids))
    rows = db.query(q.table_rows_by_ids_sql(), (ids,))
    missing = sorted(set(ids) - {r["id"] for r in rows})
    if missing:
        raise HTTPException(404, f"table(s) {', '.join(str(i) for i in missing)} not found")
    wanted = {r["task_collection"] for r in rows}
    outside = sorted(wanted - {m["task_collection"] for m in _active(members)})
    if outside:
        raise HTTPException(422, f"group(s) {', '.join(outside)} aren't in ingestion job {job_id}")
    own = {r["task_collection"] for r in db.query(q.job_own_groups_sql(), (int(job_id), int(job_id)))}
    removals = table_removal.entries(sch.options_of(job))
    now_dt = datetime.now(timezone.utc)
    still = table_removal.removing(removals, ids, {i: _removal_run(removals.get(str(i))) for i in ids}, now_dt)
    mine = [m for m in _active(members) if m["task_collection"] in wanted]
    running = bool(_job_active_runs(job, mine) or _recorded_running(mine))
    refused = table_removal.refusal(job.get("deleted_at") is not None, sorted(wanted - own), running, bool(still))
    if refused:
        raise HTTPException(409, refused)
    now = now_dt.isoformat()
    with _write() as cur:
        _lock_job(cur, int(job_id))
        cur.execute(q.set_table_remove_sql(), (json.dumps(table_removal.requests(ids, user, now)), int(job_id)))
        cur.execute(q.add_exclusions_sql(), (json.dumps(table_removal.exclusion_records(rows, user, now)), int(job_id)))
        for r in rows:
            _log(cur, user, "remove", r["id"], r["task_collection"])
    try:
        run_id = jobs.start_table_remove(job_id, ids)
    except Exception as e:
        error = f"the delete job didn't start: {str(e).split('. ')[0]}"
        with _write() as cur:
            cur.execute(q.set_table_remove_sql(),
                        (json.dumps(table_removal.requests(ids, user, now, error=error)), int(job_id)))
        return {"removing": [], "error": error, "run": None}
    with _write() as cur:
        cur.execute(q.set_table_remove_sql(),
                    (json.dumps(table_removal.requests(ids, user, now, run_id=run_id)), int(job_id)))
    host = jobs.workspace_url()
    url = f"{host}/jobs/{jobs.DELETE_JOB_ID}/runs/{run_id}" if host else None
    return {"removing": ids, "error": None, "run": {"run_id": run_id, "url": url}}


class JobScope(BaseModel):
    server: str = ""
    database: str = ""
    schema_name: str = Field("", alias="schema")
    table: str = ""


class JobEnableRequest(JobScope):
    enabled: bool


def _job_row_ids(cur, groups, scope: JobScope) -> List[int]:
    cur.execute(q.job_row_ids_sql(), (groups, scope.server, scope.server, scope.database, scope.database,
                                      scope.schema_name, scope.schema_name, scope.table, scope.table))
    return [r[0] for r in cur.fetchall()]


@app.post("/api/ingestion-jobs/{job_id}/enable")
def enable_job_tables(job_id: int, body: JobEnableRequest, request: Request):
    """Enable or disable a job's tables on a server, in a database, or one table in every database."""
    _refuse_job_tables(_load_schedule(job_id)[0], _job_groups(job_id), request)
    batch = uuid.uuid4().hex[:12]
    with _write() as cur:
        groups = _lock_job(cur, job_id)
        ids = _job_row_ids(cur, groups, body) if groups else []
        if not ids:
            return {"updated": 0}
        rows = _locked_rows(cur, ids)
        _lock_collections(cur, [r["task_collection"] for r in rows])
        cur.execute(q.set_enabled_sql(), (body.enabled, ids))
        _rewrite_rollups_for_ids(cur, ids)
        for r in rows:
            if bool(r["task_enabled"]) != body.enabled:
                _log(cur, _user(request), "enable" if body.enabled else "disable", r["id"], r["task_collection"],
                     "task_enabled", r["task_enabled"], body.enabled, batch=batch)
        _touch(cur, _user(request), [job_id])
    return {"updated": len(ids)}


JOB_TABLE_FIELDS = ("load_type", "primary_key", "select_list", "cursor_column", "load_partitioned", "partition_col",
                    "partition_size_mb")


class JobTableEditRequest(JobScope):
    changes: dict
    dry_run: bool = True


@app.post("/api/ingestion-jobs/{job_id}/tables/edit")
def edit_job_table(job_id: int, body: JobTableEditRequest, request: Request):
    """Change one table (schema and table) in every database of a job: its load type, key or columns. Checked
    and applied as a table edit, all or nothing, under the groups' run locks."""
    bad = sorted(set(body.changes) - set(JOB_TABLE_FIELDS))
    if bad or not body.schema_name or not body.table:
        raise HTTPException(400, f"give schema and table, and change only {', '.join(JOB_TABLE_FIELDS)}"
                                 + (f" (not {', '.join(bad)})" if bad else ""))
    groups = _job_groups(job_id)
    if not body.dry_run:
        _refuse_job_tables(_load_schedule(job_id)[0], groups, request)
    with _write() as cur:
        groups = _lock_job(cur, job_id)
        ids = _job_row_ids(cur, groups, body) if groups else []
        rows = _locked_rows(cur, ids) if ids else []
        plans = [q.plan_edit([r], body.changes, bulk=False)[0] for r in rows]
        summary = {"rows": len(plans), "changed": sum(1 for p in plans if p["changes"]),
                   "reseed": sum(1 for p in plans if p["reseed"]),
                   "errors": sum(1 for p in plans if p["errors"]), "applied": False}
        if body.dry_run or summary["errors"] or not summary["changed"]:
            cur.connection.rollback()
            return {"summary": summary, "plans": plans[:200]}
        _lock_collections(cur, [r["task_collection"] for r in rows])
        _apply_plans(cur, rows, plans, _user(request), uuid.uuid4().hex[:12])
        _touch(cur, _user(request), [job_id])
        summary["applied"] = True
        return {"summary": summary, "plans": plans[:200]}


class JobDatabasesRequest(BaseModel):
    csv: Optional[str] = None
    columns: dict = {"server": "server", "database": "database", "enabled": "enabled"}
    databases: Optional[List[dict]] = None
    groups_per_server: Optional[int] = Field(None, ge=1, le=64)
    # The job's group size: each server gets one group for about this many of its tables (placement.TablesPerGroup).
    tables_per_group: Optional[int] = Field(None, ge=100, le=1_000_000)
    full_list: bool = False
    dry_run: bool = True
    request_id: Optional[str] = Field(None, max_length=80)


@app.post("/api/ingestion-jobs/{job_id}/databases")
def add_job_databases(job_id: int, body: JobDatabasesRequest, request: Request):
    """Preview (dry_run, the default) or apply databases into a job, from a databases file (its columns matched
    to server, database and enabled) or a list picked by hand (job_settings.plan_databases). Applying is one
    transaction under every group's run lock: new tables inserted (a taken target is skipped), enabled flags
    set, new groups added to the job; then the job's wrapper jobs are synced."""
    user = _user(request)

    def after(result):
        if result.get("new_groups"):
            _synced(_sync(job_id, user))
        return {k: v for k, v in result.items() if k != "new_groups"}

    if (made := _made_already(body, after)) is not None:
        return made
    _refuse_unready(_load_schedule(job_id)[0], preparing.tables_refusal)
    if body.csv is not None:
        entries, errors = js.read_databases(body.csv, body.columns)
    else:
        entries = [{"line": i + 1, "server": str(d.get("server") or "").strip(),
                    "database": str(d.get("database") or "").strip(), "enabled": q._truthy(d.get("enabled", True))}
                   for i, d in enumerate(body.databases or [])]
        errors = [f"line {e['line']}: server and database are required" for e in entries
                  if not e["server"] or not e["database"]]
    if errors:
        raise HTTPException(422, {"errors": errors})
    job = _load_schedule(job_id)[0]

    def planned(rows):
        plan = js.plan_databases(rows, entries, job["name"], _sizing(body, job), body.full_list,
                                 unity_catalog=_unity_catalog(job))
        if plan["errors"]:
            raise HTTPException(422, {"errors": plan["errors"]})
        summary = {"databases": len(entries), "new_tables": len(plan["new"]), "enable": len(plan["enable"]),
                   "disable": len(plan["disable"]), "new_groups": plan["new_groups"], "applied": False}
        return plan, summary

    groups = _job_groups(job_id)
    plan, summary = planned(db.query(q.groups_control_rows_sql(), (groups,)) if groups else [])
    if body.dry_run:
        return {"summary": summary}
    _refuse_job_tables(job, groups + plan["new_groups"], request)
    batch = uuid.uuid4().hex[:12]

    def write(progress, task_id):
        with _write() as cur:
            groups = _lock_job(cur, job_id)
            rows = db.fetch(cur, q.groups_control_rows_sql(for_update=True), (groups,)) if groups else []
            plan, summary = planned(rows)
            _lock_collections(cur, groups + plan["new_groups"])
            connection = _job_connection_row(job_id)
            if connection is not None:
                _refuse_connection_rows(connection, plan["new"], cur=cur)
            else:
                _refuse_host_reads(plan["new"], user, {})
            written = _insert_controls(cur, plan["new"], user, batch, skip_taken=True, progress=progress)
            off = [control_id for row, control_id in written if not row["task_enabled"]]
            if off:
                cur.execute(q.set_enabled_sql(), (False, off))
                _rewrite_rollups_for_ids(cur, off)
            for enabled, ids in ((True, plan["enable"]), (False, plan["disable"])):
                if ids:
                    cur.execute(q.set_enabled_sql(), (enabled, ids))
                    _rewrite_rollups_for_ids(cur, ids)
                    _log_lines(cur, [(user, "enable" if enabled else "disable", i, None, "task_enabled", not enabled,
                                      enabled, None, batch) for i in ids])
            _refuse_mixed_sinks(cur, plan["new"])
            _refuse_split_databases(cur, plan["new"])
            if plan["new_groups"]:
                _, members = _load_schedule(job_id, cur)
                active = [m for m in members if m["removed_at"] is None]
                offs = sch.offsets([m["task_collection"] for m in active] + plan["new_groups"],
                                   int(job["cadence_minutes"]), bool(job.get("stagger")), sch.start_minute(job))
                for g in plan["new_groups"]:
                    cur.execute(q.insert_member_sql(), (job_id, g, offs[g]))
            _touch(cur, user, [job_id])
            summary.update(applied=True, new_tables=len(written), skipped=len(plan["new"]) - len(written))
            result = {"summary": summary}
            _task_written(cur, task_id, {**result, "new_groups": plan["new_groups"]}, job_id, len(plan["new"]))
        return {**result, "new_groups": plan["new_groups"]}

    return _write_now_or_later("databases", body.request_id, job_id, len(plan["new"]), user, write, after)


class JobGroupsRequest(BaseModel):
    # Blank: each server's default for its tables (placement.groups_for_tables); a number: that many per server.
    groups_per_server: Optional[int] = Field(None, ge=1, le=nj.MAX_GROUPS_PER_SERVER)
    # The job's group size: each server gets one group for about this many of its tables (placement.TablesPerGroup).
    tables_per_group: Optional[int] = Field(None, ge=100, le=1_000_000)
    dry_run: bool = True
    request_id: Optional[str] = Field(None, max_length=80)


def _regroup_plan(job_name: str, rows: List[dict], groups_per_server: Optional[int]) -> dict:
    """What a new number of groups per server does to a job, server by server (placement.regroup): a server whose
    number is unchanged is left alone. Returns {"servers": a line per server, "moves": [(control id, group before,
    group after, src_database)], "new": groups made, "emptied": groups left with no table, "summary"}."""
    by_server: Dict[str, Dict[str, Tuple[str, int]]] = {}
    for r in rows:
        databases = by_server.setdefault(js.server_of(r), {})
        key = str(r.get("src_database") or "").lower()
        group, tables = databases.get(key, (r["task_collection"], 0))
        databases[key] = (group, tables + 1)
    servers, target, new, emptied = [], {}, [], []
    for server in sorted(by_server):
        databases = by_server[server]
        now = sorted({group for group, _ in databases.values()})
        tables = sum(n for _, n in databases.values())
        count = placement.group_count(tables, groups_per_server)
        line = {"server": server, "databases": len(databases), "tables": tables, "groups_before": len(now),
                "groups_after": count, "databases_moving": 0, "tables_moving": 0}
        if count != len(now):
            plan = placement.regroup(f"{q.identifier(job_name)}_{q.identifier(server)}", databases, count)
            target.update({(server, name): group for name, group in plan["group_of"].items()})
            new += plan["new"]
            emptied += plan["emptied"]
            line.update(databases_moving=len(plan["moves"]),
                        tables_moving=sum(databases[name][1] for name in plan["moves"]))
        servers.append(line)
    moves = []
    for r in rows:
        after = target.get((js.server_of(r), str(r.get("src_database") or "").lower()))
        if after and after != r["task_collection"]:
            moves.append((r["id"], r["task_collection"], after, r.get("src_database") or ""))
    summary = {"servers": servers, "databases_moving": sum(line["databases_moving"] for line in servers),
               "tables_moving": len(moves), "new_groups": sorted(new), "emptied_groups": sorted(emptied),
               "unchanged": not moves and not new and not emptied, "applied": False}
    return {"servers": servers, "moves": moves, "new": sorted(new), "emptied": sorted(emptied), "summary": summary}


def _regroup_blocker(job: dict, members: List[dict]) -> Optional[str]:
    """Why a job's groups can't be changed now: a run of it is going (its wrapper, its check or a group run, queued or
    starting included, which hold no lock yet), or one of its writes still is. The groups' run locks are taken when it
    applies, too, so a run that starts in between refuses it then."""
    if _job_active_runs(job, _active(members)):
        return "a run of this job is going: change its groups once it has ended (or cancel it)"
    if any(t.get("status") == "running" for t in db.query(q.job_tasks_sql(), (int(job["id"]),))):
        return "a write to this job's tables is still going: change its groups once it has finished"
    return None


@app.post("/api/ingestion-jobs/{job_id}/groups")
def regroup_job(job_id: int, body: JobGroupsRequest, request: Request):
    """Preview (dry_run, the default) or apply a new number of groups per server for a job. Each server's databases are
    spread again over its new number of groups, moving as few as can be (placement.regroup); a database is never
    split. Applying is one transaction under the job's schedule lock and every group's run lock: the moved tables'
    control rows, their databases' check stamps, the rollups of both groups, the job's members (new groups added,
    emptied ones removed) and a change log line per table; then the job's Databricks jobs are synced. Each table keeps
    its checkpoint (by control id), so nothing reloads. Refused while a run or a write of the job is going."""
    user = _user(request)

    def after(result):
        _synced(_sync(job_id, user))
        return result

    if (made := _made_already(body, after)) is not None:
        return made
    job, members = _load_schedule(job_id)
    if not preparing.is_ready(sch.options_of(job)):
        return _regroup_stored(job, body, request)
    groups = _job_groups(job_id)
    plan = _regroup_plan(job["name"], db.query(q.groups_control_rows_sql(), (groups,)) if groups else [],
                         _sizing(body))
    blocker = _regroup_blocker(job, members)
    if body.dry_run:
        return {"summary": plan["summary"], "blocked": blocker}
    if blocker:
        raise HTTPException(409, blocker)
    if plan["summary"]["unchanged"]:
        if body.tables_per_group:      # nothing moves, but the job keeps its new size for what's added later
            _refuse_job_tables(job, groups, request)
            db.execute(q.set_schedule_option_sql(), ([TABLES_PER_GROUP_OPTION], int(body.tables_per_group), job_id))
        return {"summary": {**plan["summary"], "applied": True}, "blocked": None}
    _refuse_job_tables(job, groups + plan["new"], request)
    batch = uuid.uuid4().hex[:12]

    def write(progress, task_id):
        with _write() as cur:
            groups = _lock_job(cur, job_id)
            rows = db.fetch(cur, q.groups_control_rows_sql(for_update=True), (groups,)) if groups else []
            plan = _regroup_plan(job["name"], rows, _sizing(body))
            _lock_collections_together(cur, groups + plan["new"])
            by_target: Dict[str, List[int]] = {}
            stamps: Dict[Tuple[str, str], set] = {}
            for control_id, before, after_group, database in plan["moves"]:
                by_target.setdefault(after_group, []).append(control_id)
                stamps.setdefault((before, after_group), set()).add(database)
            for group, ids in sorted(by_target.items()):
                for at in range(0, len(ids), q.CONTROL_BATCH):
                    cur.execute(q.move_controls_sql(), (group, ids[at:at + q.CONTROL_BATCH]))
            for (before, after_group), names in sorted(stamps.items()):
                cur.execute(q.move_database_checks_sql(), (after_group, before, sorted(names), after_group))
            _log_lines(cur, [(user, "regroup", control_id, after_group, "task_collection", before, after_group, None, batch)
                             for control_id, before, after_group, _ in plan["moves"]])
            _rewrite_rollups(cur, [(before, database) for _, before, _, database in plan["moves"]]
                             + [(after_group, database) for _, _, after_group, database in plan["moves"]])
            _, now_members = _load_schedule(job_id, cur)
            active = [m for m in now_members if m["removed_at"] is None]
            gone = [m["id"] for m in active if m["task_collection"] in set(plan["emptied"])]
            if gone:
                cur.execute(q.remove_members_sql(), (gone,))
            if plan["new"]:
                kept = [m["task_collection"] for m in active if m["id"] not in set(gone)]
                offs = sch.offsets(kept + plan["new"], int(job["cadence_minutes"]), bool(job.get("stagger")),
                                   sch.start_minute(job))
                for group in plan["new"]:
                    cur.execute(q.insert_member_sql(), (job_id, group, offs[group]))
            if body.tables_per_group:      # the job's new size, for its Add tables and regroups after this one
                cur.execute(q.set_schedule_option_sql(), ([TABLES_PER_GROUP_OPTION], int(body.tables_per_group), job_id))
            _touch(cur, user, [job_id])
            result = {"summary": {**plan["summary"], "applied": True}, "blocked": None}
            _task_written(cur, task_id, result, job_id, len(plan["moves"]))
        return result

    return _write_now_or_later("regroup", body.request_id, job_id, len(plan["moves"]), user, write, after)


class _TablesFrom(BaseModel):
    """Tables given as CSV lines in the body (a small file, as before uploads), or by reference (source)."""
    csv: Optional[str] = None
    source: Optional[Selection] = None

    @model_validator(mode="after")
    def _csv_or_source(self):
        if (self.csv is None) == (self.source is None):
            raise ValueError("give the tables as csv or as a source, not both")
        return self

    def text(self, request: Optional[Request] = None, wait: bool = False):
        """(the CSV a plan reads, the selection it came from or None). A selection's database-only lines have their
        tables discovered first: while that discovery is still running (and not waited for) the text is None and the
        selection says {"discovering": True, "databases": how many}."""
        if self.source is None:
            return self.csv, None
        selected = _selected(self.source)
        resolved = _resolve_databases(selected, wait=wait, request=request)
        if resolved is not None and resolved["discovering"]:
            return None, {"discovering": True, "databases": resolved["databases"]}
        selected = _with_databases(selected, resolved)
        return selected["text"], selected


class JobTablesRequest(_TablesFrom):
    load_type: str = "ct"
    # Blank: each server's default for its tables (placement.groups_for_tables); a number: that many per server.
    groups_per_server: Optional[int] = Field(None, ge=1, le=nj.MAX_GROUPS_PER_SERVER)
    # The job's group size: each server gets one group for about this many of its tables (placement.TablesPerGroup).
    tables_per_group: Optional[int] = Field(None, ge=100, le=1_000_000)
    dry_run: bool = True
    request_id: Optional[str] = Field(None, max_length=80)


def _write_planned_tables(cur, job: dict, plan: dict, user: str, batch: str, progress=None,
                          skip_taken: bool = False, reader: Optional[str] = None) -> List[tuple]:
    """Inside the caller's transaction, with the job's groups locked: the plan's valid lines as control rows (each with
    its change log line), their groups' run locks, the mixed-sink and split-database checks, and the job's new groups
    as members with offsets. Returns the (row, id) pairs written; skip_taken leaves out a row whose target another
    table writes, instead of refusing the lot."""
    job_id = int(job["id"])
    _lock_collections(cur, plan["groups"])
    rows = [line["row"] for line in plan["lines"] if line["status"] == "valid"]
    connection = _job_connection_row(job_id)
    if connection is not None:
        _refuse_connection_rows(connection, rows, cur=cur)
    else:
        _refuse_host_reads(rows, reader or user, {})
    written = _insert_controls(cur, rows, user, batch, skip_taken=skip_taken, progress=progress)
    _refuse_mixed_sinks(cur, [row for row, _ in written])
    _refuse_split_databases(cur, [row for row, _ in written])
    if plan["new_groups"]:
        _, members = _load_schedule(job_id, cur)
        active = [m for m in members if m["removed_at"] is None]
        offs = sch.offsets([m["task_collection"] for m in active] + plan["new_groups"],
                           int(job["cadence_minutes"]), bool(job.get("stagger")), sch.start_minute(job))
        for g in plan["new_groups"]:
            cur.execute(q.insert_member_sql(), (job_id, g, offs[g]))
    return written


@app.post("/api/ingestion-jobs/{job_id}/tables")
def add_job_tables(job_id: int, body: JobTablesRequest, request: Request):
    """Preview (dry_run, the default) or add a CSV of tables to a job, as Add tables does in the Edit view: each line
    says where a table is, and the table takes the job's settings (new_job.plan_for_job). A database the job has keeps
    its group, a new one joins its server's groups, a new server gets new groups. Applying is one transaction under
    the job's lock and its groups' run locks; tables the job already writes are left; then new groups are synced. The
    tables come as CSV lines (csv) or by reference to an upload or discovery (source), which is kept with the job."""
    if _jdbc_source(body.source) is not None:
        raise HTTPException(422, {"errors": ["these tables come from a JDBC connection, whose scope and trust belong to "
                                              "a job of their own: make a new ingestion job from them"]})
    _refuse_unready(_load_schedule(job_id)[0], preparing.tables_refusal)
    return _add_job_tables(job_id, body, request)


def _explicit_tables(selected: Optional[dict]) -> bool:
    """Whether Add tables names its tables one by one: a CSV, or a selection of tables only. Then a table the job
    removed is taken back in. A whole server or database takes every table it holds, so the removed ones stay out."""
    if selected is None:
        return True
    return not selected["database_lines"] and all(part[0] == "tables" for part in selected["parts"])


def _add_job_tables(job_id: int, body: "JobTablesRequest", request: Request, inline: bool = False, progress=None,
                    task_id=None):
    """The add itself. A file with database-only lines is first discovered by name in a background task
    (_add_after_discovery), which runs this again with inline=True, so the write happens in that task."""
    user = _user(request)

    def after(result):
        if result.get("new_groups"):
            _synced(_sync(job_id, user))
        return {k: v for k, v in result.items() if k != "new_groups"}

    if body.source is not None and not _has_database_lines(body.source):
        kind, param, source_id, rest = _selection_params(body.source)
        counted = db.query(q.selected_count_sql(kind), (param, *rest))
        if counted and counted[0]["n"] > BACKGROUND_ROWS:
            return _add_by_database(job_id, body, request, kind, param, source_id, rest, int(counted[0]["n"]))
    if not inline and (made := _made_already(body, after)) is not None:
        return made
    job = _load_schedule(job_id)[0]
    if not inline and body.source is not None and not body.dry_run and _has_database_lines(body.source):
        _refuse_job_tables(job, _job_groups(job_id), request)
        return _write_now_or_later("tables", body.request_id, job_id, 0, user,
                                   lambda progress, task_id: _add_after_discovery(job_id, body, request, progress, task_id),
                                   after, background=True)
    taken = {r["task_collection"] for r in db.query(q.collections_sql())}
    text, selected = body.text(request, wait=inline)
    if selected is not None and selected.get("discovering"):
        return {"summary": None, "applied": False, "discovering": True, "databases": selected["databases"]}

    explicit = _explicit_tables(selected)

    def planned(rows, excluded):
        plan = _file_lines(nj.plan_for_job(job["name"], text, body.load_type, rows, _sizing(body, job), taken,
                                           unity_catalog=_unity_catalog(job)), selected)
        if plan["errors"]:
            raise HTTPException(422, {"errors": plan["errors"]})
        if not explicit and table_removal.leave_out(plan["lines"], excluded):
            used = {line["group"] for line in plan["lines"] if line["status"] == "valid"}
            plan["new_groups"] = [group for group in plan["new_groups"] if group in used]
        return plan

    groups = _job_groups(job_id)
    plan = planned(db.query(q.groups_control_rows_sql(), (groups,)) if groups else [],
                   table_removal.exclusions_of(sch.options_of(job)))
    if body.dry_run:
        return {"summary": nj.summary(plan), "applied": False}
    _refuse_job_tables(job, groups + plan["groups"], request)
    batch = uuid.uuid4().hex[:12]

    def write(progress, task_id):
        with _write() as cur:
            groups = _lock_job(cur, job_id)
            excluded = table_removal.exclusions_of(sch.options_of(_load_schedule(job_id, cur)[0]))
            plan = planned(db.fetch(cur, q.groups_control_rows_sql(for_update=True), (groups,)) if groups else [],
                           excluded)
            summary = nj.summary(plan)
            if summary["invalid"]:
                return {"summary": summary, "applied": False, "new_groups": []}
            written = _write_planned_tables(cur, job, plan, user, batch, progress=progress)
            if explicit:
                cur.execute(q.clear_exclusions_sql(), (table_removal.line_keys(plan["lines"]), int(job_id)))
            _keep_selection(cur, job_id, selected, user)
            _touch(cur, user, [job_id])
            result = {"summary": summary, "applied": True}
            _task_written(cur, task_id, {**result, "new_groups": plan["new_groups"]}, job_id, len(written))
        return {**result, "new_groups": plan["new_groups"]}

    if inline:
        return write(progress, task_id)
    valid = sum(line["status"] == "valid" for line in plan["lines"])
    return _write_now_or_later("tables", body.request_id, job_id, valid, user, write, after)


def _add_after_discovery(job_id: int, body: "JobTablesRequest", request: Request, progress, task_id) -> dict:
    """The background half of an add with database-only lines: their tables are discovered by name, then the add is
    written as an inline one is."""
    _task_stage(task_id, "discovering")
    _resolve_databases(_selected(body.source), wait=True, request=request)
    _task_stage(task_id, "writing")
    return _add_job_tables(job_id, body, request, inline=True, progress=progress, task_id=task_id)


def _instant(value: str, name: str) -> datetime:
    """A request's date and time, with its UTC offset (as the app's own links give it), or 400 saying what is wrong
    with it rather than a server error."""
    try:
        at = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} must be a date and time such as 2026-10-29T13:00:00+00:00, not {value!r}")
    if at.tzinfo is None:
        raise HTTPException(400, f"{name} must say its UTC offset, such as 2026-10-29T13:00:00+00:00")
    return at


@app.get("/api/ingestion-jobs/{job_id}/runs")
def ingestion_job_runs(job_id: int, before: Optional[str] = None, limit: int = 25):
    """A job's job runs, newest first, `limit` slots before `before` (now when not given); `next_before` is
    the oldest slot shown, for the next page. Placed by the job's current schedule."""
    job = _schedule(job_id)
    now = datetime.now(timezone.utc)
    end = _instant(before, "before") if before else now + timedelta(seconds=1)
    limit = max(1, min(int(limit), 100))
    runs = _job_runs_of(job, end - timedelta(minutes=int(job["cadence_minutes"]) * limit), end, now)
    return {"job_runs": [{k: v for k, v in r.items() if k != "placed"} for r in runs[:limit]],
            "next_before": runs[-1]["slot"] if len(runs) >= limit else None, "job_id": job["id"], "job_name": job["name"]}


@app.get("/api/ingestion-jobs/{job_id}/runs/{slot}")
def ingestion_job_run(job_id: int, slot: str):
    """One job run: its summary (progress over all the job's tables, speed, expected end), its group runs
    (members, waiting ones with their start), its databases (failing first) and its failures by cause, with
    how many job runs before it also had failed tables."""
    job = _schedule(job_id)
    now = datetime.now(timezone.utc)
    at = _instant(slot, "the job run's time")
    cadence = int(job["cadence_minutes"])
    runs = _job_runs_of(job, at - timedelta(minutes=cadence * ij.RECENT_JOB_RUNS), at + timedelta(seconds=1), now)
    found = next((r for r in runs if r["slot"] == at.isoformat()), None)
    if not found:
        raise HTTPException(404, f"no job run of {job['name']} at {slot}")
    earlier = [r for r in runs if r["slot"] < found["slot"]]
    running = {r["run_id"]: r for r in _running()}
    tables = {name: g["enabled_tables"] for name, g in rollup.groups(_states()).items()}
    ids = found["run_ids"]
    terminating = [str(r["run_id"]) for r in db.query(q.active_databricks_runs_sql(), (["TERMINATING"],))] \
        if run_poller.table_ready() else []
    return {**{k: v for k, v in found.items() if k != "placed"},
            "group_runs": ij.member_views(found["placed"], running, tables, now, terminating),
            "databases": db.query(q.job_run_databases_sql(), (ids,)) if ids else [],
            "failures": q.group_errors(db.query(q.run_failures_sql(), (ids,))) if ids and found["failed_tables"] else [],
            "failed_before": ij.consecutive_failed(earlier), "cadence_minutes": cadence, "paused": job.get("paused"),
            "read_at": now.isoformat(), "cache_s": MONITOR_CACHE_S}


# How far back Runs reads, at most, to fill a page an outcome filter has thinned.
JOB_RUNS_HISTORY = timedelta(days=30)


@app.get("/api/job-runs")
def job_runs_across(job: Optional[str] = None, outcome: Optional[str] = None, before: Optional[str] = None,
                    limit: int = 25):
    """Job runs of every job (or one, by id), running first, then newest first, `limit` a page before
    `before`, with outcome counts over the windows read. With an outcome filter, older windows are read until
    the page is full or JOB_RUNS_HISTORY is reached. job=none lists the group runs of the groups in no job
    instead, with today's run columns. Every job means every live one: a deleted job's runs are listed and counted
    only when it is asked for by id."""
    now = datetime.now(timezone.utc)
    end = _instant(before, "before") if before else now + timedelta(seconds=1)
    limit = max(1, min(int(limit), 100))
    schedules, members = _schedules()
    live = [s for s in schedules if s.get("deleted_at") is None]
    if job == "none":
        unscheduled = sorted({s["task_collection"] for s in _states()} - {m["task_collection"] for m in _active(members)}
                             - _deleted_groups())
        rows = _with_rows_written(db.query(q.group_runs_before_sql(), (unscheduled, end, limit + 1))) if unscheduled else []
        more = len(rows) > limit
        rows = rows[:limit]
        return {"group_runs": rows, "job_runs": [], "counts": {}, "next_before": rows[-1]["started_at"] if more else None}
    chosen = live if job is None else [s for s in schedules if str(s["id"]) == str(job)]
    found, counts, start = [], {}, end
    while chosen:
        window_start = min(start - timedelta(minutes=int(s["cadence_minutes"]) * limit) for s in chosen)
        batch = []
        for s in chosen:
            batch += _job_runs_of(s, window_start, start, now)
        for r in batch:
            counts[r["outcome"]["label"]] = counts.get(r["outcome"]["label"], 0) + 1
        found += [r for r in batch if r["outcome"]["label"] not in q.RUNNING_OUTCOMES
                  and (not outcome or r["outcome"]["label"] == outcome)]
        start = window_start
        if not outcome or len(found) > limit or end - start >= JOB_RUNS_HISTORY:
            break
    found.sort(key=lambda r: r["slot"], reverse=True)
    rest = found[:limit]
    # Running job runs come from the Overview's read, so one overrunning its window is still listed.
    running = [r for r in _jobs_view()["running_job_runs"] if (job is None or str(r["job_id"]) == str(job))
               and (not outcome or outcome in q.RUNNING_OUTCOMES)] if not before else []
    more = len(found) > limit or (not outcome and len(found) >= limit)
    return {"job_runs": [{k: v for k, v in r.items() if k != "placed"} for r in running + rest],
            "counts": counts, "next_before": rest[-1]["slot"] if rest and more else None,
            "jobs": [{"id": s["id"], "name": s["name"]} for s in live]}


# ---- sources and one database -------------------------------------------------------------------

@app.get("/api/sources")
def sources():
    """Source instances (rollup.sources): how each is read, its ingestion jobs, its tables by state and the
    tables discovered on it with no control row yet, plus readable UC connections with nothing registered.
    Shared like _states; a failure to list UC connections only drops the "nothing registered" rows. Deleted jobs, and
    the groups and tables only they hold, are left out of every count."""
    def compute():
        schedules, members = _schedules()
        try:
            listed, listed_error = _connections(), None
        except Exception as e:
            listed, listed_error = [], str(e)
        live = [s for s in schedules if s.get("deleted_at") is None]
        view = rollup.sources(_live_states(), listed, db.query(q.not_ingested_sql()), live, members, _deleted_groups())
        for instance in view["instances"]:
            for j in instance["jobs"]:
                if j.get("cadence_minutes"):
                    j["description"] = sch.describe(int(j["cadence_minutes"]), sch.start_minute(j), j.get("timezone") or "UTC")
        return {**view, "connections_error": listed_error, "read_at": datetime.now(timezone.utc).isoformat(),
                "cache_s": STATES_CACHE_S}
    return _shared("sources", compute, ttl=STATES_CACHE_S)


DATABASE_TABLES_PAGE = 100


@app.get("/api/collections/{name}/databases/{server}/{database}")
def one_database(name: str, server: str, database: str, search: str = "", offset: int = 0):
    """One database of an ingestion group: its tables by state now, the group's jobs and its running group
    run, and its tables (DATABASE_TABLES_PAGE a page, failing first, then in flight, then to go) with their
    part in that run. The server "-" stands for a blank one."""
    server = "" if server == "-" else server
    counts = db.query(q.database_counts_sql(), (name, server, database))[0]
    if not counts["control_rows"]:
        raise HTTPException(404, f"no database {database} on {server or '(no server)'} in ingestion group {name}")
    sums = {k: counts[k] for k in ("tables", "no_sla_tables", "state_current", "state_late", "state_first_load",
                                    "state_failing")}
    running = next((r for r in _running() if r["task_collection"] == name), None)
    schedules, members = _schedules()
    job_names = [{"id": s["id"], "name": s["name"]} for s in schedules
                 if any(m["schedule_id"] == s["id"] and m["task_collection"] == name for m in _active(members))]
    pattern = f"%{search.strip()}%" if search.strip() else "%"
    offset = max(0, int(offset))
    rows = db.query(q.database_tables_sql(), (name, server, database, running["run_id"] if running else None, pattern,
                                              DATABASE_TABLES_PAGE, offset))
    total = db.query(q.database_tables_count_sql(), (name, server, database, pattern))[0]["tables"]
    enabled = sums.pop("tables")     # "tables" in the answer is this page of them
    return {"task_collection": name, "server": server, "src_database": database, **sums, "enabled_tables": enabled,
            "jobs": job_names, "running": running, "tables": rows, "total": total, "offset": offset,
            "limit": DATABASE_TABLES_PAGE, "read_at": datetime.now(timezone.utc).isoformat()}


# ---- runs ---------------------------------------------------------------------------------------

@app.get("/api/runs")
def runs(collection: Optional[str] = None, limit: int = 200):
    """Recent runs, each with the rows it wrote: its table loads' counts plus its batched appends' total;
    rows_partial when some loads recorded no count."""
    sql, params = q.runs_sql(collection or None, limit=limit)
    return _with_rows_written(db.query(sql, params))


def _with_rows_written(out: List[dict]) -> List[dict]:
    """Each run with the rows it wrote, its work (queries.run_work_sql): first table start to last finish,
    and tables loaded, its outcome and its speed. Running runs take their progress from _running."""
    if out:
        work = {r["run_id"]: r for r in db.query(q.run_work_sql(), ([r["run_id"] for r in out],))}
        progress = {p["run_id"]: p for p in _running()} if any(r.get("status") == "running" for r in out) else {}
        for r in out:
            w = work.get(r["run_id"], {})
            r.update({k: w.get(k) for k in ("work_started_at", "work_finished_at", "loaded")})
            batched, batched_unknown = q.batch_rows(r.get("detail"))
            r["rows_written"] = int(w.get("rows_written") or 0) + batched
            # Partial when a batch had members without a count, or a load outside the batches had none (a full
            # reload, say): either leaves part of the total unknown.
            r["rows_partial"] = bool(batched_unknown) or bool(w.get("uncounted"))
            q.with_outcome(r, progress.get(r["run_id"]))
            r["speed_tables_per_min"] = None if r["ended_without_finishing"] else q.work_speed(r)
    return out


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int):
    """One run: its row, outcome and rows written, and while it is running its live progress (the
    overview's counts), speed over the last minutes and about how long it has left. A check job's own record of
    a check comes with what it found (check), else check is None."""
    found = _with_rows_written(db.query(q.run_sql(), (int(run_id),)))
    if not found:
        raise HTTPException(404, f"run {run_id} not found")
    run = found[0]
    schedules, members = _schedules()
    key = run.get("work_id") if sch.is_check(run) else sch.launch_of(run)
    first = ({r["task_collection"]: r["started_at"] for r in db.query(q.first_started_sql(), (key, f"{key}:%"))}
             if key else {})
    run["job_runs"] = ij.job_runs_of_run(run, schedules, members, first_started=first)
    run["check"] = ij.check_found(run) if sch.is_check(run) else None
    if run["status"] == "running":
        progress = next((r for r in _running() if r["run_id"] == run["run_id"]), None)
        now = datetime.now(timezone.utc)
        run.update(progress=progress, live_speed=progress and q.live_speed(progress, now),
                   time_left=progress and q.time_left(progress, now))
        q.with_outcome(run, progress)
    return run


@app.get("/api/runs/{run_id}/databases")
def run_databases(run_id: int):
    return db.query(q.run_databases_sql(), (int(run_id),))


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: int):
    return db.query(q.run_events_sql(), (int(run_id),))


# The most in-flight tables the group run page lists.
IN_FLIGHT_LIMIT = 50


@app.get("/api/runs/{run_id}/in-flight")
def run_in_flight(run_id: int):
    """The tables a group run is loading now, longest first (at most IN_FLIGHT_LIMIT), and how many there are."""
    tables = db.query(q.in_flight_sql(), (int(run_id), IN_FLIGHT_LIMIT))
    total = len(tables) if len(tables) < IN_FLIGHT_LIMIT else db.query(q.in_flight_count_sql(), (int(run_id),))[0]["tables"]
    return {"tables": tables, "total": total, "limit": IN_FLIGHT_LIMIT}


# ---- tables -------------------------------------------------------------------------------------

TABLES_PAGE = 100


@app.get("/api/tables")
def tables(collection: Optional[str] = None, search: Optional[str] = None, status: str = "all",
           database: Optional[str] = None, after: Optional[str] = None, limit: int = 0, job: Optional[int] = None):
    """Control rows with their checkpoint, TABLES_PAGE at a time (queries.TABLES_ORDER). `after` is the
    previous page's `next_after` (JSON); `database` filters on one database exactly."""
    if status not in q.TABLE_FILTERS:
        raise HTTPException(400, f"status must be one of {q.TABLE_FILTERS}")
    limit = max(1, min(int(limit or TABLES_PAGE), 500))
    cursor = json.loads(after) if after else None
    if cursor is not None and (not isinstance(cursor, list) or len(cursor) != 5):
        raise HTTPException(400, "after must be the previous page's next_after")
    sql, params = q.tables_sql(collection or None, search or None, status, limit + 1, database=database, after=cursor,
                               job=job)
    rows = db.query(sql, params)
    last = rows[limit - 1] if len(rows) > limit else None      # one row more than a page says there is a next page
    rows = rows[:limit]
    return {"tables": rows, "limit": limit,
            "next_after": [last["task_collection"], last.get("src_database") or "", last["src_schema"], last["src_table"],
                           last["id"]] if last else None}


class Ids(BaseModel):
    ids: List[int] = Field(min_length=1)


class EnableRequest(Ids):
    enabled: bool


@app.post("/api/tables/enable")
def set_enabled(body: EnableRequest, request: Request):
    batch = uuid.uuid4().hex[:12]
    checked = _refuse_rows_groups(body.ids, request)
    with _write() as cur:
        rows = _locked_rows(cur, body.ids)
        _recheck_locked_rows(rows, checked, request)
        _lock_collections(cur, [r["task_collection"] for r in rows])
        cur.execute(q.set_enabled_sql(), (body.enabled, body.ids))
        _rewrite_rollups_for_ids(cur, body.ids)
        for r in rows:
            if bool(r["task_enabled"]) != body.enabled:
                _log(cur, _user(request), "enable" if body.enabled else "disable", r["id"], r["task_collection"],
                     "task_enabled", r["task_enabled"], body.enabled, batch=batch)
        _touch(cur, _user(request), groups=[r["task_collection"] for r in rows])
    return {"updated": len(rows)}


@app.post("/api/tables/reseed")
def reseed(body: Ids, request: Request):
    batch = uuid.uuid4().hex[:12]
    checked = _refuse_rows_groups(body.ids, request)
    with _write() as cur:
        rows = _locked_rows(cur, body.ids)
        _recheck_locked_rows(rows, checked, request)
        _lock_collections(cur, [r["task_collection"] for r in rows])
        cur.execute(q.force_reseed_sql(), (body.ids,))
        n = cur.rowcount
        _rewrite_rollups_for_ids(cur, body.ids)
        cur.execute(q.log_reseed_sql(), ("manual", body.ids))
        for r in rows:
            _log(cur, _user(request), "reseed", r["id"], r["task_collection"], reseed="manual", batch=batch)
        _touch(cur, _user(request), groups=[r["task_collection"] for r in rows])
    return {"updated": n}


@app.post("/api/tables")
def add_table(row: dict, request: Request):
    _refuse_groups_outside_jobs([row.get("task_collection")])
    _refuse_groups([row.get("task_collection")], request)
    errs = q.validate_control_row(row)
    if errs:
        raise HTTPException(422, {"errors": errs})
    _refuse_rows_off_connections([row])
    _refuse_host_reads([row], _user(request))
    with _write() as cur:
        _lock_collections(cur, [row.get("task_collection")])
        cur.execute(q.insert_control_sql(), q.control_params(row))
        new_id = cur.fetchone()[0]
        _rewrite_rollups(cur, [(row.get("task_collection"), row.get("src_database"))])
        _refuse_mixed_sinks(cur, [row])
        _refuse_split_databases(cur, [row])
        _log(cur, _user(request), "add", new_id, row.get("task_collection"))
        _touch(cur, _user(request), groups=[row.get("task_collection")])
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
    new_coll = body.changes.get("task_collection")
    checked = set()
    if not body.dry_run:
        _refuse_groups_outside_jobs([new_coll])
        checked = _refuse_rows_groups(body.ids, request, also=[new_coll])
    try:
        with _write() as cur:
            rows = _locked_rows(cur, body.ids)
            plans = q.plan_edit(rows, body.changes, bulk=bulk)
            summary = {"rows": len(plans), "changed": sum(1 for p in plans if p["changes"]),
                       "reseed": sum(1 for p in plans if p["reseed"]),
                       "errors": sum(1 for p in plans if p["errors"]), "applied": False}
            if body.dry_run or summary["errors"] or not summary["changed"]:
                cur.connection.rollback()
                return {"summary": summary, "plans": plans}
            _recheck_locked_rows(rows, checked, request)
            _lock_collections(cur, [r["task_collection"] for r in rows] + ([new_coll] if new_coll else []))
            _apply_plans(cur, rows, plans, _user(request), batch)
            _touch(cur, _user(request), groups=[r["task_collection"] for r in rows] + [new_coll])
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
    active, recent = run_poller.runs_of(jid, active_only=True, limit=50), run_poller.runs_of(jid, limit=25)
    # A run a wrapper job started (trigger RUN_JOB_TASK) came from a schedule its collection was in
    # when the run started.
    joined = _scheduled_collections(with_added=True)
    at = lambda v: datetime.fromisoformat(v) if isinstance(v, str) else v
    for r in active + recent:
        r["schedule"] = ", ".join(n for n, added in joined.get(r["task_collection"], [])
                                  if r["started"] and at(r["started"]) >= at(added)) or None \
            if r["trigger"] == "RUN_JOB_TASK" else None
    return {"job": jobs.job_summary(jid), "active": active, "recent": recent}


class RunRequest(BaseModel):
    task_collection: str = Field(min_length=1)
    parallelism: int = Field(default=192, ge=1, le=256)


@app.post("/api/jobs/ingest/run")
def run_ingest(body: RunRequest, request: Request):
    _refuse_run_of(_require(jobs.INGEST_JOB_ID, "ingestion"), request)
    _refuse_unready_groups([body.task_collection], preparing.run_refusal)
    rid = jobs.run_now(_require(jobs.INGEST_JOB_ID, "ingestion"),
                       {"task_collection": body.task_collection, "parallelism": str(body.parallelism)})
    return {"run_id": rid}


@app.post("/api/jobs/runs/{run_id}/cancel")
def cancel_run(run_id: int, request: Request):
    """Cancel a run of one of the app's own Databricks jobs, for a user with Can Manage Run on that job."""
    job_id = run_poller.run_state_of(run_id)["job_id"]
    if str(job_id) not in {str(j) for j in _bundle_job_ids() if j}:
        raise HTTPException(403, "the app only cancels runs of its own Databricks jobs")
    _refuse_run_of(job_id, request)
    jobs.cancel(run_id)
    return {"cancelled": run_id}


# ---- Manage: the app's own Databricks jobs and the export, for those who manage the app -----------------------------
MANAGE_JOBS = (("Ingestion", "INGEST_JOB_ID"), ("Check", "CHECK_JOB_ID"), ("Discovery", "DISCOVER_JOB_ID"),
               ("Cluster copies", "CLUSTER_COPIES_JOB_ID"), ("Delete", "DELETE_JOB_ID"),
               ("Schema setup", "SETUP_JOB_ID"))


@app.get("/api/manage/jobs")
def manage_jobs(request: Request):
    """The Databricks jobs the app runs ingestion jobs with, each with its name and a link, as deployed."""
    _refuse(_manage_refusal(_user(request)))
    host, out = jobs.workspace_url(), []
    for role, variable in MANAGE_JOBS:
        job_id = getattr(jobs, variable, None)
        if not job_id:
            continue
        try:
            name, error = jobs.job_summary(job_id)["name"], None
        except Exception as e:
            name, error = None, str(e).split(". ")[0]
        out.append({"role": role, "job_id": int(job_id), "name": name, "error": error,
                    "url": f"{host}/jobs/{job_id}" if host else None})
    return out


@app.get("/api/manage/version")
def manage_version(request: Request):
    """Which version of the app runs: the source it was deployed from, and its deployment on Databricks Apps. Also
    the SQL warehouse its queries run on, and which setting chose it (warehouse.description)."""
    _refuse(_manage_refusal(_user(request)))
    try:
        deployment, error = jobs.app_deployment(), None
    except Exception as e:
        deployment, error = {}, str(e).split(". ")[0]
    return {"app": jobs.APP_NAME, "version": jobs.APP_VERSION, "deployment": deployment, "error": error,
            "schema": _schema_state(), "warehouse": warehouse.description()}


def _schema_state() -> dict:
    """The state database's schema version against the one this code expects, the last upgrade check's outcome
    (schema_upgrade.LAST), and the setup job's run going, if any, while the setup job upgrades it."""
    out = {"database": None, "app": schema_upgrade.SCHEMA_VERSION, "check": dict(schema_upgrade.LAST), "run": None,
           "error": None}
    try:
        exists = db.query(q.schema_version_table_sql())[0]["to_regclass"] is not None
        out["database"] = int(db.query(q.schema_version_sql())[0]["version"]) if exists else 0
    except Exception as e:
        out["error"] = f"can't read the state database's schema version: {str(e).split('. ')[0]}"
    try:
        going = run_poller.runs_of(jobs.SETUP_JOB_ID, active_only=True, limit=1) if jobs.SETUP_JOB_ID else []
        out["run"] = {"run_id": going[0]["run_id"], "url": going[0].get("url")} if going else None
    except Exception as e:
        out["error"] = out["error"] or f"can't read the setup job's runs: {str(e).split('. ')[0]}"
    return out


@app.get("/api/manage/export.csv")
def manage_export(request: Request, jobs_: str = Query("", alias="jobs")):
    """The configured tables of some ingestion jobs (their ids, comma-separated; blank for every one) as the CSV Create
    from one CSV reads (config_import.export_columns), each line naming its job. Streamed page by page (keyset by
    control id), so a 200,000-table export never holds more than a page. Enabled tables only: what the jobs ingest."""
    _refuse(_manage_refusal(_user(request)))
    try:
        ids = [int(part) for part in jobs_.split(",") if part.strip()] or None
    except ValueError:
        raise HTTPException(422, "jobs: comma-separated ingestion job ids")
    members = db.query(q.export_groups_sql(), (ids, ids))
    job_of = {m["task_collection"]: m["name"] for m in members}
    groups = sorted(job_of)
    attrs = [r["name"] for r in db.query(q.export_attr_names_sql(), (groups,))] if groups else []
    columns = ci.export_columns(bool(groups) and bool(db.query(q.export_reads_jdbc_sql(), (groups,))[0]["jdbc"]))

    def lines():
        yield ci.export_header(columns, attrs)
        last = 0
        while groups:
            page = db.query(q.export_page_sql(), (groups, last, q.EXPORT_PAGE))
            if not page:
                return
            yield ci.export_lines([{**r, ci.JOB_COLUMN: job_of[r["task_collection"]]} for r in page], columns, attrs)
            last = page[-1]["id"]
    name = "ingestion-jobs" if not ids or len(ids) > 1 else q.identifier(members[0]["name"]) if members \
        else "ingestion-job"
    return StreamingResponse(lines(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


def _bundle_job_ids() -> list:
    return [*jobs.ingest_variants().values(), jobs.CHECK_JOB_ID, jobs.DISCOVER_JOB_ID]


# ---- bulk import --------------------------------------------------------------------------------

class DiscoverRequest(BaseModel):
    # Blank when a JDBC connection is named: its host, port and secret key come from the connection (jdbc_connections).
    src_host: str = ""
    src_port: Optional[int] = None
    databases: str = Field(min_length=1, description="comma- or newline-separated")
    secret_key_template: str = Field(default="{database}", min_length=1)
    jdbc_connection: Optional[str] = Field(None, max_length=64)


@app.post("/api/discoveries")
def start_discovery(body: DiscoverRequest, request: Request):
    names = q.parse_databases(body.databases)
    if not names:
        raise HTTPException(422, "at least one database is required")
    if not body.jdbc_connection and not body.src_host.strip():
        raise HTTPException(422, "a host is required, or a JDBC connection")
    if not body.jdbc_connection:
        _refuse(_host_read_refusal(_user(request)))
    _refuse_run_of(_require(jobs.DISCOVER_JOB_ID, "discovery"), request)
    did = uuid.uuid4().hex[:12]
    spec = {"src_type": "sqlserver", "src_host": body.src_host.strip(), "src_port": body.src_port or None,
            "databases": names, "secret_key_template": body.secret_key_template.strip(),
            "requested_by": _user(request)}
    parameters: dict = {}
    if body.jdbc_connection:
        connection_spec, parameters = _jdbc_discovery(body.jdbc_connection, names)
        spec.update(connection_spec)
    rid = jobs.run_now(_require(jobs.DISCOVER_JOB_ID, "discovery"),
                       {"discovery_id": did, "spec": json.dumps(spec), **parameters})
    return {"discovery_id": did, "run_id": rid, "databases": len(names)}


class JdbcConnectionRequest(BaseModel):
    """A JDBC connection entered by hand (jdbc_connections.py). The secret itself is never sent: the admin creates the
    scope and its JSON secret beforehand, and the connection names the scope and the key."""
    name: str = ""
    src_type: str = "sqlserver"
    src_host: str = ""
    src_port: Optional[int] = None
    secret_scope: str = ""
    secret_key: str = ""
    trust_server_certificate: bool = False


@app.get("/api/jdbc-connections")
def jdbc_connections() -> dict:
    """The JDBC connections made by hand: each one's name, where its source is, its scope and key names, and its trust
    choice. Names only, so anyone who can see Discover can see them."""
    return {"connections": [jc.public(row) for row in db.query(jc.list_sql())]}


@app.post("/api/jdbc-connections")
def create_jdbc_connection(body: JdbcConnectionRequest, request: Request):
    """Make a JDBC connection by hand. Any signed-in user may: it names a host, a scope and a key, never a secret.
    The app stores the names it is given, and a name already taken is refused, not replaced."""
    user = _user(request)
    _refuse(_signed_in_refusal(user))
    clean, errors = jc.validate(body.model_dump())
    if errors:
        raise HTTPException(422, {"errors": errors})
    refusal = _connection_scope_refusal({**clean, "created_by": user})
    if refusal:
        raise HTTPException(422, {"errors": [refusal]})
    scope = jl.dedicated_scope()
    if scope and clean["secret_scope"] == scope and not _login_typed_for(clean, user):
        raise HTTPException(422, {"errors": [f"a connection in the login scope {scope} needs its login first: save the "
                                              "login under this connection's name and key, as the same user, then make "
                                              "the connection"]})
    with db.transaction() as cur:
        cur.execute(jc.insert_sql(), jc.insert_params(clean, user))
        made = cur.fetchone()
    if made is None:
        raise HTTPException(409, f"a JDBC connection named '{clean['name']}' already exists")
    return {"connection": jc.public(db.query(jc.get_sql(), (clean["name"],))[0])}


def _jdbc_discovery(name: str, databases: Iterable[str] = ()) -> Tuple[dict, dict]:
    """(the discovery spec's parts, the discover job's parameters) for a discovery through the JDBC connection named
    here: its host, port, type, key template and name, and its scope and trust. Read from the stored connection, never
    from the request, so a discovery can't name a scope the connection doesn't. The databases it names must each have
    a login typed under this connection (_refuse_connection_read)."""
    rows = db.query(jc.get_sql(), (name,))
    if not rows:
        raise HTTPException(422, {"errors": [f"no JDBC connection named '{name}'"]})
    connection = rows[0]
    if databases == "*" and _per_database_logins(connection):
        raise HTTPException(422, {"errors": [
            f"a whole server can't be read through the connection {connection['name']}: it takes one login per "
            "database, and each is checked by the database's name before it is read, which a whole server's "
            "databases don't have until they are listed. Name the databases instead"]})
    _refuse_connection_read(connection, _database_keys(connection, databases))
    spec = {"src_type": connection["src_type"], "src_host": connection["src_host"],
            "src_port": connection["src_port"], "secret_key_template": connection["secret_key"],
            "jdbc_connection": connection["name"]}
    return spec, jc.job_parameters(connection)


def _rediscovery_reading(spec: dict, maker: Optional[str] = None) -> Tuple[dict, dict]:
    """(the spec, the discover job's parameters) a re-discovery runs. A spec read through a JDBC connection takes the
    connection's host, port, type and key template, and its scope and trust, as stored now, and its keys are checked
    as a new discovery's are (_jdbc_discovery). A spec read by host and password outside a connection runs only for a
    job an app manager made (maker), as a new discovery by host is a manager's (_host_read_refusal). Any other spec
    runs as it is, with the job's own defaults."""
    name = str(spec.get("jdbc_connection") or "")
    if not name:
        if not spec.get("src_connection"):
            refusal = _host_read_refusal(maker or "")
            if refusal:
                raise HTTPException(403, refusal)
        return spec, {}
    connection_spec, parameters = _jdbc_discovery(name, spec.get("databases") or [])
    return {**spec, **connection_spec}, parameters


JDBC_PARAMETERS = ("secret_scope", "trust_server_certificate")


def _jdbc_source(source: Optional[Selection]) -> Optional[dict]:
    """The JDBC connection a selection's discoveries were made through (its row), or None when none was. A selection
    that mixes a JDBC discovery with another source, or with a second connection, is refused: one job reads one login."""
    if source is None or source.upload_id:
        return None
    rows = db.query(q.selection_discoveries_sql(), (source.source()[1],))
    names = {str((row.get("listing") or {}).get("jdbc_connection") or "") for row in rows}
    if not any(names):
        return None
    if "" in names or len(names) > 1:
        raise HTTPException(422, {"errors": ["a job reads one source: its tables can't come from a JDBC connection and "
                                              "from another source, or from two JDBC connections"]})
    connection = db.query(jc.get_sql(), (next(iter(names)),))
    if not connection:
        raise HTTPException(422, {"errors": [f"no JDBC connection named '{next(iter(names))}'"]})
    _refuse_connection_read(connection[0])
    return connection[0]


def _job_jdbc(job_id: int) -> Optional[dict]:
    """The JDBC connection an ingestion job reads through, for its page and its Edit: its name, host, port, type, secret
    scope and key names, and trust (jdbc_connections.public). None for a job that reads through none."""
    selection = db.query(q.job_selection_sql(), (int(job_id),))
    ids = sorted({i for part in selection if part["source_kind"] == "discovery"
                  for i in str(part["source_id"] or "").split(",") if i})
    names = {str((row.get("listing") or {}).get("jdbc_connection") or "")
             for row in (db.query(q.selection_discoveries_sql(), (ids,)) if ids else [])}
    if len(names) != 1 or "" in names:
        return None
    name = next(iter(names))
    rows = db.query(jc.get_sql(), (name,))
    return jc.public(rows[0]) if rows else {"name": name, "missing": True}


def _job_connection_row(job_id: int) -> Optional[dict]:
    """The stored row of the JDBC connection an ingestion job reads through, or None when it reads through none or its
    connection has gone. The raw row, not the public view: jc.job_parameters reads the trust choice as stored."""
    shown = _job_jdbc(job_id)
    if shown is None or shown.get("missing"):
        return None
    rows = db.query(jc.get_sql(), (shown["name"],))
    return rows[0] if rows else None


def _names_login_scope(parameters: Optional[dict]) -> bool:
    """Whether job parameters name the login scope as their secret scope."""
    scope = jl.dedicated_scope()
    return bool(scope) and str((parameters or {}).get("secret_scope") or "") == scope


def _refuse_login_scope(parameters: Optional[dict], connection: Optional[dict]) -> None:
    """The login scope keeps the logins typed into the app. A job reads it only through a JDBC connection, which reads
    its own login (see _login_typed_for); any other job that names it is refused."""
    if connection is None and _names_login_scope(parameters):
        scope = jl.dedicated_scope()
        reason = (f"the scope {scope} keeps the logins typed into the app: a job reads it only through "
                  "a JDBC connection")
        raise HTTPException(422, {"errors": [reason]})


def _login_typed_for(connection: dict, user: str) -> bool:
    """Whether this user typed a login under the connection's name for a key its key template makes. A connection in the
    login scope is made only then, so it can't name a login another user typed. The shape is matched here; each
    database's key is checked exactly where it is read (_login_key_reasons)."""
    keys = db.query(q.jdbc_connection_login_sql(), (jl.dedicated_scope(), connection["name"], user))
    return any(jc.key_fits(connection["secret_key"], row["secret_key"]) for row in keys)


def _connection_scope_refusal(connection: dict) -> Optional[str]:
    """Why a JDBC connection may not read the secret scope it names, or None when it may. The login scope is open to any
    connection: its logins are typed into the app, and a connection there is backed by one (_login_typed_for). Any other
    scope holds secrets an admin wrote, so only a connection its maker could manage the app with may name it. This is
    checked when a connection is made and again when it is read, so a connection made before the rule, or with no maker
    on record, is held to it too."""
    scope = jl.dedicated_scope()
    if scope and connection["secret_scope"] == scope:
        return None
    if _manage_refusal(connection.get("created_by") or "") is None:
        return None
    return (f"the secret scope {connection['secret_scope']} holds secrets an admin wrote, so a JDBC connection may name it "
            "only when an app manager makes it. Any signed-in user may make a connection in the login scope "
            f"{scope or '(not set here)'}, which holds the logins typed into the app")


def _per_database_logins(connection: dict) -> bool:
    """Whether a connection reads one typed login per database: it is in the login scope and its key holds {database}."""
    scope = jl.dedicated_scope()
    return bool(scope) and connection["secret_scope"] == scope and jc.DATABASE_TOKEN in connection["secret_key"]


def _database_keys(connection: dict, databases) -> List[str]:
    """The secret keys a discovery through the connection reads: its key template with each named database's name in
    place of {database}. A whole server ('*') reads its own database (master) to list the others, so that key is read
    too. The databases a whole server lists are named by the source, not by the request, so they can't be checked
    before the read: _jdbc_discovery refuses a whole server through a connection with one login per database."""
    names = [SERVER_DATABASE] if databases == "*" else [str(name) for name in databases]
    return [connection["secret_key"].replace(jc.DATABASE_TOKEN, name) for name in names]


def _login_key_reasons(connection: dict, keys: Iterable[str], cur=None) -> List[str]:
    """Why some of the keys a read through the connection names can't be read, or [] when all can. Only a connection in
    the login scope is checked. Each key must hold the newest login typed under this connection's name, so a login typed
    for another connection never reaches this connection's host. The keys are exact: one per database the read names."""
    scope = jl.dedicated_scope()
    if not scope or connection["secret_scope"] != scope:
        return []
    wanted = sorted({str(key) for key in keys if key})
    if not wanted:
        return []
    params = (scope, wanted)
    found = db.fetch(cur, q.jdbc_login_latest_per_key_sql(), params) if cur is not None \
        else db.query(q.jdbc_login_latest_per_key_sql(), params)
    newest = {row["secret_key"]: row["connection"] for row in found}
    missing = [key for key in wanted if newest.get(key) != connection["name"]]
    if not missing:
        return []
    shown = ", ".join(missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
    return [f"the newest login under the key(s) {shown} was not typed for the connection {connection['name']}: save "
            "each database's login with this connection's name first"]


def _refuse_connection_read(connection: dict, keys: Iterable[str] = (), cur=None) -> None:
    """Refuses a read through a JDBC connection whose maker may not name its scope (_connection_scope_refusal), or whose
    keys don't hold the newest login typed under its name (_login_key_reasons)."""
    refusal = _connection_scope_refusal(connection)
    if refusal:
        raise HTTPException(422, {"errors": [refusal]})
    reasons = _login_key_reasons(connection, keys, cur=cur)
    if reasons:
        raise HTTPException(422, {"errors": reasons})


# A control row's source and login: where it reads and with which secret. On a row of a job that reads through a stored
# JDBC connection they belong to the connection, so they are never edited there, and a row written into such a job must
# carry the connection's own.
CONNECTION_FIELDS = ("src_type", "src_host", "src_port", "secret_key", "use_remote_query", "src_connection")


def _port_of(value, src_type: str) -> Optional[int]:
    """A row's port as a number, its source type's default port when blank, or None when it isn't a number."""
    if value in (None, ""):
        return q.DEFAULT_PORTS.get(src_type, 1433)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _connection_row_reasons(connection: dict, rows: Iterable[dict]) -> List[str]:
    """Why some control rows don't read through the JDBC connection whose job they are written into, or [] when all do.
    Each must read the connection's host, port and source type, by host rather than through a Unity Catalog
    connection, with a secret key the connection's key template makes."""
    src_type = str(connection["src_type"] or "sqlserver").lower()
    expected = (str(connection["src_host"] or "").strip().lower(), _port_of(connection["src_port"], src_type), src_type)
    off = []
    for row in rows:
        row_type = str(row.get("src_type") or "sqlserver").lower()
        where = (str(row.get("src_host") or "").strip().lower(), _port_of(row.get("src_port"), row_type), row_type)
        if (where != expected or q._truthy(row.get("use_remote_query")) or str(row.get("src_connection") or "").strip()
                or not jc.key_fits(connection["secret_key"], str(row.get("secret_key") or ""))):
            off.append(".".join(str(row.get(part) or "") for part in ("src_database", "src_schema", "src_table")))
    if not off:
        return []
    shown = ", ".join(off[:5]) + (f" and {len(off) - 5} more" if len(off) > 5 else "")
    return [f"the job reads through the JDBC connection {connection['name']}, so its tables take that connection's host, "
            f"port, source type and key: {shown} name another"]


def _refuse_connection_rows(connection: dict, rows: List[dict], cur=None) -> None:
    """Refuses writing control rows into a job that reads through this JDBC connection unless each reads through it
    (_connection_row_reasons), the connection may read its scope, and each row's key holds the newest login typed
    under the connection's name (_refuse_connection_read)."""
    reasons = _connection_row_reasons(connection, rows)
    if reasons:
        raise HTTPException(422, {"errors": reasons})
    _refuse_connection_read(connection, [row.get("secret_key") for row in rows], cur=cur)


def _group_connections(groups: Iterable[Optional[str]]) -> Dict[str, dict]:
    """The stored JDBC connection each of these ingestion groups reads through: the connection of the job that holds it.
    A group whose job reads through none is left out. Read fresh, not shared."""
    wanted = {g for g in groups if g}
    if not wanted:
        return {}
    schedules = db.query(q.schedules_sql())
    members = db.query(q.schedule_members_sql(), ([s["id"] for s in schedules],)) if schedules else []
    of_job: Dict[int, Optional[dict]] = {}
    found: Dict[str, dict] = {}
    for member in members:
        if member["removed_at"] is not None or member["task_collection"] not in wanted:
            continue
        job_id = int(member["schedule_id"])
        if job_id not in of_job:
            of_job[job_id] = _job_connection_row(job_id)
        if of_job[job_id] is not None:
            found[member["task_collection"]] = of_job[job_id]
    return found


def _refuse_rows_off_connections(rows: List[dict], cur=None, connections: Optional[Dict[str, dict]] = None) -> None:
    """Refuses control rows written into the groups of a job that reads through a JDBC connection, unless each reads
    through that connection (_refuse_connection_rows). Rows of any other group pass."""
    by_group: Dict[str, List[dict]] = {}
    for row in rows:
        by_group.setdefault(row.get("task_collection") or "", []).append(row)
    connections = _group_connections(by_group) if connections is None else connections
    for group in sorted(by_group):
        if group in connections:
            _refuse_connection_rows(connections[group], by_group[group], cur=cur)


def _refuse_connection_edits(rows: List[dict], plans: List[dict], cur=None, user: Optional[str] = None) -> None:
    """Refuses an edit of a table's source or login (CONNECTION_FIELDS) on a row of a job that reads through a JDBC
    connection: they belong to the connection, and an edited key or host would send its login elsewhere. A row moved
    into such a job's group must read through its connection, as a row written there must. Outside a connection, an
    edit of those fields, or one that moves the table to another group, that leaves a table read by host and password
    is an app manager's (_refuse_host_reads)."""
    by_id = {r["id"]: r for r in rows}
    edited = [dict(by_id[p["id"]], **{f: v[1] for f, v in p["changes"].items()}) for p in plans if p["changes"]]
    connections = _group_connections({r["task_collection"] for r in rows} | {r["task_collection"] for r in edited})
    if user is not None:
        resourced = [dict(by_id[p["id"]], **{f: v[1] for f, v in p["changes"].items()}) for p in plans
                     if set(p["changes"]) & {*CONNECTION_FIELDS, "task_collection"}]
        _refuse_host_reads(resourced, user, connections)
    if not connections:
        return
    for p in plans:
        group = by_id[p["id"]]["task_collection"]
        touched = sorted(set(p["changes"]) & set(CONNECTION_FIELDS))
        if group in connections and touched:
            raise HTTPException(422, {"errors": [
                f"{', '.join(touched)} of a table read through the JDBC connection {connections[group]['name']} belong "
                "to the connection and can't be edited on the table"]})
    _refuse_rows_off_connections(edited, cur=cur, connections=connections)


def _reads_a_secret(row: dict) -> bool:
    """Whether a control row reads its source with a secret from its job's scope: by host (Spark JDBC) and not through
    a Unity Catalog connection's remote_query, which takes no secret."""
    return not q._truthy(row.get("use_remote_query"))


def _host_read_refusal(user: str) -> Optional[str]:
    """Why this user may not set up a read by host and password outside a JDBC connection, or None. Such a read takes
    its login from the secret scope its job names, by default the admin's scope, whose secrets an admin wrote, and
    sends it to the host the request names. So, as for a connection naming such a scope (_connection_scope_refusal),
    only an app manager may; anyone else reads through a JDBC connection in the login scope."""
    refusal = _manage_refusal(user)
    if refusal is None:
        return None
    return ("a read by host and password outside a JDBC connection takes its login from a secret scope an admin "
            "wrote, so only an app manager may set one up: make a JDBC connection in the login scope and read "
            f"through it instead ({refusal})")


def _refuse_host_reads(rows: List[dict], user: str, connections: Optional[Dict[str, dict]] = None) -> None:
    """Refuses writing control rows that read by host and password (_reads_a_secret) into groups whose job reads
    through no JDBC connection, unless the user is an app manager (_host_read_refusal). Rows of a connection's job are
    held to the connection instead (_refuse_rows_off_connections)."""
    groups = {row.get("task_collection") or "" for row in rows}
    connections = _group_connections(groups) if connections is None else connections
    if any(_reads_a_secret(row) and (row.get("task_collection") or "") not in connections for row in rows):
        _refuse(_host_read_refusal(user))


def _refuse_scope_parameter(parameters: Optional[dict], user: str, stored: Optional[dict] = None) -> None:
    """Refuses a job's secret_scope parameter set or changed by someone who isn't an app manager: it names the scope
    the job's tables read by host and password take their logins from. A job reading through a JDBC connection takes
    its connection's scope instead, so it never comes here; an edit that keeps the stored value passes."""
    wanted = str((parameters or {}).get("secret_scope") or "")
    if wanted and wanted != str((stored or {}).get("secret_scope") or ""):
        _refuse(_host_read_refusal(user))


class SourceDiscoverRequest(BaseModel):
    """A discovery through a Unity Catalog connection, as Add tables' Discover on a source starts it: list the
    server's databases (the connection test, run by Next on the connection step), or read the tables of some databases or of the whole server."""
    src_connection: str = Field(min_length=1)
    kind: str = Field(default="tables", pattern="^(list|tables)$")
    databases: List[str] = []
    whole_server: bool = False
    # A database name filter for listing the server, as SQL Server's LIKE reads it (discovery.pattern_errors).
    pattern: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_%\-\[\]\.$#@]*$")
    # For an Azure SQL server whose login can't open master: a Unity Catalog service credential with Reader on the
    # server, and the server's resource id. The discovery job then lists the databases through Azure
    # (discovery.azure_databases); the app never calls Azure. Both or neither.
    azure_credential: str = Field(default="", max_length=255, pattern=r"^[A-Za-z0-9_\-]*$")
    azure_resource_id: str = Field(default="", max_length=512, pattern=r"^(/subscriptions/[0-9A-Za-z-]+/resourceGroups/"
                                   r"[-\w.()]+/providers/Microsoft\.Sql/(servers|managedInstances)/[a-z0-9][a-z0-9-]*)?$")
    # Reading a whole server through the SQL warehouse: the discovery its connection test listed, and the filter the
    # whole-server tick was made under (in the database's name).
    listing_id: str = Field(default="", max_length=64, pattern=r"^[0-9a-f]*$")
    filter: str = Field(default="", max_length=128)
    # A read through the warehouse started as databases are chosen, before the job exists (a draft): it belongs to no
    # job until Create takes it, and a page cancelled or left discards it. draft_id adds databases to a draft already
    # started, so one choice is one draft however many times it grows.
    draft: bool = False
    draft_id: str = Field(default="", max_length=64, pattern=r"^[0-9a-f]*$")


def _connection_unseen(name: str, listing_error: Optional[str]) -> str:
    """Why the app can't use a connection, naming the grant it lacks: the app lists connections as its own service
    principal, which sees only those it has USE CONNECTION on (the connection is not the bundle's, so its owner
    grants it)."""
    who = f"its service principal ({jobs.APP_PRINCIPAL})" if jobs.APP_PRINCIPAL else "its service principal"
    why = f"the app can't list connections ({listing_error.split('. ')[0]})" if listing_error else \
        f"the app can't see the connection '{name}'"
    return (f"{why}: {who} needs USE CONNECTION on '{name}'. Its owner grants it in Catalog Explorer: Connections, "
            f"{name}, Permissions, Grant, USE CONNECTION to the app; then Refresh the list.")


# The database a server's databases are listed from.
SERVER_DATABASE = "master"


def _require_connection(name: str) -> None:
    """Refuse a connection the app can't see, naming the grant it lacks."""
    listed = connections()
    if name not in {c["name"] for c in listed["connections"]}:
        raise HTTPException(404, _connection_unseen(name, listed.get("error")))


def _listing_answer(discovery_id: str, connection: str, user: Optional[str], answer: dict) -> dict:
    """Where a listing through the warehouse stands, for the page; once it has ended, its databases saved (status
    listed) or, when it failed, why, on a row for master as the discovery job records it. A statement still going
    answers with its id and the warehouse's state, so the page can say it is starting and ask again."""
    base = {"discovery_id": discovery_id, "via": "warehouse"}
    state = answer["state"]
    if state not in warehouse.ENDED:
        return {**base, "statement_id": answer["statement_id"], "state": state, "warehouse": warehouse.warehouse_state()}
    listing = json.dumps({"via": "warehouse"})
    if state != "SUCCEEDED":
        error = answer.get("error") or f"the listing ended {state}"
        db.execute(q.insert_discovery_rows_sql(1),
                   (discovery_id, "sqlserver", connection, SERVER_DATABASE, "failed", error, None, user, listing))
        return {**base, "state": "FAILED", "error": error, "codes": source_catalog.error_codes(error)}
    names = sorted({str(r["name"]) for r in answer["rows"] or []}, key=lambda n: (n.lower(), n))
    for at in range(0, len(names), q.CONTROL_BATCH):
        chunk = names[at:at + q.CONTROL_BATCH]
        db.execute(q.insert_discovery_rows_sql(len(chunk)),
                   tuple(v for name in chunk
                         for v in (discovery_id, "sqlserver", connection, name, "listed", None, None, user, listing)))
    return {**base, "state": "SUCCEEDED", "databases": len(names)}


@app.get("/api/discoveries/{discovery_id}/warehouse/{statement_id}")
def warehouse_listing(discovery_id: str, statement_id: str, connection: str, request: Request):
    """A listing through the warehouse, asked again until it ends (_listing_answer)."""
    _require_connection(connection)
    return _listing_answer(discovery_id, connection, _user(request), warehouse.read(statement_id))


class WarehouseReadStore:
    """Lakebase's side of warehouse_reads: where a discovery's checks and reads stand, on its discovery rows."""

    def in_flight(self, kind: str, discovery_id: str) -> List[tuple]:
        return [(r["src_database"], r["statement_id"]) for r in db.query(q.warehouse_in_flight_sql(kind), (discovery_id,))]

    def claim(self, kind: str, discovery_id: str, first: List[str], limit: int, claim_s: int) -> List[str]:
        if limit <= 0:
            return []
        with db.transaction() as cur:
            return [r["src_database"] for r in db.fetch(cur, q.warehouse_claim_sql(kind), (discovery_id, claim_s, first, limit))]

    def claim_reads(self, server: str, discovery_ids: List[str], cap: int, limit: int,
                    claim_s: int) -> Tuple[List[tuple], bool]:
        """Claim up to ``limit`` databases to read of these discoveries of one server, never more than its cap leaves
        room for: the reads in flight on the server, of every discovery and app instance, are counted in this same
        transaction under the server's lock (a transaction lock: the connection goes back to the pool). Returns the
        (discovery, database) pairs claimed, and whether the server is now at its cap."""
        if limit <= 0 or not discovery_ids:
            return [], False
        with db.transaction() as cur:
            cur.execute(q.warehouse_reads_lock_sql(), (q.warehouse_reads_lock_key(server),))
            going = int(db.fetch(cur, q.warehouse_reads_going_sql(), (server, claim_s))[0]["going"] or 0)
            room = min(int(limit), int(cap) - going)
            if room <= 0:
                return [], True
            ids = list(discovery_ids)
            rows = db.fetch(cur, q.warehouse_claim_reads_sql(), (server, ids, claim_s, ids, room))
        return [(r["discovery_id"], r["src_database"]) for r in rows], going + len(rows) >= int(cap)

    def collect(self, discovery_id: str, claim_s: int, limit: int) -> List[tuple]:
        if limit <= 0:
            return []
        return [(r["src_database"], r["statement_id"])
                for r in db.query(q.warehouse_collect_sql(), (discovery_id, claim_s, limit))]

    def hold(self, reads: List[tuple]) -> None:
        if reads:
            db.execute(q.warehouse_hold_sql(), ([d for d, _ in reads], [n for _, n in reads]))

    def read_work(self, claim_s: float, max_age_s: float, draft_ttl_s: float) -> List[dict]:
        return db.query(q.warehouse_read_work_sql(), (claim_s, max_age_s, draft_ttl_s))

    def started(self, kind: str, discovery_id: str, database: str, statement_id: str) -> bool:
        """Record the statement sent for a database: False when its draft dropped it meanwhile."""
        return bool(db.query(q.warehouse_started_sql(), (statement_id, discovery_id, database)))

    def clear(self, kind: str, discovery_id: str, database: str) -> None:
        db.execute(q.warehouse_clear_sql(), (discovery_id, database))

    def finish_check(self, discovery_id: str, database: str, ok: bool, tables: Optional[int], why: Optional[str]) -> None:
        db.execute(q.warehouse_check_done_sql(), (ok, why, tables, discovery_id, database))

    def finish_read(self, discovery_id: str, database: str, rows: Optional[List[tuple]], error: Optional[str],
                    statement_id: str) -> None:
        """A database's row finished and its tables saved, in one transaction; or its row failed with why. Only its
        own statement ends it (warehouse_read_done_sql), and the tables are saved only when it did. No delete first:
        the app has no DELETE on Lakebase (grant_app_access), and each read has a discovery id of its own, so there is
        nothing to replace; a second answer for the same database inserts nothing (on conflict)."""
        if rows is None:
            db.execute(q.warehouse_read_done_sql(),
                       ("failed", None, error, discovery_id, database, statement_id, statement_id, "failed"))
            return
        columns = warehouse_reads.TABLE_COLUMNS
        with db.transaction() as cur:
            done = db.fetch(cur, q.warehouse_read_done_sql(),
                            ("ok", len(rows), None, discovery_id, database, statement_id, statement_id, "ok"))
            if not done:
                return
            for at in range(0, len(rows), q.CONTROL_BATCH):
                chunk = rows[at:at + q.CONTROL_BATCH]
                cur.execute(q.insert_discovered_tables_sql(columns, len(chunk)), tuple(v for row in chunk for v in row))

    def progress(self, kind: str, discovery_id: str) -> dict:
        row = db.query(q.warehouse_progress_sql(kind), (discovery_id,))[0]
        return {k: int(v or 0) for k, v in row.items()}


# The background reader (background_reads.py), started in _lifespan: it reads the chosen databases' tables whatever
# page is open. While it runs, the page's, Create's and re-discovery's waits only ask where the reads stand.
READER = background_reads.Reader(WarehouseReadStore(), warehouse, draft_ttl_s=run_poller.DRAFT_TTL_S)


def _reads_step(discovery_id: str, connection: str) -> dict:
    """Where a discovery's reads stand: with the background reader running, as it has them (woken, so a read just
    started starts at once), with why a statement couldn't be sent if one just couldn't; without it, after one step of
    them (warehouse_reads.advance), which keeps to the server's cap as the reader does."""
    store = WarehouseReadStore()
    if READER.running():
        READER.wake()
        step = warehouse_reads.standing(store, warehouse, warehouse_reads.READ, discovery_id)
        error = READER.recent_error(discovery_id)
        return {**step, "error": error, "warehouse": ""} if error and step["pending"] else step
    return warehouse_reads.advance(store, warehouse, warehouse_reads.READ, discovery_id, connection)


def _own_connection(discovery_id: str, connection: str) -> None:
    """Refuse a step of a discovery's statements through any connection but its own (the one it was started through
    and saved on its rows), so its statements never go to another server."""
    rows = db.query(q.discovery_connection_sql(), (discovery_id,))
    if not rows:
        raise HTTPException(404, f"no discovery {discovery_id}")
    if rows[0]["src_connection"] != connection:
        raise HTTPException(409, f"discovery {discovery_id} reads through another connection than {connection}")


class AccessCheckRequest(BaseModel):
    """The databases on the page shown now, checked before the rest."""
    first: List[str] = Field(default=[], max_length=500)


@app.post("/api/discoveries/{discovery_id}/access")
def check_access(discovery_id: str, body: AccessCheckRequest, connection: str):
    """One step of checking whether the connection's login can open the databases the page shows
    (warehouse_reads.advance): only those, so a server of 22,000 databases is checked a page at a time as it is
    looked at. The page asks while some of them aren't checked yet (pending_shown). The answers are on the listing:
    each check is sent with a short wait, so most are saved by the request that sent them."""
    _require_connection(connection)
    _own_connection(discovery_id, connection)
    step = warehouse_reads.advance(WarehouseReadStore(), warehouse, warehouse_reads.CHECK, discovery_id, connection,
                                   body.first)
    unchecked = db.query(q.warehouse_unchecked_sql(), (discovery_id, list(body.first)))[0]["unchecked"]
    return {**step, "pending_shown": int(unchecked or 0)}


@app.get("/api/discoveries/{discovery_id}/reads")
def warehouse_reads_step(discovery_id: str, connection: str):
    """Where the reading of the chosen databases' tables through the warehouse stands (_reads_step): the page asks
    until it has ended. Asking keeps a draft wanted."""
    _require_connection(connection)
    _own_connection(discovery_id, connection)
    db.execute(q.draft_touch_sql(), (discovery_id, discovery_id))
    return {"discovery_id": discovery_id, "via": "warehouse", **_reads_step(discovery_id, connection)}


@app.post("/api/discoveries/{discovery_id}/discard")
def discard_draft(discovery_id: str):
    """Discard a draft (the page was cancelled or left before Create): nothing more is read for it, the statements
    still going are cancelled, and the delete job purges its rows within the hour (run_poller.purge_drafts_if_due; the
    app has no DELETE). A draft
    a Create or an Add has taken is not a draft any more, so this changes nothing then. Sent by navigator.sendBeacon
    too, so it takes no body."""
    going = [r["statement_id"] for r in db.query(q.draft_discard_sql(), (discovery_id,)) if r.get("statement_id")]
    for statement_id in going:
        try:
            warehouse.cancel(statement_id)
        except Exception as e:     # it may have ended meanwhile; the rows are discarded either way
            logging.getLogger(__name__).info("could not cancel statement %s of draft %s: %s", statement_id, discovery_id,
                                             str(e)[:200])
    return {"discovery_id": discovery_id, "cancelled": len(going)}


class DropRequest(BaseModel):
    """Databases un-ticked: dropped from the draft that reads them."""
    databases: List[str] = Field(min_length=1, max_length=5000)


@app.post("/api/discoveries/{discovery_id}/drop")
def drop_from_draft(discovery_id: str, body: DropRequest, request: Request):
    """Drop databases from a draft (un-ticked before Create): each one is marked dropped on its row (the app has no
    DELETE), its statement still going is cancelled, and it is left out of the read's progress, Create's wait and the
    databases that couldn't be read. One read already is kept, so ticking it again, which grows the draft by it again
    (warehouse_named_rows_sql), costs nothing. Only while the draft is open, and only by the person who started it.
    A whole-server read's databases are never dropped: that draft is discarded and started again when its filter
    changes."""
    names = list(dict.fromkeys(d.strip() for d in body.databases if d.strip()))
    drafts = db.query(q.draft_of_sql(), (discovery_id,))
    if not drafts:
        raise HTTPException(409, f"{discovery_id} is not an open draft, so nothing can be dropped from it")
    owner = drafts[0].get("draft_by")
    if owner and owner != _user(request):
        raise HTTPException(403, f"draft {discovery_id} was started by {owner}: only they can drop from it")
    dropped = db.query(q.draft_drop_sql(), (discovery_id, names, discovery_id)) if names else []
    going = [r["statement_id"] for r in dropped if r.get("statement_id")]
    for statement_id in going:
        try:
            warehouse.cancel(statement_id)
        except Exception as e:     # it may have ended meanwhile; the database is dropped either way
            logging.getLogger(__name__).info("could not cancel statement %s of draft %s: %s", statement_id, discovery_id,
                                             str(e)[:200])
    return {"discovery_id": discovery_id, "dropped": len(dropped), "cancelled": len(going)}


def _draft_reading(selection: "Selection", advance: bool = True) -> Optional[dict]:
    """Where the reads of the discoveries a choice names stand, for a preview or a Create: None when every database is
    read. With ``advance`` it marks the drafts still wanted and keeps their reads going: the background reader is
    woken, or, with it off, a step of each read still going is taken (the page that started them may be gone). A
    database a whole-server read couldn't open is left out, as the check would have left it out; any other database
    that couldn't be read is a failure, named. A database its draft dropped counts nowhere."""
    kind, ids, _ = selection.source()
    if kind != "discovery" or not ids:
        return None
    reads = db.query(q.draft_reads_sql(), (ids,))
    if advance:
        stepped = False
        for r in reads:
            if int(r["pending"] or 0) and r.get("src_connection"):
                db.execute(q.draft_touch_sql(), (r["discovery_id"], r["discovery_id"]))
                if READER.running():
                    READER.wake()
                else:
                    warehouse_reads.advance(WarehouseReadStore(), warehouse, warehouse_reads.READ, r["discovery_id"],
                                            r["src_connection"])
                    stepped = True
        if stepped:
            reads = db.query(q.draft_reads_sql(), (ids,))
    total = {k: sum(int(r[k] or 0) for r in reads)
             for k in ("databases", "pending", "done", "failed", "discarded", "tables")}
    if not total["pending"] and not total["failed"] and not total["discarded"]:
        return None
    rows = db.query(q.draft_failed_sql(), (ids,)) if total["failed"] else []
    left_out = [r for r in rows if r["whole_server"] and warehouse_reads.no_access(r.get("error"))]
    failed = [r for r in rows if r not in left_out]
    return {**total, "failed": len(failed), "left_out": len(left_out),
            "failed_databases": [[r["src_database"], warehouse_reads.first_line(r.get("error"))] for r in failed[:50]]}


def _draft_problems(reading: Optional[dict]) -> List[str]:
    """Why a Create can't take a choice's reads as they are: databases that couldn't be read, by name, or a draft
    discarded meanwhile."""
    if not reading:
        return []
    problems = [f"{name}: couldn't be read: {why}" for name, why in reading["failed_databases"]]
    if reading["failed"] > len(reading["failed_databases"]):
        problems.append(f"and {reading['failed'] - len(reading['failed_databases'])} more databases couldn't be read")
    if reading["discarded"]:
        problems.append("the tables read for this choice were discarded (the page was left or cancelled): "
                        "choose them again")
    return problems


def _start_warehouse_read(body: "SourceDiscoverRequest", names: List[str], user: Optional[str]) -> dict:
    """Start reading tables through the warehouse: the whole server (the databases of its listing that the filter keeps
    and the login wasn't found unable to open) or the databases named, then where the reads stand (_reads_step). A
    database a draft dropped and named again is taken back (warehouse_named_rows_sql)."""
    did = uuid.uuid4().hex[:12]
    if body.draft_id:
        drafts = db.query(q.draft_of_sql(), (body.draft_id,))
        if not drafts or drafts[0]["src_connection"] != body.src_connection:
            raise HTTPException(409, f"draft {body.draft_id} is not an open draft of {body.src_connection}: "
                                     "choose again")
        did = body.draft_id
    marks = json.dumps({"draft": True, "draft_by": user, "touched_at": datetime.now(timezone.utc).isoformat()}
                       if body.draft or body.draft_id else {})
    left_out = 0
    if body.whole_server:
        if not body.listing_id:
            raise HTTPException(422, "reading the whole server through the warehouse takes the listing its test made "
                                     "(listing_id)")
        db.execute(q.warehouse_read_rows_sql(), (did, user, marks, body.listing_id, body.filter))
        left_out = int(db.query(q.warehouse_left_out_sql(), (body.listing_id, body.filter))[0]["left_out"])
    for at in range(0, len(names), q.CONTROL_BATCH):
        chunk = names[at:at + q.CONTROL_BATCH]
        db.execute(q.warehouse_named_rows_sql(len(chunk)),
                   tuple(v for name in chunk for v in (did, body.src_connection, name, user, marks)))
    return {"discovery_id": did, "via": "warehouse", "left_out": left_out, **_reads_step(did, body.src_connection)}


@app.post("/api/discoveries/source")
def start_source_discovery(body: SourceDiscoverRequest, request: Request):
    names = list(dict.fromkeys(d.strip() for d in body.databases if d.strip()))
    if body.kind == "tables" and not body.whole_server and not names:
        raise HTTPException(422, "choose the whole server or at least one database")
    if bool(body.azure_credential) != bool(body.azure_resource_id):
        raise HTTPException(422, "listing through Azure takes both a service credential and the server's resource id")
    _require_connection(body.src_connection)
    if body.kind == "list" and warehouse.configured() and not body.azure_credential:
        # The connection test is the listing: one statement on the warehouse, every database (the filter comes after).
        did = uuid.uuid4().hex[:12]
        statement, parameters = warehouse.remote_query_statement(body.src_connection, SERVER_DATABASE,
                                                                 source_catalog.databases_sql(""))
        return _listing_answer(did, body.src_connection, _user(request), warehouse.start(statement, parameters))
    if body.kind == "tables" and warehouse.configured() and not body.azure_credential:
        return _start_warehouse_read(body, names, _user(request))
    job_id = _require(jobs.DISCOVER_JOB_ID, "discovery")
    _refuse_run_of(job_id, request)
    did = uuid.uuid4().hex[:12]
    spec = {"src_type": "sqlserver", "src_connection": body.src_connection, "kind": body.kind,
            "databases": "*" if body.kind == "tables" and body.whole_server else names, "requested_by": _user(request),
            **({"pattern": body.pattern} if body.pattern else {}),
            **({"azure_credential": body.azure_credential, "azure_resource_id": body.azure_resource_id}
               if body.azure_credential else {})}
    if body.kind == "list":
        spec.pop("databases")
    rid = jobs.run_now(job_id, {"discovery_id": did, "spec": json.dumps(spec)})
    return {"discovery_id": did, "run_id": rid}


@app.get("/api/discoveries/{discovery_id}/tables")
def discovery_tables(discovery_id: str):
    """The tables a discovery found, with the settings it recommends for each: what Add tables turns into lines."""
    keep = ("src_database", "src_schema", "src_table", "select_list", "primary_key", "load_type", "cursor_column",
            "row_count", "size_mb", "ct_enabled", "excluded_columns", "notes", "src_connection")
    return [{k: r.get(k) for k in keep} for r in db.query(q.discovered_tables_sql(), (discovery_id,))]


@app.get("/api/discoveries/runs/{run_id}")
def discovery_run(run_id: int):
    """State of a discovery job run, so the page can tell a run that ended without results from one
    still starting. Only runs of the discovery job."""
    st = run_poller.run_state_of(run_id)
    if str(st["job_id"]) != str(jobs.DISCOVER_JOB_ID):
        raise HTTPException(404, f"run {run_id} is not a discovery run")
    return st


@app.get("/api/discoveries")
def discoveries(limit: int = 50):
    return db.query(q.discoveries_sql(), (int(limit),))


@app.get("/api/discoveries/{discovery_id}/databases")
def discovery_databases(discovery_id: str):
    """A discovery's databases; one that failed also carries the codes its error names (source_catalog.error_codes)."""
    return [{**row, "codes": source_catalog.error_codes(row.get("error"))} if row.get("status") == "failed" else row
            for row in db.query(q.discovery_databases_sql(), (discovery_id,))]


@app.get("/api/discoveries/{discovery_id}/listing")
def discovery_listing(discovery_id: str, filter: str = "", offset: int = 0, limit: int = 100):
    """One page of a discovery's databases matching the filter, by name (100 at a time), with the discovery's totals:
    total (its databases), matched (those the filter keeps). The Discover step pages through it rather than holding
    every database in the browser."""
    limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
    totals = db.query(q.discovery_totals_sql(), (filter, discovery_id))[0]
    return {"discovery_id": discovery_id, "filter": filter, "offset": offset, "limit": limit,
            "total": totals["total"], "matched": totals["matched"],
            "access": {k: totals.get(k) for k in ("open", "closed", "tables", "matched_closed", "matched_unknown",
                                                  "matched_tables")},
            "databases": db.query(q.discovery_listing_sql(), (discovery_id, filter, limit, offset))}


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
    task_collection: Optional[str] = None


@app.post("/api/discoveries/{discovery_id}/review")
def review(discovery_id: str, body: ReviewRequest):
    """Review grid rows computed from the discovery and the shared fields, named by the rule of the job holding the
    target group (a group no job holds, or none given, takes Unity Catalog's)."""
    discovered = db.query(q.discovered_tables_sql(), (discovery_id,))
    group = (body.task_collection or "").strip()
    unity_catalog = _group_unity_catalog(group) if group else True
    return [q.public_review_row(r)
            for r in q.review_rows(discovered, body.shared.model_dump(), unity_catalog=unity_catalog)]


class RegisterRequest(BaseModel):
    task_collection: str = Field(min_length=1)
    shared: Shared = Shared()
    edits: List[dict] = []
    dry_run: bool = False


@app.post("/api/discoveries/{discovery_id}/register")
def register(discovery_id: str, body: RegisterRequest, request: Request):
    """Validate (dry_run) or register the included tables. Rows are rebuilt from the stored discovery
    and only the reviewer's editable fields are applied (queries.merge_review_edits)."""
    taken = {r["task_collection"] for r in db.query(q.collections_sql())}
    connection = _jdbc_source(Selection(discovery_id=discovery_id, whole_server=True))
    if body.task_collection in taken and connection is not None:
        raise HTTPException(422, {"errors": ["these tables come from a JDBC connection, whose scope and trust belong to "
                                              "a job of their own: register them into a new job"]})
    discovered = db.query(q.discovered_tables_sql(), (discovery_id,))
    unity_catalog = _group_unity_catalog(body.task_collection)
    rows = q.merge_review_edits(q.review_rows(discovered, body.shared.model_dump(), unity_catalog=unity_catalog),
                                body.edits)
    chosen = [r for r in rows if r.get("include")]
    results, valid = [], []
    for r in chosen:
        row = q.control_row_from_review(r, body.task_collection)
        errs = q.validate_control_row(row) + js.typed_name_errors(row, unity_catalog)
        name = ".".join(q.review_key(r))
        if errs:
            results.append({"table": name, "status": "invalid", "errors": errs})
        else:
            valid.append((name, row))
    if not body.dry_run and valid:
        _refuse_groups_outside_jobs([body.task_collection])
        _refuse_groups([body.task_collection], request)
        if connection is not None:
            _refuse_connection_rows(connection, [row for _, row in valid])
        _refuse_rows_off_connections([row for _, row in valid])
        _refuse_host_reads([row for _, row in valid], _user(request))
        batch = uuid.uuid4().hex[:12]
        with _write() as cur:
            _lock_collections(cur, [body.task_collection])
            for name, row in valid:
                cur.execute(q.insert_control_if_absent_sql(), q.control_params(row))
                res = cur.fetchone()
                if res:
                    _log(cur, _user(request), "register", res[0], body.task_collection, batch=batch)
                results.append({"table": name, "status": "registered" if res else "skipped",
                                "id": res[0] if res else None,
                                "errors": [] if res else ["sink already used by another control row"]})
            _rewrite_rollups(cur, [(row.get("task_collection"), row.get("src_database")) for _, row in valid])
            _refuse_mixed_sinks(cur, [row for _, row in valid])     # all or nothing, like an edit
            _refuse_split_databases(cur, [row for _, row in valid])
            _touch(cur, _user(request), groups=[body.task_collection])
    else:
        results += [{"table": name, "status": "valid", "errors": []} for name, _ in valid]
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"selected": len(chosen), "counts": counts, "results": results}


# ---- configuration import ----------------------------------------------------------------------

class ImportRequest(BaseModel):
    task_collection: str = Field(min_length=1, max_length=128)
    settings: dict = {}
    csv: str
    dry_run: bool = True

    @field_validator("task_collection")
    @classmethod
    def _named(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name the ingestion group: a blank name would put the tables in no group")
        return v.strip()


@app.post("/api/import")
def import_csv(body: ImportRequest, request: Request):
    """Preview (dry_run) or apply a CSV of tables into one ingestion group, with the group's settings
    (config_import.plan_import). Apply plans again inside one transaction, after taking the group's run lock and
    reading its rows there, so the settings it inherits are the rows' as they are; it needs every line valid, then
    inserts each line's row unless its sink is taken (skipped), logs it as an import, and runs the mixed-sink
    check, as Bulk import's register does. Tables the group already has are skipped."""
    group = body.task_collection.strip()
    unity_catalog = _group_unity_catalog(group)

    def planned(rows):
        plan = ci.plan_import(body.csv, group, body.settings, rows, unity_catalog=unity_catalog)
        if plan["errors"]:
            raise HTTPException(422, {"errors": plan["errors"]})
        return plan

    def counted(lines):
        counts: dict = {}
        for line in lines:
            counts[line["status"]] = counts.get(line["status"], 0) + 1
        return counts

    if body.dry_run:
        plan = planned(db.query(q.collection_control_rows_sql(), (group,)))
    else:
        _refuse_groups_outside_jobs([group])
        _refuse_groups([group], request)
        batch = uuid.uuid4().hex[:12]
        with _write() as cur:
            _lock_collections(cur, [group])
            plan = planned(db.fetch(cur, q.collection_control_rows_sql(), (group,)))
            invalid = counted(plan["lines"]).get("invalid")
            if invalid:
                raise HTTPException(422, {"errors": ["fix the invalid lines first"], "invalid": invalid})
            adding = [line["row"] for line in plan["lines"] if line["status"] != "registered"]
            _refuse_rows_off_connections(adding, cur=cur)
            _refuse_host_reads(adding, _user(request))
            for line in plan["lines"]:
                if line["status"] == "registered":
                    line["status"] = "skipped"
                    continue
                cur.execute(q.insert_control_if_absent_sql(), q.control_params(line["row"]))
                res = cur.fetchone()
                line["status"] = "imported" if res else "skipped"
                if res:
                    line["id"] = res[0]
                    _log(cur, _user(request), "import", res[0], group, batch=batch)
                else:
                    line["errors"] = ["sink already used by another control row"]
            imported = [line["row"] for line in plan["lines"] if line["status"] == "imported"]
            _rewrite_rollups(cur, [(row.get("task_collection"), row.get("src_database")) for row in imported])
            _refuse_mixed_sinks(cur, imported)
            _refuse_split_databases(cur, imported)
            _touch(cur, _user(request), groups=[group])
    return {"task_collection": group, "settings": plan["settings"], "counts": counted(plan["lines"]),
            "lines": [{k: v for k, v in line.items() if k != "row"} for line in plan["lines"]]}


@app.get("/api/collections/{name}/settings")
def collection_settings(name: str):
    """An ingestion group's settings, from its rows: each one's shared value, or its mixed values with row counts."""
    rows = db.query(q.collection_control_rows_sql(), (name,))
    return {"task_collection": name, "tables": len(rows), "settings": ci.setting_summary(ci.group_settings(rows))}


@app.get("/api/collections/{name}/export.csv")
def collection_export(name: str):
    """An ingestion group's rows as the import CSV."""
    rows = db.query(q.collection_control_rows_sql(), (name,))
    return Response(ci.export_csv(rows), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{q.identifier(name)}.csv"'})


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
    options: dict = {}
    # At least one, checked by schedules.validate; a job that isn't Ready keeps its own (update_schedule).
    members: List[str] = []

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return (v or "").strip() or "UTC"      # stored as given: a blank one would break every page

    @model_validator(mode="after")
    def _utc_for_minutes(self):
        self.timezone = sch.timezone_for(self.cadence_minutes, self.timezone)
        return self


def _ingest_job_id(variant: str) -> Optional[int]:
    """The job a schedule's wrappers start: the ingestion job of its variant, or the check job."""
    jid = jobs.schedule_variants().get(variant or "default")
    return int(jid) if jid else None


def _wrapper_target(schedule: dict) -> Optional[int]:
    """The job a schedule's wrappers start: its own copy of the ingestion job when it has its own cluster (a check
    schedule's wrapper still starts the check job, and passes the copy on), else its variant's job."""
    variant = schedule.get("job_variant") or "default"
    copy = sch.copy_job_id(schedule)
    return copy if copy and variant != sch.CHECK_VARIANT else _ingest_job_id(variant)


def _variant_job_id(variant: str) -> int:
    jid = _ingest_job_id(variant)
    if jid is None:
        raise HTTPException(503, f"no Databricks ingestion job configured for variant '{variant}'")
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


def _validate_schedule(body: ScheduleBody, members_kept: bool = False):
    """The schedule's checks (schedules.validate). members_kept: the job keeps its members as they are (it isn't Ready,
    and its prepare task makes its groups, which may have no tables yet), so they aren't checked."""
    known = [r["task_collection"] for r in db.query(q.collections_sql())]
    errs = sch.validate(body.model_dump(), known, jobs.schedule_variants())
    if members_kept:
        errs = [e for e in errs if not e.startswith("members:")]
    if errs:
        raise HTTPException(422, {"errors": errs})


# Options only the app's own steps write: who last changed the job, the copies job's copy and the app's run
# applying it, and a deleted job's delete job run and whether it drops the target tables. A request's values for them are ignored, and the stored ones kept.
# A job's tables per group, set at Create or by a regroup that names one.
TABLES_PER_GROUP_OPTION = "tables_per_group"
# A job made by Create stores its target names' rule here (job_settings.UNITY_CATALOG_NAMES); a job without it keeps the
# older rule, so its names are as they were. Its preparation record (preparing.OPTION) holds its state, what Create was
# asked for and by whom, and its set-aside databases: no edit reaches it, so none changes them behind the checks.
MANAGED_OPTIONS = ("updated_by", sch.COPY_OPTION, "cluster_copy_run_id", "delete_run_id", "delete_drop_targets",
                   "delete_start_error", "delete_finished", rediscovery.RECORD, TABLES_PER_GROUP_OPTION,
                   js.TARGET_NAMES_OPTION, table_removal.OPTION, table_removal.EXCLUDED, preparing.OPTION)


def _requested_options(body: "ScheduleBody") -> dict:
    return {k: v for k, v in (body.options or {}).items() if k not in MANAGED_OPTIONS}


def _refuse_unknown_node_types(cluster: Optional[dict]) -> None:
    """A job's own cluster may only name node types this workspace has."""
    wanted = [cluster.get(k) for k in ("driver_node_type_id", "node_type_id")] if cluster else []
    wanted = [n for n in wanted if n]
    if not wanted:
        return
    try:
        known = set(jobs.node_types())
    except Exception as e:  # without the list, nothing is checked: refuse rather than start an unknown cluster
        raise HTTPException(503, f"can't read this workspace's node types: {str(e).split('. ')[0]}")
    unknown = [n for n in wanted if n not in known]
    if unknown:
        raise HTTPException(422, {"errors": [f"options.cluster: {unknown[0]} is not a node type in this workspace"]})


def _apply_cluster(sid: int) -> Optional[str]:
    """Start the copies job for a schedule (it makes, updates or deletes the schedule's copy of the ingestion job)
    and record its run, which the cluster endpoint follows. Returns why it couldn't start, or None."""
    if not jobs.CLUSTER_COPIES_JOB_ID:
        return "no cluster copies job is configured for this app, so the job keeps the ingestion job's cluster"
    try:
        run_id = jobs.start_cluster_copies(sid)
    except Exception as e:
        return f"the cluster copies job didn't start: {str(e).split('. ')[0]}"
    with _write() as cur:
        cur.execute(q.set_schedule_option_sql(), (["cluster_copy_run_id"], run_id, int(sid)))
    return None


def _created(result: dict, user: str, options: dict) -> dict:
    """A new job's wrapper jobs synced, and its own cluster copy started, once its rows are committed. Either failing is
    not done (NotSynced): the job's rows are written, and the detail says what did not sync or start."""
    sid = result["id"]
    synced = _sync(sid, user)
    cluster = _apply_cluster(sid) if sch.own_cluster({"options": options}) else None
    _synced(synced)
    if cluster:
        raise NotSynced(500, f"{TASK_WRITTEN_NOT_SYNCED} {cluster}"[:4000])
    return {**result, "sync": synced, "cluster_error": None}


def _sync(sid: int, user: str) -> List[dict]:
    """Bring the schedule's wrapper jobs in line with Lakebase and record the results. Runs under the
    schedule's sync lock, with the members read after taking it, so concurrent syncs don't both create a
    job. A failed sync never raises once the schedule's change is committed: errors are per member."""
    batch = uuid.uuid4().hex[:12]
    with _write() as cur:
        cur.execute(q.schedule_lock_sql(), (q.SCHEDULE_LOCK_CLASS, q.schedule_lock_key(sid)))
        s, members = _load_schedule(sid, cur)
        active = [m for m in members if m["removed_at"] is None]
        removed = [m for m in members if m["delete_pending"]]
        results = sch.sync(s, active, removed, jobs.schedule_client(), _wrapper_target(s),
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
    """Start times (ms) of a wrapper job's skipped runs since `since`; None if they can't be read. From the run
    poller's table, or from the Jobs API until the table exists."""
    if run_poller.table_ready():
        try:
            return run_poller.skipped_starts_of(job_id, since, now)
        except Exception:
            return None
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
    now = datetime.now(timezone.utc)
    rows = _with_delete_states(db.query(q.schedules_sql()), now)
    members = db.query(q.schedule_members_sql(), ([r["id"] for r in rows],)) if rows else []
    active = [m for m in members if m["removed_at"] is None]
    colls = sorted({m["task_collection"] for m in active})
    stats = {r["task_collection"]: r for r in
             (db.query(q.collection_run_stats_sql(), (colls, colls, colls, colls)) if colls else [])}
    skipped = _skipped_counts(active) if details else {}
    return [{**s, "description": sch.describe(int(s["cadence_minutes"]), sch.start_minute(s), s["timezone"]),
             "members": _members_view(s, [m for m in active if m["schedule_id"] == s["id"]], stats, skipped, now)}
            for s in rows]


def _scheduled_collections(with_added: bool = False) -> dict:
    """{collection: [names of the active schedules it's in]} (or [(name, added_at)])."""
    scheduled = {}
    rows = db.query(q.schedules_sql())
    if rows:
        names = {r["id"]: r["name"] for r in rows}
        for m in db.query(q.schedule_members_sql(), (list(names),)):
            if m["removed_at"] is None:
                name = names[m["schedule_id"]]
                scheduled.setdefault(m["task_collection"], []).append((name, m["added_at"]) if with_added else name)
    return scheduled


@app.get("/api/schedules/options")
def schedule_options():
    scheduled = _scheduled_collections()
    try:     # the ingestion job's own defaults, shown as the editor's placeholders
        defaults = {k: v for k, v in jobs.job_summary(jobs.INGEST_JOB_ID)["parameters"].items() if k in sch.PARAMETERS}
    except Exception:
        defaults = {}
    return {"cadences": sch.CADENCES, "parameters": list(sch.PARAMETERS), "flags": sch.FLAGS, "defaults": defaults,
            "shared_client_id": js.SHARED_CLIENT_ID, "shared_identity": js.SHARED_IDENTITY,
            "variants": sorted(jobs.schedule_variants()),
            "collections": [r["task_collection"] for r in db.query(q.collections_sql())], "scheduled": scheduled,
            "view_group": jobs.SCHEDULE_JOB_VIEW_GROUP}


@app.post("/api/schedules")
def create_schedule(body: ScheduleBody, request: Request):
    import psycopg
    body.options = _requested_options(body)
    _validate_schedule(body)
    _refuse_login_scope(body.parameters, None)
    _refuse_scope_parameter(body.parameters, _user(request))
    _refuse_unknown_node_types(sch.own_cluster(body.model_dump()))
    # Set after validation: the app writes this option, a request may not send it.
    body.options = {**body.options, js.TARGET_NAMES_OPTION: js.UNITY_CATALOG_NAMES}
    user = _user(request)
    # A check schedule's one wrapper fires every member at once.
    body.stagger = body.stagger and body.job_variant != sch.CHECK_VARIANT
    offs = sch.offsets(body.members, body.cadence_minutes, body.stagger, sch.start_minute(body.model_dump()))
    if not body.paused:   # anyone may set up a job, but one that runs needs the right to run what it starts
        _refuse(_permission_refusals({"job_variant": body.job_variant}, user)["run"])
    batch = uuid.uuid4().hex[:12]
    try:
        with _write() as cur:
            cur.execute(q.insert_schedule_sql(), (body.name.strip(), body.cadence_minutes, body.timezone,
                                                  json.dumps(body.parameters), body.job_variant or "default",
                                                  body.stagger, body.paused, body.notify_emails or None,
                                                  json.dumps(body.options) if body.options else None, user or None))
            sid = cur.fetchone()[0]
            _touch(cur, user, [sid])
            for c in sorted(body.members):
                cur.execute(q.insert_member_sql(), (sid, c, offs[c]))
                _log(cur, user, "schedule_create", None, c, "schedule", None,
                     f"{body.name} ({sch.describe(body.cadence_minutes, offs[c], body.timezone)})", batch=batch)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, f"a schedule named '{body.name}' already exists")
    synced = _sync(sid, user)
    cluster = _apply_cluster(sid) if sch.own_cluster(body.model_dump()) else None
    return {"id": sid, "sync": synced, "cluster_error": cluster}


class NewJobRun(BaseModel):
    """When a new job runs, as the Edit view's When it runs and Advanced configuration set it."""
    cadence_minutes: int = 60
    timezone: str = "UTC"
    paused: bool = True
    notify_emails: Optional[str] = None
    parameters: dict = {}
    options: dict = {}
    job_variant: str = "default"
    stagger: bool = True


class NewJobRequest(_TablesFrom):
    # Blank in a preview, which shows the tables whatever the name; Create refuses a blank name ("name the job").
    name: str = Field(default="", max_length=80)
    settings: dict = {}
    # Blank: each server's default for its tables (placement.groups_for_tables); a number: that many per server.
    groups_per_server: Optional[int] = Field(None, ge=1, le=nj.MAX_GROUPS_PER_SERVER)
    # The job's group size: each server gets one group for about this many of its tables (placement.TablesPerGroup).
    tables_per_group: Optional[int] = Field(None, ge=100, le=1_000_000)
    run: NewJobRun = NewJobRun()
    dry_run: bool = True
    # Made when the page opens the Create, so a retry of the same Create finds the task the first one made.
    request_id: Optional[str] = Field(None, max_length=80)


def _source_columns(kind: str, param) -> List[str]:
    """The CSV columns a source's rows are written as (what a plan reads): an upload's header, a discovery's columns."""
    if kind == "upload":
        return list(db.query(q.upload_sql(), (param,))[0]["columns"])
    return list(nj.DISCOVERY_COLUMNS)


def _units_of(kind: str, param, selection: "Selection"):
    """The databases a selection takes, one at a time: a whole server's databases (matching its filter) are read a page
    at a time from Lakebase, then the named ones (by_database.selection_units)."""
    keys = ()
    if selection.whole_server:
        pages = by_database.database_pages(lambda after: db.query(
            q.source_databases_after_sql(kind), (param, selection.filter, *after, by_database.DATABASE_PAGE)))
        keys = ((d["server"], d["src_database"]) for d in pages)
    return by_database.selection_units(selection, keys)


@dataclass(slots=True, kw_only=True)
class ChunkJob:
    """The job a by-database write puts its databases into: its schedule, who asked and the batch their log lines carry,
    the schedule's cadence and time zone (for the create lines' text), each group's offset, the task whose progress each
    transaction records, the write's stage clock, and whether a member it adds is logged (Create logs them as
    schedule_create; Add tables never has)."""
    sid: int
    name: str
    user: str
    batch: str
    cadence_minutes: int
    timezone: str
    offsets: dict
    task_id: int
    stages: by_database.Stages
    log_members: bool = True
    # The JDBC connection the job reads through (its rows are held to it), or None (a row read by host is a manager's).
    connection: Optional[dict] = None
    # The checks of the rows each transaction writes, check(cur, rows), in place of _refuse_job_rows: a prepare task's
    # (_prepare_checks), which reads the job's maker and its connection again as they are then.
    check: Optional[object] = None


def _schedule_create_line(job: ChunkJob, group: str) -> tuple:
    """The change log line for a member a by-database write added to the job's schedule: the group, and the schedule's
    cadence and start in words."""
    described = sch.describe(job.cadence_minutes, job.offsets.get(group, 0), job.timezone)
    return (job.user, "schedule_create", None, group, "schedule", None, f"{job.name} ({described})", None, job.batch)


def _write_chunk(cur, plans: List[dict], job: ChunkJob, state: dict) -> None:
    """The writes of one transaction, for the databases of a chunk (their plans), in the caller's transaction: each
    group's run lock once, the valid lines the chunk's databases do not have yet (a resumed write skips the rows a
    committed transaction wrote; a table the job already writes is left) with their change log lines in one statement,
    the mixed and split checks once, the members its groups are missing, and the task's progress. The tables counted
    are the valid lines reached, so a resumed write reports the whole."""
    groups = sorted({group for plan in plans for group in plan["groups"]})
    _lock_collections_together(cur, groups)
    lines = [(line, line["row"]) for plan in plans for line in plan["lines"] if line["status"] == "valid"]
    names = {str(row.get("src_database") or "") for _, row in lines}
    databases = sorted(names | {name.lower() for name in names})
    have = set()
    if groups and databases:
        cur.execute(q.job_database_sinks_chunk_sql(), (groups, databases))
        have = {tuple(found) for found in cur.fetchall()}
    todo = [row for line, row in lines
            if (str(row.get("src_database") or "").lower(), line["sink"], str(row.get("client_id") or ""),
                row["task_collection"]) not in have]
    if todo:
        if job.check is not None:
            job.check(cur, todo)
        else:
            _refuse_job_rows(todo, job.connection, job.user, cur=cur)
        _insert_controls(cur, todo, job.user, job.batch, log_per_statement=by_database.TABLES_PER_TRANSACTION)
        _refuse_mixed_sinks(cur, todo)
        _refuse_split_databases(cur, todo)
    if groups:
        cur.execute(q.insert_members_if_absent_sql(),
                    (job.sid, groups, [job.offsets.get(group, 0) for group in groups]))
        created = sorted(found[0] for found in cur.fetchall())
        if job.log_members:
            _log_lines(cur, [_schedule_create_line(job, group) for group in created])
    state["databases_written"] += len(plans)
    state["tables_written"] += len(lines)
    state["seconds"] = job.stages.seconds()
    cur.execute(q.task_progress_json_sql(), (json.dumps(state), state["tables_written"], job.task_id))


def _rows_of_units(kind, param, units: List) -> List[Tuple[object, List[dict]]]:
    """Each unit with the table rows it takes, read for all of the units in one or two statements. The databases a
    source listing returned for an upload are read by their exact names (an index range each); the rest, which a
    user named in any case, are read without regard to case."""
    if not units:
        return []
    rows = []
    exact = [unit for unit in units if kind == "upload" and unit.exact]
    if exact:
        rows += db.query(q.database_rows_exact_sql(),
                         ([unit.server for unit in exact], [unit.database for unit in exact], param))
    named = [unit for unit in units if not (kind == "upload" and unit.exact)]
    if named:
        keys = [by_database.read_key(unit.server, unit.database) for unit in named]
        rows += db.query(q.database_rows_chunk_sql(kind), (param, keys))
    return by_database.rows_by_unit(units, rows)


def _create_by_database(body: NewJobRequest, request: Request, selection: Selection, kind, param, source_id, rest,
                        inline=None):
    """A big job, by reference to more tables than BACKGROUND_ROWS: its groups are sized by tables (by_database), and it
    is written in a background task, one database at a time, each database's rows in its own transaction. First every
    database is checked (read and planned, nothing written); then the job is made (its schedule and what it was chosen
    from, in one transaction), and each database is written with its group's run lock, its group's member, and the
    task's progress, all committed together. The job's Databricks jobs are synced once, at the end. A restart leaves
    the databases written so far, and the same request id finishes the job: a database a job already has is skipped,
    so it is never written twice (the task's progress is what the page shows). ``inline`` (progress, task_id) writes in
    the calling task (a Create that waited for its draft's read) instead of a task of its own."""
    import psycopg
    name = body.name.strip()
    run = body.run.model_copy()
    run.stagger = run.stagger and run.job_variant != sch.CHECK_VARIANT
    user = _user(request)
    connection = _new_job_source(run, selection, user, applying=not body.dry_run)
    settings = nj.settings_of(body.settings, new_job=True)
    errors = nj.given_errors(body.settings) + nj.setting_errors(settings, require_target=not body.dry_run)
    if not name and not body.dry_run:
        errors.insert(0, "name the job")
    servers = db.query(q.selected_servers_sql(kind), (param, *rest))
    if not servers:
        errors.append(_no_tables_refusal(kind, param, selection))
    groups_of = by_database.groups_by_server(servers, _sizing(body))
    all_groups = [g for s in servers for g in nj.group_names(name or PREVIEW_NAME, s["server"], groups_of[s["server"]])]
    timezone_ = sch.timezone_for(run.cadence_minutes, run.timezone)
    if not errors and name:     # the schedule's checks (its name and groups free) wait for the name
        errors += sch.validate({**run.model_dump(), "name": name, "members": all_groups, "timezone": timezone_},
                               all_groups, jobs.schedule_variants())
    if errors:
        raise HTTPException(422, {"errors": errors})
    summary = by_database.summary_of(name or PREVIEW_NAME, servers, settings["load_type"], groups_of)
    if body.dry_run:
        return {"summary": summary, "settings": settings, "created": False, "target_missing": _target_missing(settings),
                "name_missing": not name}
    if not run.paused:     # anyone may set up a job, but one that runs needs the right to run what it starts
        _refuse(_permission_refusals({"job_variant": run.job_variant}, user)["run"])
    options = _create_options(body)
    _refuse_unknown_node_types(sch.own_cluster({"options": options}))
    offs = sch.offsets(all_groups, run.cadence_minutes, run.stagger,
                       sch.start_minute({"options": options, "cadence_minutes": run.cadence_minutes}))
    taken = {r["task_collection"] for r in db.query(q.collections_sql())}
    columns = _source_columns(kind, param)
    selection_parts = _selection_parts(selection, [s["server"] for s in servers] if selection.whole_server else [])
    batch = uuid.uuid4().hex[:12]
    databases_total = sum(s["databases"] for s in servers)

    def units():
        return _units_of(kind, param, selection)

    sizes = db.query(q.selected_databases_sql(kind), (param, *rest))
    group_of = by_database.group_of_placement(name, placement.server_placements(
        ((r["server"], r["src_database"], r["tables"]) for r in sizes), groups_of))

    def plan_of(rows, own, stages):
        with stages.timed("plan"):
            return by_database.plan_database(name, body.settings, rows, columns, groups_of, taken, own, group_of=group_of,
                                            new_job=True)

    def read_of(chunk, stages):
        with stages.timed("read"):
            return _rows_of_units(kind, param, chunk)

    def check(own, stages, keep):
        """Every database planned, nothing written: the file's and the settings' problems, and the groups in use.
        Returns each database that has tables, in order, for the write to pack into transactions; each keeps its plan
        when keep is True, so the write does not plan it again."""
        errors_found, counted = [], []
        for read_batch in by_database.read_batches(list(units())):
            batch_rows = []
            for unit, rows in read_of(read_batch, stages):
                if not rows:
                    continue
                plan = plan_of(rows, own, stages)
                if plan["errors"]:
                    raise HTTPException(422, {"errors": plan["errors"][:20]})
                for line in plan["lines"]:
                    if line["status"] == "invalid":
                        errors_found.append(f"{unit.database} {line['table']}: {'; '.join(line['errors'])}")
                batch_rows += [line["row"] for line in plan["lines"] if line["status"] == "valid"]
                counted.append(by_database.Checked(unit, len(rows), plan if keep else None))
            _refuse_job_rows(batch_rows, connection, user)
        if errors_found:
            raise HTTPException(422, {"errors": errors_found[:20] + (
                [f"{len(errors_found)} lines cannot be taken"] if len(errors_found) > 20 else [])})
        return counted

    def write(progress, task_id):
        task = db.query(q.task_sql(), (task_id,))[0]
        sid = task["job_id"]
        own = set(_job_groups(sid)) if sid else set()
        stages = by_database.Stages()
        state = {"stage": "checking", "databases_total": databases_total, "databases_written": 0, "tables_written": 0,
                 "seconds": stages.seconds()}
        db.execute(q.task_progress_json_sql(), (json.dumps(state), 0, task_id))
        keep = by_database.keeps_plans(summary["tables"])
        counted = check(own, stages, keep)
        stages.begin_write()
        state["stage"] = "writing"
        if sid is None:
            stored = {**options, preparing.OPTION: _create_record(body, run, user, settings)}
            try:
                with _write() as cur:
                    cur.execute(q.insert_schedule_sql(), (name, run.cadence_minutes, timezone_, json.dumps(run.parameters),
                                                          run.job_variant or "default", run.stagger, run.paused,
                                                          run.notify_emails or None, json.dumps(stored), user or None))
                    sid = cur.fetchone()[0]
                    _keep_selection(cur, sid, {"parts": selection_parts, "kind": kind, "source_id": source_id}, user)
                    _touch(cur, user, [sid])
                    cur.execute(q.task_job_sql(), (sid, task_id))
            except psycopg.errors.UniqueViolation:
                raise HTTPException(409, f"an ingestion job named '{name}' already exists")
        job = ChunkJob(sid=sid, name=name, user=user, batch=batch, cadence_minutes=run.cadence_minutes,
                       timezone=timezone_, offsets=offs, task_id=task_id, stages=stages, connection=connection)
        for chunk in by_database.transactions_of(counted):
            plans = [checked.plan for checked in chunk] if keep else [
                plan_of(rows, own, stages) for _, rows in read_of([checked.unit for checked in chunk], stages) if rows]
            if plans:
                with _write() as cur:
                    _write_chunk(cur, plans, job, state)
        result = {"id": sid, "summary": summary, "settings": settings, "created": True}
        with _write() as cur:
            _task_written(cur, task_id, result, sid, state["tables_written"])
        return result

    def after(result):
        return _created(result, user, options)

    if inline is not None:
        return write(*inline)
    made = db.query(q.insert_task_sql(), ("create", body.request_id or uuid.uuid4().hex, None,
                                          summary["tables"], None, user or None))[0]
    if made["made"] or db.query(q.resume_task_sql(), (made["id"],)):
        _start_background(lambda: _run_task(made["id"], write, after))
        return _task_answer(made["id"])
    return _answer_again(made["id"], after)


def _add_by_database(job_id: int, body: "JobTablesRequest", request: Request, kind, param, source_id, rest, total: int):
    """Add tables to a job by reference when the source has more than BACKGROUND_ROWS of them: written in a background
    task, like _create_by_database, its databases read in batches and packed into transactions under the job's schedule
    lock and their groups' run locks, with the groups' members and the task's progress committed with them. The job's
    settings, groups and tables (new_job.job_inputs, read once when the request arrives) place each database; a table
    the job already writes is left. Every database is checked first (nothing written), and the job's Databricks jobs
    are synced once at the end. The same request id after a stop finishes the job, skipping the rows already in."""
    job = _load_schedule(job_id)[0]
    selection = body.source
    explicit = not selection.whole_server and not selection.databases
    excluded = table_removal.exclusions_of(sch.options_of(job))
    groups = _job_groups(job_id)
    _refuse_job_tables(job, groups, request)
    connection = _job_connection_row(job_id)
    inputs = nj.job_inputs(job["name"], db.query(q.groups_control_rows_sql(), (groups,)) if groups else [],
                           body.groups_per_server, unity_catalog=_unity_catalog(job))
    if inputs["errors"]:
        raise HTTPException(422, {"errors": inputs["errors"]})
    taken = {r["task_collection"] for r in db.query(q.collections_sql())}
    columns = _source_columns(kind, param)
    user, batch = _user(request), uuid.uuid4().hex[:12]
    given = {**inputs["given"], "load_type": body.load_type}
    servers = db.query(q.selected_servers_sql(kind), (param, *rest))
    databases_total = sum(s["databases"] for s in servers)
    parts = _selection_parts(selection, [s["server"] for s in servers] if selection.whole_server else [])
    # A database the job has keeps its group, a new one on a server the job has joins that server's lightest group, and
    # a server new to the job gets its own groups, its databases spread evenly over them.
    new_servers = by_database.groups_by_server([s for s in servers if not inputs["join"].knows(s["server"])],
                                               _sizing(body, job))
    sizes = db.query(q.selected_databases_sql(kind), (param, *rest)) if new_servers else []
    spread = by_database.group_of_placement(job["name"], placement.server_placements(
        ((r["server"], r["src_database"], r["tables"]) for r in sizes if r["server"] in new_servers), new_servers))

    def group_of(server, database):
        return inputs["group_of"](server, database) or (spread(server, database) if server in new_servers else None)

    def plan_of(rows, stages):
        with stages.timed("plan"):
            return by_database.plan_database(job["name"], given, rows, columns, {}, taken, set(groups),
                                             group_of=group_of, have=inputs["have"], join=inputs["join"],
                                             unity_catalog=_unity_catalog(job))

    def read_of(chunk, stages):
        with stages.timed("read"):
            return _rows_of_units(kind, param, chunk)

    def not_removed(rows):
        """The rows Add tables takes: a table the job removed is left out, unless the selection named its tables."""
        if explicit:
            return rows
        return [row for row in rows if not table_removal.is_excluded(
            row["server"], row["src_database"], row["src_schema"], row["src_table"], excluded)]

    def plans_of(units, stages):
        plans = []
        for _, rows in read_of(units, stages):
            kept = not_removed(rows)
            if kept:
                plans.append(plan_of(kept, stages))
        return plans

    def check(stages, keep):
        """Every database planned, nothing written: the lines that cannot be taken, and the groups the job would add.
        Returns those totals, and each database with its table count, in order, for the write to pack into
        transactions."""
        invalid, tables, in_job, new_groups, counted, left_out = [], 0, 0, set(), [], 0
        for read_batch in by_database.read_batches(list(_units_of(kind, param, selection))):
            batch_rows = []
            for unit, all_rows in read_of(read_batch, stages):
                rows = not_removed(all_rows)
                left_out += len(all_rows) - len(rows)
                if not rows:
                    continue
                plan = plan_of(rows, stages)
                if plan["errors"]:
                    raise HTTPException(422, {"errors": plan["errors"][:20]})
                for line in plan["lines"]:
                    if line["status"] == "invalid":
                        invalid.append(f"{unit.database} {line['table']}: {'; '.join(line['errors'])}")
                    elif line["status"] == "in_job":
                        in_job += 1
                    else:
                        tables += 1
                        batch_rows.append(line["row"])
                new_groups |= set(plan["groups"]) - set(groups)
                counted.append(by_database.Checked(unit, len(rows), plan if keep else None))
            _refuse_job_rows(batch_rows, connection, user)
        if invalid:
            raise HTTPException(422, {"errors": invalid[:20] + (
                [f"{len(invalid)} lines cannot be taken"] if len(invalid) > 20 else [])})
        return {"tables": tables, "in_job": in_job, "left_out": left_out, "new_groups": sorted(new_groups)}, counted

    def write(progress, task_id):
        stages = by_database.Stages()
        state = {"stage": "checking", "databases_total": databases_total, "databases_written": 0, "tables_written": 0,
                 "seconds": stages.seconds()}
        db.execute(q.task_progress_json_sql(), (json.dumps(state), 0, task_id))
        keep = by_database.keeps_plans(total)
        found, counted = check(stages, keep)
        stages.begin_write()
        active = [m for m in _load_schedule(job_id)[1] if m["removed_at"] is None]
        offs = sch.offsets([m["task_collection"] for m in active] + found["new_groups"], int(job["cadence_minutes"]),
                           bool(job.get("stagger")), sch.start_minute(job))
        state["stage"] = "writing"
        chunk_job = ChunkJob(sid=job_id, name=job["name"], user=user, batch=batch,
                             cadence_minutes=int(job["cadence_minutes"]), timezone=job["timezone"], offsets=offs,
                             task_id=task_id, stages=stages, log_members=False, connection=connection)
        for chunk in by_database.transactions_of(counted):
            plans = ([checked.plan for checked in chunk] if keep
                     else plans_of([checked.unit for checked in chunk], stages))
            if plans:
                with _write() as cur:
                    _lock_job(cur, job_id)
                    _write_chunk(cur, plans, chunk_job, state)
                    if explicit:
                        cur.execute(q.clear_exclusions_sql(),
                                    (table_removal.line_keys([line for plan in plans for line in plan["lines"]]),
                                     int(job_id)))
        result = {"summary": {**found, "tables": state["tables_written"], "by_database": True, "checked": True},
                  "applied": True, "databases": state["databases_written"]}
        with _write() as cur:
            _keep_selection(cur, job_id, {"parts": parts, "kind": kind, "source_id": source_id}, user)
            _touch(cur, user, [job_id])
            _task_written(cur, task_id, {**result, "new_groups": found["new_groups"]}, job_id, state["tables_written"])
        return {**result, "new_groups": found["new_groups"]}

    def after(result):
        if result.get("new_groups"):
            _synced(_sync(job_id, user))
        return {k: v for k, v in result.items() if k != "new_groups"}

    if body.dry_run:
        return {"summary": {"tables": total, "by_database": True, "checked": False, "new_groups": []},
                "applied": False}
    made = db.query(q.insert_task_sql(), ("tables", body.request_id or uuid.uuid4().hex, job_id, total, None,
                                          user or None))[0]
    if made["made"] or db.query(q.resume_task_sql(), (made["id"],)):
        _start_background(lambda: _run_task(made["id"], write, after))
        return _task_answer(made["id"])
    return _answer_again(made["id"], after)


@app.post("/api/ingestion-jobs")
def create_ingestion_job(body: NewJobRequest, request: Request):
    """Preview (dry_run, the default) or create an ingestion job, as New ingestion job does: its tables from a CSV of
    where each one is (new_job.plan), written with the job's settings into groups per server, and the job (a schedule
    with those groups). The tables come as CSV lines (csv) or by reference to an upload or discovery (source), which is
    kept with the job.

    A Create from CSV lines, or from a source whose tables are all read, of BACKGROUND_ROWS tables or fewer, is one
    transaction: the schedule, every table (all or nothing: a target another table writes refuses the lot), the groups'
    members; then its Databricks jobs are synced, and the job is Ready when Create answers. Any other Create (its
    tables still read, a database that couldn't be read, more than BACKGROUND_ROWS tables, or a file with database-only
    lines) records the job at once and answers with its id (_create_preparing); its prepare task reads, writes and
    syncs it, setting aside a database it can't take. A preview of a draft whose read hasn't ended counts what is read
    so far and says how far the read is (reading); with nothing chosen read yet, it says only how far the read is."""
    database_lines = body.source is not None and _has_database_lines(body.source)
    reading = _draft_reading(body.source) if body.source is not None and not database_lines else None
    if not body.dry_run and PREPARE_CREATES and body.source is not None:
        if reading and reading["discarded"]:
            raise HTTPException(422, {"errors": _draft_problems({**reading, "failed": 0, "failed_databases": []})})
        if database_lines or reading or _selected_tables(body.source) > BACKGROUND_ROWS:
            return _create_preparing(body, request, database_lines=database_lines)
    if not body.dry_run and reading:
        problems = _draft_problems(reading)
        if problems:
            raise HTTPException(422, {"errors": problems})
        if reading["pending"]:
            return _create_after_reading(body, request)
    if body.dry_run and reading and reading["pending"] and not _selected_tables(body.source):
        # Nothing chosen is read yet: a refusal ("the selection takes no tables") would stop the page asking, while
        # the reads still going may bring every table, so the preview says it is reading and the page asks again.
        _new_job_source(body.run.model_copy(), body.source, "", applying=False)    # a preview's checks need no user
        return {"summary": None, "settings": {}, "created": False, "reading": reading}
    result = _create_now(body, request)
    if body.dry_run and reading and isinstance(result, dict):
        result["reading"] = reading
    return result


def _selected_tables(selection: Selection) -> int:
    """How many tables a selection takes from its source, counted in Lakebase."""
    kind, param, _, rest = _selection_params(selection)
    counted = db.query(q.selected_count_sql(kind), (param, *rest))
    return int(counted[0]["n"]) if counted else 0


def _create_now(body: NewJobRequest, request: Request, inline=None):
    """create_ingestion_job's write once every table is read: by database for more than BACKGROUND_ROWS tables, else in
    one go. ``inline`` (progress, task_id) writes in the calling task instead of a task of its own."""
    if body.source is not None and not _has_database_lines(body.source):
        kind, param, source_id, rest = _selection_params(body.source)
        counted = db.query(q.selected_count_sql(kind), (param, *rest))
        if counted and counted[0]["n"] > BACKGROUND_ROWS:
            return _create_by_database(body, request, body.source, kind, param, source_id, rest, inline=inline)
    if inline is not None:
        progress, task_id = inline
        return _create_job(body, request, inline=True, progress=progress, task_id=task_id)
    user = _user(request)
    create_options = _create_options(body)

    def after_create(result):
        return _created(result, user, create_options)

    if (made := _made_already(body, after_create)) is not None:
        return made
    return _create_job(body, request)


def _create_options(body: "NewJobRequest") -> dict:
    """A new job's options: the run's, less those only the app's own steps write, with the job's tables per group when
    Create names one (Add tables and a regroup later read it back, _sizing), and its target names by Unity Catalog's
    rules."""
    options = {k: v for k, v in (body.run.options or {}).items() if k not in MANAGED_OPTIONS}
    if body.tables_per_group:
        options[TABLES_PER_GROUP_OPTION] = int(body.tables_per_group)
    options[js.TARGET_NAMES_OPTION] = js.UNITY_CATALOG_NAMES
    return options


def _sizing(body, job: Optional[dict] = None) -> placement.Groups:
    """A request's grouping (placement.sizing): its groups per server, else its tables per group, else the job's."""
    stored = sch.options_of(job).get(TABLES_PER_GROUP_OPTION) if job else None
    return placement.sizing(getattr(body, "groups_per_server", None), getattr(body, "tables_per_group", None), stored)


def _unity_catalog(job: dict) -> bool:
    """Whether a job's target names follow Unity Catalog's rules (job_settings.unity_catalog_of): every job Create
    makes from now on, and not one made before."""
    return js.unity_catalog_of(sch.options_of(job))


def _group_unity_catalog(group: str) -> bool:
    """Whether the tables of an ingestion group are named by Unity Catalog's rules: the rule of the job that holds the
    group. A group no job holds takes Unity Catalog's, as a new job's would; a write into it is refused anyway."""
    held = db.query(q.group_job_options_sql(), (group,))
    return _unity_catalog(held[0]) if held else True


# The name a preview groups the tables under while the job has none yet: the groups are named from the job's name.
PREVIEW_NAME = "new job"


def _target_missing(settings: dict) -> List[str]:
    """The target fields a preview's settings leave blank, by name, for the page to say what Create needs."""
    return [label for key, label in js.TARGET_SETTINGS if not str(settings.get(key) or "").strip()]


def _new_job_source(run, source: Optional[Selection], user: str, applying: bool) -> Optional[dict]:
    """The checks of a new job's source and secret scope, on every path that makes one (_create_job, and
    _create_by_database for more than BACKGROUND_ROWS tables). A job made from a JDBC connection's discoveries takes the
    connection's scope and trust in place of the request's (run.parameters is set here); a job reading through none may
    not name the login scope, and, when it is made rather than previewed, names another scope only as an app manager.
    Returns the connection, or None."""
    connection = _jdbc_source(source)
    if connection:
        run.parameters = {**{k: v for k, v in run.parameters.items() if k not in JDBC_PARAMETERS},
                          **jc.job_parameters(connection)}
    _refuse_login_scope(run.parameters, connection)
    if connection is None and applying:
        _refuse_scope_parameter(run.parameters, user)
    return connection


def _refuse_job_rows(rows: List[dict], connection: Optional[dict], user: str, cur=None) -> None:
    """The checks of control rows written into one job: through the job's JDBC connection, each must read through it
    (_refuse_connection_rows); without one, a row read by host and password is an app manager's (_refuse_host_reads)."""
    if connection is not None:
        _refuse_connection_rows(connection, rows, cur=cur)
    else:
        _refuse_host_reads(rows, user, {})


def _create_job(body: NewJobRequest, request: Request, inline: bool = False, progress=None, task_id=None):
    """The Create itself. A file with database-only lines is first discovered by name in a background task
    (_create_after_discovery), which runs this again with inline=True, so the write happens in that task."""
    user = _user(request)
    create_options = _create_options(body)
    if not inline and body.source is not None and not body.dry_run and _has_database_lines(body.source):
        if not body.run.paused:
            _refuse(_permission_refusals({"job_variant": body.run.job_variant}, user)["run"])
        return _write_now_or_later("create", body.request_id, None, 0, user,
                                   lambda progress, task_id: _create_after_discovery(body, request, progress, task_id),
                                   lambda result: _created(result, user, create_options) if result.get("created")
                                   else result, background=True)
    import psycopg
    name = body.name.strip()
    run = body.run.model_copy()
    run.stagger = run.stagger and run.job_variant != sch.CHECK_VARIANT
    taken = {r["task_collection"] for r in db.query(q.collections_sql())}
    connection = _new_job_source(run, body.source, user, applying=not body.dry_run)
    text, selected = body.text(request, wait=inline)
    if selected is not None and selected.get("discovering"):
        return {"discovering": True, "summary": None, "settings": {}, "created": False, "databases": selected["databases"]}

    def planned():
        # A preview with no name yet groups the tables under PREVIEW_NAME and checks no group against the taken ones.
        unnamed_preview = body.dry_run and not name
        plan = _file_lines(nj.plan(PREVIEW_NAME if unnamed_preview else name, text, body.settings, _sizing(body),
                                   set() if unnamed_preview else taken, require_target=not body.dry_run,
                                   new_job=True), selected)
        errors = list(plan["errors"])
        if not errors and plan["groups"] and not unnamed_preview:
            schedule = {**run.model_dump(), "name": name, "members": plan["groups"],
                        "timezone": sch.timezone_for(run.cadence_minutes, run.timezone)}
            errors += sch.validate(schedule, plan["groups"], jobs.schedule_variants())
        if errors:
            raise HTTPException(422, {"errors": errors})
        return plan

    plan = planned()
    summary = nj.summary(plan)
    if body.dry_run or summary["invalid"]:
        return {"summary": summary, "settings": plan["settings"], "created": False,
                "target_missing": _target_missing(plan["settings"]), "name_missing": not name}
    if not run.paused:     # anyone may set up a job, but one that runs needs the right to run what it starts
        _refuse(_permission_refusals({"job_variant": run.job_variant}, user)["run"])
    _refuse_job_rows([line["row"] for line in plan["lines"]], connection, user)
    options = create_options
    _refuse_unknown_node_types(sch.own_cluster({"options": options}))
    timezone_ = sch.timezone_for(run.cadence_minutes, run.timezone)
    offs = sch.offsets(plan["groups"], run.cadence_minutes, run.stagger, sch.start_minute({"options": options, "cadence_minutes": run.cadence_minutes}))
    batch = uuid.uuid4().hex[:12]
    rows = [line["row"] for line in plan["lines"]]
    stored = {**options, preparing.OPTION: _create_record(body, run, user, plan["settings"])}

    def write(progress, task_id):
        try:
            with _write() as cur:
                _lock_collections(cur, plan["groups"])
                cur.execute(q.insert_schedule_sql(), (name, run.cadence_minutes, timezone_, json.dumps(run.parameters),
                                                      run.job_variant or "default", run.stagger, run.paused,
                                                      run.notify_emails or None, json.dumps(stored), user or None))
                sid = cur.fetchone()[0]
                _insert_controls(cur, rows, user, batch, progress=progress)
                _refuse_mixed_sinks(cur, rows)
                _refuse_split_databases(cur, rows)
                for group in plan["groups"]:
                    cur.execute(q.insert_member_sql(), (sid, group, offs[group]))
                _log_lines(cur, [(user, "schedule_create", None, group, "schedule", None,
                                  f"{name} ({sch.describe(run.cadence_minutes, offs[group], timezone_)})", None, batch)
                                 for group in plan["groups"]])
                _keep_selection(cur, sid, selected, user)
                _touch(cur, user, [sid])
                result = {"id": sid, "summary": summary, "settings": plan["settings"], "created": True}
                _task_written(cur, task_id, result, sid, len(rows))
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, f"an ingestion job named '{name}' already exists")
        return result

    def after(result):
        return _created(result, user, options)

    if inline:
        return write(progress, task_id)
    return _write_now_or_later("create", body.request_id, None, len(rows), user, write, after)


# How often a Create waiting on a draft's read asks where it stands (and takes a step of it, with the background reader
# off).
DRAFT_POLL_S = 1.5


def _create_after_reading(body: NewJobRequest, request: Request):
    """A Create chosen from a draft whose read hasn't ended: a background task does the read's steps until no database
    is left (the task's stage says how far), then writes as an inline Create does. A database that couldn't be read,
    or a draft discarded meanwhile, fails it by name before anything is written."""
    user = _user(request)
    if not body.run.paused:
        _refuse(_permission_refusals({"job_variant": body.run.job_variant}, user)["run"])
    options = {k: v for k, v in (body.run.options or {}).items() if k not in MANAGED_OPTIONS}

    def write(progress, task_id):
        while True:
            reading = _draft_reading(body.source)
            if not reading or not reading["pending"]:
                break
            db.execute(q.task_progress_json_sql(), (json.dumps({
                "stage": "reading", "databases": reading["databases"], "read": reading["done"] + reading["failed"],
                "tables": reading["tables"]}), 0, task_id))
            time.sleep(DRAFT_POLL_S)
        problems = _draft_problems(reading)
        if problems:
            raise HTTPException(422, {"errors": problems})
        _task_stage(task_id, "writing")
        return _create_now(body, request, inline=(progress, task_id))

    return _write_now_or_later("create", body.request_id, None, 0, user, write,
                               lambda result: _created(result, user, options) if result.get("created") else result,
                               background=True)


def _create_after_discovery(body: NewJobRequest, request: Request, progress, task_id) -> dict:
    """The background half of a Create with database-only lines: their tables are discovered by name (the task's stage
    says so), then the Create is written as an inline one is. A plan with lines it can't take makes no job, and answers
    as a preview does; here that is refused with each such line, so the task ends failed with the reason."""
    _task_stage(task_id, "discovering")
    _resolve_databases(_selected(body.source), wait=True, request=request)
    _task_stage(task_id, "writing")
    result = _create_job(body, request, inline=True, progress=progress, task_id=task_id)
    if not result.get("created"):
        invalid = (result.get("summary") or {}).get("invalid") or []
        raise HTTPException(422, {"errors": [f"line {line['line']} ({line['table']}): {'; '.join(line['errors'])}"
                                             for line in invalid] or ["the file takes no tables"]})
    return result


# ---- a new job's preparation (preparing.py): its state, what Create was asked for, its set-aside databases ----------
# A Create whose tables are all read, of BACKGROUND_ROWS tables or fewer, or from CSV lines in the request, writes every
# table before it answers, so the job it makes is Ready at once; its record keeps what it was asked for and by whom.
# Any other Create records the job at once and its prepare task (_prepare_job, below) writes it later: it moves the
# record with _move_prepare (stage and progress), records what it can't take (preparing.found), runs _prepare_checks
# before its write and inside each of its transactions, and ends with _make_ready once the job's Databricks jobs are
# made.

# The change log's user for what the app does by itself as a job becomes Ready (its schedule turned on).
PREPARE_USER = "preparing"
NO_RECORD = ("this job keeps no record of what Create was asked for (it was made before the app kept one), so it has "
             "nothing to prepare")
NOTHING_SET_ASIDE = "the job is ready: nothing is set aside to act on"


def _create_record(body: "NewJobRequest", run: "NewJobRun", user: str, settings: dict) -> dict:
    """A new job's preparation record (preparing.new_record), written with the job: its settings as asked, its sizing,
    its run parameters as checked (run.parameters, after _new_job_source), what it was chosen from, whether its
    schedule is to be on, and its maker, the user making it. A Create from CSV lines in the request has no source to
    read again (kind csv)."""
    source = {"kind": "csv", "source_id": None}
    if body.source is not None:
        kind, _, source_id = body.source.source()
        source = {"kind": kind, "source_id": source_id}
    return preparing.new_record(user, settings, {"tables_per_group": body.tables_per_group,
                                                 "groups_per_server": body.groups_per_server},
                                run.parameters, source, turn_on=not run.paused)


def _update_prepare(sid: int, change, cur=None) -> tuple:
    """Change a job's preparation record in one transaction (the caller's, or one of its own) under the job's schedule
    lock, which its edits and syncs take too: the record and the job read fresh, change(record, job) gives back
    (the new record, anything else), and the record is written whole. Returns what change gave. 409 without a record."""
    def apply(cur):
        cur.execute(q.schedule_lock_sql(), (q.SCHEDULE_LOCK_CLASS, q.schedule_lock_key(sid)))
        job = _load_schedule(sid, cur)[0]
        record = preparing.record_of(sch.options_of(job))
        if record is None:
            raise HTTPException(409, NO_RECORD)
        new, extra = change(record, job)
        if new != record:
            cur.execute(q.set_prepare_sql(), (json.dumps(new, default=str), int(sid)))
        return new, extra

    if cur is not None:
        return apply(cur)
    with _write() as cur:
        return apply(cur)


def _move_prepare(sid: int, state: Optional[str] = None, stage: Optional[str] = None, progress: Optional[dict] = None,
                  cur=None) -> dict:
    """For the prepare task: move the job's record to a state and stage (preparing.moved), with its progress
    merged over the last. Nothing prepares a deleted job, so its record is left as it is. Returns the record."""
    def change(record, job):
        if job.get("deleted_at") is not None:
            return record, None
        return preparing.moved(record, state, stage, progress), None
    return _update_prepare(sid, change, cur)[0]


def _set_aside(sid: int, entries: List[dict], cur=None) -> dict:
    """Set these databases aside (preparing.entry each, with why), in the transaction that
    found them when given one; the job Needs you while one waits for a choice. Returns the record."""
    return _update_prepare(sid, lambda record, job: (preparing.with_set_aside(record, entries), None), cur)[0]


def _clear_set_aside(sid: int, keys: Iterable[str], cur=None) -> dict:
    """These databases (preparing.database_key) were read or planned again and taken, so they leave the set-aside list.
    Returns the record."""
    return _update_prepare(sid, lambda record, job: (preparing.cleared(record, list(keys)), None), cur)[0]


def _make_ready(sid: int) -> dict:
    """The prepare task's last step: the job is Ready. A schedule that was to turn on does when whoever asked for it
    (at Create, its maker) may still run what it starts, read now, with no request behind it; otherwise it stays
    paused and the record says why. The wrapper jobs are synced after. Returns the record."""
    job = _load_schedule(sid)[0]
    record = preparing.record_of(sch.options_of(job))
    if record is None:
        raise HTTPException(409, NO_RECORD)
    asked_by = record.get("turn_on_by") or record.get("maker") or ""
    refusal = _permission_refusals(job, asked_by)["run"] if record.get("turn_on") else None

    def change(record, job):
        if preparing.is_ready({preparing.OPTION: record}):
            return record, False
        wanted = bool(record.get("turn_on")) and job.get("deleted_at") is None
        kept = refusal if wanted else None
        return preparing.ready(record, kept_paused=kept), wanted and kept is None and bool(job["paused"])

    with _write() as cur:
        record, turn_on = _update_prepare(int(sid), change, cur)
        if turn_on:
            cur.execute(q.update_schedule_sql(["paused"]), (False, int(sid)))
            _log(cur, PREPARE_USER, "schedule_resume", None, None, f"{job['name']}.paused", True, False)
    _sync(int(sid), PREPARE_USER)
    return record


@dataclass(slots=True, kw_only=True)
class PreparedSource:
    """What a prepared job's write takes, checked again from its record (_prepare_checks): who made the job, the JDBC
    connection it reads through as stored now (None for none), its run parameters with that connection's scope and
    trust, what it was chosen from (None for CSV lines in the request), its settings as asked and its sizing."""
    maker: str
    connection: Optional[dict]
    parameters: dict
    selection: Optional[Selection]
    settings: dict
    sizing: placement.Groups


def _prepare_checks(sid: int, rows: Optional[List[dict]] = None, cur=None) -> PreparedSource:
    """The write-time checks of a job's preparation (N1, N4 and N6), from its record, with its maker, read now. The
    prepare task calls it before its write (no rows) and again inside each transaction of the write, with that transaction's rows
    and cursor, as _create_by_database runs _new_job_source and _refuse_job_rows. No request is behind it: the maker is
    the one Create stored, and their rights are read again (one who is no longer an app manager is refused a read by
    host and password, or a secret scope); the JDBC connection and its logins are read as they are now (a login typed
    again under one of its keys for another connection refuses the rows that key reads). Refusals raise as they do on
    Create's own path."""
    job = (_load_schedule(sid, cur) if cur is not None else _load_schedule(sid))[0]
    options = sch.options_of(job)
    record = preparing.record_of(options)
    if record is None:
        raise HTTPException(409, NO_RECORD)
    maker = record.get("maker") or ""
    selection = _stored_selection(int(sid), record, cur)
    kept = {k: v for k, v in _parameters_of(job).items() if k not in preparing.SCOPE_PARAMETERS}
    run = NewJobRun(parameters={**kept, **(record.get("scope") or {})})
    connection = _new_job_source(run, selection, maker, applying=True)
    if rows:
        _refuse_job_rows(rows, connection, maker, cur=cur)
    sizing = record.get("sizing") or {}
    return PreparedSource(maker=maker, connection=connection, parameters=run.parameters, selection=selection,
                          settings=dict(record.get("settings") or {}),
                          sizing=placement.sizing(sizing.get("groups_per_server"), sizing.get("tables_per_group"),
                                                  options.get(TABLES_PER_GROUP_OPTION)))


def _stored_selection(sid: int, record: dict, cur=None) -> Optional[Selection]:
    """What a job was chosen from, as a selection again: its job_selection rows (only the app writes them, and while the
    job isn't Ready only its Create has), and the source its record names. None for a Create from CSV lines in the
    request, which has no source to read again."""
    parts = (db.fetch(cur, q.job_selection_sql(), (int(sid),)) if cur is not None
             else db.query(q.job_selection_sql(), (int(sid),)))
    source = record.get("source") or {}
    kind = source.get("kind") or next((p["source_kind"] for p in parts), None)
    source_id = source.get("source_id") or next((p["source_id"] for p in parts), None)
    if kind not in SOURCE_KINDS or not source_id:
        return None
    whole = [p for p in parts if p["scope"] == "server"]
    chosen = {"whole_server": bool(whole), "filter": (whole[0].get("filter") or "") if whole else "",
              "databases": [SelectedDatabase(server=p["server"], database=p["src_database"])
                            for p in parts if p["scope"] == "database"],
              "tables": [SelectedTables(server=p["server"], database=p["src_database"], tables=list(p["tables"] or []))
                         for p in parts if p["scope"] == "tables" and p.get("tables")]}
    try:
        if kind == "upload":
            return Selection(upload_id=str(source_id), **chosen)
        return Selection(discovery_ids=str(source_id).split(","), **chosen)
    except ValueError:
        raise HTTPException(409, "what this job was chosen from can't be read back from its selection")


def _job_reads(job: dict) -> Optional[dict]:
    """Where the reads of a job's claimed drafts stand (queries.job_reads_sql), or None when it was chosen from no
    discovery."""
    selection = db.query(q.job_selection_sql(), (int(job["id"]),))
    source = (preparing.record_of(sch.options_of(job)) or {}).get("source") or {}
    ids = {i for part in selection if part["source_kind"] == "discovery" for i in str(part["source_id"] or "").split(",")}
    if source.get("kind") == "discovery":
        ids |= set(str(source.get("source_id") or "").split(","))
    ids.discard("")
    if not ids:
        return None
    return db.query(q.job_reads_sql(), (preparing.RATE_WINDOW_S, sorted(ids), str(int(job["id"]))))[0]


@app.get("/api/ingestion-jobs/{job_id}/prepare")
def job_preparation(job_id: int, request: Request):
    """Where a job's preparation stands, for its page (preparing.view): its state and stage, the reading of its claimed
    drafts while it isn't Ready (of how many databases, how many are read, the tables so far, the minutes left), the
    write's progress as the prepare task reports it, the set-aside and left-out databases (their counts, every clash and
    the first LISTED of the rest; GET .../prepare/set-aside pages them all), whether it stopped, and why the user may
    not take each action. Read only. A job made before the record is Ready."""
    job = _load_schedule(job_id)[0]
    options = sch.options_of(job)
    reads = None if preparing.is_ready(options) else _job_reads(job)
    refusals = _permission_refusals(job, _user(request))
    acting = refusals["change"] or (NOTHING_SET_ASIDE if preparing.is_ready(options) else None)
    stopped = preparing.stopped_of(options)
    return {"id": int(job_id), **preparing.view(options, reads),
            "actions": {"read_again": acting, "leave_out": acting, "targets": acting, "delete": refusals["delete"],
                        "stop": acting or ("the job's preparation is stopped already" if stopped else None),
                        "again": acting}}


class SetAsideRequest(BaseModel):
    """The set-aside databases an action takes: those named, or, with none named, every one it applies to."""
    databases: Optional[List[SelectedDatabase]] = Field(None, max_length=5000)


def _set_aside_keys(body: SetAsideRequest) -> Optional[List[str]]:
    return None if body.databases is None else [preparing.database_key(d.server, d.database) for d in body.databases]


def _named_reads(body: SetAsideRequest) -> tuple:
    """The params prepare_read_again_sql and prepare_leave_out_reads_sql take for the databases named: (all, servers,
    databases), each server without its port, all in lower case."""
    named = body.databases or []
    return (body.databases is None, [rediscovery.server_key(d.server) for d in named],
            [d.database.lower() for d in named])


def _refuse_set_aside_action(job: dict, request: Request) -> None:
    """The set-aside actions change the job, so they follow Edit's check: the user's Can Manage on the job's Databricks
    job (or ownership, or a workspace admin), read now; a deleted job refuses them. Only while the job isn't Ready."""
    _refuse_job(job, request, "change")
    if preparing.is_ready(sch.options_of(job)):
        raise HTTPException(409, NOTHING_SET_ASIDE)


def _set_aside_action(job_id: int, request: Request, action: str, change) -> tuple:
    """One set-aside action: checked (_refuse_set_aside_action), then the record changed under the job's lock (still not
    Ready, still not deleted), with a change log line naming how many databases it took, all in one transaction; then
    the prepare task asked to go on (_start_prepare) when the record has work for it. change(record, user, cur) gives
    back (the record, how many)."""
    user = _user(request)
    job = _load_schedule(job_id)[0]
    _refuse_set_aside_action(job, request)

    with _write() as cur:
        def apply(record, job_now):
            if job_now.get("deleted_at") is not None:
                raise HTTPException(409, DELETED_JOB)
            if preparing.is_ready({preparing.OPTION: record}):
                raise HTTPException(409, NOTHING_SET_ASIDE)
            return change(record, user, cur)

        record, count = _update_prepare(int(job_id), apply, cur)
        if count:
            _log(cur, user, action, None, None, job["name"], None, f"{count} {'database' if count == 1 else 'databases'}")
            _touch(cur, user, [int(job_id)])
    if preparing.has_work(record):
        _start_prepare(int(job_id))
    return record, count


@app.post("/api/ingestion-jobs/{job_id}/prepare/read-again")
def read_set_aside_again(job_id: int, body: SetAsideRequest, request: Request):
    """Read them again: the databases that couldn't be read (those named, or all of them) are read again: their rows
    are reset in the same transaction, so the background reader takes them (a file's database-only lines are read by a
    new discovery run), and the job is Preparing again once none waits for a choice."""
    job = _load_schedule(job_id)[0]
    ids = _job_discovery_ids(job)

    def change(record, user, cur):
        if (record.get("source") or {}).get("kind") == "discovery":
            reset = len(db.fetch(cur, q.prepare_read_again_sql(), (ids, str(int(job_id)), *_named_reads(body)))) \
                if ids else 0
        else:
            keys = _set_aside_keys(body)
            reset = sum(1 for k, e in (record.get("set_aside") or {}).items()
                        if e.get("kind") == preparing.UNREADABLE and (keys is None or k in keys))
            if reset and record.get("database_read"):
                with _database_reads_lock:
                    for key in [k for k, read in _database_reads.items()
                                if read.get("run_id") == record["database_read"].get("run_id")]:
                        del _database_reads[key]
                record = dict(record, database_read=None)
        return preparing.read_again(record, _set_aside_keys(body), user, reset)

    record, marked = _set_aside_action(job_id, request, "prepare_read_again", change)
    return {"id": int(job_id), "marked": marked, **preparing.summary({preparing.OPTION: record})}


def _left_out_entries(job_id: int, record: dict, body: SetAsideRequest, cur) -> List[dict]:
    """The set-aside databases Leave out takes (those named, or every one that waits for a choice): the clashes from
    the record, which keeps them all, and, for a job chosen from discoveries, the databases of its claimed drafts that
    couldn't be read, from their rows, marked left out in the same transaction (a whole-server read's closed ones are
    left out already)."""
    keys = _set_aside_keys(body)
    entries = [e for k, e in (record.get("set_aside") or {}).items()
               if e.get("kind") in preparing.NEEDS_ACTION and (keys is None or k in keys)]
    if (record.get("source") or {}).get("kind") != "discovery":
        return entries
    job = _load_schedule(job_id, cur)[0]
    ids = _job_discovery_ids(job)
    failed = [e for e in (_failed_reads(job) if ids else []) if e["kind"] == preparing.UNREADABLE
              and (keys is None or preparing.database_key(e["server"], e["database"]) in keys)]
    if failed:
        db.fetch(cur, q.prepare_leave_out_reads_sql(), (ids, str(int(job_id)), False,
                                                        [rediscovery.server_key(e["server"]) for e in failed],
                                                        [e["database"].lower() for e in failed]))
    return [e for e in entries if e["kind"] == preparing.CLASH] + failed


@app.post("/api/ingestion-jobs/{job_id}/prepare/leave-out")
def leave_set_aside_out(job_id: int, body: SetAsideRequest, request: Request):
    """Leave them out: the set-aside databases (those named, or all that wait for a choice) come out of the job's choice,
    which its write and a later re-discovery leave out, and the job goes on (Ready, when nothing else is left)."""
    record, left = _set_aside_action(job_id, request, "prepare_leave_out", lambda record, user, cur: preparing.leave_out(
        record, _left_out_entries(job_id, record, body, cur), user))
    return {"id": int(job_id), "left_out": left, **preparing.summary({preparing.OPTION: record})}


class TargetNamesRequest(BaseModel):
    """The job's target name templates, any of the three; the others keep what Create was asked for."""
    sink_catalog: Optional[str] = Field(None, max_length=255)
    sink_schema: Optional[str] = Field(None, max_length=255)
    sink_table: Optional[str] = Field(None, max_length=255)
    dry_run: bool = True


def _target_names(job: dict, record: dict, body: TargetNamesRequest) -> dict:
    """The record's settings with the request's target templates, checked as Create checks them (422 naming why)."""
    given = {k: getattr(body, k) for k in preparing.TARGET_FIELDS if getattr(body, k) is not None}
    settings = nj.settings_of({**(record.get("settings") or {}), **given}, new_job=True)
    errors = nj.setting_errors(settings, require_target=True, unity_catalog=_unity_catalog(job))
    if errors:
        raise HTTPException(422, {"errors": errors})
    return given


@app.post("/api/ingestion-jobs/{job_id}/prepare/targets")
def change_set_aside_targets(job_id: int, body: TargetNamesRequest, request: Request):
    """Change the job's target names: preview (dry_run, the default) or store new target templates in the job's record;
    the databases set aside for a clash come off the list, and the prepare task plans them again with the new names
    (with the rest not written yet)."""
    job = _load_schedule(job_id)[0]
    record = preparing.record_of(sch.options_of(job))
    if record is None or preparing.is_ready(sch.options_of(job)):
        raise HTTPException(409, NOTHING_SET_ASIDE)
    given = _target_names(job, record, body)
    clashes = preparing.counts_of(record)[preparing.CLASH]
    if body.dry_run:
        return {"id": int(job_id), "targets": given, "replan": clashes, "applied": False}
    record, marked = _set_aside_action(
        job_id, request, "prepare_targets",
        lambda record, user, cur: preparing.change_targets(record, _target_names(job, record, body), user))
    return {"id": int(job_id), "targets": given, "replan": marked, "applied": True,
            **preparing.summary({preparing.OPTION: record})}


def _keep_paused_when_ready(job: dict, user: str) -> dict:
    """Pause on a job that isn't Ready: it is paused already, so Pause keeps its schedule off once it is Ready too (its
    record's turn_on), logged as a pause."""
    def change(record, job_now):
        return preparing.with_turn_on(record, False, user), bool(record.get("turn_on"))

    with _write() as cur:
        _, was_on = _update_prepare(int(job["id"]), change, cur)
        if was_on:
            _log(cur, user, "schedule_pause", None, None, f"{job['name']}.turn_on_when_ready", True, False)
            _touch(cur, user, [int(job["id"])])
    return {"id": int(job["id"]), "paused": True, "turn_on": False, "sync": []}


# The settings Edit shows (job_settings.read's keys), read from a record for a job that isn't Ready.
STORED_SETTINGS_SHOWN = ("fan_in", "write_mode_changes", "write_mode_full", "freshness_sla_minutes", "sink_catalog",
                         "sink_schema", "sink_table")


def _stored_settings(job: dict, groups: List[str]) -> dict:
    """A job that isn't Ready has no tables yet, or not all of them: its settings are those its record keeps (what
    Create was asked for), in the shape job_settings.read gives them (each a value), with why they can't be changed now
    (settings_locked), if they can't."""
    options = sch.options_of(job)
    stored = nj.settings_of((preparing.record_of(options) or {}).get("settings") or {}, new_job=True)
    shown = {k: {"value": stored.get(k)} for k in STORED_SETTINGS_SHOWN}
    shown["client_id"] = {"value": stored["client_id"] if stored["fan_in"] else None}
    shown["identity"] = {"value": dict(stored.get("identity") or {})}
    shown["unity_catalog"] = {"value": _unity_catalog(job)}
    tables = db.query(q.groups_row_count_sql(), (groups,))[0]["n"] if groups else 0
    return {"job_id": int(job["id"]), "groups": groups, "tables": tables, "settings": shown, "identity_differs": [],
            "reads_by_host": _job_jdbc(int(job["id"])) is not None, "stored": True,
            "settings_locked": preparing.settings_refusal(options)}


def _edit_stored_settings(job: dict, edits: dict, dry_run: bool, request: Request) -> dict:
    """A settings edit of a job that isn't Ready: while its tables are still read, it changes the settings its record
    keeps, which its write takes, checked as Create checks them; once the write has begun it waits for Ready
    (preparing.settings_refusal). Applying needs the change permission, and is one transaction under the job's lock."""
    _refuse_unready(job, preparing.settings_refusal)

    def checked(record):
        before = record.get("settings") or {}
        settings = nj.settings_of({**before, **(edits or {})}, new_job=True)
        errors = nj.given_errors(edits or {}) + nj.setting_errors(settings, require_target=True,
                                                                  unity_catalog=_unity_catalog(job))
        changed = sorted(k for k in settings if settings[k] != before.get(k))
        summary = {"rows": 0, "changed": len(changed), "reseed": 0, "errors": len(errors), "applied": False,
                   "stored": True, "settings_changed": changed}
        if errors:
            raise HTTPException(422, {"errors": errors, "summary": summary})
        return settings, summary

    if dry_run:
        return {"summary": checked(preparing.record_of(sch.options_of(job)) or {})[1], "errors": []}
    _refuse_job(job, request, "change")
    user = _user(request)

    def change(record, job_now):
        _refuse_unready(job_now, preparing.settings_refusal)
        settings, summary = checked(record)
        summary["applied"] = True
        return dict(record, settings=settings, updated_at=datetime.now(timezone.utc).isoformat()), summary

    with _write() as cur:
        _, summary = _update_prepare(int(job["id"]), change, cur)
        for name in summary["settings_changed"]:
            _log(cur, user, "schedule_edit", None, None, f"{job['name']}.settings.{name}", None, None)
        if summary["changed"]:
            _touch(cur, user, [int(job["id"])])
    return {"summary": summary, "errors": []}


def _regroup_stored(job: dict, body: "JobGroupsRequest", request: Request) -> dict:
    """Groups of a job that isn't Ready: while its tables are still read, a new tables per group or groups per server is
    kept on its record (and tables per group in its options, as a regroup keeps it), and its write places its databases
    so; once the write has begun it waits for Ready. Nothing moves, as there are no tables to move yet."""
    _refuse_unready(job, preparing.settings_refusal)
    summary = {"servers": [], "databases_moving": 0, "tables_moving": 0, "new_groups": [], "emptied_groups": [],
               "unchanged": True, "applied": False, "stored": True}
    if body.dry_run:
        return {"summary": summary, "blocked": None}
    _refuse_job(job, request, "change")
    user = _user(request)

    def change(record, job_now):
        _refuse_unready(job_now, preparing.settings_refusal)
        sizing = {"tables_per_group": body.tables_per_group, "groups_per_server": body.groups_per_server}
        return dict(record, sizing=sizing, updated_at=datetime.now(timezone.utc).isoformat()), None

    with _write() as cur:
        _update_prepare(int(job["id"]), change, cur)
        if body.tables_per_group:
            cur.execute(q.set_schedule_option_sql(), ([TABLES_PER_GROUP_OPTION], int(body.tables_per_group),
                                                      int(job["id"])))
        _log(cur, user, "schedule_edit", None, None, f"{job['name']}.sizing", None,
             f"{body.tables_per_group} tables per group" if body.tables_per_group
             else f"{body.groups_per_server} groups per server" if body.groups_per_server else "the default")
        _touch(cur, user, [int(job["id"])])
    return {"summary": {**summary, "applied": True}, "blocked": None}


# ---- Create at once, then the prepare task ----------------------------------------------------------------------------
# A Create whose tables aren't all read yet, or that takes more than BACKGROUND_ROWS tables, or whose file has
# database-only lines, records the job at once (_create_preparing): every check that needs only names and settings,
# then, in one transaction, the job paused with no groups and its record Preparing, what it was chosen from with its
# drafts claimed, and its prepare task. It answers with the job's id. The prepare task (_prepare_job) then reads, writes
# and syncs the job on a thread with no request behind it, held to the job's maker and its connection as they are at
# each write. PREPARE_CREATES=0 keeps the earlier Create (the request waits in a task of its own until the job is
# written), for a rollback.
PREPARE_CREATES = os.environ.get("PREPARE_CREATES", "1") != "0"
# How often the prepare task looks at the reads it waits for, and how often each app instance looks for a job whose
# prepare task no thread holds (an app restarted while it prepared); 0 turns that look off on this instance.
PREPARE_POLL_S = float(os.environ.get("PREPARE_POLL_S", "2"))
PREPARE_RESUME_S = float(os.environ.get("PREPARE_RESUME_S", "30"))
NOTHING_LEFT = ("nothing is left to ingest: every database of the job's choice was left out or takes no tables; "
                "delete the job, or create it again")
NOT_RECORDED = "this job was not recorded by Create, so it has no preparation to stop or go on with"
STOPPED_WHY = "stopped by {user}"
DELETED_WHY = "the job was deleted by {user}"
TAKEN_GROUPS = ("the ingestion groups this job would make are taken already ({groups}): another job uses them, so give "
                "the job another name")


class PrepareStopped(Exception):
    """The prepare task ends here and writes nothing more: the job was stopped or deleted, is Ready, or another
    instance holds its task now."""


class PrepareRefused(Exception):
    """A check at the write refused the whole job (its maker, its connection or its plan as they are now): the
    preparation stops, saying why, until Prepare again."""

    def __init__(self, error: HTTPException):
        super().__init__(_error_text(error))
        self.error = error


def _job_discovery_ids(job: dict) -> List[str]:
    """The discoveries a job was chosen from (its record's source and its job_selection rows), whose claimed rows its
    preparation reads."""
    record = preparing.record_of(sch.options_of(job)) or {}
    source = record.get("source") or {}
    ids = set(str(source.get("source_id") or "").split(",")) if source.get("kind") == "discovery" else set()
    ids |= {i for part in db.query(q.job_selection_sql(), (int(job["id"]),)) if part["source_kind"] == "discovery"
            for i in str(part["source_id"] or "").split(",")}
    ids.discard("")
    return sorted(ids)


def _discovery_key_row(row: dict) -> dict:
    """A discovery row (selection_discoveries_sql) as the fields a control row read from it takes (as
    queries.source_rows_sql builds them), for its database key."""
    if row.get("src_connection"):
        return {"src_type": row.get("src_type"), "src_connection": row["src_connection"],
                "src_database": row["src_database"]}
    return {"src_type": row.get("src_type"), "src_host": row.get("src_host"), "src_port": row.get("src_port"),
            "secret_key": row.get("secret_key"), "src_database": row["src_database"]}


def _chosen_databases(selection: Selection, kind: str, param) -> List[dict]:
    """The databases a choice takes, one row each ({server, src_database} and the fields its database key reads), from
    what is known before any table is read: a discovery's rows (not one its draft dropped), or an upload's lines."""
    whole, keys = selection.whole_server, {q.selection_key(d.server, d.database) for d in selection.databases}
    keys |= {q.selection_key(t.server, t.database) for t in selection.tables}
    filter_ = (selection.filter or "").lower()
    if kind == "discovery":
        rows = [{**_discovery_key_row(r), "server": r["server"]} for r in db.query(q.selection_discoveries_sql(), (param,))
                if not (r.get("listing") or {}).get("dropped") and r.get("src_database")]
    else:
        rows = [{**{k: (r.get("fields") or {}).get(k) for k in ("src_type", "src_connection", "src_host", "src_port",
                                                                "secret_key")},
                 "src_database": r["src_database"], "server": r["server"]}
                for r in db.query(q.upload_database_sources_sql(), (param,))]
    chosen, seen = [], set()
    for row in rows:
        key = q.selection_key(row["server"], row["src_database"])
        if key in seen or not ((whole and filter_ in row["src_database"].lower()) or key in keys):
            continue
        seen.add(key)
        chosen.append(row)
    return chosen


def _refuse_owned_databases(rows: List[dict]) -> None:
    """Create's check, by database key alone, before any table is read: a database already in another job's groups,
    live or deleted and not purged, refuses the Create, as the split and deleted-job checks would at the write."""
    keys = sorted({q.database_key(row) for row in rows})
    for at in range(0, len(keys), q.CONTROL_BATCH * 10):
        found = db.query(q.database_owners_sql(), (keys[at:at + q.CONTROL_BATCH * 10],))
        if not found:
            continue
        database, job, deleted = found[0]["database"], found[0]["job"], found[0]["deleted"]
        if deleted:
            raise HTTPException(409, f"database {database} is in the deleted ingestion job {job}, which is kept until "
                                     f"it is purged: Restore {job}, or Delete now to purge it, then create this job")
        raise HTTPException(409, f"database {database} is in the ingestion job {job} already: a database's tables stay "
                                 "in one job, so leave it out of this one, or add tables to that job")


def _taken_group_errors(name: str, servers: Iterable[str], taken: Iterable[str]) -> List[str]:
    """The groups other jobs use that this job's would be named (the job's name, each server, c01 on): by name alone,
    as the number of groups waits for the tables."""
    prefixes = [f"{q.identifier(name)}_{q.identifier(server)}_c" for server in sorted(set(servers))]
    clashing = sorted(g for g in taken if any(g.startswith(p) and g[len(p):].isdigit() for p in prefixes))
    return [TAKEN_GROUPS.format(groups=", ".join(clashing[:3]))] if clashing else []


def _prepared_already(body: NewJobRequest) -> tuple:
    """(the answer to a Create sent again with the request id of one that recorded its job: that job, recorded once;
    whether the request id names a task already, of any kind)."""
    if not body.request_id:
        return None, False
    made = db.query(q.task_of_request_sql(), (body.request_id,))
    task = db.query(q.task_sql(), (made[0]["id"],))[0] if made else None
    if not task or task["kind"] != q.PREPARE_KIND or task.get("job_id") is None:
        return None, task is not None
    job = _load_schedule(int(task["job_id"]))[0]
    return {"id": int(task["job_id"]), "created": True, "prepare": preparing.summary(sch.options_of(job))}, True


def _create_preparing(body: NewJobRequest, request: Request, database_lines: bool = False) -> dict:
    """Create at once (decision B): the checks that need only names and settings (the job's name, its targets, its
    group names free, no database already in another job, live or deleted, the source and scope rules, the right to run
    what it starts when its schedule is to be on, and the right to run discovery when the file has database-only lines,
    which a discovery run reads), then in one transaction the job (paused, no groups, its record Preparing), what it
    was chosen from with its drafts claimed, and its prepare task, which is started after the commit. Answers with the
    job's id; the job's page follows its preparation."""
    import psycopg
    made, named = _prepared_already(body)
    if made is not None:
        return made
    name = body.name.strip()
    run = body.run.model_copy()
    run.stagger = run.stagger and run.job_variant != sch.CHECK_VARIANT
    user = _user(request)
    selection = body.source
    connection = _new_job_source(run, selection, user, applying=True)
    settings = nj.settings_of(body.settings, new_job=True)
    errors = nj.given_errors(body.settings) + nj.setting_errors(settings, require_target=True)
    if not name:
        errors.insert(0, "name the job")
    timezone_ = sch.timezone_for(run.cadence_minutes, run.timezone)
    kind, param, source_id, _ = _selection_params(selection)
    chosen = _chosen_databases(selection, kind, param)
    if not chosen and not database_lines:
        errors.append(_no_tables_refusal(kind, param, selection))
    if name and not errors:
        errors += [e for e in sch.validate({**run.model_dump(), "name": name, "members": [], "timezone": timezone_}, [],
                                           jobs.schedule_variants()) if not e.startswith("members:")]
        errors += _taken_group_errors(name, [row["server"] for row in chosen],
                                      [r["task_collection"] for r in db.query(q.collections_sql())])
    if errors:
        raise HTTPException(422, {"errors": errors})
    if not run.paused:     # anyone may set up a job, but one that runs needs the right to run what it starts
        _refuse(_permission_refusals({"job_variant": run.job_variant}, user)["run"])
    if database_lines:     # a discovery run reads them later, started by the app for this user
        _refuse_run_of(_require(jobs.DISCOVER_JOB_ID, "discovery"), request)
    options = _create_options(body)
    _refuse_unknown_node_types(sch.own_cluster({"options": options}))
    _refuse_owned_databases(chosen)
    if database_lines:
        selected = _selected(selection)
    else:
        whole = sorted({row["server"] for row in chosen}) if selection.whole_server else []
        selected = {"parts": _selection_parts(selection, whole), "kind": kind, "source_id": source_id}
    record = preparing.moved(_create_record(body, run, user, settings), state=preparing.PREPARING,
                             stage=preparing.READING)
    stored = {**options, preparing.OPTION: record}
    try:
        with _write() as cur:
            cur.execute(q.insert_schedule_sql(), (name, run.cadence_minutes, timezone_, json.dumps(run.parameters),
                                                  run.job_variant or "default", run.stagger, True,
                                                  run.notify_emails or None, json.dumps(stored), user or None))
            sid = cur.fetchone()[0]
            _keep_selection(cur, sid, selected, user)
            request_id = body.request_id if body.request_id and not named else uuid.uuid4().hex
            cur.execute(q.insert_task_sql(), (q.PREPARE_KIND, request_id, sid, 0, None, user or None))
            _log(cur, user, "schedule_create", None, None, "schedule", None, f"{name} (preparing)")
            _touch(cur, user, [sid])
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, f"an ingestion job named '{name}' already exists")
    _start_prepare(sid)
    return {"id": sid, "created": True, "settings": settings, "summary": None,
            "prepare": preparing.summary(stored)}


def _start_prepare(sid: int, backoff_s: float = 0.0) -> Optional[int]:
    """Claim the job's prepare task (claim_prepare_sql: one thread across every instance) and start its thread. Returns
    the task's id, or None when another thread holds it, or it ended less than backoff_s ago."""
    owner = uuid.uuid4().hex
    claimed = db.query(q.claim_prepare_sql(), (owner, int(sid), TASK_STALE_S, float(backoff_s)))
    if not claimed:
        return None
    task_id = int(claimed[0]["id"])
    _start_background(lambda: _prepare_job(int(sid), task_id, owner))
    return task_id


def _resume_preparing() -> List[int]:
    """Start the prepare task of every job still Preparing whose task no thread holds (its app restarted while it
    prepared, or it failed a while ago): what each instance looks for every PREPARE_RESUME_S. Returns those started."""
    started = []
    for row in db.query(q.preparing_jobs_sql(), (TASK_STALE_S, TASK_STALE_S)):
        if _start_prepare(int(row["id"]), backoff_s=TASK_STALE_S) is not None:
            started.append(int(row["id"]))
    return started


def _prepare_resumer() -> None:
    while True:
        try:
            _resume_preparing()
        except Exception:
            logging.getLogger(__name__).exception("the look for jobs to prepare again failed")
        time.sleep(PREPARE_RESUME_S)


def _prepare_job(sid: int, task_id: int, owner: str) -> None:
    """The prepare task's thread: passes (_prepare_pass) until the job needs nothing more, then the task ends. A beat
    keeps its heartbeat fresh while it holds the task; once another instance holds it (or it was ended), the next
    write sees so and stops. A check that refuses the whole job stops its preparation, saying why."""
    held, stop = threading.Event(), threading.Event()
    held.set()

    def beat():
        while not stop.wait(TASK_BEAT_S):
            try:
                if not db.query(q.prepare_beat_sql(), (task_id, owner)):
                    held.clear()
                    return
            except Exception:
                pass     # a missed beat is caught up by the next; TASK_STALE_S allows for many

    threading.Thread(target=beat, daemon=True, name="lakefed-app-prepare-beat").start()
    status, error = "succeeded", None
    try:
        while True:
            asks = _prepare_pass(sid, task_id, owner, held)
            record = preparing.record_of(sch.options_of(_load_schedule(sid)[0]))
            if not preparing.has_work(record) or int((record or {}).get("asks") or 0) == asks:
                break
    except PrepareStopped:
        return
    except PrepareRefused as e:
        status, error = "failed", str(e)
        _stop_prepare(sid, PREPARE_USER, error, task_ended=False)
    except Exception as e:
        status, error = "failed", _error_text(e)
        logging.getLogger(__name__).warning("the preparation of job %s failed: %s", sid, error)
    finally:
        stop.set()
    db.execute(q.finish_prepare_sql(), (status, error, int(sid), owner, owner))


def _prepare_alive(sid: int, held: threading.Event) -> dict:
    """The job, while its preparation goes on: PrepareStopped once it was stopped, deleted or made Ready, or another
    instance holds the task."""
    if not held.is_set():
        raise PrepareStopped()
    job = _load_schedule(sid)[0]
    record = preparing.record_of(sch.options_of(job))
    if job.get("deleted_at") is not None or not record or record.get("stopped") or preparing.is_ready(
            {preparing.OPTION: record}):
        raise PrepareStopped()
    return job


def _prepare_fence(cur, sid: int, task_id: int, owner: str) -> None:
    """The first statements of each transaction of a prepare write: the job's schedule lock (Delete, Stop preparing and
    the job's edits take it too, so they wait for this write, briefly, and it for them), the task row while this
    thread still holds it, and the job still preparing."""
    cur.execute(q.schedule_lock_sql(), (q.SCHEDULE_LOCK_CLASS, q.schedule_lock_key(sid)))
    if not db.fetch(cur, q.prepare_fence_sql(), (task_id, owner)):
        raise PrepareStopped()
    job = _load_schedule(sid, cur)[0]
    record = preparing.record_of(sch.options_of(job))
    if job.get("deleted_at") is not None or not record or record.get("stopped") or preparing.is_ready(
            {preparing.OPTION: record}):
        raise PrepareStopped()


def _prepare_pass(sid: int, task_id: int, owner: str, held: threading.Event) -> int:
    """One pass of a job's preparation, from its record and what it was chosen from: the reads waited for (each
    database the reader takes; nothing read is read again), every database not written yet planned and written by
    database (_prepare_write), what can't be taken set aside (preparing.found), then, with nothing waiting, its
    Databricks jobs synced and the job Ready. Returns the record's asks when the pass began: an action taken during it
    asks for one more."""
    job = _prepare_alive(sid, held)
    record = preparing.record_of(sch.options_of(job)) or {}
    asks = int(record.get("asks") or 0)
    try:
        source = _prepare_checks(sid)
    except HTTPException as e:
        raise PrepareRefused(e)
    if source.selection is None:
        raise PrepareRefused(HTTPException(409, NO_RECORD))
    kind, param, _, rest = _selection_params(source.selection)
    if kind == "upload" and _has_database_lines(source.selection):
        entries, counts, checked = _prepare_file(sid, held, job, source)
    else:
        if kind == "discovery":
            _prepare_reads(sid, held, job)
        entries, counts, checked = _prepare_databases(sid, held, job, source, kind, param, rest)
    _move_prepare(sid, stage=preparing.WRITING)
    entries += _prepare_write(sid, task_id, owner, held, job, source, checked)
    counts = {**counts, preparing.CLASH: sum(1 for e in entries if e["kind"] == preparing.CLASH)}
    record = _update_prepare(sid, lambda r, j: (preparing.found(r, entries, counts), None))[0]
    if preparing.waiting_count(record):
        return asks
    _prepare_alive(sid, held)
    if not _job_groups(sid):
        raise PrepareRefused(HTTPException(409, NOTHING_LEFT))
    _move_prepare(sid, stage=preparing.SYNCING)
    cluster = _apply_cluster(sid) if sch.own_cluster(job) else None
    _synced(_sync(sid, PREPARE_USER))
    if cluster:
        raise NotSynced(500, f"{TASK_WRITTEN_NOT_SYNCED} {cluster}"[:4000])
    _make_ready(sid)
    return asks


def _step_reads(ids: List[str]) -> int:
    """One look at the reads of these discoveries: the background reader woken while any is left (with it off, one
    step of each taken, as the page would). Returns how many databases are left to read."""
    left = 0
    for r in db.query(q.draft_reads_sql(), (list(ids),)):
        pending = int(r["pending"] or 0)
        left += pending
        if pending and r.get("src_connection"):
            if READER.running():
                READER.wake()
            else:
                warehouse_reads.advance(WarehouseReadStore(), warehouse, warehouse_reads.READ, r["discovery_id"],
                                        r["src_connection"])
    return left


def _prepare_reads(sid: int, held: threading.Event, job: dict) -> None:
    """Reading: wait for the job's claimed drafts to be read (the background reader reads them, whatever page is open),
    the record's progress moved on as they go."""
    ids = _job_discovery_ids(job)
    _move_prepare(sid, stage=preparing.READING)
    while ids:
        _prepare_alive(sid, held)
        if not _step_reads(ids):
            return
        reading = preparing.reading_view(_job_reads(job)) or {}
        _move_prepare(sid, progress={"percent": reading.get("percent"), "minutes_left": reading.get("minutes_left"),
                                     "databases": reading.get("databases"), "read": reading.get("read")})
        time.sleep(PREPARE_POLL_S)


def _failed_reads(job: dict) -> List[dict]:
    """Every database of the job's claimed drafts that couldn't be read (job_failed_reads_sql), each as a set-aside
    entry: one a whole-server read couldn't open is closed (left out, as the whole-server read always has), any other
    couldn't be read."""
    ids = _job_discovery_ids(job)
    if not ids:
        return []
    rows = db.query(q.job_failed_reads_sql(), (ids, str(int(job["id"])), 1_000_000, 0))
    return [preparing.entry(r["server"], r["src_database"],
                            preparing.CLOSED if r["whole_server"] and warehouse_reads.no_access(r.get("error"))
                            else preparing.UNREADABLE, warehouse_reads.first_line(r.get("error")) or "it couldn't be read")
            for r in rows]


def _written_databases(sid: int) -> set:
    """The databases the job has written already, by server (without its port) and name in lower case: a later pass
    and a resumed one leave them as they are."""
    groups = _job_groups(sid)
    if not groups:
        return set()
    return {(rediscovery.server_key(r["server"]), str(r["src_database"] or "").lower())
            for r in db.query(q.job_database_groups_sql(), (groups,))}


def _unit_key(server: str, database: str) -> tuple:
    return rediscovery.server_key(server), str(database or "").lower()


def _prepare_databases(sid: int, held: threading.Event, job: dict, source: PreparedSource, kind, param, rest):
    """Plan every database of the choice not written yet, not left out and read: (set-aside entries, counts by kind,
    the databases to write as by_database.Checked with their plans). The groups are placed with the real table counts
    of the whole choice, as Create placed them before. A database whose plan has a line that can't be taken is set
    aside whole, with why. A plan that fails as a whole (the job's settings) stops the preparation."""
    selection = source.selection
    unreadable = _failed_reads(job) if kind == "discovery" else []
    counts = {k: sum(1 for e in unreadable if e["kind"] == k) for k in preparing.KINDS}
    record = preparing.record_of(sch.options_of(job)) or {}
    skip = {_unit_key(e["server"], e["database"]) for e in unreadable}
    skip |= {_unit_key(e["server"], e["database"]) for e in (record.get("left_out") or {}).values()}
    written = _written_databases(sid)
    servers = db.query(q.selected_servers_sql(kind), (param, *rest))
    groups_of = by_database.groups_by_server(servers, source.sizing)
    sizes = db.query(q.selected_databases_sql(kind), (param, *rest))
    group_of = by_database.group_of_placement(job["name"], placement.server_placements(
        ((r["server"], r["src_database"], r["tables"]) for r in sizes), groups_of))
    own = set(_job_groups(sid))
    taken = {r["task_collection"] for r in db.query(q.collections_sql())}
    columns = _source_columns(kind, param)
    keep = by_database.keeps_plans(sum(int(s["tables"]) for s in servers))
    skip |= written
    units = [u for u in _units_of(kind, param, selection) if _unit_key(u.server, u.database) not in skip]
    entries, checked = list(unreadable), []
    for read_batch in by_database.read_batches(units):
        _prepare_alive(sid, held)
        for unit, rows in _rows_of_units(kind, param, read_batch):
            if not rows:
                continue
            plan = by_database.plan_database(job["name"], source.settings, rows, columns, groups_of, taken, own,
                                             group_of=group_of, new_job=True, unity_catalog=_unity_catalog(job))
            if plan["errors"]:
                raise PrepareRefused(HTTPException(422, {"errors": plan["errors"][:20]}))
            invalid = [line for line in plan["lines"] if line["status"] == "invalid"]
            if invalid:
                entries.append(preparing.entry(unit.server, unit.database, preparing.CLASH,
                                               f"{invalid[0]['table']}: {'; '.join(invalid[0]['errors'])}"))
                continue
            checked.append(by_database.Checked(unit, len(rows), plan if keep else None))
    return entries, counts, (checked, keep, lambda units: [
        (unit, by_database.plan_database(job["name"], source.settings, rows, columns, groups_of, taken, own,
                                         group_of=group_of, new_job=True, unity_catalog=_unity_catalog(job)))
        for unit, rows in _rows_of_units(kind, param, units) if rows])


def _prepare_database_lines(sid: int, held: threading.Event, selected: dict) -> Optional[dict]:
    """Reading a file's database-only lines: one discovery run reads them by name through the connection, as Create's
    background half did. The run is kept on the record, so a resumed preparation waits for the same run rather than
    reading them again."""
    lines = selected["database_lines"]
    if not lines:
        return None
    names = {}
    for line in lines:
        names.setdefault(line["database"].lower(), line)
    key = (lines[0]["connection"].lower(), tuple(sorted(names)))
    stored = (preparing.record_of(sch.options_of(_load_schedule(sid)[0])) or {}).get("database_read")
    if stored and _database_read(key) is None:
        with _database_reads_lock:
            _database_reads[key] = {"run_id": stored["run_id"], "discovery_id": stored["discovery_id"], "ended": None,
                                    "started": time.monotonic()}
    _move_prepare(sid, stage=preparing.READING)
    while True:
        _prepare_alive(sid, held)
        resolved = _resolve_databases(selected, wait=False, request=None)
        read = _database_read(key)
        if read is not None and not stored:
            stored = {"run_id": read["run_id"], "discovery_id": read["discovery_id"]}
            _update_prepare(sid, lambda r, j: (dict(r, database_read=stored), None))
        if not resolved["discovering"]:
            return resolved
        time.sleep(PREPARE_POLL_S)


def _prepare_file(sid: int, held: threading.Event, job: dict, source: PreparedSource):
    """An upload with database-only lines: those are read first (_prepare_database_lines), then the file is planned as
    Create planned it and split by database, each written in a transaction of its own (set aside whole when it can't be
    taken). A database-only line whose database couldn't be read is set aside, with why."""
    selected = _selected(source.selection)
    resolved = _prepare_database_lines(sid, held, selected)
    selected = _with_databases(selected, resolved)
    connection = (selected["database_lines"][0]["connection"] if selected["database_lines"] else "")
    record = preparing.record_of(sch.options_of(job)) or {}
    left_out = {_unit_key(e["server"], e["database"]) for e in (record.get("left_out") or {}).values()}
    entries = [preparing.entry(connection, u["database"], preparing.UNREADABLE, u["error"])
               for u in (resolved or {}).get("unreadable") or [] if _unit_key(connection, u["database"]) not in left_out]
    counts = {k: sum(1 for e in entries if e["kind"] == k) for k in preparing.KINDS}
    skip = left_out | {_unit_key(e["server"], e["database"]) for e in entries} | _written_databases(sid)
    own = set(_job_groups(sid))
    taken = {r["task_collection"] for r in db.query(q.collections_sql())}
    plan = _file_lines(nj.plan(job["name"], selected["text"], source.settings, source.sizing, taken - own,
                               require_target=True, new_job=True), selected)
    if plan["errors"]:
        raise PrepareRefused(HTTPException(422, {"errors": plan["errors"][:20]}))
    by_unit: Dict[tuple, list] = {}
    for line in plan["lines"]:
        by_unit.setdefault((line["server"], str(line["row"].get("src_database") or "")), []).append(line)
    checked = []
    for (server, database), lines in by_unit.items():
        if _unit_key(server, database) in skip:
            continue
        invalid = [line for line in lines if line["status"] == "invalid"]
        if invalid:
            entries.append(preparing.entry(server, database, preparing.CLASH,
                                           f"{invalid[0]['table']}: {'; '.join(invalid[0]['errors'])}"))
            continue
        groups = sorted({line["row"]["task_collection"] for line in lines if line["status"] == "valid"})
        checked.append(by_database.Checked(by_database.Unit(server, database, True), len(lines),
                                           {"groups": groups, "lines": lines}))
    return entries, counts, (checked, True, None)


def _prepare_write(sid: int, task_id: int, owner: str, held: threading.Event, job: dict, source: PreparedSource,
                   planned) -> List[dict]:
    """Writing: the planned databases packed into transactions (by_database.transactions_of), each written by
    _write_chunk with the job's maker as stored and its write-time checks run again in it (_prepare_checks, N1, N4 and
    N6), behind _prepare_fence. A transaction one database refuses (a target another table already writes, two of the
    job's tables writing one target, a sink both dedicated and shared, a database another job or a deleted job holds)
    is written again one database at a time, and the database that refuses is set aside with why; the rest are
    written. Returns the databases set aside."""
    checked, keep, replan = planned
    if not checked:
        return []
    stages = by_database.Stages()
    stages.begin_write()
    groups = sorted(set(_job_groups(sid)) | {g for c in checked if c.plan for g in c.plan["groups"]})
    offsets = sch.offsets(groups, int(job["cadence_minutes"]), bool(job.get("stagger")), sch.start_minute(job))
    total = sum(c.tables for c in checked)
    state = {"owner": owner, "stage": "writing", "databases_total": len(checked), "databases_written": 0,
             "tables_written": 0, "seconds": stages.seconds()}

    def check(cur, rows):
        try:
            _prepare_checks(sid, rows, cur)
        except HTTPException as e:
            raise PrepareRefused(e)

    chunk_job = ChunkJob(sid=sid, name=job["name"], user=source.maker, batch=uuid.uuid4().hex[:12],
                         cadence_minutes=int(job["cadence_minutes"]), timezone=job["timezone"], offsets=offsets,
                         task_id=task_id, stages=stages, connection=source.connection, check=check)

    def write(pairs) -> List[dict]:
        before = dict(state)
        try:
            with _write() as cur:
                _prepare_fence(cur, sid, task_id, owner)
                _write_chunk(cur, [plan for _, plan in pairs], chunk_job, state)
                _move_prepare(sid, progress={"tables_written": state["tables_written"], "tables_total": total,
                                             "percent": preparing.percent(state["tables_written"], total)}, cur=cur)
            return []
        except (PrepareStopped, PrepareRefused):
            raise
        except HTTPException as e:
            state.clear()
            state.update(before)
            if _refused_while_running(e):
                raise
            if len(pairs) > 1:
                return [found for pair in pairs for found in write([pair])]
            unit = pairs[0][0]
            return [preparing.entry(unit.server, unit.database, preparing.CLASH, _error_text(e))]
        except BaseException:
            state.clear()
            state.update(before)
            raise

    entries = []
    for chunk in by_database.transactions_of(checked):
        _prepare_alive(sid, held)
        pairs = [(c.unit, c.plan) for c in chunk] if keep else replan([c.unit for c in chunk])
        if pairs:
            entries += write(pairs)
    return entries


def _stop_prepare(sid: int, user: str, why: str, task_ended: bool = True) -> int:
    """Stop a job's preparation: its record stopped (why, by whom), its prepare task ended (task_ended: the caller is not
    the task itself), and the reads of its claimed drafts still to go discarded, so the reader takes none, all in one
    transaction under the job's schedule lock (a write of the task in progress commits first). The statements going
    are cancelled after, as discarding a draft does. Returns how many were cancelled."""
    job = _load_schedule(sid)[0]
    ids = _job_discovery_ids(job)
    with _write() as cur:
        _update_prepare(int(sid), lambda record, job_now: (preparing.stopped(record, user, why), None), cur)
        if task_ended:
            cur.execute(q.finish_prepare_sql(), ("failed", why, int(sid), None, None))
        going = [r["statement_id"] for r in (db.fetch(cur, q.prepare_stop_reads_sql(), (ids, str(int(sid))))
                                             if ids else []) if r.get("statement_id")]
    for statement_id in going:
        try:
            warehouse.cancel(statement_id)
        except Exception as e:     # it may have ended meanwhile; the read is discarded either way
            logging.getLogger(__name__).info("could not cancel statement %s of job %s: %s", statement_id, sid,
                                             str(e)[:200])
    return len(going)


def _refuse_prepare_action(job: dict, request: Request) -> dict:
    """Stop preparing and Prepare again change the job: Edit's check, read now; only for a job Create recorded that
    isn't Ready. Returns its record."""
    _refuse_job(job, request, "change")
    record = preparing.record_of(sch.options_of(job))
    if record is None:
        raise HTTPException(409, NOT_RECORDED)
    if preparing.is_ready(sch.options_of(job)):
        raise HTTPException(409, NOTHING_SET_ASIDE)
    return record


@app.post("/api/ingestion-jobs/{job_id}/prepare/stop")
def stop_preparing(job_id: int, request: Request):
    """Stop preparing: the reads stop (the statements going cancelled) and the prepare task ends, writing nothing more;
    the job stays paused with its choice and what was read, until Prepare again."""
    job = _load_schedule(job_id)[0]
    if job.get("deleted_at") is not None:
        raise HTTPException(409, DELETED_JOB)
    _refuse_prepare_action(job, request)
    user = _user(request)
    cancelled = _stop_prepare(int(job_id), user, STOPPED_WHY.format(user=user))
    with _write() as cur:
        _log(cur, user, "prepare_stop", None, None, job["name"], None, None)
        _touch(cur, user, [int(job_id)])
    return {"id": int(job_id), "cancelled": cancelled, **preparing.summary(sch.options_of(_load_schedule(job_id)[0]))}


@app.post("/api/ingestion-jobs/{job_id}/prepare/again")
def prepare_again(job_id: int, request: Request):
    """Prepare again: the job's preparation goes on from where it stopped (Stop preparing, a check that refused it, or a
    failure): the reads its Stop left are read, and nothing read or written already is again. Held to the job's maker,
    as before."""
    job = _load_schedule(job_id)[0]
    if job.get("deleted_at") is not None:
        raise HTTPException(409, DELETED_JOB)
    _refuse_prepare_action(job, request)
    user = _user(request)
    ids = _job_discovery_ids(job)
    with _write() as cur:
        record, _ = _update_prepare(int(job_id), lambda record, job_now: (preparing.restarted(record), None), cur)
        if ids:
            db.fetch(cur, q.prepare_resume_reads_sql(), (ids, str(int(job_id))))
        _log(cur, user, "prepare_again", None, None, job["name"], None, None)
        _touch(cur, user, [int(job_id)])
    task_id = _start_prepare(int(job_id)) if preparing.has_work(record) else None
    return {"id": int(job_id), "started": task_id is not None, **preparing.summary({preparing.OPTION: record})}


@app.get("/api/ingestion-jobs/{job_id}/prepare/set-aside")
def job_set_aside(job_id: int, kind: str = "reads", offset: int = 0, limit: int = 100):
    """Every set-aside database of a job, a page at a time, read only: the record keeps their counts, every clash and
    only the first LISTED of the rest. kind reads: the databases of its claimed drafts that couldn't be read, from
    their rows (each unreadable, or closed: a whole-server read couldn't open it); kind clash: the clashes it keeps."""
    job = _load_schedule(job_id)[0]
    record = preparing.record_of(sch.options_of(job)) or {}
    limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
    if kind == "clash" or (record.get("source") or {}).get("kind") != "discovery":
        entries = sorted((e for e in (record.get("set_aside") or {}).values()
                          if kind != "clash" or e.get("kind") == preparing.CLASH),
                         key=lambda e: (str(e.get("server") or "").lower(), str(e.get("database") or "").lower()))
        return {"id": int(job_id), "kind": kind, "offset": offset, "limit": limit, "total": len(entries),
                "databases": entries[offset:offset + limit]}
    if kind != "reads":
        raise HTTPException(422, "kind is reads or clash")
    ids = _job_discovery_ids(job)
    rows = db.query(q.job_failed_reads_sql(), (ids, str(int(job_id)), limit, offset)) if ids else []
    return {"id": int(job_id), "kind": kind, "offset": offset, "limit": limit,
            "total": int(rows[0]["total"]) if rows else 0,
            "databases": [{"server": r["server"], "database": r["src_database"],
                           "kind": preparing.CLOSED if r["whole_server"] and warehouse_reads.no_access(r.get("error"))
                           else preparing.UNREADABLE, "why": warehouse_reads.first_line(r.get("error"))} for r in rows]}


@app.post("/api/schedules/{sid}")
def update_schedule(sid: int, body: ScheduleBody, request: Request):
    """Edit a schedule: fields, and members added or removed. Offsets are recomputed; jobs are synced. A change to its
    own cluster starts the copies job, which makes, updates or deletes its copy of the ingestion job."""
    import psycopg
    sent_options = "options" in body.model_fields_set   # read first: assigning body.options marks it as sent
    body.options = _requested_options(body)
    s, members = _load_schedule(sid)
    # A job that isn't Ready keeps the groups its prepare task makes, whatever the request names; its name, schedule,
    # e-mails and advanced settings change as for any job, and its schedule turns on only once it is Ready.
    prepared = preparing.is_ready(sch.options_of(s))
    if not prepared:
        body.members = sorted({m["task_collection"] for m in members if m["removed_at"] is None})
    _validate_schedule(body, members_kept=not prepared)
    _refuse_unknown_node_types(sch.own_cluster(body.model_dump()))
    user = _user(request)
    if s["deleted_at"] is not None:
        raise HTTPException(409, "schedule was deleted")
    _refuse_job(s, request, "change")
    if not body.paused:
        _refuse_unready(s, preparing.turn_on_refusal)
    if (body.job_variant or "default") != (s["job_variant"] or "default") and not body.paused:
        _refuse(_permission_refusals({"job_variant": body.job_variant}, user)["run"])
    body.stagger = body.stagger and body.job_variant != sch.CHECK_VARIANT
    new = body.model_dump()
    job_connection = _job_connection_row(sid)
    if job_connection is not None:
        new["parameters"] = {**{k: v for k, v in (new.get("parameters") or {}).items() if k not in JDBC_PARAMETERS},
                             **jc.job_parameters(job_connection)}
    _refuse_login_scope(new.get("parameters"), job_connection)
    if job_connection is None:
        _refuse_scope_parameter(new.get("parameters"), user, _parameters_of(s))
    if not prepared:
        changed_scope = preparing.scope_changed(_parameters_of(s), new.get("parameters"))
        _refuse_unready(s, lambda options: preparing.scope_refusal(options, changed_scope))
    new["notify_emails"] = new["notify_emails"] or None
    new["job_variant"] = new["job_variant"] or "default"
    stored = sch.options_of(s)
    # A body without options (an older page) keeps the stored ones; an explicit {} clears them.
    new["options"] = (new["options"] or None) if sent_options else (stored or None)
    kept = {k: stored[k] for k in MANAGED_OPTIONS if k in stored}
    if kept:
        new["options"] = {**(new["options"] or {}), **kept}
    as_json = lambda v: json.loads(v) if isinstance(v, str) else (v or {})
    cluster_changed = sch.own_cluster(new) != sch.own_cluster(s)
    changed = [f for f in q.SCHEDULE_FIELDS if (s[f] or None) != (new[f] or None) and not
               (f in q.JSON_SCHEDULE_FIELDS and as_json(s[f]) == as_json(new[f]))]
    active = {m["task_collection"]: m for m in members if m["removed_at"] is None}
    added = sorted(set(body.members) - set(active))
    gone = sorted(set(active) - set(body.members))
    with_tables = sorted(set(gone) & {r["task_collection"] for r in db.query(q.collections_sql())})
    if with_tables:
        raise HTTPException(422, f"every ingestion group belongs to an ingestion job: {', '.join(with_tables)} still "
                                 "has tables, so it can't leave this job")
    offs = sch.offsets(body.members, body.cadence_minutes, body.stagger, sch.start_minute(new))
    batch = uuid.uuid4().hex[:12]
    try:
        with _write() as cur:
            # Serialized with job-wide edits (_lock_job) and syncs, which read the members under this lock.
            cur.execute(q.schedule_lock_sql(), (q.SCHEDULE_LOCK_CLASS, q.schedule_lock_key(sid)))
            if "options" in changed:
                # The app's own options as they are now, under the lock: a prepare task or a re-discovery may have
                # moved its record on since the request read them.
                fresh = sch.options_of(_load_schedule(sid, cur)[0])
                new["options"] = {**{k: v for k, v in (new["options"] or {}).items() if k not in MANAGED_OPTIONS},
                                  **{k: fresh[k] for k in MANAGED_OPTIONS if k in fresh}} or None
            if changed:
                vals = [(json.dumps(new[f]) if new[f] is not None else None) if f in q.JSON_SCHEDULE_FIELDS else new[f]
                        for f in changed]
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
            if changed or gone or added:
                _touch(cur, user, [sid])
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, f"a schedule named '{body.name}' already exists")
    synced = _sync(sid, user)
    cluster = _apply_cluster(sid) if cluster_changed else None
    return {"id": sid, "changed": changed, "added": added, "removed": gone, "sync": synced, "cluster_error": cluster,
            "cluster_started": cluster_changed and cluster is None}


def _set_paused(sid: int, paused: bool, request: Request):
    user = _user(request)
    s, _ = _load_schedule(sid)
    if s["deleted_at"] is not None:
        raise HTTPException(409, "schedule was deleted")
    _refuse_job(s, request, "change")
    if not paused:
        _refuse_unready(s, preparing.turn_on_refusal)
    elif not preparing.is_ready(sch.options_of(s)):
        return _keep_paused_when_ready(s, user)
    if bool(s["paused"]) != paused:
        with _write() as cur:
            cur.execute(q.update_schedule_sql(["paused"]), (paused, int(sid)))
            _log(cur, user, "schedule_pause" if paused else "schedule_resume", None, None, f"{s['name']}.paused",
                 s["paused"], paused)
            _touch(cur, user, [sid])
    return {"id": sid, "paused": paused, "sync": _sync(sid, user)}


@app.post("/api/schedules/{sid}/pause")
def pause_schedule(sid: int, request: Request):
    return _set_paused(sid, True, request)


@app.post("/api/schedules/{sid}/resume")
def resume_schedule(sid: int, request: Request):
    return _set_paused(sid, False, request)


class DeleteJobBody(BaseModel):
    drop_targets: bool = False


@app.post("/api/schedules/{sid}/delete")
def delete_schedule(sid: int, request: Request, body: Optional[DeleteJobBody] = None):
    """Delete an ingestion job softly: it is marked deleted and kept for the retention period, so Restore can bring it
    back. Its wrapper jobs are deleted and its members removed. Its groups' tables and state stay until the purge
    (Delete
    now, or the delete job's daily run once the retention has run out), which deletes them as the deployer: the app has
    no DELETE privilege. ``drop_targets`` says the purge also drops the target tables only its groups write.
    Refused while
    a run of the job is going."""
    user = _user(request)
    s, members = _load_schedule(sid)
    _refuse_job(s, request, "delete")
    if s.get("deleted_at") is not None:
        raise HTTPException(409, "ingestion job is already deleted: Restore it, or Delete now to purge it")
    active = [m for m in members if m["removed_at"] is None]
    plan = _delete_plan(sid)
    blocker = _delete_blocker(s, active, plan["groups"])
    if blocker:
        raise HTTPException(409, blocker)
    record = preparing.record_of(sch.options_of(s))
    if record and not preparing.is_ready(sch.options_of(s)) and not record.get("stopped"):
        # Its preparation stops first, as Stop preparing stops it; a write of it in progress commits first.
        _stop_prepare(int(sid), user, DELETED_WHY.format(user=user))
        s, members = _load_schedule(sid)
        active = [m for m in members if m["removed_at"] is None]
        plan = _delete_plan(sid)
    if plan["tables"] and not jobs.DELETE_JOB_ID:
        raise HTTPException(409, NO_DELETE_JOB)
    drop = bool(body and body.drop_targets)
    with _write() as cur:
        cur.execute(q.schedule_lock_sql(), (q.SCHEDULE_LOCK_CLASS, q.schedule_lock_key(sid)))
        cur.execute(q.delete_schedule_sql(), (int(sid),))
        if active:
            # Their wrapper jobs go in the sync below.
            cur.execute(q.remove_members_sql(), ([m["id"] for m in active],))
        _record_delete(cur, sid, drop)
        _log(cur, user, "schedule_delete", None, None, s["name"], None, "drop_targets" if drop else None)
    synced = _sync(sid, user)
    if sch.options_of(s).get(sch.COPY_OPTION):     # its copy of the ingestion job goes too
        _apply_cluster(sid)
    return {"id": sid, "deleted": True, "retention_days": jobs.DELETED_JOB_RETENTION_DAYS, "sync": synced,
            "delete_run": None}


NO_DELETE_JOB = ("no delete job is configured for this app, so the job's groups and their tables would stay behind "
                 "outside any job: deploy the engine bundle, then the app, and delete again")
NO_TABLE_REMOVE_JOB = ("no delete job is configured for this app, so a table can't be removed from its job: deploy the "
                       "engine bundle, then the app, and remove it again")
# Kept deleted jobs the Ingestion jobs page lists, a page at a time: the default page and the largest one.
DELETIONS_LISTED = 10
DELETIONS_PAGE_MAX = 50


def _delete_plan(sid: int) -> dict:
    """What a job's Delete removes, read fresh: the groups no other live job holds, their tables (control rows), the
    target tables only they write, and those another group's rows also write (kept, the first DELETE_KEPT_LISTED
    named)."""
    groups = [r["task_collection"] for r in db.query(q.delete_groups_sql(), (int(sid), int(sid)))]
    if not groups:
        return {"groups": [], "tables": 0, "targets": 0, "kept": 0, "kept_names": []}
    row = db.query(q.delete_targets_sql(), (groups, groups, groups))[0]
    names = row.get("kept_names") or []
    return {"groups": groups, "tables": int(row.get("tables") or 0), "targets": int(row.get("targets") or 0),
            "kept": int(row.get("kept") or 0), "kept_names": json.loads(names) if isinstance(names, str) else list(names)}


def _delete_blocker(job: dict, active: List[dict], groups: List[str]):
    """Why the job can't be deleted right now: a run of it, or of a group it deletes, is going (Lakebase sees a run
    once its cluster is up, the Jobs API from its start), a background write to it is still running (its heartbeat is
    fresh), or its delete job is still running."""
    if any(r["task_collection"] in {m["task_collection"] for m in active} | set(groups) for r in _running()):
        return "a run of this job is going: wait for it to end, or stop it, then delete the job"
    if db.query(q.running_task_sql(), (int(job["id"]), TASK_STALE_S)):
        return "a write to this job is still running (adding or creating tables): wait for it to finish, then delete the job"
    going = _active_wrapper_runs(active)
    if going:
        return {"message": f"a run of this job is going, started {going[0].get('started') or 'just now'}: wait for it "
                           "to end, or stop it, then delete the job",
                "run_url": going[0].get("url"), "run_id": going[0].get("run_id")}
    run = _delete_run(job)
    if run and ij.is_active(run):
        return {"message": "this job's delete is still running", "run_url": run.get("url"), "run_id": run.get("run_id")}
    return None


def _record_delete(cur, sid: int, drop: bool) -> None:
    """Record that the job's Delete runs the delete job, and with which flag. The earlier run, its start error and its
    finished mark are forgotten, so the job shows Deleting until this run's state is known."""
    cur.execute(q.set_schedule_flag_sql(), (["delete_drop_targets"], bool(drop), int(sid)))
    for key in ("delete_run_id", "delete_start_error", "delete_finished"):
        cur.execute(q.clear_schedule_option_sql(), (key, int(sid)))


def _start_delete_job(sid: int, drop: bool) -> dict:
    """Start the delete job for a deleted job and record its run. A start that fails is reported, not raised: the job
    is deleted already, and Run again on Ingestion jobs starts it later."""
    try:
        run_id = jobs.start_delete_job(sid, drop)
    except Exception as e:
        error = f"the delete job didn't start: {str(e).split('. ')[0]}"
        with _write() as cur:
            cur.execute(q.set_schedule_text_option_sql(), (["delete_start_error"], error, int(sid)))
        return {"run_id": None, "url": None, "error": error}
    with _write() as cur:
        cur.execute(q.set_schedule_option_sql(), (["delete_run_id"], run_id, int(sid)))
    host = jobs.workspace_url()
    return {"run_id": run_id, "url": f"{host}/jobs/{jobs.DELETE_JOB_ID}/runs/{run_id}" if host else None, "error": None}


def _delete_run(job: dict) -> Optional[dict]:
    """The job's latest delete job run, as the Jobs API has it, or None (not started, or unreadable: error set)."""
    run_id = sch.options_of(job).get("delete_run_id")
    if not run_id:
        return None
    try:
        return run_poller.run_state_of(run_id)
    except Exception as e:
        return {"run_id": run_id, "state": None, "result": None, "url": None,
                "message": f"can't read the delete job's run: {str(e).split('. ')[0]}"}


PURGED = ("ingestion job was purged: its state and tables are gone, so it can't be restored; create it again")


@app.post("/api/schedules/{sid}/purge")
def purge_schedule(sid: int, request: Request):
    """Delete now: the delete job purges a deleted job at once, not waiting for its retention to run out. Its groups'
    tables and state go for good, and its target tables too when its Delete asked for them."""
    s, _ = _load_schedule(sid)
    _refuse_job(s, request, "delete")
    if s.get("deleted_at") is None:
        raise HTTPException(409, "ingestion job is not deleted: delete it first")
    if sch.options_of(s).get("delete_finished"):
        raise HTTPException(409, PURGED)
    if not jobs.DELETE_JOB_ID:
        raise HTTPException(409, NO_DELETE_JOB)
    blocker = _delete_blocker(s, [], _delete_plan(sid)["groups"])
    if blocker:
        raise HTTPException(409, blocker)
    with _write() as cur:
        cur.execute(q.clear_schedule_option_sql(), ("delete_start_error", int(sid)))
    return {"id": sid, "delete_run": _start_delete_job(sid, bool(sch.options_of(s).get("delete_drop_targets")))}


@app.post("/api/schedules/{sid}/restore")
def restore_schedule(sid: int, request: Request):
    """Restore a deleted job that its purge has not reached. It is live again, paused (nothing runs until it is
    resumed),
    with the members its Delete removed; members removed before the delete stay removed, and its groups' tables and
    state are as they were. Refused when its name is taken, one of its groups is now in another live job, a live job
    shares a database or target table with it, or its purge is going or has finished."""
    user = _user(request)
    s, _ = _load_schedule(sid)
    _refuse_job(s, request, "delete")
    if s.get("deleted_at") is None:
        raise HTTPException(409, "ingestion job is not deleted")
    with _write() as cur:
        cur.execute(q.schedule_lock_sql(), (q.SCHEDULE_LOCK_CLASS, q.schedule_lock_key(sid)))
        s, members = _load_schedule(sid, cur)
        if s.get("deleted_at") is None:
            raise HTTPException(409, "ingestion job is not deleted: it was restored already")
        if sch.options_of(s).get("delete_finished"):
            raise HTTPException(409, PURGED)
        restored = sorted({m["task_collection"] for m in members
                           if m["removed_at"] is not None and m["removed_at"] == s["deleted_at"]})
        blocker = _delete_blocker(s, [], restored)
        if blocker:
            raise HTTPException(409, blocker)
        _restore_refusals(cur, s, restored)
        cur.execute(q.restore_members_sql(), (int(sid), int(sid)))
        cur.execute(q.restore_schedule_sql(), (int(sid),))
        if cur.rowcount != 1:
            raise HTTPException(409, PURGED)
        _log(cur, user, "schedule_restore", None, None, s["name"], None, None)
        _touch(cur, user, [sid])
    synced = _sync(sid, user)
    cluster = _apply_cluster(sid) if sch.own_cluster(s) else None
    return {"id": sid, "restored": True, "sync": synced, "cluster_error": cluster}


def _restore_refusals(cur, job: dict, groups: List[str]) -> None:
    """Inside a Restore's transaction: refuse when a live job has the deleted job's name, holds one of its groups now,
    or
    has a database or a target table in common with them. Each message says what to change first."""
    cur.execute(q.live_name_taken_sql(), (job["name"], int(job["id"])))
    if cur.fetchall():
        raise HTTPException(409, f"another ingestion job is already named {job['name']}: rename or delete it, then "
                                 "restore this one")
    if not groups:
        return
    cur.execute(q.groups_held_elsewhere_sql(), (groups, int(job["id"])))
    held = cur.fetchall()
    if held:
        group, holder = held[0]
        raise HTTPException(409, f"ingestion group {group} is now in the ingestion job {holder}: take it out "
                                 f"of that job, "
                                 "then restore this one")
    cur.execute(q.restore_conflicts_sql(), (groups, groups))
    conflicts = cur.fetchall()
    if conflicts:
        kind, item, holder = conflicts[0]
        what = f"database {item}" if kind == "database" else f"target table {item}"
        raise HTTPException(409, f"{what} is also read or written by the live ingestion job {holder}: move one "
                                 f"of the two "
                                 "jobs apart, then restore this one")


def _delete_refusal(job: dict, groups: List[str], user: str) -> Optional[str]:
    """Why a deleted job can't be restored or purged now, for its Restore and Delete now buttons: the user may not
    delete
    it, or its runs or its purge are going."""
    refusal = _permission_refusals(job, user)["delete"]
    if refusal:
        return refusal
    blocker = _delete_blocker(job, [], groups)
    return blocker.get("message") if isinstance(blocker, dict) else blocker


@app.post("/api/schedules/{sid}/sync")
def sync_schedule(sid: int, request: Request):
    """Re-sync the wrapper jobs (recreates missing ones and undoes edits made outside the app)."""
    s = _load_schedule(sid)[0]
    # A deleted job keeps its sync: it is how a wrapper job whose delete failed is deleted again, so only the
    # change permission is checked here, not the deleted-job refusal.
    _refuse(_job_permission_refusals(s, _user(request))["change"])
    cluster = _apply_cluster(sid) if sch.own_cluster(s) else None     # copies the ingestion job again
    return {"id": sid, "sync": _sync(sid, _user(request)), "cluster_error": cluster}


@app.get("/api/schedules/{sid}/check")
def check_schedule(sid: int):
    """Drift report for the schedule's wrapper jobs; changes nothing."""
    s, members = _load_schedule(sid)
    active = [m for m in members if m["removed_at"] is None]
    target = _wrapper_target(s)
    if target is None:
        raise HTTPException(503, f"no Databricks ingestion job configured for variant '{s['job_variant']}'")
    return sch.check(s, active, jobs.schedule_client(), target, _owner())


def _timeline_run(job_run: dict, now: datetime) -> dict:
    """One job run as a bar on the Timeline: from its first group run's start to its last one's finish (to now while
    running; a trigger that did not run has no span), its outcome, and each group with its group run, for the hover."""
    members = job_run["placed"]["members"]
    runs = [m["run"] for m in members if m.get("run")]
    finishes = [q._when(r["finished_at"]) for r in runs if r.get("finished_at")]
    running = job_run["outcome"]["label"] == "Running"
    ended = now if running else (max(finishes) if finishes and job_run["started_at"] else None)
    return {**{k: job_run.get(k) for k in ("slot", "started_at", "outcome", "ran", "members", "failed_tables", "took_s")},
            "ended_at": ended.isoformat() if ended else None,
            "groups": [{"group": m["task_collection"], "status": (m.get("run") or {}).get("status"),
                        "run_id": (m.get("run") or {}).get("run_id")} for m in members]}


@app.get("/api/timeline")
def timeline(hours: int = 24):
    """The last `hours` as the Jobs UI's run timeline, one lane per ingestion job: its job runs (each with its span,
    outcome and groups; a paused job's triggers that did not run left out), its planned triggers (the current
    schedule, since it or the group last changed) and the triggers Databricks skipped because the previous run was
    still going (from the wrapper jobs' runs). The group runs of groups in no job make one more lane. Deleted jobs, and
    the groups only they hold, have no lane."""
    hours = max(1, min(int(hours), 168))
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    schedules, members = _schedules()
    active = _active(members)
    client = None
    lanes = []
    for s in sorted((s for s in schedules if s.get("deleted_at") is None), key=lambda s: str(s["name"]).lower()):
        planned, skipped = set(), []
        for m in (m for m in active if m["schedule_id"] == s["id"]):
            if not s["paused"]:
                since = max([start] + [q._when(v) for v in (m.get("added_at"), s.get("updated_at")) if v])
                planned |= {t.isoformat() for t in sch.fire_times(int(s["cadence_minutes"]), int(m["offset_minutes"]),
                                                                   s["timezone"], since, end)}
            if m.get("job_id"):
                client = client or jobs.schedule_client()
                skipped += [jobs.ts(t) for t in (_skipped_starts(m["job_id"], start, end, client) or [])]
        lanes.append({"job_id": s["id"], "name": s["name"], "paused": bool(s["paused"]),
                      "description": sch.describe(int(s["cadence_minutes"]), sch.start_minute(s), s["timezone"] or "UTC"),
                      # A paused job's triggers that did not run are left out, as Runs folds them away.
                      "runs": [_timeline_run(jr, end) for jr in _job_runs_of(s, start, end, end)
                               if not (jr["outcome"]["label"] == "Not run" and jr["outcome"].get("paused"))],
                      "planned": sorted(planned), "skipped": sorted(skipped)})
    unscheduled = sorted({st["task_collection"] for st in _live_states()} - {m["task_collection"] for m in active})
    group_runs = [{k: r.get(k) for k in ("run_id", "task_collection", "status", "started_at", "finished_at")}
                  for r in _group_runs(unscheduled, start, end)] if unscheduled else []
    return {"start": start.isoformat(), "end": end.isoformat(), "jobs": lanes,
            "not_in_job": {"groups": unscheduled, "group_runs": group_runs} if group_runs else None}


# ---- UC connections (bulk import) ----------------------------------------------------------------

DISCOVERABLE_CONNECTION_TYPES = {"SQLSERVER"}
# Connection types lakefed can read: its source adapters (queries.SRC_TYPES). HTTP and other connections are left out.
READABLE_CONNECTION_TYPES = {t.upper() for t in q.SRC_TYPES}


def _connections() -> List[dict]:
    """The database connections of a type lakefed can read that this app's identity can see, with host and port
    from their options."""
    out = []
    for c in db.workspace().connections.list():
        options = c.options or {}
        kind = getattr(c.connection_type, "value", c.connection_type)
        if str(kind or "").upper() not in READABLE_CONNECTION_TYPES:
            continue
        out.append({"name": c.name, "connection_type": kind, "host": options.get("host"), "port": options.get("port"),
                    "owner": c.owner, "comment": c.comment, "discoverable": kind in DISCOVERABLE_CONNECTION_TYPES})
    return sorted(out, key=lambda c: c["name"])


@app.post("/api/jdbc-logins")
def write_jdbc_login(body: jl.JdbcLoginRequest, request: Request):
    """Write a JDBC login into the dedicated secret scope as the app's own service principal, and record who entered
    it. Writing a key again replaces its login, recorded as 'rotated'; who may is _login_write_refusal's. The password
    is never stored, logged or returned."""
    scope = jl.dedicated_scope()
    if not scope:
        raise HTTPException(503, f"no dedicated login scope is set ({jl.SCOPE_ENV}): deploy the bundle")
    password = body.password.get_secret_value()
    entered_by = _user(request)
    _refuse(_signed_in_refusal(entered_by))
    key = body.resolved_secret_key()
    seen = _newest_login(scope, key)
    _refuse(_login_write_refusal(scope, key, entered_by, body.connection, seen))
    value = jl.secret_value(body.user_name, body.password)
    entry_id = uuid.uuid4().hex
    put = False
    # One transaction under the key's lock: the record first, then the secret, then the commit. A refused write rolls
    # the record back, and two saves of one key take turns, so the newest record always names the login the key holds.
    try:
        with db.transaction() as cur:
            cur.execute(q.jdbc_login_lock_sql(), (q.jdbc_login_lock_key(scope, key),))
            now = db.fetch(cur, q.jdbc_login_latest_sql(), (scope, key))
            if (now[0]["id"] if now else None) != (seen["id"] if seen else None):
                raise HTTPException(409, f"another login was saved under {key} meanwhile: look at it, then save again")
            try:
                entry = db.fetch(cur, q.jdbc_login_insert_sql(),
                                 (entry_id, body.connection, scope, key, entered_by, scope, key))[0]
            except Exception as error:
                raise HTTPException(500, jl.redact(f"the login could not be recorded ({error}); nothing was saved",
                                                   password)) from None
            try:
                jl.put_login(scope, key, value)
            except Exception as error:
                status, message = jl.write_refusal(error, scope, key, (password, value))
                logging.getLogger(__name__).warning("jdbc login write to %s/%s refused with %s", scope, key, status)
                raise HTTPException(status, message) from None
            put = True
    except HTTPException:
        raise
    except Exception as error:
        said = (f"the login was written to {scope}/{key} but its record did not commit ({error}): save it again"
                if put else f"the login could not be saved ({error}); nothing was written")
        raise HTTPException(500, jl.redact(said, password, value)) from None
    logging.getLogger(__name__).info("jdbc login %s for %s/%s by %s", entry["outcome"], scope, key, entered_by)
    return {"id": entry_id, "connection": body.connection, "scope": scope, "secret_key": key,
            "entered_by": entered_by, "entered_at": entry["entered_at"], "outcome": entry["outcome"]}


def _parameters_of(job: dict) -> dict:
    """A job's stored parameters as a dict, whether the read gave them as JSON text or already decoded."""
    parameters = job.get("parameters") or {}
    if isinstance(parameters, str):
        try:
            parameters = json.loads(parameters)
        except ValueError:
            return {}
    return parameters if isinstance(parameters, dict) else {}


def _scope_groups(groups: Iterable[str], scope: str) -> set:
    """The groups among these whose job reads its secrets from this scope (its secret_scope parameter): control rows
    don't record their scope, so a row in another job reads the same key name from another scope. Read fresh."""
    wanted = {g for g in groups if g}
    if not wanted:
        return set()
    in_scope = [s["id"] for s in db.query(q.schedules_sql()) if _parameters_of(s).get("secret_scope") == scope]
    members = db.query(q.schedule_members_sql(), (in_scope,)) if in_scope else []
    return {m["task_collection"] for m in members if m["removed_at"] is None and m["task_collection"] in wanted}


def _newest_login(scope: str, key: str) -> Optional[dict]:
    """The newest jdbc_login_write row for one scope and key, or None when the key holds no typed login."""
    rows = db.query(q.jdbc_login_latest_sql(), (scope, key))
    return rows[0] if rows else None


def _same_user(one, other) -> bool:
    return bool(str(one or "").strip()) and str(one or "").strip().lower() == str(other or "").strip().lower()


def _login_write_refusal(scope: str, key: str, user: str, connection: str = "",
                         newest: Optional[dict] = None) -> Optional[str]:
    """Why this user may not write the login under a key of the login scope for this connection, or None.

    - A key some job reads through this scope is written by whoever may change those jobs (the edit path's check).
    - A key with no login yet is open to any signed-in user, as a new job is.
    - A key whose newest login was typed for another connection: only someone who may change that connection's jobs,
      or, while it has none, who typed that login or made that connection, may save one under another connection.
    - Otherwise, for the same connection: someone who may change its jobs, typed a login under the key before, or made
      a connection one was typed for. So a login a connection uses can't be replaced by any signed-in user before a job
      reads it."""
    groups = {r["task_collection"] for r in db.query(q.control_groups_for_secret_key_sql(), (key,))}
    reading = _scope_groups(groups, scope)
    if reading:
        return _groups_refusal(reading, user)
    newest = newest if newest is not None else _newest_login(scope, key)
    if newest is None:
        return None
    owner = newest["connection"]
    owner_groups = sorted({r["task_collection"] for r in db.query(q.jdbc_connection_groups_sql(), (owner,))})
    if owner_groups and _groups_refusal(owner_groups, user) is None:
        return None
    maker = next((row.get("created_by") for row in db.query(jc.get_sql(), (owner,))), None)
    if owner != connection:
        if not owner_groups and (_same_user(user, newest["entered_by"]) or _same_user(user, maker)):
            return None
        return (f"the newest login under the key {key} was typed for the JDBC connection {owner}: only someone who may "
                "change that connection's jobs (or, while it has none, who typed that login or made the connection) "
                "may save one under another connection")
    earlier = db.query(q.jdbc_login_writers_sql(), (scope, key))
    if any(_same_user(user, row["entered_by"]) or _same_user(user, row["created_by"]) for row in earlier):
        return None
    return (f"the key {key} already holds a login typed into the app: only someone who typed a login under it, who "
            "made a connection one was typed for, or who may change its connection's jobs may replace it")


@app.get("/api/jdbc-logins")
def last_jdbc_login(secret_key: str = Query(default="", max_length=128)):
    """The dedicated login scope, and, when a secret key is named, the newest login written for it: who entered it,
    when, and whether it replaced an earlier one. Never the password, which the app doesn't keep. The form asks for the
    scope alone, before a key is named, to know which scope its logins go to."""
    scope = jl.dedicated_scope()
    if not scope:
        raise HTTPException(503, f"no dedicated login scope is set ({jl.SCOPE_ENV}): deploy the bundle")
    if not secret_key:
        return {"scope": scope, "secret_key": "", "login": None}
    rows = db.query(q.jdbc_login_latest_sql(), (scope, secret_key))
    return {"scope": scope, "secret_key": secret_key, "login": rows[0] if rows else None}


@app.get("/api/connections")
def connections(fresh: bool = False):
    """UC connections with what lakefed tracks on each (control rows naming the connection or its host).
    A listing error (no rights on any connection, for example) is returned, not raised, so the page
    still offers discovery by host and secret. `fresh` lists them again now (the picker's Refresh, after a
    connection is made in Catalog Explorer) instead of from the shared read."""
    if fresh:
        with _shared_lock:
            _shared_reads.pop("connections", None)
    def compute():
        try:
            found = _connections()
        except Exception as e:                          # the app's identity may have no rights on connections
            return {"connections": [], "error": str(e)[:500]}
        states = _states()
        for c in found:
            c["tracked"] = rollup.tracked(states, c["name"], c["host"], c.get("port"), c.get("connection_type"))
        return {"connections": found, "error": None}
    return _shared("connections", compute)


@app.get("/api/connections/{name}/databases")
def connection_databases(name: str):
    """The databases lakefed tracks on one connection (by its name or its host)."""
    found = next((c for c in connections()["connections"] if c["name"] == name), None)
    if not found:
        raise HTTPException(404, f"connection '{name}' not found")
    return rollup.connection_databases(_states(), name, found["host"], found.get("port"), found.get("connection_type"))


# ---- single-page app ----------------------------------------------------------------------------

if (STATIC / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    # Only files inside static/: the path is decoded, so "%2e%2e/" must not climb out of it.
    target = (STATIC / path).resolve()
    if path and target.is_file() and target.is_relative_to(STATIC.resolve()):
        return FileResponse(target)
    index = STATIC / "index.html"
    if not index.is_file():
        return JSONResponse(status_code=503, content={"detail": "frontend not built (npm run build)"})
    return FileResponse(index)


# ---- re-discovery: a job's selection read again each day at its hour (rediscovery.py) ----------------------------------
# The scheduler is a thread of the app, so it runs while the app does. A job that is due is claimed in Lakebase first
# (its record becomes running), so one start runs however many ticks or app instances look; the re-discovery then runs
# on its own thread, and its tables are written by the path Add tables takes, as the change log's re-discovery user.
REDISCOVERY_TICK_S = float(os.environ.get("REDISCOVERY_TICK_S", "300"))   # 0 turns the scheduler off
REDISCOVERY_POLL_S = float(os.environ.get("REDISCOVERY_POLL_S", "30"))
REDISCOVERY_MAX_WAIT_S = float(os.environ.get("REDISCOVERY_MAX_WAIT_S", str(6 * 3600)))
# How often re-discovery asks where its warehouse reads stand: short, since the background reader saves each one as it
# ends (and, with the reader off, each step starts only as many statements as the server's cap leaves room for).
REDISCOVERY_WAREHOUSE_POLL_S = float(os.environ.get("REDISCOVERY_WAREHOUSE_POLL_S", "2"))
REDISCOVERY_USER = "re-discovery"


def _rediscovery_scheduler() -> None:
    while True:
        time.sleep(REDISCOVERY_TICK_S)
        try:
            _rediscovery_tick()
        except Exception:
            logging.getLogger(__name__).exception("re-discovery tick failed")


def _rediscovery_tick(now: Optional[datetime] = None) -> List[int]:
    """Start the re-discovery of each job that is due now, each on its own thread. Returns the jobs started."""
    now = now or datetime.now(timezone.utc)
    started = []
    for job in db.query(q.schedules_sql()):
        options = sch.options_of(job)
        if not preparing.is_ready(options) or not rediscovery.is_due(options, now, bool(job["paused"])):
            continue
        claimed = db.query(q.claim_rediscovery_sql(),
                           (json.dumps(rediscovery.running_record(now)), job["id"], rediscovery.STALE_S))
        if claimed:
            started.append(job["id"])
            _start_background(lambda sid=job["id"]: _run_rediscovery(sid, now))
    return started


@app.post("/api/ingestion-jobs/{job_id}/rediscover")
def rediscover_now(job_id: int, request: Request):
    """Re-discover a job now, the run the daily hour starts, on its own thread. It takes the same claim as the daily run,
    so a run already going (not yet stale) refuses it: the job page's Run now stays off until that run has ended."""
    s = _load_schedule(job_id)[0]
    _refuse(_job_permission_refusals(s, _user(request))["change"])
    _refuse_unready(s, preparing.tables_refusal)
    now = datetime.now(timezone.utc)
    claimed = db.query(q.claim_rediscovery_sql(),
                       (json.dumps(rediscovery.running_record(now)), int(job_id), rediscovery.STALE_S))
    if not claimed:
        raise HTTPException(409, "a re-discovery of this job is already running")
    _start_background(lambda: _run_rediscovery(int(job_id), now))
    return {"id": int(job_id), "started_at": now.isoformat()}


def _run_rediscovery(sid: int, started: datetime) -> None:
    """A job's re-discovery on its own thread. Its outcome (what was found and added, what vanished, the errors) or why
    it did not run is recorded on the job, even when it failed. A 409 because a group is running is 'deferred': the
    next re-discovery is the next day's. Each phase is recorded as it starts, so the job page can show where it is."""
    record = rediscovery.running_record(started)
    stop = threading.Event()

    def beat():
        while not stop.wait(rediscovery.BEAT_S):
            try:
                db.execute(q.beat_rediscovery_sql(), (datetime.now(timezone.utc).isoformat(), int(sid)))
            except Exception:
                pass     # a missed beat is caught up by the next; STALE_S allows for several

    def report(**fields) -> None:
        record.update(fields, heartbeat_at=datetime.now(timezone.utc).isoformat())
        db.execute(q.set_rediscovery_sql(), (json.dumps(record, default=str), int(sid)))

    threading.Thread(target=beat, daemon=True, name="lakefed-rediscovery-beat").start()
    try:
        record.update(status="done", **_rediscover(sid, report))
    except HTTPException as e:
        deferred = _refused_while_running(e)
        record.update(status="deferred" if deferred else "failed", error=_error_text(e))
    except Exception as e:
        record.update(status="failed", error=_error_text(e))
    finally:
        stop.set()
    for progress_key in ("phase", "run_id", "server"):
        record.pop(progress_key, None)
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    db.execute(q.set_rediscovery_sql(), (json.dumps(record, default=str), int(sid)))
    _forget_shared_reads()


def _rediscovery_view(job: dict, now: datetime) -> dict:
    """The job page's re-discovery: the hour, the last record and its progress. While a discovery run goes, the run
    poller's state for it decides whether that is pending or past its wait (a timeout, not done)."""
    options = sch.options_of(job)
    last = (options or {}).get(rediscovery.RECORD) or None
    outcome = None
    if last and last.get("phase") == rediscovery.DISCOVERING and last.get("run_id"):
        try:
            run = run_poller.run_state_of(last["run_id"])
        except Exception:
            run = None
        outcome = action_states.run_outcome(run, now, REDISCOVERY_MAX_WAIT_S)
    return rediscovery.view(options, now, outcome)


def _rediscover(sid: int, report=lambda **fields: None) -> dict:
    """Read a job's selection's servers again (one discovery run each), then add what the rules allow: in one
    transaction under the job's lock and its groups' run locks, through the path Add tables takes. The job's new groups
    are synced after the commit."""
    job, _ = _load_schedule(sid)
    selection = db.query(q.job_selection_sql(), (int(sid),))
    if not selection:
        return {"found": 0, "added": 0, "note": "the job was not made from a selection, so there is nothing to read again"}
    discovery_parts = [p for p in selection if p["source_kind"] == "discovery"]
    if not discovery_parts:
        return {"found": 0, "added": 0, "note": "the job was made from an uploaded CSV, which has no source to read again"}
    if not any(p["scope"] != "tables" for p in discovery_parts):
        return {"found": 0, "added": 0, "note": "the job's selection is specific tables, which add nothing"}
    ids = sorted({i for p in discovery_parts for i in str(p["source_id"] or "").split(",") if i})
    readings, errors = rediscovery.specs(discovery_parts, db.query(q.selection_discoveries_sql(), (ids,)))
    new_ids = []
    for server, spec in readings:
        discovery_id = uuid.uuid4().hex[:12]
        new_ids.append(discovery_id)
        if _through_warehouse(spec):
            report(phase=rediscovery.DISCOVERING, run_id=None, server=server)
            failed = _discover_through_warehouse(spec, discovery_id)
            if failed:
                errors.append(f"{server}: {failed}")
            continue
        try:
            spec, parameters = _rediscovery_reading(spec, job.get("created_by"))
        except HTTPException as refused:
            errors.append(f"{server}: {_error_text(refused)}")
            continue
        run_id = jobs.run_now(_require(jobs.DISCOVER_JOB_ID, "discovery"),
                              {"discovery_id": discovery_id, "spec": json.dumps(spec), **parameters})
        report(phase=rediscovery.DISCOVERING, run_id=run_id, server=server)
        ended = _wait_for_run(run_id)
        if ended.get("result") != "SUCCESS":
            errors.append(f"{server}: its discovery run ended {ended.get('result') or ended.get('state')}"
                          + (f": {ended['message']}" if ended.get("message") else ""))
    statuses = db.query(q.discovery_statuses_sql(), (new_ids,)) if new_ids else []
    found = db.query(q.source_rows_sql("discovery"), (new_ids,)) if new_ids else []
    taken = {r["task_collection"] for r in db.query(q.collections_sql())}
    report(phase=rediscovery.ADDING, run_id=None, server=None)
    user, batch = REDISCOVERY_USER, uuid.uuid4().hex[:12]

    def write() -> tuple:
        with _write() as cur:
            groups = _lock_job(cur, sid)
            job_rows = db.fetch(cur, q.groups_control_rows_sql(for_update=True), (groups,)) if groups else []
            options = sch.options_of(_load_schedule(sid, cur)[0])
            excluded = table_removal.exclusions_of(options)
            picked = rediscovery.wanted(discovery_parts, preparing.without_left_out(found, options), statuses, job_rows,
                                        excluded)
            limit = rediscovery.REPORT_LIMIT
            outcome = {"found": len(picked["add"]), "added": 0, "skipped": 0,
                       "new_databases": picked["new_databases"][:limit],
                       "vanished": {"count": len(picked["vanished"]), "tables": picked["vanished"][:limit]},
                       "errors": (errors + [f"{e['database']}: {e['error']}" for e in picked["errors"]])[:limit],
                       "discovery_ids": new_ids}
            if not picked["add"]:
                return outcome, []
            plan = nj.plan_for_job(job["name"], nj.csv_of(nj.DISCOVERY_COLUMNS, [r["fields"] for r in picked["add"]]),
                                   "ct", job_rows, None, taken, unity_catalog=_unity_catalog(job))
            if plan["errors"]:
                raise HTTPException(422, {"errors": plan["errors"]})
            written = _write_planned_tables(cur, job, plan, user, batch, skip_taken=True,
                                            reader=job.get("created_by") or "")
            if written:
                _touch(cur, user, [sid])
            outcome["added"] = len(written)
            outcome["skipped"] = len(picked["add"]) - len(written)
            outcome["skipped_tables"] = [{"table": line["table"], "errors": line["errors"]}
                                         for line in plan["lines"] if line["status"] == "invalid"][:limit]
            return outcome, plan["new_groups"]

    outcome, new_groups = write()
    if new_groups:
        _sync(sid, user)
    return outcome


def _through_warehouse(spec: dict) -> bool:
    """A discovery the SQL warehouse can run: through a Unity Catalog connection, not listed through Azure (which takes
    a service credential only Databricks compute holds). A source read by host and password stays on the discovery
    job."""
    return warehouse.configured() and bool(spec.get("src_connection")) and not spec.get("azure_credential")


def _discover_through_warehouse(spec: dict, discovery_id: str) -> Optional[str]:
    """Re-discovery's read of one server through the SQL warehouse, as Add tables reads it: its databases listed again
    (under the listing's pattern) when the selection takes the whole server, then each database's tables. Returns why
    it failed, or nothing; a database that couldn't be read is on its discovery row, as the discovery job leaves it."""
    connection = spec["src_connection"]
    names = spec.get("databases") or []
    if names == "*":
        statement, parameters = warehouse.remote_query_statement(connection, SERVER_DATABASE,
                                                                 source_catalog.databases_sql(spec.get("pattern") or ""))
        answer = warehouse.start(statement, parameters)
        waited = 0.0
        while answer["state"] not in warehouse.ENDED:
            if waited >= REDISCOVERY_MAX_WAIT_S:
                return f"listing its databases is still going after {int(waited)} seconds"
            time.sleep(REDISCOVERY_WAREHOUSE_POLL_S)
            waited += REDISCOVERY_WAREHOUSE_POLL_S
            answer = warehouse.read(answer["statement_id"])
        if answer["state"] != "SUCCEEDED":
            return f"listing its databases ended {answer['state']}: {warehouse_reads.first_line(answer.get('error'))}"
        names = sorted({str(r["name"]) for r in answer["rows"] or []}, key=lambda n: (n.lower(), n))
    for at in range(0, len(names), q.CONTROL_BATCH):
        chunk = names[at:at + q.CONTROL_BATCH]
        db.execute(q.warehouse_named_rows_sql(len(chunk)),
                   tuple(v for name in chunk for v in (discovery_id, connection, name, spec.get("requested_by"), "{}")))
    waited = 0.0
    while _reads_step(discovery_id, connection)["pending"]:
        if waited >= REDISCOVERY_MAX_WAIT_S:
            return f"its tables are still being read after {int(waited)} seconds"
        time.sleep(REDISCOVERY_WAREHOUSE_POLL_S)
        waited += REDISCOVERY_WAREHOUSE_POLL_S
    return None


def _wait_for_run(run_id: int) -> dict:
    """A Databricks run's end (jobs.run_ended), polled every REDISCOVERY_POLL_S until REDISCOVERY_MAX_WAIT_S."""
    waited = 0.0
    while (ended := jobs.run_ended(run_id)) is None:
        if waited >= REDISCOVERY_MAX_WAIT_S:
            raise HTTPException(504, f"discovery run {run_id} is still going after {int(waited)} seconds")
        time.sleep(REDISCOVERY_POLL_S)
        waited += REDISCOVERY_POLL_S
    return ended


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("DATABRICKS_APP_PORT", "8000")))
