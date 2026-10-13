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


def _ts(ms) -> Optional[str]:
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
            "started": _ts(r.start_time),
            "ended": _ts(r.end_time) if r.end_time else None,
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


def cancel(run_id) -> None:
    workspace().jobs.cancel_run(run_id=int(run_id))
