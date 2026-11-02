"""Removing one table from a live ingestion job, as the app says it: what is refused, what is recorded, and how a table's
removal shows on the Failing tables list. Pure, so the rules are tested without Lakebase or Databricks.

The app never deletes. A removal is a request the app records on the job (its ``table_remove`` option, by control id)
and starts the delete job for; the delete job (delete_job.delete_table_rows) deletes the table's control row and its
state as the deployer, and keeps the target table.
"""
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

import rediscovery

# The job's option holding each requested removal: {"<control id>": {"run_id", "requested_at", "requested_by", "error"}}.
OPTION = "table_remove"
# The job's option holding the tables it removed: {<exclusion key>: {"removed_at", "removed_by"}}. Such a table stays
# out of a re-discovery and of whole databases until Add tables names it again.
EXCLUDED = "table_exclude"
# Databricks life cycle states of a run that has ended.
FINISHED_STATES = ("TERMINATED", "SKIPPED", "INTERNAL_ERROR")
# What a removal shows on its row: still going, or failed (the row stays, and it can be asked again).
REMOVING, FAILED = "removing", "failed"
# A removal recorded without a run id for longer than this never got its run started (the app stopped between recording
# it and starting the delete job): it is closed as failed, so Remove can be pressed again.
START_S = 120
NOT_STARTED = "the delete job didn't start: no run was recorded within 2 minutes; press Remove again"
# An Add tables line whose table the job removed, and that wasn't named explicitly: not written.
LEFT_OUT = "left_out"


def confirmation_missing(confirmed: bool) -> Optional[str]:
    """Why a removal is refused without the user's confirmation (the page asks first), or None."""
    return None if confirmed else "confirm the removal first: it deletes the table's ingestion state from its job"


def refusal(deleted: bool, shared_groups: List[str], running: bool, already_removing: bool) -> Optional[str]:
    """Why a removal can't start now, or None. A table's group must be one only this job holds: removing it would
    change the tables of another live job too."""
    if deleted:
        return "this job is deleted: its delete job removes its tables, so they can't be removed one by one"
    if shared_groups:
        return (f"group(s) {', '.join(shared_groups)} also belong to another live ingestion job: remove the table in "
                "that job's delete, or remove the group from the other job first")
    if running:
        return "a run of this job's group is going: wait for it to end, or cancel it, then remove the table"
    if already_removing:
        return "this table's removal is already going"
    return None


def requests(control_ids: List[int], user: str, requested_at: str, run_id: Optional[int] = None,
             error: Optional[str] = None) -> Dict[str, dict]:
    """The removal records for these control ids: whole records, since storing one replaces its entry. Recorded before
    the delete job starts (no run id yet), then again with its run id, or with the error when it didn't start."""
    return {str(control_id): {"run_id": None if run_id is None else int(run_id), "requested_at": requested_at,
                              "requested_by": user or None, "error": error} for control_id in control_ids}


def entries(job_options: dict) -> dict:
    """The job's removal records by control id (string keys), or {} when it has none."""
    return dict((job_options or {}).get(OPTION) or {})


def state_of(entry: Optional[dict], run: Optional[dict], now: Optional[datetime] = None) -> Optional[dict]:
    """How a table's removal shows on its row: {"state": "removing" | "failed", "message"}, or None when there is no
    removal to show. ``run`` is the delete job's run as the Jobs API reports it (state, result, message), or None when
    it can't be read. A run that ended with success shows nothing: the table is gone from the list. With ``now``, a
    removal that never got a run within START_S is failed, so it can be pressed again."""
    if not entry:
        return None
    if entry.get("error"):
        return {"state": FAILED, "message": entry["error"]}
    if entry.get("run_id") is None:
        if now is not None and _unstarted_too_long(entry, now):
            return {"state": FAILED, "message": NOT_STARTED}
        return {"state": REMOVING, "message": None}
    if run is None:
        return {"state": FAILED, "message": "the delete job's run can't be read"}
    if run.get("state") in FINISHED_STATES:
        if run.get("result") == "SUCCESS":
            return None
        return {"state": FAILED, "message": run.get("message") or f"the delete job ended {run.get('result') or 'without a result'}"}
    return {"state": REMOVING, "message": None}


def removing(entries_by_control: dict, control_ids: List[int], runs: Dict[int, Optional[dict]],
             now: Optional[datetime] = None) -> List[int]:
    """The control ids whose removal is still going (their record shows REMOVING), for the refusal of a new one."""
    return [control_id for control_id in control_ids
            if (state := state_of(entries_by_control.get(str(control_id)), runs.get(control_id), now))
            and state["state"] == REMOVING]


def _unstarted_too_long(entry: dict, now: datetime) -> bool:
    """Whether a removal recorded without a run has waited past START_S since it was requested."""
    requested = _as_utc(entry.get("requested_at"))
    return requested is not None and (_as_utc(now) - requested).total_seconds() > START_S


def _as_utc(value) -> Optional[datetime]:
    """A timestamp (a datetime, or its ISO text) as an aware UTC datetime; None when it isn't one."""
    if isinstance(value, datetime):
        parsed = value
    elif value:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def exclusion_key(server: str, database: str, schema: str, table: str) -> str:
    """A table's key in the job's exclusions: its table key (rediscovery.table_key) as JSON text, for an object key."""
    return json.dumps(list(rediscovery.table_key(server, database, schema, table)))


def exclusions_of(options: Optional[dict]) -> Set[tuple]:
    """The tables a job removed, as table keys (rediscovery.table_key), to test with ``in``."""
    return {tuple(json.loads(key)) for key in ((options or {}).get(EXCLUDED) or {})}


def is_excluded(server: str, database: str, schema: str, table: str, excluded: Set[tuple]) -> bool:
    return rediscovery.table_key(server, database, schema, table) in excluded


def exclusion_records(rows: List[dict], user: Optional[str], removed_at: str) -> Dict[str, dict]:
    """The exclusions a removal of these control rows records, keyed as exclusion_key keys them."""
    return {exclusion_key(r["server"], r["src_database"], r["src_schema"], r["src_table"]):
            {"removed_at": removed_at, "removed_by": user or None} for r in rows}


def leave_out(lines: List[dict], excluded: Set[tuple]) -> int:
    """Mark the valid lines of tables a job removed as left out, for an Add tables that didn't name them. Returns how
    many were left out."""
    count = 0
    for line in lines:
        row = line["row"]
        if line["status"] == "valid" and is_excluded(rediscovery.control_server(row), row["src_database"],
                                                     row["src_schema"], row["src_table"], excluded):
            line["status"] = LEFT_OUT
            count += 1
    return count


def line_keys(lines: List[dict]) -> List[str]:
    """The exclusion keys of the valid lines' tables: what Add tables takes back in once it writes them."""
    return sorted({exclusion_key(rediscovery.control_server(line["row"]), line["row"]["src_database"],
                                 line["row"]["src_schema"], line["row"]["src_table"])
                   for line in lines if line["status"] == "valid"})
