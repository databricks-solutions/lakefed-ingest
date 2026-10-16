"""Schedules: run collections at a fixed cadence through small Databricks "wrapper" jobs.

A schedule has a cadence (minutes, dividing a day), a time zone, ingestion job parameters and one or
more member collections. For each member the app creates a wrapper job: a Quartz cron schedule
(shifted by the member's offset when the schedule staggers its members) and one ``run_job`` task that
starts the ingestion job with ``task_collection`` set. Wrapper jobs allow one run at a time and don't
queue, so a run that is still going when the next trigger fires makes Databricks skip that trigger;
the collection's run lock stays the backstop for manual runs.

Pure helpers (cron, offsets, next runs, job settings, drift, warnings) are unit-tested; ``sync`` and
``check`` take a ``JobsClient`` so they can be tested with a fake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CADENCES = (5, 10, 15, 20, 30, 60, 120, 180, 240, 360, 720, 1440)   # minutes; each divides a day
PARAMETERS = {   # ingestion job parameters a schedule may set (strings, as job parameters are)
    "parallelism": lambda v: v.isdigit() and 1 <= int(v) <= 256,
    "detection_chunk": lambda v: v.isdigit() and 1 <= int(v) <= 5000,
    "trust_server_certificate": lambda v: v in ("true", "false"),
    "secret_scope": lambda v: bool(re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", v)),
}
TAG_SCHEDULE = "lakefed_ingest_schedule"
TAG_COLLECTION = "lakefed_ingest_collection"
JOB_NAME_PREFIX = "lakefed_ingest schedule"
_EMAIL = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.:-]{0,79}")


# ---- validation ---------------------------------------------------------------------------------

def emails(text: Optional[str]) -> List[str]:
    return [e.strip() for e in (text or "").replace(";", ",").split(",") if e.strip()]


def validate(body: dict, known_collections: Iterable[str], variants: Iterable[str]) -> List[str]:
    """Errors in a schedule body (empty list = valid)."""
    errs = []
    if not _NAME.fullmatch(str(body.get("name") or "")):
        errs.append("name: 1-80 characters (letters, digits, space, _ . : -), starting with a letter or digit")
    if body.get("cadence_minutes") not in CADENCES:
        errs.append(f"cadence_minutes must be one of {', '.join(map(str, CADENCES))}")
    try:
        ZoneInfo(str(body.get("timezone") or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        errs.append(f"timezone: unknown time zone '{body.get('timezone')}'")
    params = body.get("parameters") or {}
    if not isinstance(params, dict):
        errs.append("parameters must be an object")
    else:
        for k, v in params.items():
            if k not in PARAMETERS:
                errs.append(f"parameters: '{k}' isn't a schedulable parameter ({', '.join(PARAMETERS)})")
            elif not PARAMETERS[k](str(v)):
                errs.append(f"parameters.{k}: invalid value '{v}'")
    if (body.get("job_variant") or "default") not in set(variants):
        errs.append(f"job_variant must be one of {', '.join(sorted(variants))}")
    members = body.get("members") or []
    known = set(known_collections)
    if not members:
        errs.append("members: at least one collection")
    if len(set(members)) != len(members):
        errs.append("members: a collection is listed twice")
    unknown = [m for m in members if m not in known]
    if unknown:
        errs.append(f"members: unknown collections {', '.join(sorted(unknown))}")
    bad = [e for e in emails(body.get("notify_emails")) if not _EMAIL.fullmatch(e)]
    if bad:
        errs.append(f"notify_emails: invalid address {bad[0]}")
    return errs


# ---- cadence, offsets, cron, next runs ----------------------------------------------------------

def offsets(collections: Iterable[str], cadence: int, stagger: bool) -> Dict[str, int]:
    """Start offset (minutes within the cadence) per collection: spread evenly when staggering, in name
    order so they are stable; all 0 otherwise. For cadences over an hour the offsets stay within the
    first hour, so staggered members keep starting near the top of their cadence."""
    names = sorted(collections)
    span = min(cadence, 60)
    return {c: (i * span // len(names) if stagger else 0) for i, c in enumerate(names)}


def quartz_cron(cadence: int, offset: int) -> str:
    """Quartz expression firing when minutes-since-midnight is congruent to ``offset`` modulo ``cadence``."""
    if cadence not in CADENCES or not 0 <= offset < cadence:
        raise ValueError(f"bad cadence/offset {cadence}/{offset}")
    if cadence < 60:
        return f"0 {offset}/{cadence} * * * ?"
    hours, first_hour, minute = cadence // 60, offset // 60, offset % 60
    if cadence == 60:
        return f"0 {minute} * * * ?"
    if cadence == 1440:
        return f"0 {minute} {first_hour} * * ?"
    return f"0 {minute} {first_hour}/{hours} * * ?"


def describe(cadence: int, offset: int = 0, tz: str = "UTC") -> str:
    if cadence < 60:
        return f"every {cadence} minutes" + (f" at :{offset:02d} past" if offset else "")
    if cadence == 60:
        return f"hourly at :{offset:02d}"
    if cadence == 1440:
        return f"daily at {offset // 60:02d}:{offset % 60:02d} ({tz})"
    return f"every {cadence // 60} hours from {offset // 60:02d}:{offset % 60:02d} ({tz})"


def fire_times(cadence: int, offset: int, tz: str, start: datetime, end: datetime) -> List[datetime]:
    """Trigger times in [start, end), as UTC datetimes."""
    zone = ZoneInfo(tz)
    local = start.astimezone(zone)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    out, day = [], midnight
    while day.astimezone(timezone.utc) < end:
        for m in range(offset, 1440, cadence):
            t = (day + timedelta(minutes=m)).astimezone(timezone.utc)
            if start <= t < end:
                out.append(t)
        day = (day + timedelta(days=1, hours=2)).replace(hour=0, minute=0)   # safe across DST changes
    return out


def next_run(cadence: int, offset: int, tz: str, after: datetime) -> datetime:
    return fire_times(cadence, offset, tz, after, after + timedelta(days=2))[0]


# ---- wrapper job settings and drift -------------------------------------------------------------

def job_name(schedule_name: str, collection: str) -> str:
    return f"{JOB_NAME_PREFIX}: {schedule_name} / {collection}"


def job_settings(schedule: dict, collection: str, offset: int, ingest_job_id: int,
                 view_group: Optional[str] = None) -> dict:
    """Jobs API 2.2 settings of a member's wrapper job."""
    params = {"task_collection": collection,
              **{k: str(v) for k, v in sorted((schedule.get("parameters") or {}).items())}}
    s = {
        "name": job_name(schedule["name"], collection),
        "description": (f"Managed by the lakefed_ingest operations app (schedule '{schedule['name']}'). "
                        "Edit the schedule in the app: changes made here are reported as drift and undone "
                        "by the next sync."),
        "schedule": {"quartz_cron_expression": quartz_cron(int(schedule["cadence_minutes"]), offset),
                     "timezone_id": schedule.get("timezone") or "UTC",
                     "pause_status": "PAUSED" if schedule.get("paused") else "UNPAUSED"},
        "max_concurrent_runs": 1,
        "queue": {"enabled": False},
        "tasks": [{"task_key": "ingest",
                   "run_job_task": {"job_id": int(ingest_job_id), "job_parameters": params}}],
        "tags": {TAG_SCHEDULE: str(schedule["id"]), TAG_COLLECTION: collection[:255]},
    }
    addrs = emails(schedule.get("notify_emails"))
    if addrs:
        s["email_notifications"] = {"on_failure": addrs}
    if view_group:
        s["access_control_list"] = [{"group_name": view_group, "permission_level": "CAN_VIEW"}]
    return s


def _comparable(s: dict) -> dict:
    """The parts of job settings the app owns, normalized for comparison."""
    task = ((s.get("tasks") or [{}])[0]) or {}
    rj = task.get("run_job_task") or {}
    sched = s.get("schedule") or {}
    return {
        "name": s.get("name"),
        "cron": sched.get("quartz_cron_expression"),
        "timezone": sched.get("timezone_id"),
        "paused": sched.get("pause_status") == "PAUSED",
        "max_concurrent_runs": s.get("max_concurrent_runs", 1),
        "queue": bool((s.get("queue") or {}).get("enabled")),
        "tasks": len(s.get("tasks") or []),
        "target_job_id": rj.get("job_id"),
        "job_parameters": {k: str(v) for k, v in (rj.get("job_parameters") or {}).items()},
        "tags": {k: v for k, v in (s.get("tags") or {}).items() if k in (TAG_SCHEDULE, TAG_COLLECTION)},
        "notify": sorted((s.get("email_notifications") or {}).get("on_failure") or []),
    }


def drift(actual: dict, desired: dict) -> List[str]:
    """Names of the app-owned settings that differ between the job and the schedule."""
    a, d = _comparable(actual), _comparable(desired)
    return [k for k in d if a.get(k) != d.get(k)]


# ---- warnings -----------------------------------------------------------------------------------

def warnings(cadence: int, p95_s: Optional[float], min_sla_minutes: Optional[float],
             skipped: int = 0) -> List[str]:
    """Overrun and SLA warnings for one member."""
    out = []
    period = cadence * 60
    if p95_s is not None and p95_s >= period:
        out.append(f"overrun: p95 run time {round(p95_s / 60, 1)} min is longer than the {cadence}-minute cadence, "
                   "so triggers are skipped")
    elif p95_s is not None and p95_s >= 0.8 * period:
        out.append(f"near overrun: p95 run time {round(p95_s / 60, 1)} min is over 80% of the cadence")
    if skipped:
        out.append(f"{skipped} trigger(s) skipped in the last 24 hours (the previous run was still going)")
    if min_sla_minutes is not None and p95_s is not None and cadence + p95_s / 60 > min_sla_minutes:
        out.append(f"can't meet SLA: cadence + p95 run time ({round(cadence + p95_s / 60)} min) is longer than the "
                   f"strictest table SLA ({int(min_sla_minutes)} min)")
    return out


# ---- sync with the Jobs API ---------------------------------------------------------------------

class JobsClient:
    """The Jobs API calls sync needs (wraps the SDK's raw API client, so settings stay plain JSON)."""

    def __init__(self, api_client):
        self.api = api_client

    def get(self, job_id: int) -> Optional[dict]:
        try:
            return self.api.do("GET", "/api/2.2/jobs/get", query={"job_id": int(job_id)})
        except Exception as e:      # deleted outside the app
            if "does not exist" in str(e) or "RESOURCE_DOES_NOT_EXIST" in str(e) or "not found" in str(e).lower():
                return None
            raise

    def create(self, settings: dict) -> int:
        return int(self.api.do("POST", "/api/2.2/jobs/create", body=settings)["job_id"])

    def reset(self, job_id: int, settings: dict) -> None:
        body = {k: v for k, v in settings.items() if k != "access_control_list"}
        self.api.do("POST", "/api/2.2/jobs/reset", body={"job_id": int(job_id), "new_settings": body})

    def delete(self, job_id: int) -> None:
        try:
            self.api.do("POST", "/api/2.2/jobs/delete", body={"job_id": int(job_id)})
        except Exception as e:
            if "does not exist" not in str(e) and "RESOURCE_DOES_NOT_EXIST" not in str(e):
                raise

    def runs(self, job_id: int, since_ms: int, limit: int = 200) -> List[dict]:
        out, token = [], None
        while len(out) < limit:
            q = {"job_id": int(job_id), "start_time_from": since_ms, "limit": 25}
            if token:
                q["page_token"] = token
            r = self.api.do("GET", "/api/2.2/jobs/runs/list", query=q)
            out += r.get("runs") or []
            token = r.get("next_page_token")
            if not token:
                break
        return out[:limit]


@dataclass
class MemberResult:
    task_collection: str
    job_id: Optional[int]
    action: str            # created | updated | unchanged | deleted | error
    detail: str = ""


def sync(schedule: dict, members: List[dict], removed: List[dict], client: JobsClient, ingest_job_id: int,
         view_group: Optional[str] = None) -> List[MemberResult]:
    """Make each active member's wrapper job match the schedule (create, or reset when it drifted), and
    delete the jobs of removed members (or of every member when the schedule is deleted)."""
    out = []
    deleting = schedule.get("deleted_at") is not None
    for m in (members if not deleting else []):
        desired = job_settings(schedule, m["task_collection"], int(m["offset_minutes"]), ingest_job_id, view_group)
        try:
            job = client.get(m["job_id"]) if m.get("job_id") else None
            if job is None:
                jid = client.create(desired)
                out.append(MemberResult(m["task_collection"], jid, "created"))
            else:
                diff = drift(job.get("settings") or {}, desired)
                if diff:
                    client.reset(m["job_id"], desired)
                    out.append(MemberResult(m["task_collection"], m["job_id"], "updated", ", ".join(diff)))
                else:
                    out.append(MemberResult(m["task_collection"], m["job_id"], "unchanged"))
        except Exception as e:
            out.append(MemberResult(m["task_collection"], m.get("job_id"), "error", str(e)[:500]))
    for m in removed + (members if deleting else []):
        if not m.get("job_id"):
            continue
        try:
            client.delete(m["job_id"])
            out.append(MemberResult(m["task_collection"], m["job_id"], "deleted"))
        except Exception as e:
            out.append(MemberResult(m["task_collection"], m["job_id"], "error", str(e)[:500]))
    return out


def check(schedule: dict, members: List[dict], client: JobsClient, ingest_job_id: int) -> Dict[str, dict]:
    """Per member: in_sync | drifted (with the fields) | missing | not_synced. Changes nothing."""
    out = {}
    for m in members:
        if not m.get("job_id"):
            out[m["task_collection"]] = {"status": "not_synced"}
            continue
        job = client.get(m["job_id"])
        if job is None:
            out[m["task_collection"]] = {"status": "missing"}
            continue
        desired = job_settings(schedule, m["task_collection"], int(m["offset_minutes"]), ingest_job_id)
        diff = drift(job.get("settings") or {}, desired)
        out[m["task_collection"]] = {"status": "drifted", "fields": diff} if diff else {"status": "in_sync"}
    return out


def skipped_triggers(runs: List[dict]) -> int:
    """Wrapper runs Databricks skipped because the previous run was still going."""
    n = 0
    for r in runs:
        st = r.get("state") or {}
        if st.get("life_cycle_state") == "SKIPPED" or st.get("result_state") == "SKIPPED" \
                or (r.get("status") or {}).get("state") == "SKIPPED":
            n += 1
    return n
