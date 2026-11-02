"""Start the ingest runs the check asks for: one run of lakefed_ingest per collection with work.

The default version sweeps one collection per run of the ingestion job and leaves starting those runs
to the caller (``run-now`` with the collection as a job parameter). ``check_launch.ipynb`` is that
caller: it runs the check (``initial_check.run_check``) on serverless, records each collection's
databases with work (``check_work``), then starts one run of the ingestion job per collection with
work, with that list's ``work_id``. A collection with nothing to do starts no run, so a quiet cycle
starts no ingest cluster. Inside each run nothing changes: its tables run on threads and scheduler
pools, as in any sweep.

The runs are started through the Jobs API, the way the operations app's Run now starts them: no For
each, and no new job (every run is a run of the one saved ingestion job). By default the check starts
the runs and ends; each run reports its own result. Before it ends it watches the runs it started for
a short while (``confirm_s``): a run Databricks skips (the ingestion job at its ``max_concurrent_runs``,
with its queue off) ends at once, and the check fails naming it. It starts no more runs than the job
has room for (``capacity``); the rest are reported, not started, and wait for the next cycle. A job with
its queue on has room for every run: Databricks holds those beyond ``max_concurrent_runs`` in the queue
and starts them as runs finish.
``wait`` keeps the older behaviour: poll every run until all have ended.

The calls go through ``jobs``, an object with ``run_now``, ``get_run``, ``cancel_run``,
``active_runs`` and ``max_concurrent_runs`` (``SdkJobs`` in a notebook, a fake in tests). No Spark here.
"""

from __future__ import annotations

import hashlib
import time
from typing import Callable, Dict, Iterable, List, Optional

# A run is over in these life cycle states (the SDK's own wait stops on the same ones).
TERMINAL_STATES = ("TERMINATED", "SKIPPED", "INTERNAL_ERROR")
# Outcomes that need nothing more: the run did its work, it started and is going, the collection's own run,
# already going, has the work, or its databases wait for the next cycle (deferred, reported).
OK_OUTCOMES = ("succeeded", "started", "already_running", "deferred")
# The ingestion job's parameters (resources/lakefed_ingest.yml) a launch hands on to every run, besides
# task_collection and work_id, which each run gets for itself.
RUN_PARAMETERS = (
    "db_instance", "db_name", "parallelism", "secret_scope", "trust_server_certificate", "snapshot_isolation",
    "reseed_on_new_columns", "append_batch_size", "max_seeds_per_run", "freshness_interval_s", "detection_chunk",
    "max_queries_per_source", "table_retries", "table_retry_budget", "fail_on_table_errors", "spark_conf",
)


def run_parameters(values: Dict[str, str]) -> Dict[str, str]:
    """The ingestion job parameters to pass on from ``values`` (the launching notebook's settings): only
    the job's own (``RUN_PARAMETERS``), and only those set, so the job's defaults apply to the rest."""
    return {name: str(values[name]) for name in RUN_PARAMETERS if str(values.get(name) or "").strip()}


def work_id(launch_id: str, collection: str) -> str:
    """The id of one collection's work list in one attempt of the check (``launch_id``, its task run id)."""
    return f"{launch_id}:{collection}"


def run_requests(per_collection: Iterable[dict], parameters: Optional[Dict[str, str]] = None,
                 launch_id: str = "") -> List[dict]:
    """One ingest run per collection with work, for its databases with work.

    ``per_collection`` is ``run_check``'s ``summary["per_collection"]`` (each ``{collection, work}``,
    work as [source, src_database] pairs); ``parameters`` are the job parameters every run gets (the
    tuning settings). A collection without work gets no run. Each request carries its ``work`` (for
    ``StateStore.record_check_work``, before the run starts) and passes its ``work_id`` to the run.
    Returns ``[{collection, databases, work, work_id, job_parameters}]`` in collection order.
    """
    out = []
    for result in sorted(per_collection, key=lambda r: r["collection"]):
        work = sorted({tuple(key) for key in result.get("work") or []})
        if not work:
            continue
        work_list = work_id(launch_id, result["collection"])
        out.append({"collection": result["collection"], "databases": len(work), "work": work, "work_id": work_list,
                    "job_parameters": {**(parameters or {}), "task_collection": result["collection"],
                                       "work_id": work_list}})
    return out


def cycle_requests(per_collection: Iterable[dict], group_size: int, parameters: Optional[Dict[str, str]] = None,
                   launch_id: str = "", running: Optional[Dict[str, Optional[frozenset]]] = None) -> dict:
    """A cycle's runs shared out by its changes (groups B): ``initial_check.plan_cycle`` over the databases with work,
    one ingest run per share, each database whole on one run.

    ``running`` (``active_collections``) leaves out what a run already going has: a database it was given, or a whole
    collection it sweeps without a work list; those are ``deferred``, and the run going ingests them. A run with one
    collection is started as today (``task_collection`` set); a run spanning collections gets ``task_collection``
    blank and its ``work_id``, and takes them in turn on its cluster (ingest.ipynb). Every request's work is recorded
    per collection under its ``work_id`` (``StateStore.record_check_work``) before it starts.

    Returns {requests: [{collection (a label, unique in the cycle), collections, databases, work (per collection),
    tables, first_loads, work_id, job_parameters}], deferred: [[collection, source, src_database]]}."""
    import initial_check     # its planner; imported here so the launcher stays importable on its own
    running = running or {}
    deferred, kept = [], []
    for result in per_collection:
        collection = result["collection"]
        covered = running.get(collection, frozenset())
        weights = []
        for source, database, *rest in result.get("weights") or []:
            if covered is None or (source, database) in covered:
                deferred.append([collection, source, database])
            else:
                weights.append([source, database, *rest])
        kept.append({"collection": collection, "weights": weights})
    requests = []
    for number, run in enumerate(initial_check.plan_cycle(kept, group_size), start=1):
        work: Dict[str, List[tuple]] = {}
        for collection, source, database in run["databases"]:
            work.setdefault(collection, []).append((source, database))
        list_id = f"{launch_id}:run{number}"
        single = run["collections"][0] if len(run["collections"]) == 1 else ""
        requests.append({"collection": single or f"{len(run['collections'])} groups, run {number}",
                         "collections": run["collections"], "databases": len(run["databases"]),
                         "work": {c: sorted(w) for c, w in sorted(work.items())}, "tables": run["tables"],
                         "first_loads": run["first_loads"], "work_id": list_id,
                         "job_parameters": {**(parameters or {}), "task_collection": single, "work_id": list_id}})
    return {"requests": requests, "deferred": sorted(deferred)}


def idempotency_token(launch_id: str, collection: str) -> str:
    """The run-now token for one collection in one attempt of the launching step (``launch_id``, its task
    run id). Databricks starts at most one run per token, so the start call's own retries can't start a
    second run. A retried or repaired step is a new attempt: it starts afresh, and ``active_collections``
    keeps it off collections whose run is still going. At most 64 characters."""
    return "lfi-" + hashlib.sha256(f"{launch_id}:{collection}".encode()).hexdigest()[:60]


def active_collections(runs: Iterable[dict], work_of: Callable[[str, str], set],
                       work_all_of: Optional[Callable[[str], set]] = None) -> Dict[str, Optional[frozenset]]:
    """The collections that already have an ingest run going, from ``jobs.active_runs``, each with the
    databases its run has: a run without a work list has the whole collection (None), else its work list
    as ``work_of(work_id, collection)`` reads it. A run of a cycle's share spanning collections (no
    task_collection, a work list) has every database of its list, read with ``work_all_of(work_id)``. A run still
    starting its cluster has not taken its collection's lock yet, so the check can't see it."""
    covered: Dict[str, Optional[frozenset]] = {}
    for run in runs:
        parameters = run.get("job_parameters", {})
        collection = str(parameters.get("task_collection") or "").strip()
        work = str(parameters.get("work_id") or "").strip()
        if not collection and work and work_all_of is not None:
            for name, source, database in work_all_of(work):
                if covered.get(name, frozenset()) is not None:
                    covered[name] = covered.get(name, frozenset()) | {(source, database)}
            continue
        if not collection or (collection in covered and covered[collection] is None):
            continue
        if not work:
            covered[collection] = None
        else:
            covered[collection] = covered.get(collection, frozenset()) | frozenset(work_of(work, collection))
    return covered


def outcome(state: dict) -> Optional[str]:
    """What a run came to, from ``jobs.get_run``: None while it is going, else succeeded, canceled,
    skipped (Databricks didn't start it, e.g. the job's max_concurrent_runs with its queue off) or failed."""
    life = state.get("life_cycle_state")
    if life not in TERMINAL_STATES:
        return None
    result = state.get("result_state")
    if life == "SKIPPED" or result == "MAXIMUM_CONCURRENT_RUNS_REACHED":
        return "skipped"
    if result == "SUCCESS":
        return "succeeded"
    if result == "CANCELED":
        return "canceled"
    return "failed"


def capacity(max_concurrent_runs: int, active: int, queue_enabled: bool = False) -> Optional[int]:
    """How many more runs the ingestion job takes now: its ``max_concurrent_runs`` less those going.
    None (no limit) when its queue is on: the queue holds the runs beyond it."""
    if queue_enabled:
        return None
    return max(0, int(max_concurrent_runs) - int(active))


def launch(jobs, job_id: int, requests: List[dict], *, launch_id: str = "", max_running: int = 0,
           wait: bool = False, confirm_s: float = 30, poll_s: float = 30, timeout_s: float = 0,
           already_running: Dict[str, Optional[frozenset]] = None, room: Optional[int] = None,
           on_settled: Optional[Callable[[str], None]] = None,
           sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict:
    """Start the requested runs, then confirm they started, or, with ``wait``, wait until all have ended.

    - A collection in ``already_running`` (``active_collections``: its running databases, None for all) is not
      started again: its own run has the work. When that run lacks some of the collection's databases (it was
      started from an earlier work list), the collection is ``deferred``: those databases wait for the next cycle.
    - At most ``room`` runs are started (None: no limit; see ``capacity``); the rest are ``not_started``.
    - One run failing does not stop the others: each collection's run is independent and safe to finish.
    - Without ``wait`` (the default) every run is started, then the started runs are polled (at once, then
      every few seconds) for ``confirm_s``: one that ends in that time (skipped by Databricks, or failed at
      once) is reported with its outcome; one still going after a successful poll is ``started`` and reports
      its own result; one no poll of which succeeded is ``unconfirmed``, a failure: nothing says it started.
    - With ``wait``, at most ``max_running`` runs go at once (0: all at once), the next starting as one
      ends; each run is polled every ``poll_s`` seconds (a failed poll is tried again on the next
      one); after ``timeout_s`` (0: no limit) the runs still going are cancelled and reported as timed
      out; if the step itself is stopped while it waits, the runs it started that are still going are
      cancelled, then the error is raised again.

    ``on_settled(collection)`` is called once nothing more will be started for a collection: right after its start
    call, whatever it returned, or at once when it won't be started. The check releases that collection's lock
    there, held since its check so that no second check could start a run for it meanwhile.

    Returns ``{runs: [{collection, databases, run_id, outcome, state_message, url, poll_errors}], counts,
    seconds}``; ``failures`` lists the runs that need attention.
    """
    started = clock()
    runs = [{"collection": r["collection"], "databases": r["databases"], "run_id": None, "outcome": None,
             "state_message": "", "url": None, "poll_errors": 0, "request": r} for r in requests]
    pending = []
    already_running = already_running or {}

    # A request of a cycle's share can span collections: a collection is settled once every request with it is.
    left: Dict[str, int] = {}
    for entry in runs:
        for name in entry["request"].get("collections") or [entry["collection"]]:
            left[name] = left.get(name, 0) + 1

    def settled(entry):
        for name in entry["request"].get("collections") or [entry["collection"]]:
            left[name] -= 1
            if on_settled and not left[name]:
                on_settled(name)

    for entry in runs:
        if entry["collection"] in already_running:
            covered = already_running[entry["collection"]]
            if covered is None or set(entry["request"]["work"]) <= covered:
                entry["outcome"] = "already_running"
            else:
                entry["outcome"] = "deferred"
                entry["state_message"] = ("a run already going was started from an earlier work list without these "
                                          "databases: the next cycle starts them")
        elif room is not None and len(pending) >= room:
            entry["outcome"] = "not_started"
            entry["state_message"] = "the ingestion job is at its maximum concurrent runs; the next cycle starts it"
        else:
            pending.append(entry)
            continue
        settled(entry)
    going: Dict[int, dict] = {}
    polled = set()          # run ids whose state a poll has read

    def start(entry):
        try:
            entry["run_id"] = int(jobs.run_now(job_id, entry["request"]["job_parameters"],
                                               idempotency_token(launch_id, entry["request"].get("work_id")
                                                                 if entry["request"].get("collections")
                                                                 else entry["collection"])))
        except Exception as e:
            entry["outcome"], entry["state_message"] = "start_failed", str(e)[:500]
            return
        else:
            going[entry["run_id"]] = entry
        finally:
            settled(entry)

    def poll():
        for run_id, entry in list(going.items()):
            try:
                state = jobs.get_run(run_id)
            except Exception as e:
                entry["poll_errors"] += 1
                entry["state_message"] = f"poll failed: {str(e)[:200]}"
                continue
            polled.add(run_id)
            entry["url"] = state.get("run_page_url") or entry["url"]
            result = outcome(state)
            if result:
                entry["outcome"], entry["state_message"] = result, state.get("state_message") or ""
                del going[run_id]

    def cancel_going(why):
        for run_id, entry in list(going.items()):
            try:
                jobs.cancel_run(run_id)
            except Exception:
                pass                # best effort: the run's own timeout still bounds it
            entry["outcome"], entry["state_message"] = why, entry["state_message"] or why
            del going[run_id]

    if not wait:
        for entry in pending:
            start(entry)
        pending = []
        confirm_until = clock() + max(0.0, confirm_s)
        poll()
        while going and clock() < confirm_until:
            sleep(min(5.0, confirm_s))
            poll()
        for run_id, entry in going.items():
            entry["outcome"] = "started" if run_id in polled else "unconfirmed"
            entry["state_message"] = entry["state_message"] or "no poll of this run succeeded in the confirm window"
        going.clear()
    try:
        while pending or going:
            while pending and (not max_running or len(going) < max_running):
                start(pending.pop(0))
            if not going:
                continue
            if timeout_s and clock() - started >= timeout_s:
                cancel_going("timed_out")
                for entry in pending:
                    entry["outcome"] = "timed_out"
                pending = []
                break
            sleep(poll_s)
            poll()
    except BaseException:
        cancel_going("canceled")
        raise
    counts: Dict[str, int] = {}
    for entry in runs:
        counts[entry["outcome"]] = counts.get(entry["outcome"], 0) + 1
    return {"runs": [{k: v for k, v in entry.items() if k != "request"} for entry in runs], "waited": wait,
            "counts": counts, "seconds": round(clock() - started, 1)}


def failures(summary: dict) -> List[dict]:
    """The runs that did not do their work: failed, cancelled, timed out, skipped by Databricks, not
    started for lack of room, or not started at all."""
    return [r for r in summary["runs"] if r["outcome"] not in OK_OUTCOMES]


def failure_message(failed: List[dict]) -> str:
    """One line per run that needs attention, with its link, for the step's error."""
    lines = [f"{len(failed)} ingest run(s) did not succeed:"]
    for r in failed:
        lines.append(f"  {r['collection']}: {r['outcome']} (run {r['run_id']}, {r['url'] or 'no link'})"
                     + (f": {r['state_message']}" if r["state_message"] else ""))
    return "\n".join(lines)


class SdkJobs:
    """The Jobs API calls the launcher makes, through the Databricks SDK. In a notebook the SDK
    uses the run's own credentials, so the calls are made as the launching job's run-as identity; it
    needs CAN_MANAGE_RUN on the ingestion job. The runs themselves run as that job's own run-as."""

    def __init__(self, client=None):
        if client is None:
            from databricks.sdk import WorkspaceClient
            client = WorkspaceClient()
        self.w = client

    def run_now(self, job_id: int, job_parameters: Dict[str, str], idempotency_token: str) -> int:
        waiter = self.w.jobs.run_now(job_id=int(job_id), job_parameters=job_parameters,
                                     idempotency_token=idempotency_token)
        return int(getattr(waiter, "response", waiter).run_id)      # as the app's jobs.run_now reads it

    def get_run(self, run_id: int) -> dict:
        run = self.w.jobs.get_run(int(run_id))
        state = run.state
        value = lambda e: e.value if e is not None else None
        return {"life_cycle_state": value(state.life_cycle_state) if state else None,
                "result_state": value(state.result_state) if state else None,
                "state_message": (state.state_message or "") if state else "", "run_page_url": run.run_page_url}

    def cancel_run(self, run_id: int) -> None:
        self.w.jobs.cancel_run(int(run_id))     # not waited for: the step goes on cancelling the others

    def active_runs(self, job_id: int) -> List[dict]:
        """The job's runs going now, each with the parameters it runs with: a parameter the run wasn't given
        has its default (a run of the job's own schedule ingests its default task_collection)."""
        return [{"run_id": r.run_id, "job_parameters": {p.name: p.value if p.value is not None else p.default
                                                        for p in (r.job_parameters or [])}}
                for r in self.w.jobs.list_runs(job_id=int(job_id), active_only=True)]

    def max_concurrent_runs(self, job_id: int) -> int:
        return int(self.w.jobs.get(int(job_id)).settings.max_concurrent_runs or 1)

    def queue_enabled(self, job_id: int) -> bool:
        queue = self.w.jobs.get(int(job_id)).settings.queue
        return bool(queue and queue.enabled)
