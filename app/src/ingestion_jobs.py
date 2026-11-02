"""Ingestion jobs (schedules) and their job runs, as the Overview, the Ingestion jobs list, Runs and the job
pages show them. Pure: main.py reads the schedules, the shared table states (rollup), the running pass and the
group runs of a window, and this module places and sums them, so it is tested without a database.

A job run is one trigger of a schedule (schedules.job_runs places group runs by cadence slot). Placement is
by the schedule as it is now, so it is exact for scheduled runs; a run started by hand inside a slot is an
extra run, and a cluster start slower than the cadence can land a run in the next slot."""
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

import queries as q
import rollup
import schedules as sch

# How many of a job's latest job runs the Overview places: enough to say "in each of the last N job runs".
RECENT_JOB_RUNS = 6


def _when(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def window(cadence: int, slots: int, end: datetime) -> datetime:
    """The start of a window holding a job's last `slots` triggers before `end`, at most 7 days long."""
    return end - min(timedelta(minutes=cadence * slots), timedelta(days=7))


def place(job: dict, members: List[dict], runs: List[dict], start: datetime, end: datetime, now: datetime):
    """The job's job runs whose slot starts in [start, end), newest first (schedules.job_runs), with their
    members' runs. Triggers are placed from one slot earlier, so a window starting mid-slot keeps every
    member of its first job run (a member's offset is under a cadence, and under an hour past hourly). A paused
    job fires nothing, so none of its members is waiting or starting."""
    mine = [m for m in members if m["schedule_id"] == job["id"]]
    groups = {m["task_collection"] for m in mine}
    cadence = int(job["cadence_minutes"])
    placed = sch.job_runs(cadence, job.get("timezone") or "UTC", mine, [r for r in runs if r["task_collection"] in groups],
                          start - timedelta(minutes=max(cadence, 60)), end, now, sch.start_minute(job),
                          check_variant=job.get("job_variant") == sch.CHECK_VARIANT)
    if job.get("paused"):
        for jr in placed:
            for m in jr["members"]:
                if m["state"] in ("waiting", "starting"):
                    m["state"] = "no run"
    return [jr for jr in placed if jr["slot"] >= start]


def current_and_past(job: dict, members: List[dict], runs: List[dict], start: datetime, now: datetime) -> List[dict]:
    """The job runs whose slot started in [start, now], newest first. Placed through the end of the current
    slot, so its members whose start is still ahead are in it as waiting."""
    cadence = int(job["cadence_minutes"])
    return [jr for jr in place(job, members, runs, start, now + timedelta(minutes=cadence), now) if jr["slot"] <= now]


def _progress(member: dict, running_by_run: dict, group_tables: int) -> dict:
    """One member's part of its job run's progress bar, in the shape of RunDetail's Progress: a running
    group run from the running pass, a finished one from its totals (its ok counts its unchanged tables),
    one whose check found nothing to load as all unchanged, one that did not run as not run, and one not
    started yet as all to go."""
    run = member.get("run")
    if run and run.get("status") == "running":
        p = running_by_run.get(run.get("run_id")) or {}
        loaded = q._count(p, "tables_ok")
        return {"total": q._count(p, "enabled_tables") or group_tables, "loaded": loaded,
                "unchanged": max(q._count(p, "tables_checked") - loaded, 0),
                "in_flight": q._count(p, "tables_running"), "failed": q._count(p, "tables_failed"), "not_run": 0}
    if run and run.get("status") != "skipped" and run.get("total") is not None:
        loaded = q.loaded_tables(run)     # its unchanged tables and reseeds queued count as checked, not loaded
        total, unchanged = q._count(run, "total"), max(q._count(run, "ok") - loaded, 0)
        if run.get("work_id") and group_tables > total:
            # A run the check job started read only the databases the check listed; the check found the rest unchanged.
            unchanged, total = unchanged + group_tables - total, group_tables
        return {"total": total, "unchanged": unchanged, "loaded": loaded, "in_flight": 0, "failed": q._count(run, "failed"),
                "not_run": 0}
    unchanged = group_tables if member.get("state") == "unchanged" else 0
    not_run = group_tables if member.get("state") in ("no run", "skipped", "check failed") else 0
    return {"total": group_tables, "unchanged": unchanged, "loaded": 0, "in_flight": 0, "failed": 0, "not_run": not_run}


def _sum(parts: Iterable[dict]) -> dict:
    out = {"total": 0, "unchanged": 0, "loaded": 0, "in_flight": 0, "failed": 0, "not_run": 0}
    for p in parts:
        for k in out:
            out[k] += p[k]
    return out


def summarize(job: dict, job_run: dict, running_by_run: dict, group_tables: Dict[str, int],
              p50_by_group: Dict[str, Optional[float]], now: datetime) -> dict:
    """One job run as its rows and pages show it: outcome, group runs (ran, of members), took (first group
    run start to last finish), tables loaded, speed over its work span (first table start to last finish
    across its group runs), rows written, parallelism, clusters, failed tables, its progress bar over all
    the job's tables, and while running its speed now, expected end, running for and typical run time.
    Group runs come with their work (main._with_rows_written)."""
    members = job_run["members"]
    runs = [m["run"] for m in members if m.get("run")] + [r for m in members for r in m.get("extra") or []]
    ran = [r for r in runs if r.get("status") != "skipped"]       # every group run that ran, extra ones included
    members_ran = sum(1 for m in members if m.get("run") and m["run"].get("status") != "skipped")
    outcome = q.job_run_outcome(members)
    if outcome["label"] == "Not run" and job.get("paused"):
        outcome = dict(outcome, note="the job is paused", paused=True)
    starts = [_when(r["started_at"]) for r in ran if r.get("started_at")]
    finishes = [_when(r["finished_at"]) for r in ran if r.get("finished_at")]
    work_starts = [_when(r["work_started_at"]) for r in ran if r.get("work_started_at")]
    work_ends = [_when(r["work_finished_at"]) for r in ran if r.get("work_finished_at")]
    loaded = sum(q.loaded_tables(r) for r in ran)
    running = outcome["label"] in q.RUNNING_OUTCOMES
    speed = None
    if work_starts and work_ends and loaded and not running:
        speed = round(loaded / (max((max(work_ends) - min(work_starts)).total_seconds(), 1) / 60), 1)
    progress = _sum(_progress(m, running_by_run, group_tables.get(m["task_collection"], 0)) for m in members)
    out = {
        "slot": job_run["slot"].isoformat(), "job_id": job["id"], "job_name": job["name"], "outcome": outcome,
        "members": len(members), "ran": members_ran, "extra": sum(len(m.get("extra") or []) for m in members),
        "started_at": min(starts).isoformat() if starts else None,
        "took_s": (round((max(finishes) - min(starts)).total_seconds())
                   if starts and finishes and not running and outcome["label"] != "Ended without finishing" else None),
        "loaded": loaded, "speed_tables_per_min": speed,
        "rows_written": sum(q._count(r, "rows_written") for r in ran),
        "rows_partial": any(r.get("rows_partial") for r in ran),
        "parallelism": sorted({int(r["parallelism"]) for r in ran if r.get("parallelism") is not None}),
        "clusters": len({r["cluster_id"] for r in ran if r.get("cluster_id")}),
        "failed_tables": sum(q._count(r, "failed") for r in ran if r.get("status") != "running")
        + sum(q._count(running_by_run.get(r.get("run_id")) or {}, "tables_failed") for r in ran
              if r.get("status") == "running"),
        "run_ids": [r["run_id"] for r in runs if r.get("run_id") is not None],
        "checks": [check_summary(c) for c in job_run.get("checks") or []],     # never counted as group runs
        "progress": progress,
        "outside": bool(job_run.get("outside")),     # started outside every trigger: Run now, a group page, the Jobs UI
    }
    if running:
        live = [q.live_speed(running_by_run[r["run_id"]], now) for r in ran
                if r.get("status") == "running" and r.get("run_id") in running_by_run]
        live = [s for s in live if s]
        end = q.job_run_expected_end(members, running_by_run, p50_by_group, now)
        typical = [m["offset_minutes"] * 60 + p50_by_group[m["task_collection"]] for m in members
                   if p50_by_group.get(m["task_collection"]) is not None]
        out.update({
            "speed_now": ({"tables_per_min": round(sum(s["tables_per_min"] for s in live), 1),
                           "rows_per_min": round(sum(s["rows_per_min"] for s in live), 1)} if live else None),
            "expected_end": end.isoformat() if end else None,
            "running_s": round((now - min(starts)).total_seconds()) if starts else None,
            "typical_s": round(max(typical)) if typical else None,
            "waiting": sum(1 for m in members if m["state"] in ("waiting", "starting")),
            "running_group_runs": sum(1 for r in ran if r.get("status") == "running"),
            "finished_group_runs": sum(1 for r in ran if r.get("status") != "running"),     # extra runs included
        })
    return out


def check_summary(check: dict) -> dict:
    """One of a job run's checks: its record, status, and what it found for its collection."""
    detail = sch._detail(check).get("check") or {}
    return {"run_id": check.get("run_id"), "task_collection": check["task_collection"], "status": check.get("status"),
            "unchanged_tables": detail.get("unchanged_tables"), "databases_with_work": len(detail.get("work") or [])}


def check_found(check: dict) -> dict:
    """A check's own page: its summary, what it found in words, and its error if it failed."""
    status = _check_status(check)
    return dict(check_summary(check), found=status["label"], error=status["note"])


def _check_status(check: dict) -> dict:
    found = check_summary(check)
    if check.get("status") == "running":
        return {"label": "Checking", "tone": "run", "note": None}
    if check.get("status") == "failed":
        return {"label": "Check failed", "tone": "bad", "note": (sch._detail(check).get("check") or {}).get("error")}
    n = found["databases_with_work"]
    found = f"{n} {'database' if n == 1 else 'databases'} with work"
    return {"label": f"Checked: {found}" if n else "Checked: no changes",
            "tone": "ok", "note": None}


def member_views(job_run: dict, running_by_run: dict, group_tables: Dict[str, int], now: datetime,
                 terminating: Iterable[str] = ()) -> List[dict]:
    """A job run's lines: its checks first, each a line of its own (check: True), then its group runs, one row per
    member: running first, then waiting, then finished. Each with its state, start or planned start, took or
    running for, progress, speed, parallelism, cluster and failed tables; extra runs follow their member."""
    checks = [{"task_collection": c["task_collection"], "check": True, "state": "check", "extra": False,
               "offset_minutes": None, "fire_at": None, "status": _check_status(c), "run_id": c.get("run_id"),
               "started_at": c.get("started_at"), "duration_s": c.get("duration_s"), "progress": None,
               "speed_tables_per_min": None, "parallelism": None, "cluster_id": c.get("cluster_id"), "failed": 0}
              for c in job_run.get("checks") or []]
    rows = []
    for m in job_run["members"]:
        for i, run in enumerate([m.get("run")] + list(m.get("extra") or [])):
            if i and not run:
                continue
            if run and str(run.get("job_run_id") or "") in terminating:
                run = dict(run, cancelling=True)
            p = running_by_run.get((run or {}).get("run_id")) if run else None
            if run:
                status = q.run_outcome(run, p if run.get("status") == "running" else None)
            elif m["state"] == "waiting":
                status = {"label": f"Waiting, starts {_when(m['fire_at']).isoformat()}", "tone": "", "note": None}
            elif m["state"] == "starting":
                status = {"label": "Starting", "tone": "run", "note": None}
            elif m["state"] == "unchanged":
                status = {"label": "Unchanged", "tone": "ok", "note": "its check found nothing to load"}
            elif m["state"] == "checking":
                status = {"label": "Checking", "tone": "run",
                          "note": "its check is going; a group run starts if it finds work"}
            elif m["state"] == "check failed":
                status = {"label": "Check failed", "tone": "bad", "note": "its check failed, so no group run started"}
            elif m["state"] == "skipped":
                status = {"label": "Skipped: still running", "tone": "neutral", "note": "the previous group run was still going"}
            else:
                status = {"label": "Not run", "tone": "neutral", "note": None}
            live = run and run.get("status") == "running"
            rows.append({
                "task_collection": m["task_collection"], "offset_minutes": m["offset_minutes"],
                "fire_at": _when(m["fire_at"]).isoformat(), "state": m["state"], "extra": bool(i),
                "status": status, "run_id": (run or {}).get("run_id"), "started_at": (run or {}).get("started_at"),
                "duration_s": (run or {}).get("duration_s"),
                "progress": _progress(dict(m, run=run), running_by_run, group_tables.get(m["task_collection"], 0)),
                "speed_tables_per_min": (q.live_speed(p, now) or {}).get("tables_per_min") if live and p
                else (run or {}).get("speed_tables_per_min"),
                "parallelism": (run or {}).get("parallelism"), "cluster_id": (run or {}).get("cluster_id"),
                "failed": q._count(p or {}, "tables_failed") if live else q._count(run or {}, "failed"),
            })
    return checks + sorted(rows, key=lambda r: (0 if r["status"]["tone"] == "run" else 1 if r["state"] == "waiting"
                                                else 2,
                                                r["offset_minutes"], r["task_collection"], r["extra"]))


def status(job: dict, running: Optional[dict], last: Optional[dict], top_cause: Optional[str],
           warnings: List[str]) -> dict:
    """A job's Status cell: its running state and its issues together. Running (with failing tables, the
    progress, expected end and top cause), else failing (paused or not), late while paused, late, paused,
    ended without finishing, or current. A paused job whose databases are only late shows late, not failing."""
    failing, late = q._count(job, "state_failing"), q._count(job, "late_databases")
    if running:
        p = running["progress"]
        checked = p["unchanged"] + p["loaded"]
        parts = [f"{100 * checked // p['total'] if p['total'] else 0}% checked"]
        if failing:
            parts.append(f"{failing:,} {'table' if failing == 1 else 'tables'} failing")
        return {"label": running["outcome"]["label"], "tone": "run", "detail": parts,
                "expected_end": running.get("expected_end"),
                "cause": top_cause if failing else None}
    paused = {"detail": ["paused"], "paused_since": job.get("updated_at")} if job.get("paused") else {"detail": []}
    if failing:
        return {"label": f"Failing: {failing:,} {'table' if failing == 1 else 'tables'}", "tone": "bad", **paused,
                "cause": top_cause}
    if job.get("paused") and late:
        return {"label": f"Late: {late:,} {'database' if late == 1 else 'databases'}", "tone": "warn", **paused,
                "cause": None}
    if late:
        return {"label": f"Late: {late:,} {'database' if late == 1 else 'databases'}", "tone": "warn",
                "detail": warnings[:1], "cause": None}
    if job.get("paused"):
        return {"label": "Paused", "tone": "neutral", "detail": [], "cause": None}
    if last and last["outcome"]["label"] == "Ended without finishing":
        return {"label": "Ended without finishing", "tone": "bad", "detail": [], "cause": None}
    return {"label": "Current", "tone": "ok", "detail": [], "cause": None}


KEPT, DELETING, DELETE_FAILED, DELETE_GONE = "kept", "deleting", "failed", "gone"


def _aware(value) -> datetime:
    value = _when(value)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def expires_at(deleted_at, retention_days: int) -> datetime:
    """When a deleted job's retention runs out: the purge deletes it from then on."""
    return _aware(deleted_at) + timedelta(days=retention_days)


def days_left(deleted_at, retention_days: int, now: datetime) -> int:
    """Whole days until the retention runs out, rounded up; 0 once it has (the daily purge takes the job then)."""
    return max(0, math.ceil((expires_at(deleted_at, retention_days) - now).total_seconds() / 86400))


def delete_state(job: dict, tables_left: bool, run: Optional[dict], now: datetime, retention_days: int) -> dict:
    """Where a deleted job stands. ``kept`` until its purge starts (the daily purge takes it once the retention has run
    out, Delete now at once), with the days left. ``deleting`` while its delete job run goes, ``failed`` with the reason
    when a start or a run failed. ``gone`` once the purge finished: the list leaves the job. A run that finished with
    tables still there is a failure, since the purge should have removed them. ``run`` is the job's latest delete job
    run, None when none has started."""
    options = sch.options_of(job)
    state = {"drop_targets": bool(options.get("delete_drop_targets")), "run": run,
             "expires_at": expires_at(job["deleted_at"], retention_days).isoformat(),
             "days_left": days_left(job["deleted_at"], retention_days, now)}
    if options.get("delete_start_error"):
        return {**state, "state": DELETE_FAILED, "reason": options["delete_start_error"], "run": None}
    if run is None:
        return {**state, "state": KEPT, "reason": None}
    if is_active(run):
        return {**state, "state": DELETING, "reason": None}
    if run.get("state") == "TERMINATED" and run.get("result") == "SUCCESS":
        if tables_left:
            return {**state, "state": DELETE_FAILED, "reason": "the delete job finished, but tables are still there"}
        return {**state, "state": DELETE_GONE, "reason": None}
    how = run.get("result") or run.get("state") or "without a result"
    return {**state, "state": DELETE_FAILED, "reason": run.get("message") or f"the delete job ended {how}"}


def kept_text(days: int) -> str:
    """How long a kept deleted job has left, in words for a status cell."""
    if days <= 0:
        return "due for purge"
    return f"{days} {'day' if days == 1 else 'days'} left"


def delete_status(delete: dict) -> dict:
    """A deleted job's Status cell: kept (Deleted, with the days left), its delete job running, or its delete failed."""
    if delete["state"] == KEPT:
        return {"label": "Deleted", "tone": "neutral", "detail": [kept_text(delete["days_left"])], "cause": None}
    if delete["state"] == DELETING:
        return {"label": "Deleting", "tone": "run", "detail": ["its delete job is running"], "cause": None}
    return {"label": "Delete failed", "tone": "bad", "detail": [delete["reason"]] if delete.get("reason") else [],
            "cause": None}


def last_finished(summaries: List[dict]) -> Optional[dict]:
    """A job's last finished job run (newest first): the newest one that ran. A trigger that did not run (a paused
    job's, say) is never it; None when none of them ran."""
    return next((s for s in summaries if s["outcome"]["label"] not in (*q.RUNNING_OUTCOMES, "Not run")), None)


def newest_slots(runs: List[dict], job: dict, members: List[dict]) -> List[datetime]:
    """The job runs some group runs of a job belong to (job_runs_of_run), newest slot first: a late run of an
    older slot can start after a run of the newest."""
    slots = {datetime.fromisoformat(p["slot"]) for r in runs for p in job_runs_of_run(r, [job], members)}
    return sorted(slots, reverse=True)


def earlier_last(summaries: List[dict]) -> Optional[dict]:
    """A job's last job run that ran, from the job runs of its newest slot found before its recent triggers; None
    when none ran. Only that job run is read, so its failures in a row are at least its own (one, or none)."""
    last = last_finished(summaries)
    if not last:
        return None
    return dict({k: v for k, v in last.items() if k != "placed"},
                failed_in_a_row=1 if last.get("failed_tables") else 0, failed_in_a_row_at_least=True)


def consecutive_failed(summaries: List[dict]) -> int:
    """How many of a job's latest finished job runs in a row had failed tables (newest first). Triggers that
    did not run (Not run) are passed over, as they say nothing about the tables."""
    count = 0
    for s in summaries:
        if s["outcome"]["label"] in (*q.RUNNING_OUTCOMES, "Not run"):
            continue
        if not s["failed_tables"]:
            break
        count += 1
    return count


def needs_attention(job: dict) -> bool:
    """Whether a job needs a look: tables failing now, or its last ingestion run ended without finishing."""
    last = job["last_job_run"]
    return bool(job["state_failing"]) or bool(last and last["outcome"]["label"] == "Ended without finishing")


def jobs_view(schedules: List[dict], members: List[dict], states: List[dict], running: List[dict],
              runs: List[dict], stats_by_group: Dict[str, dict], now: datetime,
              causes_by_job: Optional[Dict[int, List[dict]]] = None,
              earlier_last_by_job: Optional[Dict[int, dict]] = None, deleting: Iterable[str] = ()) -> dict:
    """Every ingestion job as a row (sums, cadence, next start, last finished job run, the running one, and
    Status), "Not in any job", the running job runs, and the jobs that need attention. `runs` are the group
    runs of the last RECENT_JOB_RUNS triggers of every job, with their work; `stats_by_group` each group's
    run-time percentiles and strictest freshness target (queries.collection_run_stats_sql); `causes_by_job`
    the grouped failures (queries.group_errors) of each job's last job run with failed tables;
    `earlier_last_by_job` the last job run that ran of each job none of whose recent triggers ran, found before
    the window (main._last_ran). Running group runs of groups in no job are listed apart, as they belong to no
    job run."""
    causes_by_job, earlier_last_by_job = causes_by_job or {}, earlier_last_by_job or {}
    p50_by_group = {g: st.get("p50_s") for g, st in stats_by_group.items()}
    job_sums, not_in_job = rollup.jobs(states, schedules, members, deleting)
    groups = rollup.groups(states)
    group_tables = {name: g["enabled_tables"] for name, g in groups.items()}
    running_by_run = {r["run_id"]: r for r in running}
    active = [m for m in members if m.get("removed_at") is None]
    rows, running_job_runs = [], []
    for s, sums in zip(schedules, job_sums):
        cadence = int(s["cadence_minutes"])
        start = window(cadence, RECENT_JOB_RUNS, now)
        member_groups = {m["task_collection"] for m in members if m["schedule_id"] == s["id"]}
        oldest_running = [_when(r["started_at"]) for r in running if r["task_collection"] in member_groups and r.get("started_at")]
        if oldest_running:     # a group run overrunning the window still belongs to its job run
            start = min(start, min(oldest_running) - timedelta(minutes=cadence))
        placed = current_and_past(s, members, runs, start, now)     # removed members keep their past job runs
        summaries = [summarize(s, jr, running_by_run, group_tables, p50_by_group, now) for jr in placed]
        current = next((x for x in summaries if x["outcome"]["label"] in q.RUNNING_OUTCOMES), None)
        last = last_finished(summaries) or earlier_last_by_job.get(s["id"])
        mine = [m for m in active if m["schedule_id"] == s["id"]]
        causes = causes_by_job.get(s["id"]) or []
        warnings = [w for m in mine for w in sch.warnings(cadence, (stats_by_group.get(m["task_collection"]) or {}).get("p95_s"),
                                                           (stats_by_group.get(m["task_collection"]) or {}).get("min_sla_minutes"))]
        next_starts = [sch.next_run(cadence, int(m["offset_minutes"]), s.get("timezone") or "UTC", now) for m in mine]
        row = {**sums, "description": sch.describe(cadence, sch.start_minute(s), s.get("timezone") or "UTC"),
               "next_start": min(next_starts).isoformat() if next_starts and not s.get("paused") else None,
               "last_job_run": last, "running_job_run": current,
               "failed_in_a_row": (consecutive_failed(summaries) if last_finished(summaries) or not last
                                   else last.get("failed_in_a_row", 0)),
               "causes": causes[:3]}
        row["status"] = status(row, current, last, causes[0]["label"] if causes else None, warnings)
        if s.get("delete"):
            row.update(status=delete_status(s["delete"]), next_start=None, delete=s["delete"])
        rows.append(row)
        running_job_runs += [x for x in summaries if x["outcome"]["label"] in q.RUNNING_OUTCOMES]
    rows.sort(key=lambda j: (-1 if j["running_job_run"] and j["state_failing"] else 0 if j["state_failing"] else
                             1 if j["late_databases"] else 2, str(j["name"]).lower()))
    attention = [j for j in rows if needs_attention(j)]
    attention.sort(key=lambda j: (-q._count(j, "state_failing"), str(j["name"]).lower()))
    running_job_runs.sort(key=lambda r: r.get("started_at") or r["slot"])
    totals = {**{k: sum(q._count(g, k) for g in groups.values())
                 for k in ("enabled_tables", "no_sla_tables", "late_databases") + rollup.STATES},
              "groups": len(groups), "databases": len({(s["server"], s["src_database"]) for s in states
                                                        if q._count(s, "tables")}),
              "servers": len({s["server"] for s in states if q._count(s, "tables")}),
              "jobs": len(rows), "running_jobs": sum(1 for j in rows if j["running_job_run"]),
              "failing_jobs": sum(1 for j in rows if j["state_failing"]),
              "late_jobs": sum(1 for j in rows if j["late_databases"])}
    unscheduled = set((not_in_job or {}).get("groups") or [])
    running_not_in_job = [dict(r, outcome=q.run_outcome({"status": "running"}, r)) for r in running
                          if r["task_collection"] in unscheduled]
    return {"jobs": rows, "not_in_job": not_in_job, "running_job_runs": running_job_runs,
            "running_not_in_job": running_not_in_job, "attention": attention, "totals": totals}


def job_runs_of_run(run: dict, schedules: List[dict], members: List[dict],
                    first_started: Optional[Dict[str, object]] = None) -> List[dict]:
    """The job runs a group run belongs to, one per schedule its group was in when it started: the slot of
    its member's latest trigger at or before the run's start. A check schedule's one wrapper fires at the slot,
    and only a check schedule has the check job's own records (as schedules.job_runs places them).
    first_started: per group, the first start in the run's check job run or launch. On a check schedule the run
    shares the trigger of its members' first one, and the member must have belonged at that trigger rather than
    at the run's own start."""
    own = _when(run.get("started_at"))
    out = []
    if own is None:
        return out
    check = sch.is_check(run)
    for s in schedules:
        check_variant = s.get("job_variant") == sch.CHECK_VARIANT
        if check and not check_variant:
            continue
        mine = {m["task_collection"] for m in members if m["schedule_id"] == s["id"]}
        firsts = [_when(t) for g, t in (first_started or {}).items() if g in mine and t] if check_variant else []
        started = min(firsts + [own])
        for m in members:
            if m["schedule_id"] != s["id"] or m["task_collection"] != run["task_collection"]:
                continue
            cadence, tz, start = int(s["cadence_minutes"]), s.get("timezone") or "UTC", sch.start_minute(s)
            offset = start % cadence if check_variant else int(m["offset_minutes"])
            fires = sch.fire_times(cadence, offset, tz, started - timedelta(minutes=cadence + 1),
                                   started + timedelta(seconds=1))
            if not fires:
                continue
            at = fires[-1] if firsts else started
            added, removed = _when(m.get("added_at")), _when(m.get("removed_at"))
            if (added is not None and at < added) or (removed is not None and at >= removed):
                continue
            out.append({"job_id": s["id"], "job_name": s["name"],
                        "slot": sch.slot_of(fires[-1], sch.stagger_of(offset, cadence, start), tz).isoformat()})
    return out


# ---- Running now: a job run as it happens, from the Jobs API ----------------------------------------------------
# Lakebase records a group run only once its cluster is up and the run has started; until then (and for the check
# job, which records nothing of its own) only the Jobs API knows it is going. These read the job's runs there.

ACTIVE_STATES = {"QUEUED", "PENDING", "RUNNING", "TERMINATING", "BLOCKED", "WAITING_FOR_RETRY"}


def is_active(run: dict) -> bool:
    return run.get("state") in ACTIVE_STATES


def check_run_of(run: dict, schedule_id: int, groups: Iterable[str]) -> bool:
    """Whether a check job run is this ingestion job's: started with its schedule_id, or with a group list that is
    its groups (a run over every collection, with no list, belongs to no one job)."""
    params = run.get("parameters") or {}
    if str(params.get("schedule_id") or "").strip():
        return str(params["schedule_id"]).strip() == str(schedule_id)
    named = {g.strip() for g in str(params.get("task_collections") or "").split(",") if g.strip()}
    return bool(named) and named <= set(groups)


IDLE, STARTING, RUNNING, CANCELLING = "idle", "starting", "running", "cancelling"
# How long Databricks may take to start a run, or to end the runs of a cancel, before the page says plainly that
# Databricks has not confirmed it. The page keeps Run now off meanwhile: a run is never shown as ended before it is.
START_CONFIRM_S = 15 * 60
CANCEL_CONFIRM_S = 10 * 60


def run_phase(active: List[dict], now: datetime) -> dict:
    """Where a job's Run now and Cancel run stand, from its active Databricks runs as the poller records them: idle
    when none is active; cancelling while any is terminating, until every one has ended; running once one is RUNNING;
    starting while the rest are queued or pending. A run dict has state, started, seen (when its state last changed),
    run_id and url. ``timed_out`` says Databricks has not confirmed the change within its limit."""
    if not active:
        return {"phase": IDLE, "since": None, "timed_out": False, "run_id": None, "run_url": None}
    terminating = [r for r in active if r.get("state") == "TERMINATING"]
    running = [r for r in active if r.get("state") == "RUNNING"]
    if terminating:
        phase, limit, runs = CANCELLING, CANCEL_CONFIRM_S, terminating
        since_of = [r.get("seen") or r.get("started") for r in terminating]
    elif running:
        phase, limit, runs = RUNNING, None, running
        since_of = [r.get("started") or r.get("seen") for r in running]
    else:
        phase, limit, runs = STARTING, START_CONFIRM_S, active
        since_of = [r.get("started") or r.get("seen") for r in active]
    moments = [_when(v) for v in since_of if v]
    moments = [m if m.tzinfo else m.replace(tzinfo=timezone.utc) for m in moments]
    since = min(moments, default=None)
    first = min(runs, key=lambda r: r.get("started") or "")
    timed_out = bool(limit and since is not None and (now - since).total_seconds() > limit)
    return {"phase": phase, "since": since.isoformat() if since else None, "timed_out": timed_out,
            "run_id": first.get("run_id"), "run_url": first.get("url")}


def run_state_label(run: dict) -> dict:
    """A Databricks run's state in the app's words, as the Jobs UI shows it: Starting while its cluster comes up."""
    state, result = run.get("state"), run.get("result")
    if state in ("QUEUED", "BLOCKED", "WAITING_FOR_RETRY"):
        return {"label": "Queued", "tone": ""}
    if state == "PENDING":
        return {"label": "Starting", "tone": "run", "note": "its cluster is starting"}
    if state == "TERMINATING":
        return {"label": "Cancelling", "tone": "run"}
    if state == "RUNNING":
        return {"label": "Running", "tone": "run"}
    if result == "SUCCESS":
        return {"label": "Succeeded", "tone": "ok"}
    if result in ("CANCELED", "CANCELLED"):
        return {"label": "Cancelled", "tone": "neutral"}
    if state == "SKIPPED" or result == "EXCLUDED":
        return {"label": "Skipped", "tone": "neutral"}
    return {"label": "Failed", "tone": "bad"} if result else {"label": state or "Unknown", "tone": ""}


def live_view(wrappers: List[dict], checks: List[dict], ingest_runs: List[dict], groups: Iterable[str],
              found: List[dict], stamped: Optional[int], running: List[dict], now: datetime) -> dict:
    """The job run going now, as the Jobs UI shows a run with its tasks: the wrapper runs, then (check kind) the
    check and what it found, then the group runs it started, each with its state and, once Lakebase has it, its
    progress. Nothing when none of them is going. `ingest_runs` are the ingestion job's runs (active and recent);
    the group runs shown are this job's groups' runs that are going or began since the run being shown did."""
    groups = set(groups)
    mine = [r for r in ingest_runs if (r.get("parameters") or {}).get("task_collection") in groups]
    check = max(checks, key=lambda r: r.get("started") or "", default=None)
    going = [r for r in wrappers + checks + mine if is_active(r)]
    if not going:
        return {"active": False, "wrappers": [], "check": None, "found": None, "group_runs": [], "read_at": now.isoformat()}
    since = min(r.get("started") or now.isoformat() for r in going + ([check] if check else []))
    shown = [r for r in mine if is_active(r) or (r.get("started") or "") >= since]
    progress = {str(p.get("job_run_id")): p for p in running}
    group_runs = []
    for r in sorted(shown, key=lambda r: ((r.get("parameters") or {}).get("task_collection") or "", r.get("started") or "")):
        p = progress.get(str(r["run_id"]))
        group_runs.append({
            "task_collection": r["parameters"]["task_collection"], "run_id": r["run_id"], "url": r.get("url"),
            "started": r.get("started"), "ended": r.get("ended"), "status": run_state_label(r), "active": is_active(r),
            "work_id": r["parameters"].get("work_id") or None,
            "progress": {"done": int(p.get("tables_checked") or 0), "planned": int(p.get("enabled_tables") or 0),
                         "failed": int(p.get("tables_failed") or 0), "group_run_id": p.get("run_id")} if p else None})
    with_work = {f["task_collection"]: int(f["databases"]) for f in found}
    return {
        "active": True,
        "wrappers": [dict(w, status=run_state_label(w)) for w in wrappers if is_active(w)],
        "check": dict(check, status=run_state_label(check)) if check else None,
        "found": None if not check else {
            "groups_with_work": len(with_work), "databases_with_work": sum(with_work.values()),
            "unchanged_databases": stamped, "groups": len(groups), "started": len(group_runs)},
        "group_runs": group_runs, "read_at": now.isoformat()}
