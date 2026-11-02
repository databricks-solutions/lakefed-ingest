// Tests for the job page's pending-state helpers (app/src/frontend/src/pending.ts). Run with `node --test` (Node 22.18+
// strips the types in pending.ts as it loads it). frontend_pending_test.py runs this file from pytest.
import { test } from "node:test";
import assert from "node:assert/strict";
import { changeInProgress, connectionFailure, connectionNextBlock, connectionNextLabel, daysLeftText, deleteNotice, longWait, readsDone, waitLine, isUnconfirmed, requestWriteNotice, runButton, runTimeoutNotice, rediscoveryBusy, rediscoveryButton, rediscoveryNotice, saveNotice, syncFailureLines }
  from "../../src/frontend/src/pending.ts";

const IDLE = { phase: "idle", timed_out: false, run_url: null, blocked: null, refused: null };
const NOTHING_PENDING = { starting: false, cancelling: false };

test("an idle job offers Run now, enabled", () => {
  const button = runButton(IDLE, NOTHING_PENDING);
  assert.deepEqual(button, { label: "Run now", tone: "primary", disabled: false, action: "run", title: null });
});

test("a job that the server blocks from running shows Run now off with the reason", () => {
  const button = runButton({ ...IDLE, blocked: "Another job of this schedule is going" }, NOTHING_PENDING);
  assert.equal(button.label, "Run now");
  assert.equal(button.disabled, true);
  assert.equal(button.title, "Another job of this schedule is going");
});

test("Run now reads Starting… from the moment it is pressed, before the server phase arrives", () => {
  const button = runButton(IDLE, { starting: true, cancelling: false });
  assert.equal(button.label, "Starting…");
  assert.equal(button.disabled, true);
  assert.equal(button.action, null);
});

test("Run now reads Starting… while the server says a run is starting, and stays off", () => {
  const button = runButton({ ...IDLE, phase: "starting" }, NOTHING_PENDING);
  assert.equal(button.label, "Starting…");
  assert.equal(button.disabled, true);
});

test("a run Databricks reports going turns the button into Cancel run", () => {
  const button = runButton({ ...IDLE, phase: "running", run_url: "https://dbx/run/1" }, NOTHING_PENDING);
  assert.equal(button.label, "Cancel run");
  assert.equal(button.tone, "danger");
  assert.equal(button.disabled, false);
  assert.equal(button.action, "cancel");
});

test("Cancel run is off with the refusal when the server refuses a cancel", () => {
  const button = runButton({ ...IDLE, phase: "running", refused: "You can't cancel this job's runs" }, NOTHING_PENDING);
  assert.equal(button.label, "Cancel run");
  assert.equal(button.disabled, true);
  assert.equal(button.title, "You can't cancel this job's runs");
});

test("Cancel run reads Cancelling… from the moment it is pressed, and stays off until every run has ended", () => {
  const pressed = runButton({ ...IDLE, phase: "running" }, { starting: false, cancelling: true });
  assert.equal(pressed.label, "Cancelling…");
  assert.equal(pressed.disabled, true);
  const terminating = runButton({ ...IDLE, phase: "cancelling" }, NOTHING_PENDING);
  assert.equal(terminating.label, "Cancelling…");
  assert.equal(terminating.disabled, true);
  assert.equal(terminating.action, null);
});

test("cancelling wins over starting, so a stop never shows as a start", () => {
  const button = runButton({ ...IDLE, phase: "starting" }, { starting: true, cancelling: true });
  assert.equal(button.label, "Cancelling…");
});

test("Run now is offered only when the server reports the job idle", () => {
  assert.equal(runButton(IDLE, NOTHING_PENDING).action, "run");
  assert.equal(runButton(undefined, NOTHING_PENDING).label, "Run now");
});

test("a job with no run phase at all reads as idle", () => {
  assert.equal(runButton(undefined, NOTHING_PENDING).label, "Run now");
});

test("changeInProgress is true while a request is in flight or the server shows a change", () => {
  assert.equal(changeInProgress(IDLE, true), true);
  assert.equal(changeInProgress({ ...IDLE, phase: "starting" }, false), true);
  assert.equal(changeInProgress({ ...IDLE, phase: "cancelling" }, false), true);
  assert.equal(changeInProgress({ ...IDLE, phase: "running" }, false), true);
  assert.equal(changeInProgress(IDLE, false), false);
  assert.equal(changeInProgress(undefined, false), false);
});

test("no timeout notice while the change is within its limit", () => {
  assert.equal(runTimeoutNotice(IDLE), null);
  assert.equal(runTimeoutNotice({ ...IDLE, phase: "starting" }), null);
  assert.equal(runTimeoutNotice(undefined), null);
});

test("a start Databricks has not confirmed in time says so, and that Run now stays off", () => {
  const notice = runTimeoutNotice({ ...IDLE, phase: "starting", timed_out: true });
  assert.match(notice, /after 15 minutes/);
  assert.match(notice, /Run now stays off/);
  assert.match(notice, /has not confirmed/);
  assert.doesNotMatch(notice, /Open the run/);
});

test("a cancel Databricks has not confirmed in time says so, with a link to the run when there is one", () => {
  const notice = runTimeoutNotice({ ...IDLE, phase: "cancelling", timed_out: true, run_url: "https://dbx/run/2" });
  assert.match(notice, /after 10 minutes/);
  assert.match(notice, /still listed as going/);
  assert.match(notice, /Run now stays off/);
  assert.match(notice, /Open the run in Databricks to check it\./);
});

test("the timeout never says the run has ended", () => {
  const notice = runTimeoutNotice({ ...IDLE, phase: "cancelling", timed_out: true });
  assert.doesNotMatch(notice, /has ended|stopped|cancelled\./);
});

test("the button stays off after a timeout until Databricks reports the change", () => {
  const timedOut = { ...IDLE, phase: "starting", timed_out: true };
  assert.equal(runButton(timedOut, NOTHING_PENDING).disabled, true);
  assert.equal(changeInProgress(timedOut, false), true);
});

test("no delete notice for a job that is not deleted", () => {
  assert.equal(deleteNotice(undefined, "1 Nov"), null);
  assert.equal(deleteNotice(null, "1 Nov"), null);
});

test("a kept job says when it is kept until and how many days are left", () => {
  const notice = deleteNotice({ state: "kept", days_left: 5, expires_at: "2026-11-05T10:00:00Z" }, "5 Nov, 10:00");
  assert.equal(notice.tone, "info");
  assert.equal(notice.text, "Kept until 5 Nov, 10:00, 5 days left.");
});

test("a kept job with one day left says day in the singular", () => {
  const notice = deleteNotice({ state: "kept", days_left: 1 }, "5 Nov");
  assert.equal(notice.text, "Kept until 5 Nov, 1 day left.");
});

test("a kept job due for purge says the daily purge deletes it next", () => {
  const notice = deleteNotice({ state: "kept", days_left: 0 }, "5 Nov");
  assert.equal(notice.text, "Kept until 5 Nov. Due for purge: the daily purge deletes it next.");
});

test("a deleting job says it is deleting, and is not a failure", () => {
  const notice = deleteNotice({ state: "deleting", reason: null, run: null }, "5 Nov");
  assert.equal(notice.tone, "info");
  assert.equal(notice.text, "Deleting");
});

test("a failed delete shows its reason and says Delete now runs it again", () => {
  const notice = deleteNotice({ state: "failed", reason: "table x would not drop" }, "5 Nov");
  assert.equal(notice.tone, "bad");
  assert.equal(notice.text, "Delete failed. table x would not drop Delete now runs it again.");
});

test("a failed delete with no reason still says it failed, and offers Delete now", () => {
  const notice = deleteNotice({ state: "failed", reason: null }, "5 Nov");
  assert.equal(notice.text, "Delete failed. Delete now runs it again.");
});

test("a state the page does not know has no notice", () => {
  assert.equal(deleteNotice({ state: "gone", reason: null }, "5 Nov"), null);
});

test("days left reads as a count, or as due for purge at zero", () => {
  assert.equal(daysLeftText(7), "7 days left");
  assert.equal(daysLeftText(1), "1 day left");
  assert.equal(daysLeftText(0), "Due for purge");
  assert.equal(daysLeftText(-3), "Due for purge");
});

test("a write sent as one request is pending until its answer, which comes after its jobs are synced", () => {
  const notice = requestWriteNotice("Adding databases", { pending: true, error: null });
  assert.equal(notice.tone, "info");
  assert.equal(notice.text, "Adding databases");
});

test("a write that finished or failed with its own message has no pending notice", () => {
  assert.equal(requestWriteNotice("Adding tables", { pending: false, error: null }), null);
  assert.equal(requestWriteNotice("Adding tables", { pending: false, error: { status: 500, message: "Not done" } }), null);
});

test("a gateway timeout is not confirmed: the write may have finished, so it is a warning and says to check first", () => {
  const notice = requestWriteNotice("Adding tables", { pending: false, error: { status: 504, message: "timeout" } });
  assert.equal(notice.tone, "warn");
  assert.match(notice.text, /^Adding tables: not confirmed/);
  assert.match(notice.text, /Check the job's tables before sending it again/);
});

test("a dropped connection is not confirmed either", () => {
  assert.equal(isUnconfirmed(new TypeError("Failed to fetch")), true);
  assert.equal(isUnconfirmed(new TypeError("Cannot read properties of undefined")), false);
  assert.equal(isUnconfirmed({ status: 500 }), false);
  assert.equal(isUnconfirmed(null), false);
});


const SAVED = "Saved: 2 tables changed.";

test("a save is pending while its settings are written and its wrapper jobs synced", () => {
  const notice = saveNotice({ pending: true, done: false, savedText: SAVED, applying: false });
  assert.equal(notice.tone, "info");
  assert.equal(notice.text, "Saving");
});

test("a save is not done while the cluster copies run is still going", () => {
  const notice = saveNotice({ pending: false, done: true, savedText: SAVED, applying: true });
  assert.equal(notice.tone, "info");
  assert.equal(notice.text, "Applying the job cluster");
});

test("a save whose cluster copies run is past its limit says not confirmed, not saved", () => {
  const notice = saveNotice({ pending: false, done: true, savedText: SAVED, applying: true,
    applyTimeout: "Databricks has not reported it ended after 30 minutes: it is still listed as running" });
  assert.equal(notice.tone, "warn");
  assert.match(notice.text, /not confirmed yet/);
  assert.match(notice.text, /still being applied/);
});

test("a save is done with its summary once everything under it has finished", () => {
  assert.deepEqual(saveNotice({ pending: false, done: true, savedText: SAVED, applying: false }),
    { tone: "ok", text: SAVED });
});

test("a save whose cluster copies run failed says the cluster was not applied, plainly", () => {
  const notice = saveNotice({ pending: false, done: true, savedText: SAVED, applying: false,
    applyError: "the cluster copies run did not apply the cluster: node type unavailable" });
  assert.equal(notice.tone, "bad");
  assert.equal(notice.text, "Saved, but the job cluster was not applied: the cluster copies run did not apply the cluster: "
    + "node type unavailable. The job's Databricks jobs still use the cluster they had before. Change the cluster and save again to retry.");
});

test("a save whose wrapper jobs did not sync names each one", () => {
  const failures = syncFailureLines([{ task_collection: "sales", action: "error", detail: "job is locked" },
    { task_collection: "hr", action: "updated", detail: "" }]);
  assert.deepEqual(failures, ["sales: job is locked"]);
  const notice = saveNotice({ pending: false, done: true, savedText: SAVED, applying: false, syncFailures: failures });
  assert.deepEqual(notice, { tone: "bad", text: "Saved, but a Databricks job did not sync: sales: job is locked. Save again to retry." });
  const two = saveNotice({ pending: false, done: true, savedText: SAVED, applying: false, syncFailures: ["a: x", "b: y"] });
  assert.match(two.text, /^Saved, but 2 Databricks jobs did not sync: a: x; b: y\./);
});

test("a save whose answer never came is not confirmed, and never a failure", () => {
  const notice = saveNotice({ pending: false, done: false, savedText: SAVED, applying: false,
    error: Object.assign(new Error("gateway"), { status: 504 }) });
  assert.equal(notice.tone, "warn");
  assert.match(notice.text, /may have finished/);
});

test("a save the server refused shows no notice here, the error box carries it", () => {
  assert.equal(saveNotice({ pending: false, done: false, savedText: SAVED, applying: false, error: new Error("422") }), null);
});

const RUNNING_HERE = { state: "pending", server: "h1", url: "https://h/run/5" };

test("a re-discovery is busy from its request until its tables are written, and through a timeout", () => {
  assert.equal(rediscoveryBusy(undefined, true), true);
  assert.equal(rediscoveryBusy(RUNNING_HERE, false), true);
  assert.equal(rediscoveryBusy({ state: "adding" }, false), true);
  assert.equal(rediscoveryBusy({ state: "timeout", reason: "still listed as running" }, false), true);
  assert.equal(rediscoveryBusy({ state: "done", found: 1, added: 1 }, false), false);
  assert.equal(rediscoveryBusy({ state: "failed", reason: "no" }, false), false);
  assert.equal(rediscoveryBusy(null, false), false);
});

test("Run it now is off while the discovery run goes, and says so", () => {
  const button = rediscoveryButton(RUNNING_HERE, false);
  assert.deepEqual(button, { label: "Running…", disabled: true, title: "Re-discovering" });
});

test("Run it now is off while the tables are written", () => {
  assert.equal(rediscoveryButton({ state: "adding" }, false).label, "Adding tables…");
  assert.equal(rediscoveryButton({ state: "adding" }, false).disabled, true);
});

test("Run it now stays off after a timeout until the re-discovery ends", () => {
  const button = rediscoveryButton({ state: "timeout", reason: "still listed as running" }, false);
  assert.equal(button.disabled, true);
  assert.match(button.title, /stays off until the re-discovery ends/);
});

test("Run it now is on once the re-discovery is done or failed, and off when the job cannot change", () => {
  assert.deepEqual(rediscoveryButton({ state: "done", found: 0, added: 0 }, false), { label: "Run it now", disabled: false, title: null });
  assert.equal(rediscoveryButton({ state: "failed", reason: "x" }, false).disabled, false);
  assert.deepEqual(rediscoveryButton(null, false, "the job is being deleted"),
    { label: "Run it now", disabled: true, title: "the job is being deleted" });
});

test("a re-discovery whose discovery run goes shows pending with its server and no end state", () => {
  const notice = rediscoveryNotice(RUNNING_HERE, false, null);
  assert.equal(notice.tone, "info");
  assert.equal(notice.text, "Re-discovering h1");
});

test("a re-discovery that is adding tables says so", () => {
  assert.deepEqual(rediscoveryNotice({ state: "adding" }, false, null),
    { tone: "info", text: "Adding tables" });
});

test("a finished re-discovery says what it found and added", () => {
  assert.deepEqual(rediscoveryNotice({ state: "done", found: 3, added: 2 }, false, null),
    { tone: "ok", text: "Re-discovery done: found 3 to add, added 2." });
  assert.deepEqual(rediscoveryNotice({ state: "done", found: 0, added: 0, note: "the job's selection is specific tables, which add nothing" }, false, null),
    { tone: "ok", text: "Re-discovery done: the job's selection is specific tables, which add nothing." });
});

test("a failed re-discovery says why", () => {
  assert.deepEqual(rediscoveryNotice({ state: "failed", reason: "discovery run ended FAILED: no route" }, false, null),
    { tone: "bad", text: "Re-discovery failed: discovery run ended FAILED: no route." });
});

test("a timed-out re-discovery is not confirmed, not failed", () => {
  const notice = rediscoveryNotice({ state: "timeout", reason: "it has gone on for more than 6 hours" }, false, null);
  assert.equal(notice.tone, "warn");
  assert.match(notice.text, /^Re-discovery not confirmed: it has gone on for more than 6 hours\. Run it now stays off/);
});

test("a re-discovery whose start failed says so, and nothing when none has run", () => {
  assert.deepEqual(rediscoveryNotice(null, false, new Error("409 already running")),
    { tone: "bad", text: "Re-discovery did not start: 409 already running." });
  assert.equal(rediscoveryNotice(null, false, null), null);
});

// Local times, as the page shows them: 20:42 and 20:45 on 29 Oct.
const ASKED = new Date(2026, 9, 29, 20, 42, 46);
const LATER = new Date(2026, 9, 29, 20, 45, 30);

test("a wait is one plain line, with its elapsed time only once it runs long", () => {
  assert.equal(waitLine("Testing the connection", ASKED, ASKED), "Testing the connection");
  assert.equal(waitLine("Reading tables", ASKED, new Date(2026, 9, 29, 20, 43, 5)), "Reading tables");
  assert.equal(waitLine("Reading tables", ASKED, new Date(2026, 9, 29, 20, 43, 6)), "Reading tables · 20 s");
  assert.equal(waitLine("Reading tables", ASKED, LATER), "Reading tables · 2 min 44 s");
  assert.equal(longWait(ASKED, ASKED), null);
});

test("no wait line names a warehouse, a job or a queue", () => {
  for (const line of [waitLine("Testing the connection", ASKED, LATER), waitLine("Reading tables", ASKED, LATER)]) {
    assert.doesNotMatch(line, /warehouse|job|queue|compute|so far|since/i);
  }
});

test("Next on the connection step runs the test itself: off until a connection is chosen, and while the test runs", () => {
  assert.equal(connectionNextBlock(false, false, null), "Choose a connection first");
  // Chosen and not tested yet, or after a failed test: Next is on, and pressing it tests.
  assert.equal(connectionNextBlock(true, false, null), undefined);
  assert.equal(connectionNextBlock(true, false, waitLine("Testing the connection", ASKED, LATER)), "Testing the connection · 2 min 44 s");
  assert.equal(connectionNextBlock(true, true, null), undefined);
  assert.equal(connectionNextLabel(false), "Next: choose tables");
  assert.equal(connectionNextLabel(true), "Testing the connection…");
});

test("a failed connection test shows its codes, the source's message and what to do", () => {
  const login = connectionFailure("[FAILED_JDBC.CONNECTION] Login failed for user 'reader'. SQLSTATE: HV000",
                                  { error_class: "FAILED_JDBC.CONNECTION", sql_server_error: 18456, sqlstate: "HV000" });
  assert.equal(login.code, "FAILED_JDBC.CONNECTION · SQL Server error 18456 · SQLSTATE HV000");
  assert.equal(login.message, "[FAILED_JDBC.CONNECTION] Login failed for user 'reader'. SQLSTATE: HV000");
  assert.match(login.hint, /refused the connection's login: check its user and password/);
  // SQL Server's number says more than Databricks' class; the class alone still gets its own hint.
  assert.match(connectionFailure("x", { error_class: "FAILED_JDBC.CONNECTION", sql_server_error: 40615 }).hint, /firewall/);
  assert.match(connectionFailure("x", { error_class: "FAILED_JDBC.CONNECTION" }).hint, /host, port, user and password/);
  assert.match(connectionFailure("x", { sqlstate: "28000" }).hint, /refused the connection's login/);
  assert.match(connectionFailure("x", { sqlstate: "08S01" }).hint, /network path/);
});

test("a failed test with no code, such as the app refusing the request, says so plainly and still says what to do", () => {
  const plain = connectionFailure("the app has no USE CONNECTION on sql_a", null);
  assert.equal(plain.code, null);
  assert.equal(plain.hint, "Check the connection in Catalog Explorer (Connections, the connection), then press Next to test again.");
  assert.equal(connectionFailure("x", { error_class: "SOMETHING_NEW" }).code, "SOMETHING_NEW");
  assert.equal(connectionFailure("x", { error_class: "SOMETHING_NEW" }).hint, plain.hint);
});

test("a read of the chosen databases' tables counts the databases ended, for its progress bar", () => {
  assert.deepEqual(readsDone({ databases: 100, done: 10, failed: 2, tables: 3400 }), { done: 12, total: 100 });
  assert.deepEqual(readsDone({ databases: 1, done: 0 }), { done: 0, total: 1 });
});

test("a draft still being read has a progress bar, none once all are read", async () => {
  const { draftReadProgress } = await import("../../src/frontend/src/pending.ts");
  assert.deepEqual(draftReadProgress({ databases: 100, pending: 60, done: 38, failed: 2, tables: 38200 }), { done: 40, total: 100 });
  assert.equal(draftReadProgress({ databases: 1, pending: 0, done: 1 }), null);
  assert.equal(draftReadProgress(null), null);
});

test("a draft with databases that couldn't be read, or discarded, holds Create back by name", async () => {
  const { draftCreateBlock } = await import("../../src/frontend/src/pending.ts");
  assert.equal(draftCreateBlock({ failed: 1, failed_databases: [["eng_009", "Login failed"]] }),
               "1 database couldn't be read (eng_009): choose without them, or add them again");
  assert.equal(draftCreateBlock({ failed: 5, failed_databases: [["a", ""], ["b", ""], ["c", ""], ["d", ""], ["e", ""]] }),
               "5 databases couldn't be read (a, b, c and 2 more): choose without them, or add them again");
  assert.match(draftCreateBlock({ discarded: 3 }), /discarded/);
  assert.equal(draftCreateBlock({ pending: 4, failed: 0, left_out: 2 }), null);
  assert.equal(draftCreateBlock(undefined), null);
});
