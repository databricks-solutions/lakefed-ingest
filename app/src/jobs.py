"""Databricks Jobs operations for the app (the app's service principal holds CAN_MANAGE_RUN on the
ingestion, setup and discovery jobs through the app's `job` resources: view, run, and cancel; not edit)."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import List, Optional

from lakebase import workspace

INGEST_JOB_ID = os.environ.get("INGEST_JOB_ID")
SETUP_JOB_ID = os.environ.get("SETUP_JOB_ID")
DISCOVER_JOB_ID = os.environ.get("DISCOVER_JOB_ID")
# The check job (lakefed_ingest_check): a schedule of the check variant starts it with all its members.
CHECK_JOB_ID = os.environ.get("CHECK_JOB_ID")
# The copies job (lakefed_ingest_cluster_copies): makes the copies of the ingestion job that app jobs with their own
# cluster run. The app passes its own service principal (DATABRICKS_CLIENT_ID, set by Databricks Apps) for Can Manage.
CLUSTER_COPIES_JOB_ID = os.environ.get("CLUSTER_COPIES_JOB_ID")
APP_PRINCIPAL = os.environ.get("DATABRICKS_CLIENT_ID")
# The delete job (lakefed_ingest_delete_job): deletes a deleted app job's groups and, if asked, their target tables,
# as the deployer, since the app has no DELETE privilege on the Lakebase tables.
DELETE_JOB_ID = os.environ.get("DELETE_JOB_ID")
# The running app's own name, set by Databricks Apps: who may manage the app is read from its permissions.
APP_NAME = os.environ.get("DATABRICKS_APP_NAME")
# The source this app was deployed from: the bundle's git commit, or what the deploy passed (app_version).
APP_VERSION = os.environ.get("APP_VERSION") or None
# Who manages the app besides workspace admins: users or groups, comma separated (the bundle's app_managers, by default
# the deployer). The app's service principal can't read its own app's access list without Can Manage on the app, which
# it isn't given, so this list is how Manage knows its managers.
APP_MANAGERS = {name.strip().lower() for name in os.environ.get("APP_MANAGERS", "").split(",") if name.strip()}
# How many days a deleted app job is kept for a Restore before the delete job's daily run purges it. Must equal the
# engine bundle's deleted_job_retention_days (the delete job purges by its own variable).
DELETED_JOB_RETENTION_DAYS = int(os.environ.get("DELETED_JOB_RETENTION_DAYS") or 7)
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


def schedule_variants() -> dict:
    """What a schedule's wrappers can start: the ingestion job variants, plus ``check`` (one wrapper for the
    whole schedule, starting the check job with every member) when CHECK_JOB_ID is set."""
    import schedules
    out = ingest_variants()
    if CHECK_JOB_ID:
        out[schedules.CHECK_VARIANT] = CHECK_JOB_ID
    return out


def schedule_client():
    """Jobs API client for schedule sync and the wrapper jobs' runs."""
    import schedules
    return schedules.JobsClient(workspace())


def ts(ms) -> Optional[str]:
    """Epoch milliseconds (as the Jobs API reports times) to ISO 8601 UTC."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms else None


def workspace_url() -> str:
    """The workspace's address, for links out of the app to a run or a cluster; empty when it can't be read."""
    try:
        return workspace().config.host.rstrip("/")
    except Exception:
        return ""


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
        "url": f"{workspace_url()}/jobs/{j.job_id}",
    }


# Every action on an existing job follows Databricks' own job permissions for the signed-in user: the levels in
# Databricks' order, what each action needs (as the Jobs UI asks: run from Can Manage Run, change settings or the
# schedule and delete from Can Manage), and how a level reads on the page.
PERMISSION_RANK = {"CAN_VIEW": 1, "CAN_MANAGE_RUN": 2, "CAN_MANAGE": 3, "IS_OWNER": 3}
ACTION_NEEDS = {"run": ("starting a run", "CAN_MANAGE_RUN"), "change": ("changing the job or its tables", "CAN_MANAGE"),
                "delete": ("deleting", "CAN_MANAGE")}
PERMISSION_LABEL = {"CAN_VIEW": "Can View", "CAN_MANAGE_RUN": "Can Manage Run", "CAN_MANAGE": "Can Manage",
                    "IS_OWNER": "Is Owner", "CAN_USE": "Can Use"}


def highest_permission(acl: List[dict], user: str, groups: List[str]) -> Optional[str]:
    """The user's highest level on a job from its access list (Permissions API entries as dicts), counting entries
    for the user and for any of their groups; a workspace admin manages every job. Without a user there is no level
    at all: entries for groups and service principals have no user name, and must never match a blank one."""
    email = (user or "").strip().lower()
    if not email:
        return None
    if "admins" in groups:
        return "CAN_MANAGE"
    member_of = set(groups) | {"users"}
    levels = [p["permission_level"] for entry in acl
              if (entry.get("user_name") or "").strip().lower() == email or entry.get("group_name") in member_of
              for p in entry.get("all_permissions") or []]
    return max(levels, key=lambda level: PERMISSION_RANK.get(level, 0), default=None)


def action_refusal(action: str, level: Optional[str], job_label: str) -> Optional[str]:
    """Why a user with this level on the job may not take the action, or None when they may."""
    doing, needs = ACTION_NEEDS[action]
    if PERMISSION_RANK.get(level or "", 0) >= PERMISSION_RANK[needs]:
        return None
    have = f"you have {PERMISSION_LABEL.get(level, level)}" if level else "you have no access to it"
    return f"{doing} needs {PERMISSION_LABEL[needs]} on {job_label}; {have}"


# A page asks for every button's permission at once and each action checks again, so reads are kept briefly: a grant
# or revoke in Databricks shows here within this many seconds.
PERMISSION_CACHE_S = 20
_permission_cache: dict = {}


def _cached(key, read):
    now = time.monotonic()
    hit = _permission_cache.get(key)
    if hit and now - hit[0] < PERMISSION_CACHE_S:
        return hit[1]
    value = read()
    _permission_cache[key] = (now, value)
    return value


def scim_string(value: str) -> str:
    """A value as a SCIM filter string literal (RFC 7644: a JSON string), so quotes or backslashes in it can't change
    the filter."""
    return json.dumps(str(value))


# Nested groups: a user in team-x, which is in data-eng, gets data-eng's grants in Databricks. SCIM lists a user's
# direct groups only, so the app follows each group's own memberships, this deep and this many groups at most. A
# lookup that fails or runs past the caps keeps what was found, so it can only refuse, never grant, too much.
NESTED_GROUP_DEPTH = 5
NESTED_GROUP_LIMIT = 200


def user_groups(user: str) -> List[str]:
    """The workspace groups of a user, by the email the Apps proxy forwards: their direct groups and the groups those
    are in. No user, no groups."""
    user = (user or "").strip()
    if not user:
        return []

    def read():
        found = workspace().users.list(filter=f"userName eq {scim_string(user)}", attributes="groups")
        direct = [g for u in found for g in (u.groups or [])]
        names = {g.display for g in direct if g.display}
        frontier, seen = [g.value for g in direct if g.value], set()
        for _ in range(NESTED_GROUP_DEPTH):
            parents = []
            for group_id in frontier:
                if group_id in seen or len(seen) >= NESTED_GROUP_LIMIT:
                    continue
                seen.add(group_id)
                try:
                    group = workspace().groups.get(group_id)
                except Exception:
                    continue
                for parent in group.groups or []:
                    if parent.display:
                        names.add(parent.display)
                    if parent.value:
                        parents.append(parent.value)
            frontier = parents
            if not frontier:
                break
        return sorted(names)
    return _cached(("groups", user), read)


def user_job_permission(job_id, user: str) -> Optional[str]:
    """The signed-in user's highest permission on a Databricks job, read by the app's service principal (which
    needs Can Manage on the job to read its access list)."""
    def read():
        acl = workspace().permissions.get("jobs", str(int(job_id))).access_control_list or []
        return [entry.as_dict() for entry in acl]
    return highest_permission(_cached(("acl", str(int(job_id))), read), user, user_groups(user))


def user_app_permission(user: str) -> Optional[str]:
    """The signed-in user's highest permission on this app (Can Use or Can Manage). A workspace admin, and a user or a
    member of a group in APP_MANAGERS, manages it, with no call to Databricks. Anyone else is asked of the app's access
    list as its service principal reads it; when it may not read it (it has no Can Manage on its own app), a signed-in
    user has Can Use, since the Apps proxy let them in."""
    groups = user_groups(user)
    if not (user or "").strip():
        return None
    if "admins" in groups or {user.strip().lower(), *(g.lower() for g in groups)} & APP_MANAGERS:
        return "CAN_MANAGE"

    def read():
        acl = workspace().permissions.get("apps", str(APP_NAME)).access_control_list or []
        return [entry.as_dict() for entry in acl]
    try:
        return highest_permission(_cached(("app-acl", str(APP_NAME)), read), user, groups)
    except Exception:
        return "CAN_USE"


def app_deployment() -> dict:
    """The app's active deployment as Databricks Apps records it: its id, when it was made and by whom."""
    deployment = workspace().apps.get(str(APP_NAME)).active_deployment
    if deployment is None:
        return {}
    return {"id": deployment.deployment_id, "created": deployment.create_time, "creator": deployment.creator}


def job_settings(job_id) -> dict:
    """A job's settings as the Jobs API returns them (a plain dict), for compute.describe."""
    return workspace().jobs.get(int(job_id)).settings.as_dict()


def run_row(run) -> dict:
    """A Jobs API run (the SDK's Run) as the app reads a run: its ids, parameters (a parameter the run wasn't given
    takes its default, as a run of a schedule ingests its default task_collection), state, times and page."""
    params = {p.name: p.value if p.value is not None else p.default for p in (getattr(run, "job_parameters", None) or [])}
    st = run.state
    return {
        "run_id": run.run_id,
        "job_id": run.job_id,
        "parameters": params,
        "task_collection": params.get("task_collection"),
        "state": st.life_cycle_state.value if st and st.life_cycle_state else None,
        "result": st.result_state.value if st and st.result_state else None,
        "message": st.state_message if st else None,
        "started": ts(run.start_time),
        "ended": ts(run.end_time),
        "trigger": run.trigger.value if run.trigger else None,
        "url": run.run_page_url,
    }


def runs(job_id, active_only: bool = False, limit: int = 25) -> List[dict]:
    """A job's runs, newest first, from the Jobs API (at most ``limit``)."""
    rows = []
    # The Jobs API caps a page at 25 runs; the SDK pages through, and we stop at `limit`.
    for r in workspace().jobs.list_runs(job_id=int(job_id), active_only=active_only, limit=min(limit, 25)):
        rows.append(run_row(r))
        if len(rows) >= limit:
            break
    return rows


def get_run(run_id):
    """One Jobs API run (the SDK's Run); raises NotFound when Databricks has no such run."""
    return workspace().jobs.get_run(int(run_id))


def is_not_found(error: Exception) -> bool:
    """Whether a Jobs API error says the run or job does not exist (as opposed to a failure to ask)."""
    from databricks.sdk.errors import NotFound
    return isinstance(error, NotFound)


def run_now(job_id, job_parameters: dict) -> int:
    waiter = workspace().jobs.run_now(job_id=int(job_id), job_parameters=job_parameters)
    resp = getattr(waiter, "response", waiter)
    run_id = int(resp.run_id)
    import run_poller
    run_poller.note_started(job_id, run_id, job_parameters)
    return run_id


def start_cluster_copies(schedule_id) -> int:
    """Start the copies job for one schedule: it makes, updates or deletes that job's copy of the ingestion job."""
    return run_now(CLUSTER_COPIES_JOB_ID, {"schedule_id": str(int(schedule_id)), "app_service_principal": APP_PRINCIPAL or ""})


def start_draft_purge(ttl_s: int) -> int:
    """Start the delete job's drafts mode: the tables read for choices no job took, discarded or left for ttl_s, go."""
    return run_now(DELETE_JOB_ID, {"purge_drafts": "true", "draft_ttl_s": str(int(ttl_s))})


def start_delete_job(schedule_id, drop_targets: bool) -> int:
    """Start the delete job for one deleted app job: its groups' rows go, and its target tables too when asked."""
    return run_now(DELETE_JOB_ID, {"schedule_id": str(int(schedule_id)), "drop_targets": "true" if drop_targets else "false"})


def start_table_remove(schedule_id, control_ids) -> int:
    """Start the delete job for some tables of one live job: their control rows and their state go, their target tables
    stay (delete_job.delete_table_rows). The job runs as the deployer, as the whole-job delete does."""
    ids = ",".join(str(int(i)) for i in control_ids)
    return run_now(DELETE_JOB_ID, {"schedule_id": str(int(schedule_id)), "control_ids": ids})


_node_types: dict = {}


def node_type_details() -> List[dict]:
    """The workspace's node types a job cluster can use (not deprecated), each with its memory and cores, as the job
    cluster form lists them; read once an hour."""
    now = time.monotonic()
    if not _node_types or now - _node_types["at"] > 3600:
        found = workspace().clusters.list_node_types().node_types or []
        _node_types.update(at=now, details=sorted(
            ({"id": n.node_type_id, "memory_gb": round((n.memory_mb or 0) / 1024), "cores": int(n.num_cores or 0)}
             for n in found if not getattr(n, "is_deprecated", False)), key=lambda d: d["id"]))
    return _node_types["details"]


def node_types() -> List[str]:
    """The workspace's node type ids a job cluster can use."""
    return [d["id"] for d in node_type_details()]


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
    import run_poller
    run_poller.mark_stopping([int(run_id)])


# A run's life cycle as the Jobs API ends it; a run recorded as running whose Databricks run is in one of these
# never finishes its record, so the app closes it (main._close_ended_runs).
ENDED_STATES = ("TERMINATED", "SKIPPED", "INTERNAL_ERROR")
# How long a run's state is kept: a page asks every few seconds while a run is going.
RUN_STATE_CACHE_S = 15
_run_states: dict = {}


def run_ended(run_id) -> Optional[dict]:
    """How a Databricks run ended ({state, result, message, ended}), or None while it is still going. A run the
    Jobs API no longer has counts as ended (state NOT_FOUND). Any other error reading it is raised, so a run is
    never taken for ended on a guess. Read at most every RUN_STATE_CACHE_S seconds per run."""
    from databricks.sdk.errors import NotFound
    now = time.monotonic()
    hit = _run_states.get(str(run_id))
    if hit and now - hit[0] < RUN_STATE_CACHE_S:
        return hit[1]
    try:
        r = workspace().jobs.get_run(int(run_id))
        st = r.state
        state = st.life_cycle_state.value if st and st.life_cycle_state else None
        ended = None if state not in ENDED_STATES else {
            "state": state, "result": st.result_state.value if st.result_state else None,
            "message": st.state_message or None, "ended": ts(r.end_time)}
    except NotFound:
        ended = {"state": "NOT_FOUND", "result": None, "message": None, "ended": None}
    _run_states[str(run_id)] = (now, ended)
    return ended


def forget_run_states() -> None:
    """Read every run's state again next time: after a cancel, so the page shows it at once."""
    _run_states.clear()
