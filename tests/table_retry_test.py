"""Table retries: the classifier, the backoff, the run's limits, and retries with injected faults.

The per-table worker lives in ingest.ipynb, so the end-to-end tests drive ``table_retry.with_retries``
and ``planner.retry_plan`` with ``_Worker``, a fake that keeps ingest_one's contract: it records an
event per attempt, asks the policy on failure and raises ``Retry`` when one is granted, records a
pending append before a CT append, checks the sink first on a ``pending`` route or a
``verify_run_id``, and advances the checkpoint after the commit. ``_Sink`` holds rows tagged
(client, run) and can be told to fail before writing, or to commit and then fail.
"""
import threading

import pytest

import batch_append as ba
import ct_engine as ce
import planner
import state_store as ss
import table_retry as tr

CONNECTION = "[FAILED_JDBC.CONNECTION] Failed JDBC <redacted> on the operation: Failed to connect to the database. SQLSTATE: HV000"


class _JavaError:
    """A py4j Java throwable: class name, message, SQL error code and SQLSTATE, and its cause."""
    def __init__(self, name, message, code=None, state=None, cause=None):
        self.name, self.message, self.code, self.state, self.cause = name, message, code, state, cause

    def getClass(self):
        return type("C", (), {"getName": lambda _: self.name})()

    def getMessage(self):
        return self.message

    def getErrorCode(self):
        if self.code is None:
            raise AttributeError("not a SQLException")
        return self.code

    def getSQLState(self):
        return self.state

    def getCause(self):
        return self.cause


def _spark_error(message=CONNECTION, cause=None):
    e = RuntimeError(message)
    e._origin = _JavaError("org.apache.spark.SparkException", message, cause=cause)
    return e


def _cfg(cid, mode="append", load_type="ct", host="h1", client="auto"):
    return {"id": cid, "src_host": host, "src_port": 1433, "src_database": f"db{cid}", "src_schema": "dbo",
            "src_table": "orders", "secret_key": f"k{cid}", "select_list": "id, v", "primary_key": "id",
            "sink_catalog": "main", "sink_schema": "bronze", "sink_table": "orders", "write_mode": mode,
            "load_type": load_type, "client_id": f"eng-{cid}" if client == "auto" else client}


def _plan(cid, route="seed", cp=None, **kw):
    return {"cfg": _cfg(cid, **kw), "route": route, "cp": cp, "cur": 150}


class _Sink:
    """Rows as (client, run). ``faults[id]``: a list of 'before' (fail, nothing written) or 'after'
    (commit, then fail), used up one per append; ``check_fails``: the sink check raises."""
    def __init__(self, faults=None, check_fails=False, error=CONNECTION):
        self.rows, self.faults, self.check_fails, self.error = [], faults or {}, check_fails, error

    def append(self, cfg, run_id):
        fault = (self.faults.get(cfg["id"]) or [None]).pop(0) if self.faults.get(cfg["id"]) else None
        if fault == "before":
            raise _spark_error(self.error)
        self.rows.append((ce.client_id(cfg), run_id))
        if fault == "after":
            raise _spark_error(self.error)

    def committed(self, cfg, run_id):
        if self.check_fails:
            raise RuntimeError("[DELTA_TABLE_NOT_FOUND] the sink could not be read")
        return (ce.client_id(cfg), run_id) in self.rows

    def appends(self, cfg, run_id):
        return self.rows.count((ce.client_id(cfg), run_id))


class _Worker:
    """ingest_one's retry contract, over ``_Sink`` and an in-memory checkpoint table."""
    def __init__(self, sink, policy, run_id=42, merge_faults=None, advance_faults=None):
        self.sink, self.policy, self.run_id = sink, policy, run_id
        self.checkpoints, self.events, self.merges = {}, [], []
        self.merge_faults, self.advance_faults = merge_faults or {}, advance_faults or {}

    def _advance(self, cfg, position, phase):
        cp = self.checkpoints.setdefault(cfg["id"], {})
        cp.update(position=position, phase=phase, pending_run_id=None, pending_position=None, strategy=cfg["load_type"])
        if self.advance_faults.get(cfg["id"]):
            self.advance_faults[cfg["id"]] -= 1
            raise _spark_error()            # e.g. recording CT's begin version after the advance

    def __call__(self, plan, attempt=0):
        cfg, route, run_id = plan["cfg"], plan["route"], self.run_id
        stage = "plan"
        try:
            if plan.get("verify_run_id") is not None:
                stage = "verify"
                if self.sink.committed(cfg, plan["verify_run_id"]):
                    if cfg["load_type"] == "full":
                        self._advance(cfg, None, ss.LOADED)
                    route = "landed"
            if route == "pending":
                stage = "verify"
                cp = plan["cp"]
                if self.sink.committed(cfg, cp["pending_run_id"]):
                    self._advance(cfg, cp["pending_position"], ss.INCREMENTAL)
                    route = "landed"
                else:
                    route = "seed" if cp.get("phase") != ss.INCREMENTAL else "merge"
            if route == "landed":
                outcome = "recovered"
            elif not ce.is_append(cfg):
                stage = "merge"
                if self.merge_faults.get(cfg["id"]):
                    self.merge_faults[cfg["id"]] -= 1
                    raise _spark_error()
                self.merges.append((cfg["id"], plan["cur"]))
                stage = "checkpoint"
                self._advance(cfg, plan["cur"], ss.INCREMENTAL)
                outcome = "incremented"
            else:
                stage = "seed"
                if cfg["load_type"] == "ct":
                    cp = self.checkpoints.setdefault(cfg["id"], {"phase": ss.SEEDING, "strategy": "ct"})
                    cp.update(pending_run_id=run_id, pending_position=500)
                self.sink.append(cfg, run_id)
                stage = "checkpoint"
                self._advance(cfg, 500 if cfg["load_type"] == "ct" else None,
                              ss.INCREMENTAL if cfg["load_type"] == "ct" else ss.LOADED)
                outcome = "seeded"
        except Exception as e:
            delay = self.policy.claim(cfg, e, attempt)
            self.events.append((cfg["id"], "retried" if delay is not None else "failed", stage, attempt))
            if delay is not None:
                raise tr.Retry(delay, e) from e
            raise
        self.events.append((cfg["id"], "ok", stage, attempt))
        return outcome

    def replan(self, plan):
        cp = self.checkpoints.get(plan["cfg"]["id"])
        return planner.retry_plan(plan, dict(cp) if cp is not None else None, self.run_id)


def _run(worker, policy, plans, sleeps):
    run = tr.with_retries(worker, policy, worker.replan, sleep=sleeps.append)
    out = []
    for p in plans:
        try:
            out.append(("ok", run(p)))
        except Exception as e:
            out.append(("failed", str(e)))
    return out


def _policy(retries=2, budget=20, per_source=5):
    return tr.RetryPolicy(retries=retries, budget=budget, per_source=per_source, base_s=10, max_s=120,
                          rand=lambda: 0.5)


# ---- 1. classify ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("error, expected", [
    (_spark_error(), tr.TRANSIENT),                                          # the exact text Lakebase recorded today
    (_spark_error(cause=_JavaError("com.microsoft.sqlserver.jdbc.SQLServerException",
                                   "Login failed for user 'lfc_ingest'.", 18456, "S0001")), tr.PERMANENT),
    (_spark_error("[FAILED_JDBC.UNCLASSIFIED] x", cause=_JavaError(
        "com.microsoft.sqlserver.jdbc.SQLServerException", "Resource ID : 1. The request limit for the database "
        "is 400 and has been reached.", 10928, "S0001")), tr.TRANSIENT),
    (_spark_error("[FAILED_JDBC.UNCLASSIFIED] x", cause=_JavaError(
        "com.microsoft.sqlserver.jdbc.SQLServerException", "Connection reset", 0, "08S01")), tr.TRANSIENT),
    (RuntimeError("java.net.SocketTimeoutException: Read timed out"), tr.TRANSIENT),
    (RuntimeError("Invalid object name 'dbo.orders'."), tr.PERMANENT),
    (RuntimeError("The SELECT permission was denied on the object 'orders'"), tr.PERMANENT),
    (RuntimeError(ce.CT_TABLE_NOT_ENABLED_MSG), tr.PERMANENT),
    (RuntimeError(ce.SOURCE_TABLE_NOT_FOUND_MSG.format(table="[dbo].[t]")), tr.PERMANENT),
    (RuntimeError("collection lock lost; table deferred to the next sweep"), tr.PERMANENT),
    (RuntimeError(f"{ce.RETENTION_LAPSED_MARKER}: Connection reset"), tr.PERMANENT),   # never wins over transient
    (ValueError("control row 3: Connection reset"), tr.PERMANENT),            # a configuration error
    (RuntimeError("[DELTA_FAILED_TO_MERGE_FIELDS] Failed to merge fields 'v'"), tr.PERMANENT),
    (RuntimeError("something nobody has seen before"), tr.UNKNOWN),          # no rule: one retry, then fails
    # bad data (SQLSTATE class 22) and Spark's schema-compatibility and Delta schema-change classes
    (RuntimeError("[NUMERIC_VALUE_OUT_OF_RANGE] 1e9 cannot be represented. SQLSTATE: 22003"), tr.PERMANENT),
    (RuntimeError('org.postgresql.util.PSQLException: ERROR: invalid input syntax for type integer: "x" '
                  "state=22P02"), tr.PERMANENT),
    (_spark_error(cause=_JavaError("org.postgresql.util.PSQLException", "ERROR: value too long for type varchar(3)",
                                   0, "22001")), tr.PERMANENT),
    (RuntimeError("[INCOMPATIBLE_DATA_FOR_TABLE.CANNOT_SAFELY_CAST] Cannot write incompatible data for table `x`"),
     tr.PERMANENT),
    (RuntimeError("[DELTA_SCHEMA_CHANGE] The schema of your Delta table has changed in an incompatible way"),
     tr.PERMANENT),
    (RuntimeError("[DELTA_SCHEMA_CHANGE_SINCE_ANALYSIS] The schema changed since the query was analyzed"),
     tr.PERMANENT),
    # deadlocks and serialization failures: the victim's transaction runs again and succeeds
    (RuntimeError("com.microsoft.sqlserver.jdbc.SQLServerException: Transaction (Process ID 61) was deadlocked on "
                  "lock resources with another process and has been chosen as the deadlock victim."), tr.TRANSIENT),
    (_spark_error(cause=_JavaError("org.postgresql.util.PSQLException", "ERROR: deadlock detected", 0, "40P01")),
     tr.TRANSIENT),
    (RuntimeError("org.postgresql.util.PSQLException: ERROR: could not serialize access due to concurrent update "
                  "state=40001"), tr.TRANSIENT),
    (RuntimeError("java.sql.SQLTransactionRollbackException: Transaction was rolled back SQLSTATE: 40001"),
     tr.TRANSIENT),
    # a cancelled query (SQLSTATE 57014, e.g. a statement timeout or a cancel from the server side)
    (_spark_error(cause=_JavaError("org.postgresql.util.PSQLException",
                                   "ERROR: canceling statement due to statement timeout", 0, "57014")), tr.TRANSIENT),
    (RuntimeError("java.sql.SQLException: ORA-01013: user requested cancel of current operation SQLSTATE: 57014"),
     tr.TRANSIENT),
    # Oracle listener: no instance of the service is up, or all of them are blocking new connections
    (RuntimeError("java.sql.SQLRecoverableException: IO Error: The Network Adapter could not establish the connection "
                  "ORA-12514: TNS:listener does not currently know of service requested in connect descriptor"),
     tr.TRANSIENT),
    (RuntimeError("java.sql.SQLRecoverableException: ORA-12528: TNS:listener: all appropriate instances are blocking "
                  "new connections"), tr.TRANSIENT),
    # the other source adapters: Spark wraps their refused logins in FAILED_JDBC.CONNECTION too
    (_spark_error(cause=_JavaError("java.sql.SQLException", "ORA-01017: invalid username/password; logon denied",
                                   1017, "72000")), tr.PERMANENT),
    (RuntimeError("java.sql.SQLSyntaxErrorException: ORA-00942: table or view does not exist"), tr.PERMANENT),
    (_spark_error(cause=_JavaError("org.postgresql.util.PSQLException",
                                   'FATAL: password authentication failed for user "x"', 0, "28P01")), tr.PERMANENT),
    (RuntimeError('org.postgresql.util.PSQLException: ERROR: relation "orders" does not exist'), tr.PERMANENT),
    (_spark_error(cause=_JavaError("java.sql.SQLException", "Access denied for user 'x'@'h'", 1045, "28000")),
     tr.PERMANENT),
    (RuntimeError("java.sql.SQLSyntaxErrorException: Table 'db.orders' doesn't exist"), tr.PERMANENT),
    (RuntimeError("java.sql.SQLRecoverableException: ORA-03113: end-of-file on communication channel"),
     tr.TRANSIENT),
    (_spark_error(cause=_JavaError("org.postgresql.util.PSQLException", "FATAL: sorry, too many clients already",
                                   0, "53300")), tr.TRANSIENT),
    (RuntimeError("com.mysql.cj.jdbc.exceptions.CommunicationsException: Communications link failure"),
     tr.TRANSIENT),
    # SQL Server: a session killed in the middle of a fetch (live run r3b, Spark task retries off)
    (_spark_error("An error occurred while calling o543.sql.\n: org.apache.spark.SparkException: Job aborted",
                  cause=_JavaError("com.microsoft.sqlserver.jdbc.SQLServerException",
                                   "An error occurred during the current command (Done status 0).", 0, "S0001")),
     tr.TRANSIENT),
    (RuntimeError("com.microsoft.sqlserver.jdbc.SQLServerException: An error occurred during the current command "
                  "(Done status 0)."), tr.TRANSIENT),
    (RuntimeError("An error occurred during the current command (Done status 0)."), tr.UNKNOWN),   # no driver
    (RuntimeError("com.microsoft.sqlserver.jdbc.SQLServerException: An error occurred during the current command "
                  "(Done status 16)."), tr.UNKNOWN),
    (RuntimeError("com.microsoft.sqlserver.jdbc.SQLServerException: Invalid object name 'gen.h0800'. "
                  "An error occurred during the current command (Done status 0)."), tr.PERMANENT),  # deny wins
    # Unity Catalog throttling a remote_query read (a 100,000-table lab load, 29 Oct: lfc_eng_065.gen.t0936)
    (RuntimeError("[UC_SERVER_UNCAUGHT_EXCEPTION] Unity Catalog error: TEMPORARILY_UNAVAILABLE: Too many requests. "
                  "Please retry later. SQLSTATE: XXKUC"), tr.TRANSIENT),
    (RuntimeError("REQUEST_LIMIT_EXCEEDED: the rate limit for this API has been exceeded"), tr.TRANSIENT),
    (RuntimeError("[TABLE_OR_VIEW_NOT_FOUND] gen.t0936 TEMPORARILY_UNAVAILABLE"), tr.PERMANENT),     # deny wins
])
def test_classify_retries_transient_source_errors_only(error, expected):
    assert tr.classify(error) == expected


def test_a_permanent_rule_wins_over_a_transient_one_in_the_same_message():
    message = "[INCOMPATIBLE_DATA_FOR_TABLE] write failed after a deadlock on the retry"
    assert tr.classify(RuntimeError(message)) == tr.PERMANENT


def test_describe_adds_the_java_cause_spark_leaves_out():
    e = _spark_error(cause=_JavaError("com.microsoft.sqlserver.jdbc.SQLServerException", "Connection reset", 0,
                                      "08S01"))
    assert tr.describe(e) == (CONNECTION + " | caused by com.microsoft.sqlserver.jdbc.SQLServerException"
                                           "[code=0,state=08S01]: Connection reset")
    assert tr.describe(RuntimeError("plain")) == "plain"
    assert tr.error_class(e) == "FAILED_JDBC.CONNECTION" and tr.error_class(KeyError("k")) == "KeyError"


# ---- 2. transient, then success ------------------------------------------------------------------------

def test_a_transient_failure_is_retried_after_a_pause_and_recorded_per_attempt():
    sink, policy, sleeps = _Sink(faults={1: ["before"]}), _policy(), []
    worker = _Worker(sink, policy)
    assert _run(worker, policy, [_plan(1)], sleeps) == [("ok", "seeded")]
    assert worker.events == [(1, "retried", "seed", 0), (1, "ok", "checkpoint", 1)]
    assert sleeps == [7.5] and sink.appends(_cfg(1), 42) == 1
    assert policy.summary() == {"retries": 1, "recovered": 1, "failed_after_retry": 0, "budget": 20, "budget_left": 19,
                                "budget_exhausted": False, "sources_stopped": [],
                                "by_class": {"FAILED_JDBC.CONNECTION": 1}}


# ---- 3. a permanent failure fails at once ----------------------------------------------------------------

def test_a_permanent_failure_fails_at_once_without_a_pause():
    sink, policy, sleeps = _Sink(faults={1: ["before"]}, error="Invalid object name 'dbo.orders'."), _policy(), []
    worker = _Worker(sink, policy)
    assert _run(worker, policy, [_plan(1)], sleeps)[0][0] == "failed"
    assert worker.events == [(1, "failed", "seed", 0)] and sleeps == []
    assert policy.summary()["retries"] == 0 and policy.summary()["budget_left"] == 20


# ---- 3b. an unknown error gets one retry, then fails as permanent ------------------------------------------

def test_an_unknown_failure_is_retried_once_and_then_fails():
    unknown = "something nobody has seen before"
    sink, policy, sleeps = _Sink(faults={1: ["before", "before"]}, error=unknown), _policy(), []
    worker = _Worker(sink, policy)
    assert _run(worker, policy, [_plan(1)], sleeps)[0][0] == "failed"
    assert worker.events == [(1, "retried", "seed", 0), (1, "failed", "seed", 1)] and len(sleeps) == 1
    assert policy.summary()["retries"] == 1 and policy.summary()["failed_after_retry"] == 1


def test_an_unknown_failure_that_recovers_on_its_retry_is_not_a_failure():
    sink, policy, sleeps = _Sink(faults={1: ["before"]}, error="something nobody has seen before"), _policy(), []
    worker = _Worker(sink, policy)
    assert _run(worker, policy, [_plan(1)], sleeps) == [("ok", "seeded")]
    assert worker.events == [(1, "retried", "seed", 0), (1, "ok", "checkpoint", 1)]
    assert policy.summary()["recovered"] == 1


def test_claim_grants_an_unknown_error_one_retry_and_a_permanent_one_none():
    policy, cfg = _policy(retries=3), _cfg(1)
    assert policy.claim(cfg, RuntimeError("something nobody has seen before"), 0) is not None
    assert policy.claim(cfg, RuntimeError("something nobody has seen before"), 1) is None
    assert policy.claim(cfg, RuntimeError("Invalid object name 'dbo.orders'."), 0) is None
    assert policy.claim(cfg, _spark_error(), 1) is not None      # a transient error keeps its retries


# ---- 4. the run's budget -----------------------------------------------------------------------------------

def test_once_the_budget_is_spent_failures_fail_at_once():
    sink, policy, sleeps = _Sink(faults={i: ["before"] for i in range(1, 6)}), _policy(budget=2), []
    worker = _Worker(sink, policy)
    out = _run(worker, policy, [_plan(i, host=f"h{i}") for i in range(1, 6)], sleeps)
    assert [o[0] for o in out] == ["ok", "ok", "failed", "failed", "failed"] and len(sleeps) == 2
    summary = policy.summary()
    assert (summary["retries"], summary["budget_left"], summary["budget_exhausted"]) == (2, 0, True)


# ---- 4b. a budget of 0: no retries at all -----------------------------------------------------------------

def test_the_default_budget_is_zero_so_no_table_is_retried_until_a_job_raises_it():
    assert tr.DEFAULT_BUDGET == 0 and tr.RetryPolicy().budget == 0 and tr.RetryPolicy(retries=2).budget == 0


def test_with_a_budget_of_zero_a_transient_failure_fails_at_once_and_is_not_reported_as_spent():
    sink, policy, sleeps = _Sink(faults={1: ["before"]}), _policy(retries=2, budget=0), []
    worker = _Worker(sink, policy)
    assert _run(worker, policy, [_plan(1)], sleeps)[0][0] == "failed"
    assert worker.events == [(1, "failed", "seed", 0)] and sleeps == []
    assert policy.summary() == {"retries": 0, "recovered": 0, "failed_after_retry": 0, "budget": 0, "budget_left": 0,
                                "budget_exhausted": False, "sources_stopped": [], "by_class": {}}


def test_with_a_budget_of_zero_an_unknown_failure_fails_at_once_without_its_one_retry():
    sink = _Sink(faults={1: ["before"]}, error="something nobody has seen before")
    policy, sleeps = _policy(budget=0), []
    worker = _Worker(sink, policy)
    assert _run(worker, policy, [_plan(1)], sleeps)[0][0] == "failed"
    assert worker.events == [(1, "failed", "seed", 0)] and sleeps == []


def test_claim_with_a_budget_of_zero_grants_no_retry_whatever_the_error():
    policy, cfg = _policy(retries=3, budget=0), _cfg(1)
    assert policy.claim(cfg, _spark_error(), 0) is None
    assert policy.claim(cfg, RuntimeError("something nobody has seen before"), 0) is None
    assert policy.summary()["retries"] == 0 and policy.summary()["budget_exhausted"] is False


# ---- 5. a source that keeps failing stops retrying ---------------------------------------------------------

def test_a_source_stops_retrying_after_its_failed_retries_and_others_carry_on():
    down = {i: ["before"] * 3 for i in range(1, 11)}                    # source h1: every attempt fails
    sink = _Sink(faults={**down, 11: ["before"]})               # source h2: fails once
    policy, sleeps = _policy(retries=2, budget=100, per_source=3), []
    worker = _Worker(sink, policy)
    out = _run(worker, policy, [_plan(i) for i in range(1, 11)] + [_plan(11, host="h2")], sleeps)
    assert [o[0] for o in out[:10]] == ["failed"] * 10 and out[10] == ("ok", "seeded")
    assert policy.summary()["sources_stopped"] == ["host:h1:1433"]
    retried_on_h1 = sum(1 for e in worker.events if e[0] <= 10 and e[3] > 0)
    assert retried_on_h1 == 3                                            # 3 failed retries, then no more


def test_failures_at_once_on_one_source_are_bounded_by_the_runs_budget():
    # A source outage fails many tables at the same moment, before any retry has failed: the source
    # stop can't act yet, so the run's budget is what bounds the extra queries.
    policy, start = _policy(retries=2, budget=20, per_source=5), threading.Barrier(32)
    granted = []

    def fail_first_attempt(cid):
        start.wait()
        granted.append(policy.claim(_cfg(cid), _spark_error(), 0))
    threads = [threading.Thread(target=fail_first_attempt, args=(cid,)) for cid in range(1, 33)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(d is not None for d in granted) == 20 and policy.summary()["budget_left"] == 0


def test_a_source_keeps_retrying_while_its_retries_succeed():
    # A blip that fails many tables of one server at once is what retries are for: only failed
    # retries count towards the source's stop.
    policy = _policy(retries=2, budget=100, per_source=1)
    for cid in range(1, 11):
        assert policy.claim(_cfg(cid), _spark_error(), 0) is not None
    for cid in range(1, 11):
        policy.finished(_cfg(cid), True, 1)
    assert policy.claim(_cfg(11), _spark_error(), 0) is not None and policy.summary()["sources_stopped"] == []


def test_a_run_stopped_during_a_pause_counts_the_table_as_failed():
    sink, policy = _Sink(faults={1: ["before"]}), _policy(per_source=1)
    worker = _Worker(sink, policy)

    def cancelled(seconds):
        raise KeyboardInterrupt("the run was cancelled")
    run = tr.with_retries(worker, policy, worker.replan, sleep=cancelled)
    with pytest.raises(KeyboardInterrupt):
        run(_plan(1))
    assert worker.events == [(1, "retried", "seed", 0)] and sink.rows == []
    summary = policy.summary()
    assert (summary["retries"], summary["recovered"], summary["failed_after_retry"]) == (1, 0, 1)


# ---- 6. a commit whose outcome is uncertain is never appended twice ------------------------------------------

def test_a_ct_append_that_committed_before_failing_is_recovered_not_appended_again():
    sink, policy, sleeps = _Sink(faults={1: ["after"]}), _policy(), []
    worker = _Worker(sink, policy)
    assert _run(worker, policy, [_plan(1)], sleeps) == [("ok", "recovered")]
    assert sink.appends(_cfg(1), 42) == 1
    assert worker.checkpoints[1]["position"] == 500 and worker.checkpoints[1]["pending_run_id"] is None
    assert [e[1:3] for e in worker.events] == [("retried", "seed"), ("ok", "verify")]


def test_a_full_append_that_committed_before_failing_is_checked_before_reloading():
    sink, policy, sleeps = _Sink(faults={1: ["after"]}), _policy(), []
    worker = _Worker(sink, policy)
    assert _run(worker, policy, [_plan(1, route="full", load_type="full")], sleeps) == [("ok", "recovered")]
    assert sink.appends(_cfg(1, load_type="full"), 42) == 1 and worker.checkpoints[1]["phase"] == ss.LOADED


def test_a_failure_after_the_checkpoint_advance_is_not_appended_again():
    sink, policy, sleeps = _Sink(), _policy(), []
    worker = _Worker(sink, policy, advance_faults={1: 1})
    assert _run(worker, policy, [_plan(1)], sleeps) == [("ok", "recovered")]
    assert sink.appends(_cfg(1), 42) == 1


def test_when_the_sink_cannot_be_checked_nothing_is_appended_and_the_pending_append_stays():
    sink, policy, sleeps = _Sink(faults={1: ["after"]}), _policy(), []
    worker = _Worker(sink, policy)
    sink.check_fails = True
    assert _run(worker, policy, [_plan(1)], sleeps)[0][0] == "failed"
    assert sink.appends(_cfg(1), 42) == 1 and worker.checkpoints[1]["pending_run_id"] == 42
    assert [e[1:3] for e in worker.events] == [("retried", "seed"), ("failed", "verify")]


# ---- 7. batches --------------------------------------------------------------------------------------------

class _BatchEngine:
    """ct_engine for BatchRunner: member ``bad`` fails the batch read; everything else lands in ``sink``."""
    sink_fqn = staticmethod(ce.sink_fqn)
    client_id = staticmethod(ce.client_id)
    change_read = staticmethod(ce.change_read)
    snapshot_read = staticmethod(ce.snapshot_read)

    def __init__(self, sink, bad):
        self.sink, self.bad = sink, bad

    def ensure_sink_table(self, spark, cfg, sink, slots=None):
        pass

    def seed_prep(self, spark, cfgs, slots=None):
        return {int(c["id"]): (500, 1, 7) for c in cfgs}

    def append_batch(self, spark, sink, reads, slots=None):
        if any(int(r[0]["id"]) == self.bad for r in reads):
            raise _spark_error()
        for r in reads:
            self.sink.rows.append((ce.client_id(r[0]), 42))
        return len(reads)

    def committed_clients(self, spark, sink, cfgs, run_id, slots=None):
        return {ce.client_id(c) for c in cfgs if self.sink.committed(c, run_id)}


class _Store:
    def __getattr__(self, name):
        return lambda *a, **k: None


def test_a_batch_member_that_fails_alone_is_retried_and_the_others_are_written_once():
    sink, policy, sleeps = _Sink(faults={2: ["after"]}), _policy(), []
    worker = _Worker(sink, policy)
    per_table = tr.with_retries(worker, policy, worker.replan, sleep=sleeps.append)
    runner = ba.BatchRunner(None, _Store(), None, 42, per_table=per_table, engine=_BatchEngine(sink, bad=2))
    out = runner.run({"kind": "seed", "members": [_plan(i) for i in (1, 2, 3)]})
    assert [r["status"] for r in out["members"]] == ["ok", "ok", "ok"]
    assert [sink.appends(_cfg(i), 42) for i in (1, 2, 3)] == [1, 1, 1]
    assert [e[1:3] for e in worker.events] == [("retried", "seed"), ("ok", "verify")]


# ---- 8. merge mode -----------------------------------------------------------------------------------------

def test_merge_mode_retries_the_same_bounded_window_and_advances_once():
    sink, policy, sleeps = _Sink(), _policy(), []
    worker = _Worker(sink, policy, merge_faults={1: 1})
    plan = _plan(1, route="merge", mode="merge", cp={"position": "100", "phase": ss.INCREMENTAL, "strategy": "ct"})
    assert _run(worker, policy, [plan], sleeps) == [("ok", "incremented")]
    assert worker.merges == [(1, 150)] and worker.checkpoints[1]["position"] == 150


# ---- 9. retries off ------------------------------------------------------------------------------------------

def test_with_retries_off_a_failure_fails_as_today():
    sink, policy, sleeps = _Sink(faults={1: ["before"]}), tr.RetryPolicy(retries=0), []
    worker = _Worker(sink, policy)
    replans = []
    run = tr.with_retries(worker, policy, lambda p: replans.append(p) or p, sleep=sleeps.append)
    with pytest.raises(RuntimeError, match="FAILED_JDBC.CONNECTION"):
        run(_plan(1))
    assert worker.events == [(1, "failed", "seed", 0)] and sleeps == [] and replans == []
    assert policy.summary()["retries"] == 0


# ---- 10. the backoff -------------------------------------------------------------------------------------------

def test_each_pause_doubles_with_jitter_up_to_the_cap_and_never_near_zero():
    assert [tr.backoff_delay(k, 10, 120, rand=lambda: 0.0) for k in range(5)] == [5, 10, 20, 40, 60]
    assert [tr.backoff_delay(k, 10, 120, rand=lambda: 1.0) for k in range(5)] == [10, 20, 40, 80, 120]


# ---- retry_plan ----------------------------------------------------------------------------------------------

def test_retry_plan_verifies_every_append_and_keeps_merges_as_they_were():
    fresh = {"position": "100", "phase": ss.INCREMENTAL, "strategy": "ct", "pending_run_id": None}
    merge = _plan(1, route="merge", mode="merge")
    assert planner.retry_plan(merge, fresh, 42) == dict(merge, cp=fresh)
    pending = dict(fresh, pending_run_id=42, pending_position="150")
    assert planner.retry_plan(_plan(2, route="merge"), pending, 42) == {"cfg": _cfg(2), "route": "pending",
                                                                       "cp": pending}
    seed = _plan(3)
    assert planner.retry_plan(seed, None, 42) == dict(seed, cp=None, verify_run_id=42)
    full = _plan(4, route="full", load_type="full")
    assert planner.retry_plan(full, dict(fresh, pending_run_id=7), 42)["verify_run_id"] == 42   # full: never pending


def test_a_policy_that_cannot_judge_a_failure_fails_it_as_today():
    def no_source(cfg):
        raise ValueError("unknown src_type")
    policy = tr.RetryPolicy(retries=2, source_of=no_source)
    assert policy.claim(_cfg(1), _spark_error(), 0) is None and policy.summary()["retries"] == 0


def test_the_spark_condition_is_read_once_preferring_the_current_name():
    class _Captured(RuntimeError):
        calls = []

        def getCondition(self):
            self.calls.append("getCondition")
            return "FAILED_JDBC.CONNECTION"

        def getErrorClass(self):
            self.calls.append("getErrorClass")
            return "FAILED_JDBC.CONNECTION"

        def getSqlState(self):
            return "08001"
    error = _Captured("Failed to connect to the database.")
    assert tr.classify(error) == tr.TRANSIENT and _Captured.calls == ["getCondition"]


def test_a_spark_cancellation_is_recognised_from_text_and_exception_and_never_retried():
    cancelled = "org.apache.spark.SparkException: [SPARK_JOB_CANCELLED] Job 12 cancelled part of cancelled job group"
    aborted = RuntimeError("Job aborted due to cancellation: stage 4 was cancelled")
    assert tr.is_cancellation(cancelled) and tr.is_cancellation(aborted)
    assert tr.classify(aborted) == tr.PERMANENT
    assert not tr.is_cancellation("Connection reset by peer")
    assert not tr.is_cancellation("")
