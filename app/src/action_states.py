"""What an action shows while Databricks has not yet confirmed its work, and how it ended. An action never shows its end
state until everything under it has finished: a Databricks run it started is pending until the run has ended (then
succeeded, or failed with Databricks' message), and a write is done only once the Databricks jobs it changed are
synced. Plain functions: no database and no workspace is read here; the callers pass the run's recorded state."""
from datetime import datetime, timezone
from typing import Optional, List

from run_poller import ENDED_STATES, NOT_FOUND

PENDING, SUCCEEDED, FAILED, TIMEOUT = "pending", "succeeded", "failed", "timeout"


def run_outcome(run: Optional[dict], now: datetime, limit_s: Optional[int] = None) -> dict:
    """Where a Databricks run an action started stands: ``pending`` while Databricks lists it as going, ``timeout``
    once it has gone on past limit_s from its start (Databricks has not confirmed its end, so it is still not done),
    ``succeeded`` when it ended with SUCCESS, else ``failed`` with the message Databricks gives. ``run`` is the state
    the run poller records (run_id, state, result, message, url, started); None means the run is not recorded yet,
    which is still pending."""
    if run is None:
        return {"state": PENDING, "reason": None, "url": None, "run_id": None}
    state, result = run.get("state"), run.get("result")
    outcome = {"run_id": run.get("run_id"), "url": run.get("url"), "reason": None}
    if state == NOT_FOUND:
        return {**outcome, "state": FAILED, "reason": "Databricks has no record of the run"}
    if state in ENDED_STATES:
        if result == "SUCCESS":
            return {**outcome, "state": SUCCEEDED}
        return {**outcome, "state": FAILED, "reason": run.get("message") or f"the run ended {result or state}"}
    started = _utc(run.get("started"))
    if limit_s and started and (now.astimezone(timezone.utc) - started).total_seconds() > limit_s:
        minutes = max(1, round(limit_s / 60))
        return {**outcome, "state": TIMEOUT,
                "reason": f"Databricks has not reported it ended after {minutes} minutes: it is still listed as "
                          f"{str(state).lower()}"}
    return {**outcome, "state": PENDING}


def sync_failures(results: Optional[List[dict]]) -> List[str]:
    """The wrapper jobs that could not be synced, as the text a page shows: each collection (when there is one) and why.
    Empty when every job was synced."""
    failures = []
    for result in results or []:
        if result.get("action") != "error":
            continue
        collection, detail = result.get("task_collection"), result.get("detail") or "its Databricks job was not synced"
        failures.append(f"{collection}: {detail}" if collection else detail)
    return failures


def _utc(value) -> Optional[datetime]:
    if not value:
        return None
    parsed = value if isinstance(value, datetime) else _parse(str(value))
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _parse(text: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
