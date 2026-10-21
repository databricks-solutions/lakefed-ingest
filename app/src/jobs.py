"""Databricks Jobs operations for the app (the app's service principal holds CAN_MANAGE_RUN on the
ingestion, setup and discovery jobs through the app's `job` resources: view, run, and cancel; not edit)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import List, Optional

from lakebase import workspace

INGEST_JOB_ID = os.environ.get("INGEST_JOB_ID")
SETUP_JOB_ID = os.environ.get("SETUP_JOB_ID")
DISCOVER_JOB_ID = os.environ.get("DISCOVER_JOB_ID")
# Group given CAN_VIEW on the wrapper jobs the app creates for schedules (blank = owner and admins only).
SCHEDULE_JOB_VIEW_GROUP = os.environ.get("SCHEDULE_JOB_VIEW_GROUP", "users") or None


def ingest_variants() -> dict:
    """Ingestion jobs a schedule can start: 'default' (INGEST_JOB_ID), plus INGEST_JOB_ID_<NAME> variants
    (e.g. a larger cluster). The app needs CAN_MANAGE_RUN on each (an app `job` resource)."""
    out = {"default": INGEST_JOB_ID} if INGEST_JOB_ID else {}
    for k, v in os.environ.items():
        if k.startswith("INGEST_JOB_ID_") and v:
            out[k[len("INGEST_JOB_ID_"):].lower()] = v
    return out


def schedule_client():
    """Jobs API client for schedule sync and the wrapper jobs' runs."""
    import schedules
    return schedules.JobsClient(workspace())


def ts(ms) -> Optional[str]:
    """Epoch milliseconds (as the Jobs API reports times) to ISO 8601 UTC."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms else None


def job_summary(job_id) -> dict:
    """Name, schedule and default parameters of a job."""
    j = workspace().jobs.get(int(job_id))
    s = j.settings
    sched = s.schedule
    params = {p.name: p.default for p in (s.parameters or [])}
    return {
        "job_id": j.job_id,
        "name": s.name,
        "schedule": sched.quartz_cron_expression if sched else None,
        "timezone": sched.timezone_id if sched else None,
        "schedule_status": (sched.pause_status.value if sched and sched.pause_status else None),
        "default_task_collection": params.get("task_collection"),
        "parameters": params,
        "url": f"{workspace().config.host.rstrip('/')}/jobs/{j.job_id}",
    }


def runs(job_id, active_only: bool = False, limit: int = 25) -> List[dict]:
    rows = []
    # The Jobs API caps a page at 25 runs; the SDK pages through, and we stop at `limit`.
    for r in workspace().jobs.list_runs(job_id=int(job_id), active_only=active_only, limit=min(limit, 25)):
        params = {p.name: p.value for p in (getattr(r, "job_parameters", None) or [])}
        st = r.state
        rows.append({
            "run_id": r.run_id,
            "task_collection": params.get("task_collection"),
            "state": st.life_cycle_state.value if st and st.life_cycle_state else None,
            "result": st.result_state.value if st and st.result_state else None,
            "started": ts(r.start_time),
            "ended": ts(r.end_time),
            "trigger": r.trigger.value if r.trigger else None,
            "url": r.run_page_url,
        })
        if len(rows) >= limit:
            break
    return rows


def run_now(job_id, job_parameters: dict) -> int:
    waiter = workspace().jobs.run_now(job_id=int(job_id), job_parameters=job_parameters)
    resp = getattr(waiter, "response", waiter)
    return int(resp.run_id)


def run_state(run_id) -> dict:
    """A run's state: life cycle, result, the state message and its page."""
    r = workspace().jobs.get_run(int(run_id))
    st = r.state
    return {"run_id": r.run_id, "job_id": r.job_id,
            "state": st.life_cycle_state.value if st and st.life_cycle_state else None,
            "result": st.result_state.value if st and st.result_state else None,
            "message": st.state_message if st else None, "url": r.run_page_url}


def cancel(run_id) -> None:
    workspace().jobs.cancel_run(run_id=int(run_id))
