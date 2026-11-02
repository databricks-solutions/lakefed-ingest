"""One copy of the ingestion job per app job that sets its own cluster (pure; run by cluster_copies.ipynb).

An app job's runs start the bundle's ``lakefed_ingest`` job, and neither a ``run_job`` task nor run-now can change
a job's cluster. So an app job whose cluster differs (its schedule's ``options.cluster``: driver type, worker type,
worker count) gets its own copy of ``lakefed_ingest``: the same tasks, parameters and libraries, with only those
cluster settings changed. The copy is made by the ``lakefed_ingest_cluster_copies`` job, which runs as the deployer
like ``lakefed_ingest``, so the copy runs with exactly the ingestion job's rights. An app job at the defaults uses
the bundle's job, and its copy, if any, is deleted.

The bundle's job is copied again on every run, so a redeploy reaches the copies the next time the job runs (the app
starts it on every cluster change and every Sync jobs).

A copy is found by its tags before a new one is made (``adoptable``), so a copy whose id was never recorded is
adopted rather than duplicated."""
import copy
import uuid
from typing import Dict, List, Optional

CLUSTER_KEYS = ("driver_node_type_id", "node_type_id", "num_workers")
# Tags on a copy: the schedule it serves, and the app deployment (Lakebase instance/database) it belongs to, so a
# copies job never touches another deployment's copies.
TAG_SCHEDULE = "lakefed_ingest_copy_of_schedule"
TAG_OWNER = "lakefed_ingest_app"
# Parts of the bundle's job a copy leaves out: its own triggers (the app job's wrapper starts it) and the markers
# that make it bundle-managed (a copy is managed by this job instead).
NOT_COPIED = ("schedule", "trigger", "continuous", "deployment", "edit_mode")


def cluster_of(schedule: dict) -> Optional[dict]:
    """The cluster an app job asks for ({key: value} for CLUSTER_KEYS), or None to use the bundle's job."""
    options = schedule.get("options") or {}
    wanted = options.get("cluster") if isinstance(options, dict) else None
    if not wanted:
        return None
    return {k: wanted[k] for k in CLUSTER_KEYS if wanted.get(k) not in (None, "")}


def copy_name(schedule_name: str) -> str:
    return f"lakefed_ingest: {schedule_name}"


def matching_copies(found_jobs: List[dict], schedule_id: int, owner: str) -> List[int]:
    """The job ids, lowest first, of the found jobs whose tags say they are this schedule's copy for this owner.

    ``found_jobs`` are Jobs API list entries from a lookup by ``copy_name``. The name only narrows that lookup, so the
    tags decide: a job with the same name that belongs to another schedule or another deployment, or that has no tags
    of ours, is not a match."""
    matches = []
    for job in found_jobs:
        tags = (job.get("settings") or {}).get("tags") or {}
        if tags.get(TAG_SCHEDULE) == str(schedule_id) and tags.get(TAG_OWNER) == owner[:255]:
            matches.append(int(job["job_id"]))
    return sorted(matches)


def adoptable(found_jobs: List[dict], schedule_id: int, owner: str) -> Optional[int]:
    """The copy to adopt instead of making a new one: the lowest-id match (see matching_copies), or None.

    Adopting it means the plan updates it and records its id, so a copy whose id was never recorded (its run stopped
    before the record was written) is picked up rather than duplicated. A schedule is looked up by name only when it
    has no recorded copy of its own, so a schedule renamed after its copy was made keeps that copy by its recorded
    id."""
    matches = matching_copies(found_jobs, schedule_id, owner)
    return matches[0] if matches else None


def surplus(found_jobs: List[dict], schedule_id: int, owner: str) -> List[dict]:
    """The matches that are not adopted, each as {"job_id", "name", "tags"}, to be listed for someone to remove by
    hand. Every match is an unrecorded copy (a schedule with a recorded copy is not looked up), and the app starts only
    the recorded copy, so a second one is idle; it is never deleted here, as a job in someone's workspace is theirs to
    remove."""
    extra = set(matching_copies(found_jobs, schedule_id, owner)[1:])
    return sorted(({"job_id": int(job["job_id"]), "name": (job.get("settings") or {}).get("name"),
                    "tags": (job.get("settings") or {}).get("tags") or {}}
                   for job in found_jobs if int(job["job_id"]) in extra), key=lambda e: e["job_id"])


def copy_settings(bundle_job: dict, schedule: dict, cluster: dict, owner: str) -> dict:
    """Jobs API 2.2 settings of a schedule's copy: the bundle job's settings with its job clusters set to
    ``cluster`` (a worker count replaces any autoscale range)."""
    settings = {k: v for k, v in copy.deepcopy(bundle_job).items() if k not in NOT_COPIED}
    settings["name"] = copy_name(schedule["name"])
    settings["description"] = (f"Managed by lakefed_ingest_cluster_copies for the app job '{schedule['name']}': a copy "
                               "of lakefed_ingest with its own cluster. Change the cluster in the app's Edit view; "
                               "changes made here are undone by the next copy.")
    for job_cluster in settings.get("job_clusters") or []:
        new_cluster = job_cluster.setdefault("new_cluster", {})
        new_cluster.update({k: v for k, v in cluster.items()})
        if "num_workers" in cluster:
            new_cluster.pop("autoscale", None)
            new_cluster["num_workers"] = int(cluster["num_workers"])
    settings["tags"] = {**(settings.get("tags") or {}), TAG_SCHEDULE: str(schedule["id"]), TAG_OWNER: owner[:255]}
    return settings


def plan(schedules: List[dict], copies: Dict[str, int], bundle_job: dict, owner: str,
         extras: Optional[Dict[str, List[dict]]] = None) -> List[dict]:
    """What to do: {"action": create | update | delete | extra, "schedule_id", "job_id" (update, delete, extra),
    "settings" (create, update), "name" and "tags" (extra)}. ``schedules`` are every schedule to consider (a deleted
    one has ``deleted_at``); ``copies`` maps a schedule id (text) to its copy's job id, as found by its tags;
    ``extras`` maps a schedule id to its unrecorded copies that were not adopted (``surplus``): each is listed as an
    "extra" action, for removal by hand, and nothing is done to it."""
    out = []
    wanted = {str(s["id"]): s for s in schedules if s.get("deleted_at") is None and cluster_of(s)}
    for sid, schedule in sorted(wanted.items()):
        settings = copy_settings(bundle_job, schedule, cluster_of(schedule), owner)
        if sid in copies:
            out.append({"action": "update", "schedule_id": int(sid), "job_id": copies[sid], "settings": settings})
        else:
            out.append({"action": "create", "schedule_id": int(sid), "settings": settings})
    considered = {str(s["id"]) for s in schedules}
    for sid, job_id in sorted(copies.items()):
        if sid in considered and sid not in wanted:
            out.append({"action": "delete", "schedule_id": int(sid), "job_id": job_id})
    for sid, found in sorted((extras or {}).items()):
        out += [{"action": "extra", "schedule_id": int(sid), **extra} for extra in found]
    return out


def principal_of(identity: str) -> dict:
    """The access-control entry for a deployer or app identity. A service principal's user name is its application
    id (a UUID), and the permissions API refuses that as a user, so a UUID is named as a service principal."""
    try:
        uuid.UUID(identity)
    except ValueError:
        return {"user_name": identity}
    return {"service_principal_name": identity}


def copy_permissions(bundle_acl: List[dict], app_principal: Optional[str], owner_user: str) -> List[dict]:
    """A copy's access list: the bundle job's own direct grants (so whoever may run or manage the ingestion job may
    run or manage its copies, as the app's checks expect), the deployer as owner, and the app's service principal
    with Can Manage (it starts the copy and reads its access list)."""
    owner = principal_of(owner_user)
    out = [{**owner, "permission_level": "IS_OWNER"}]
    for entry in bundle_acl:
        direct = [p["permission_level"] for p in entry.get("all_permissions") or []
                  if not p.get("inherited") and p["permission_level"] != "IS_OWNER"]
        principal = {k: entry[k] for k in ("user_name", "group_name", "service_principal_name") if entry.get(k)}
        if direct and principal and principal != owner and principal.get("service_principal_name") != app_principal:
            out.append({**principal, "permission_level": direct[0]})
    if app_principal:
        out.append({"service_principal_name": app_principal, "permission_level": "CAN_MANAGE"})
    return out
