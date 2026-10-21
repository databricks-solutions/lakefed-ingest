"""Schedules: run collections at a fixed cadence through small Databricks "wrapper" jobs.

A schedule has a cadence (minutes, dividing a day), a time zone, ingestion job parameters and one or
more member collections. For each member the app creates a wrapper job: a Quartz cron schedule
(shifted by the member's offset when the schedule staggers its members) and one ``run_job`` task that
starts the ingestion job with ``task_collection`` set. Wrapper jobs allow one run at a time and don't
queue, so a run that is still going when the next trigger fires makes Databricks skip that trigger;
the collection's run lock stays the backstop for manual runs.

Wrapper jobs carry tags naming their schedule, collection and app (the Lakebase database whose
schedules they implement), so ``sync`` reconciles by tag: it adopts a job whose id was never recorded
and deletes jobs no active member owns, whatever left them behind.

Pure helpers (cron, offsets, next runs, job settings, drift, warnings) are unit-tested; ``sync`` and
``check`` take a ``JobsClient`` so they can be tested with a fake.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CADENCES = (5, 10, 15, 20, 30, 60, 120, 180, 240, 360, 720, 1440)   # minutes; each divides a day
_whole = lambda lo, hi: (lambda v: v.isdigit() and lo <= int(v) <= hi)
_flag = lambda v: v in ("true", "false")
# Ingestion job parameters a schedule may set (strings, as job parameters are), checked the way
# ingest.ipynb parses them; bounds only catch typos. The job's own defaults apply to the rest.
PARAMETERS = {
    "parallelism": _whole(1, 256),
    "detection_chunk": _whole(1, 5000),
    "trust_server_certificate": _flag,
    "secret_scope": lambda v: bool(re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", v)),
    "snapshot_isolation": _flag,
    "reseed_on_new_columns": _flag,
    "append_batch_size": _whole(1, 10000),
    "max_seeds_per_run": _whole(0, 100000),
    "freshness_interval_s": lambda v: bool(re.fullmatch(r"\d{1,6}(\.\d+)?", v)) and float(v) <= 86400,
    "max_queries_per_source": _whole(0, 1024),
    "table_retries": _whole(0, 10),
    "table_retry_budget": _whole(0, 100000),
}
FLAGS = [k for k, check in PARAMETERS.items() if check is _flag]
TAG_SCHEDULE = "lakefed_ingest_schedule"
TAG_COLLECTION = "lakefed_ingest_collection"
TAG_OWNER = "lakefed_ingest_app"     # instance/database: apps of other deployments in the workspace keep out
JOB_NAME_PREFIX = "lakefed_ingest schedule"
_EMAIL = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.,:()-]{0,79}")


# ---- validation ---------------------------------------------------------------------------------

def emails(text: Optional[str]) -> List[str]:
    return [e.strip() for e in (text or "").replace(";", ",").split(",") if e.strip()]


def validate(body: dict, known_collections: Iterable[str], variants: Iterable[str]) -> List[str]:
    """Errors in a schedule body (empty list = valid)."""
    errs = []
    if not _NAME.fullmatch(str(body.get("name") or "")):
        errs.append("name: 1-80 characters (letters, digits, space, _ . , : ( ) -), starting with a letter or digit")
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
    zone = ZoneInfo(tz or "UTC")
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
                 view_group: Optional[str] = None, owner: Optional[str] = None) -> dict:
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
        "tags": {TAG_SCHEDULE: str(schedule["id"]), TAG_COLLECTION: collection[:255],
                 **({TAG_OWNER: owner[:255]} if owner else {})},
        # Skipped triggers are how an overrun is absorbed, not failures: don't email them.
        "notification_settings": {"no_alert_for_skipped_runs": True},
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
        "tags": {k: v for k, v in (s.get("tags") or {}).items() if k in (TAG_SCHEDULE, TAG_COLLECTION, TAG_OWNER)},
        "notify": sorted((s.get("email_notifications") or {}).get("on_failure") or []),
        "no_alert_for_skipped_runs": bool((s.get("notification_settings") or {}).get("no_alert_for_skipped_runs")),
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
    """The Jobs API calls sync needs. Settings stay plain JSON (the SDK's raw API client for get, create,
    reset and delete; its paging iterators, as dicts, for listing jobs and runs)."""

    def __init__(self, w):
        self.w = w
        self.api = w.api_client

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

    def tagged(self, schedule_id: int, owner: Optional[str]) -> Dict[int, str]:
        """The schedule's wrapper jobs, found by tag: {job_id: collection}. Lists every job the app can see
        (the Jobs API can't filter by tag), so it runs once per sync, not per poll."""
        out = {}
        for j in self.w.jobs.list(limit=100):
            tags = (j.settings.tags if j.settings else None) or {}
            if tags.get(TAG_SCHEDULE) == str(schedule_id) and owner and tags.get(TAG_OWNER) == owner[:255]:
                out[int(j.job_id)] = tags.get(TAG_COLLECTION) or ""
        return out

    def runs(self, job_id: int, since_ms: int, until_ms: Optional[int] = None) -> List[dict]:
        """Every run of the job that started in [since_ms, until_ms) (no cap: the window bounds it)."""
        return [r.as_dict() for r in self.w.jobs.list_runs(job_id=int(job_id), start_time_from=int(since_ms),
                                                            start_time_to=int(until_ms) if until_ms else None)]


@dataclass
class MemberResult:
    task_collection: str
    job_id: Optional[int]
    action: str            # created | adopted | updated | unchanged | deleted | kept | error
    detail: str = ""
    member_id: Optional[int] = None     # None: a job no member owns (an untracked job deleted, or a listing error)


def sync(schedule: dict, members: List[dict], removed: List[dict], client: JobsClient,
         ingest_job_id: Optional[int], view_group: Optional[str] = None,
         owner: Optional[str] = None) -> List[MemberResult]:
    """Make each active member's wrapper job match the schedule (create, adopt an unrecorded tagged job, or
    reset when it drifted); delete the jobs of removed members (or of every member when the schedule is
    deleted), then any other job tagged with the schedule. Callers serialize syncs of one schedule.
    ``ingest_job_id`` None (its job variant is no longer configured): active members report an error,
    deletes still happen."""
    out: List[MemberResult] = []
    deleting = schedule.get("deleted_at") is not None
    try:
        tagged: Optional[Dict[int, str]] = client.tagged(schedule["id"], owner)
    except Exception as e:      # without the listing, sync by the recorded ids and delete no untracked job
        tagged = None
        out.append(MemberResult("", None, "error", f"listing the schedule's jobs: {e}"[:500]))
    claimed = set()             # job ids an active member keeps
    for m in (members if not deleting else []):
        c = m["task_collection"]
        if m.get("job_id"):
            claimed.add(int(m["job_id"]))
        if ingest_job_id is None:       # keep its jobs: only the target is unknown
            claimed.update(t for t, tc in (tagged or {}).items() if tc == c[:255])
            out.append(MemberResult(c, m.get("job_id"), "error",
                                    f"no ingestion job configured for job variant '{schedule.get('job_variant')}'",
                                    m.get("id")))
            continue
        desired = job_settings(schedule, c, int(m["offset_minutes"]), ingest_job_id, view_group, owner)
        try:
            jid, job, action = m.get("job_id"), None, "updated"
            if jid:
                job = client.get(jid)
            if job is None:     # never recorded, or deleted outside the app: adopt a tagged job before creating
                for t, tc in sorted((tagged or {}).items()):
                    if tc == c[:255] and t not in claimed:
                        job = client.get(t)
                        if job is not None:
                            jid, action = t, "adopted"
                            break
            if job is None:
                jid = client.create(desired)
                claimed.add(jid)
                out.append(MemberResult(c, jid, "created", member_id=m.get("id")))
                continue
            claimed.add(int(jid))
            diff = drift(job.get("settings") or {}, desired)
            if diff:
                client.reset(jid, desired)
            if diff or action == "adopted":
                out.append(MemberResult(c, jid, action, ", ".join(diff), m.get("id")))
            else:
                out.append(MemberResult(c, jid, "unchanged", member_id=m.get("id")))
        except Exception as e:
            out.append(MemberResult(c, m.get("job_id"), "error", str(e)[:500], m.get("id")))
    deleted = set()
    for m in removed + (members if deleting else []):
        if not m.get("job_id"):
            continue
        if int(m["job_id"]) in claimed:     # the collection was added back and its member adopted the job
            out.append(MemberResult(m["task_collection"], m["job_id"], "kept", "adopted by the active member",
                                    m.get("id")))
            continue
        try:
            client.delete(m["job_id"])
            deleted.add(int(m["job_id"]))
            out.append(MemberResult(m["task_collection"], m["job_id"], "deleted", member_id=m.get("id")))
        except Exception as e:
            out.append(MemberResult(m["task_collection"], m["job_id"], "error", str(e)[:500], m.get("id")))
    for t, tc in sorted((tagged or {}).items()):
        if t in claimed or t in deleted:
            continue
        try:
            client.delete(t)
            out.append(MemberResult(tc, t, "deleted", "untracked job"))
        except Exception as e:
            out.append(MemberResult(tc, t, "error", f"deleting untracked job: {e}"[:500]))
    return out


def check(schedule: dict, members: List[dict], client: JobsClient, ingest_job_id: int,
          owner: Optional[str] = None) -> Dict[str, dict]:
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
        desired = job_settings(schedule, m["task_collection"], int(m["offset_minutes"]), ingest_job_id, owner=owner)
        diff = drift(job.get("settings") or {}, desired)
        out[m["task_collection"]] = {"status": "drifted", "fields": diff} if diff else {"status": "in_sync"}
    return out


def is_skipped(r: dict) -> bool:
    """A wrapper run Databricks skipped because the previous run was still going."""
    st = r.get("state") or {}
    return (st.get("life_cycle_state") == "SKIPPED" or st.get("result_state") == "SKIPPED"
            or (r.get("status") or {}).get("state") == "SKIPPED")


class SkippedRuns:
    """Start times of the wrapper jobs' skipped runs, kept between requests. A job's runs are listed in full
    once per window; after that a refresh (at most every ``ttl_s``) lists only the runs that started since
    the previous refresh, so counts cover the whole window without paging a day of runs on every poll.
    A skipped run is final when it appears, so nothing listed needs listing again."""

    MARGIN_MS = 10 * 60 * 1000      # re-list a little overlap at each refresh (runs are de-duplicated by id)

    def __init__(self, ttl_s: int = 60, keep_hours: int = 8 * 24):
        self.ttl_ms, self.keep_ms = ttl_s * 1000, keep_hours * 3600 * 1000
        self._jobs: Dict[int, dict] = {}
        self._mu = threading.Lock()

    def starts(self, client: JobsClient, job_id: int, since_ms: int, now_ms: int) -> List[int]:
        """The job's skipped runs that started at or after ``since_ms`` (ms, ascending). Raises if the Jobs
        API does; what was cached stays."""
        job_id = int(job_id)
        with self._mu:
            e = self._jobs.get(job_id)
            if e is None:
                e = {"from": since_ms, "to": now_ms, "skipped": self._list(client, job_id, since_ms, None)}
                self._jobs[job_id] = e
            elif since_ms < e["from"]:          # a longer window than listed so far: list the earlier part
                e["skipped"].update(self._list(client, job_id, since_ms, e["from"]))
                e["from"] = since_ms
            if now_ms - e["to"] >= self.ttl_ms:
                e["skipped"].update(self._list(client, job_id, e["to"] - self.MARGIN_MS, None))
                e["to"] = now_ms
            floor = now_ms - self.keep_ms
            if e["from"] < floor:
                e["skipped"] = {r: t for r, t in e["skipped"].items() if t >= floor}
                e["from"] = floor
            return sorted(t for t in e["skipped"].values() if t >= since_ms)

    @staticmethod
    def _list(client, job_id, since_ms, until_ms) -> Dict[int, int]:
        return {int(r["run_id"]): int(r["start_time"]) for r in client.runs(job_id, since_ms, until_ms)
                if is_skipped(r) and r.get("start_time")}
