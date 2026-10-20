"""Table retries: a table whose load fails with a transient source error (a refused or dropped
connection, a timeout, Azure SQL throttling) is tried again in the same run after a pause, instead
of waiting for the next run. Off unless job parameter ``table_retries`` is above 0.

* ``classify`` (pure): "transient" or "permanent". The rules that rule a retry out are checked
  first and win; an error no rule recognises is permanent, so it fails as it always has.
* ``RetryPolicy``: one run's limits, shared by its threads: attempts per table, a budget of retries
  for the whole run, and a stop per source server after repeated failed retries, so a source outage
  costs a few extra queries, not one per table. ``claim`` decides whether a failure is retried and
  after how long; the pause doubles each time, with jitter.
* ``with_retries``: wraps the per-table worker. The worker raises ``Retry`` when the policy granted
  one; the wrapper waits (holding no slot), re-plans the table from its checkpoint
  (``planner.retry_plan``) and runs it again.

A retry never appends blind: in append mode the next attempt first checks the sink for this run's
rows of the table (``planner.retry_plan``, ``ct_engine.append_committed``), so an append whose
commit landed before its statement failed is not written twice.

Pure Python apart from reading a Java exception's causes through py4j when one is attached;
unit-tested in tests/table_retry_test.py.
"""

from __future__ import annotations

import os
import random
import re
import threading
import time
from collections import Counter
from typing import Callable, Dict, List, Optional

import ct_engine

PER_SOURCE = int(os.environ.get("LAKEFED_TABLE_RETRY_PER_SOURCE", "5"))
BASE_S = float(os.environ.get("LAKEFED_TABLE_RETRY_BASE_S", "10"))
MAX_S = float(os.environ.get("LAKEFED_TABLE_RETRY_MAX_S", "120"))
DEFAULT_BUDGET = 20

TRANSIENT, PERMANENT = "transient", "permanent"

# Never retried, whatever else the error says: the run must stop writing, or another path owns it.
_NEVER = (
    "collection lock lost",            # the sweep lost its lock: the next sweep redoes the table
    ct_engine.RETENTION_LAPSED_MARKER,  # CT retention passed the checkpoint: the worker reseeds
    "SPARK_JOB_CANCELLED", "Job aborted due to cancellation", "OutOfMemoryError", "DRIVER_OOM",
)
# Permanent: the same attempt would fail again. Checked before the transient rules, because Spark
# wraps a refused login in the same FAILED_JDBC.CONNECTION error as a dropped connection.
_PERMANENT = (
    "Login failed for user", "Cannot open database", "Invalid object name", "Invalid column name",
    "permission was denied", "VIEW CHANGE TRACKING permission",
    ct_engine.CT_NOT_ENABLED_MSG, ct_engine.CT_TABLE_NOT_ENABLED_MSG,
    "TABLE_OR_VIEW_NOT_FOUND", "UNRESOLVED_COLUMN", "DELTA_FAILED_TO_MERGE_FIELDS", "CAST_INVALID_INPUT",
    "DATATYPE_MISMATCH", "PKIX path building failed", "UnknownHostException",
    # Oracle, PostgreSQL, MySQL: a refused login, a missing object, a missing privilege.
    "ORA-01017", "ORA-00942", "ORA-01031", "ORA-28000", "ORA-01045", "password authentication failed",
    "does not exist", "doesn't exist", "Access denied for user", "Unknown database", "Unknown column",
)
_PERMANENT_CODES = {18456, 4060, 208, 207, 229, 230}
_PERMANENT_TYPES = (ValueError, KeyError, TypeError)
_TRANSIENT = (
    "FAILED_JDBC.CONNECTION", "Connection reset", "Broken pipe", "SocketTimeoutException", "Read timed out",
    "connect timed out", "The TCP/IP connection to the host", "The connection is closed", "forcibly closed",
    "ORA-03113", "ORA-03114", "ORA-03135", "ORA-12170", "ORA-12541", "Communications link failure",
    "too many clients", "Too many connections",
)
# Azure SQL throttling and availability, deadlock victim, and transport errors.
_TRANSIENT_CODES = {40501, 40613, 40197, 49918, 49919, 49920, 10928, 10929, 1205, 233, 10053, 10054, 10060}
# SQL Server's driver when its session is killed in the middle of a fetch (a failover, a KILL): matched
# with the driver's exception name so a generic "error occurred" message elsewhere doesn't qualify.
_SQLSERVER_FETCH_ABORTED = re.compile(
    r"SQLServerException(?:\[[^\]]*\])?: An error occurred during the current command \(Done status 0\)")
_CODE = re.compile(r"\bcode=(\d+)")
# SQLSTATE classes: 08 connection exception, 53300 too many connections, 57P01/57P03 server shutting
# down or starting (transient); 28 invalid authorization, 42 syntax or access rule, 3D unknown
# database (permanent).
_SQLSTATE_TRANSIENT = re.compile(r"(?:SQLSTATE:?\s*|state=)(?:08[0-9A-Z]{3}|53300|57P01|57P03)\b")
_SQLSTATE_PERMANENT = re.compile(r"(?:SQLSTATE:?\s*|state=)(?:28|42|3D)[0-9A-Z]{3}\b")
_ERROR_CLASS = re.compile(r"\[([A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*)\]")


class Retry(Exception):
    """Raised by the worker when the policy granted a retry of the failure ``error`` after ``delay`` seconds."""

    def __init__(self, delay: float, error: BaseException):
        super().__init__(f"retrying in {delay:.1f}s: {error}")
        self.delay, self.error = delay, error


def _java_causes(exc: BaseException, limit: int = 8) -> List[str]:
    """The Java exception chain behind a PySpark error, one line each with its SQL error code and
    SQLSTATE when it has them (``java.sql.SQLException``). Empty when no Java exception is attached
    or py4j can't reach it."""
    throwable = getattr(exc, "_origin", None) or getattr(exc, "java_exception", None)
    lines = []
    while throwable is not None and len(lines) < limit:
        try:
            name, message = throwable.getClass().getName(), throwable.getMessage()
        except Exception:
            break
        detail = ""
        try:
            detail = f"[code={int(throwable.getErrorCode())},state={throwable.getSQLState()}]"
        except Exception:
            pass
        lines.append(f"{name}{detail}: {message}")
        try:
            throwable = throwable.getCause()
        except Exception:
            break
    return lines


def _chain(exc: BaseException) -> List[BaseException]:
    out, seen = [], set()
    while exc is not None and id(exc) not in seen and len(out) < 8:
        seen.add(id(exc))
        out.append(exc)
        exc = exc.__cause__ or exc.__context__
    return out


def error_text(exc: BaseException) -> str:
    """Everything the classifier reads: each exception in the Python chain (type, message, Spark
    error class, SQLSTATE) and the Java causes behind it."""
    parts = []
    for e in _chain(exc):
        parts.append(f"{type(e).__name__}: {e}")
        condition = _call(e, "getCondition") if hasattr(e, "getCondition") else _call(e, "getErrorClass")
        if condition:
            parts.append(f"[{condition}]")
        state = _call(e, "getSqlState")
        if state:
            parts.append(f"SQLSTATE: {state}")
        parts.extend(_java_causes(e))
    return "\n".join(parts)


def _call(obj, method: str):
    """``obj.method()``, or None when it has no such method or it fails (PySpark errors only have
    them when the JVM reported a condition; ``getErrorClass`` is the older name of ``getCondition``)."""
    try:
        return getattr(obj, method)()
    except Exception:
        return None


def classify(exc: BaseException) -> str:
    """'transient' when retrying the table could succeed, else 'permanent' (also for anything not
    recognised). Rules that rule a retry out win over the transient ones."""
    text = error_text(exc)
    codes = {int(c) for c in _CODE.findall(text)}
    if (any(m in text for m in _NEVER) or any(m in text for m in _PERMANENT) or codes & _PERMANENT_CODES
            or _SQLSTATE_PERMANENT.search(text)):
        return PERMANENT
    if any(isinstance(e, _PERMANENT_TYPES) for e in _chain(exc)):
        return PERMANENT
    if (any(m in text for m in _TRANSIENT) or codes & _TRANSIENT_CODES or _SQLSTATE_TRANSIENT.search(text)
            or _SQLSERVER_FETCH_ABORTED.search(text)):
        return TRANSIENT
    return PERMANENT


def error_class(exc: BaseException) -> str:
    """A short name for the error, for the run's counts: its Spark error class, else its type."""
    match = _ERROR_CLASS.search(str(exc))
    return match.group(1) if match else type(exc).__name__


def describe(exc: BaseException) -> str:
    """The error as recorded on the table: its message, plus its deepest Java cause when there is one
    (Spark's message alone doesn't say why a connection failed)."""
    text = str(exc)
    causes = [c for e in _chain(exc) for c in _java_causes(e)]
    if causes and causes[-1] not in text:
        text += f" | caused by {causes[-1][:500]}"
    return text


def backoff_delay(attempt: int, base: float = None, cap: float = None, rand=random.random) -> float:
    """Pause before retry ``attempt`` (0-based): half of min(cap, base * 2^attempt) plus a random part
    of up to the other half. Unlike a full jitter it never drops near zero: a source that refused a
    connection needs a real pause."""
    base = BASE_S if base is None else base
    cap = MAX_S if cap is None else cap
    ceiling = min(cap, base * (2 ** attempt))
    return ceiling / 2 + rand() * ceiling / 2


class RetryPolicy:
    """One run's retry limits. ``retries`` 0 turns retries off.

    * ``retries``: extra attempts per table.
    * ``budget``: retries in the whole run; once spent, failures fail at once.
    * ``per_source``: failed retries on one source server (``ct_engine.source_key``) before that
      source stops retrying for the rest of the run. Retries that succeed don't count, so a blip that
      fails many of a server's tables at once is retried in full. An outage that fails them all at
      the same moment gets retries before any has failed: the run's ``budget`` bounds those."""

    def __init__(self, retries: int = 0, budget: int = DEFAULT_BUDGET, per_source: int = None,
                 base_s: float = None, max_s: float = None, rand=random.random,
                 source_of: Callable[[dict], str] = ct_engine.source_key):
        self.retries, self.budget = max(0, int(retries or 0)), max(0, int(budget or 0))
        self.per_source = PER_SOURCE if per_source is None else int(per_source)
        self.base_s, self.max_s, self.rand, self.source_of = base_s, max_s, rand, source_of
        self._lock = threading.Lock()
        self._left = self.budget
        self._held: Dict[object, str] = {}          # control id -> source, for a granted retry not yet over
        self._failed_retries: Counter = Counter()   # per source: retries that failed
        self._stopped: List[str] = []
        self._by_class: Counter = Counter()
        self._granted = self._recovered = self._failed_after_retry = 0
        self._exhausted = False

    def _retry_over(self, key, ok: bool) -> None:
        """The granted retry of control row ``key`` ended (caller holds the lock)."""
        source = self._held.pop(key, None)
        if source is None:
            return
        if not ok:
            self._failed_retries[source] += 1
            if self._failed_retries[source] >= self.per_source and source not in self._stopped:
                self._stopped.append(source)

    def claim(self, cfg: dict, exc: BaseException, attempt: int) -> Optional[float]:
        """Seconds to wait before retrying ``cfg`` after ``exc`` failed attempt ``attempt`` (0-based), or
        None: fail now (permanent, out of attempts, budget spent, or the source stopped)."""
        if self.retries == 0:
            return None
        try:
            transient = classify(exc) == TRANSIENT
            source = self.source_of(cfg)
        except Exception:                 # can't judge it: fail it, as without retries
            return None
        key = cfg.get("id")
        with self._lock:
            if attempt > 0:
                self._retry_over(key, ok=False)      # the retry granted for it failed
            if not transient or attempt >= self.retries or source in self._stopped:
                return None
            if self._left <= 0:
                self._exhausted = True
                return None
            self._left -= 1
            self._granted += 1
            self._by_class[error_class(exc)] += 1
            self._held[key] = source
        return backoff_delay(attempt, self.base_s, self.max_s, rand=self.rand)

    def finished(self, cfg: dict, ok: bool, attempt: int) -> None:
        """A table's final outcome after ``attempt`` (0-based) attempts. A retry still held for it ends
        here: it succeeded, or the run stopped before its next attempt could fail through ``claim``."""
        if attempt == 0:
            return
        with self._lock:
            self._retry_over(cfg.get("id"), ok)
            if ok:
                self._recovered += 1
            else:
                self._failed_after_retry += 1

    def summary(self) -> dict:
        """For ``ingest_run.detail``."""
        with self._lock:
            return {"retries": self._granted, "recovered": self._recovered,
                    "failed_after_retry": self._failed_after_retry, "budget": self.budget,
                    "budget_left": self._left, "budget_exhausted": self._exhausted,
                    "sources_stopped": list(self._stopped), "by_class": dict(self._by_class)}


def with_retries(worker: Callable, policy: RetryPolicy, replan: Callable, sleep=time.sleep) -> Callable:
    """``worker(plan, attempt=k)`` run until it returns or fails for good. When it raises ``Retry``,
    wait the granted delay, then run it again on ``replan(plan)`` (the plan rebuilt from the table's
    checkpoint as it is now). Returns ``run(plan)``."""
    def run(plan):
        cfg, attempt = plan["cfg"], 0
        while True:
            try:
                out = worker(plan, attempt=attempt)
            except Retry as granted:
                attempt += 1
                try:
                    sleep(granted.delay)
                    plan = replan(plan)
                except BaseException:     # cancelled while waiting, or the retry couldn't be planned
                    policy.finished(cfg, False, attempt)
                    raise
                continue
            except BaseException:
                policy.finished(cfg, False, attempt)
                raise
            policy.finished(cfg, True, attempt)
            return out
    return run
