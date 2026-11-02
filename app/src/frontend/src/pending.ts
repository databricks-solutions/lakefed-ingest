// What the job page shows while Databricks has not yet confirmed a change. Every answer comes from the server (the
// run phase on the job, and the delete state), so a button never shows an end state before the work under it has
// ended. Plain functions: the Node tests in app/tests/js read this file as it is.

export type RunPhase = "idle" | "starting" | "running" | "cancelling";

// The server's limits (ingestion_jobs.START_CONFIRM_S and CANCEL_CONFIRM_S), in minutes, for the timeout's wording.
export const START_CONFIRM_MINUTES = 15;
export const CANCEL_CONFIRM_MINUTES = 10;

export interface RunNowState {
  phase?: string;
  timed_out?: boolean;
  run_url?: string | null;
  blocked?: string | null;
  refused?: string | null;
}

export interface DeleteState {
  state: string;
  reason?: string | null;
  run?: { url?: string | null } | null;
  expires_at?: string | null;
  days_left?: number;
}

export interface ButtonState {
  label: string;
  tone: "primary" | "danger";
  disabled: boolean;
  action: "run" | "cancel" | null;
  title: string | null;
}

/** The job's Run now or Cancel run button. `pending` is true while this page's own request is still in flight, so
 * the button waits from the moment it is pressed. Cancelling wins over starting, and a run only becomes Cancel run
 * once Databricks reports it running. */
export function runButton(runNow: RunNowState | undefined, pending: { starting: boolean; cancelling: boolean }): ButtonState {
  const phase = runNow?.phase ?? "idle";
  if (pending.cancelling || phase === "cancelling") {
    return { label: "Cancelling…", tone: "primary", disabled: true, action: null,
             title: "Waiting for Databricks to report every run of this job ended" };
  }
  if (pending.starting || phase === "starting") {
    return { label: "Starting…", tone: "primary", disabled: true, action: null,
             title: "Waiting for Databricks to report the run going" };
  }
  if (phase === "running") {
    return { label: "Cancel run", tone: "danger", disabled: !!runNow?.refused, action: "cancel",
             title: runNow?.refused ?? "Cancel the run of this job going now" };
  }
  return { label: "Run now", tone: "primary", disabled: !!runNow?.blocked, action: "run", title: runNow?.blocked ?? null };
}

/** Whether the page should poll quickly: a start or a cancel is pending, here or in Databricks. */
export function changeInProgress(runNow: RunNowState | undefined, pendingRequest: boolean): boolean {
  return pendingRequest || (runNow?.phase ?? "idle") !== "idle";
}

/** The plain notice when Databricks has not confirmed a start or a cancel in time, or null while it is within its
 * limit. Run now stays off after it: the page never shows a run as ended before Databricks says so. */
export function runTimeoutNotice(runNow: RunNowState | undefined): string | null {
  if (!runNow?.timed_out) return null;
  const open = runNow.run_url ? " Open the run in Databricks to check it." : "";
  if (runNow.phase === "cancelling") {
    return `Databricks has not confirmed the cancel after ${CANCEL_CONFIRM_MINUTES} minutes: its runs are still listed as going, so Run now stays off until Databricks reports them ended.${open}`;
  }
  return `Databricks has not confirmed that the run started after ${START_CONFIRM_MINUTES} minutes: it is still listed as starting, so Run now stays off until Databricks reports it running or ended.${open}`;
}

/** How many days a deleted job has before the daily purge deletes it, in words for a list row. */
export function daysLeftText(daysLeft: number): string {
  return daysLeft <= 0 ? "Due for purge" : `${daysLeft} ${daysLeft === 1 ? "day" : "days"} left`;
}

/** The notice for a deleted job: kept until the purge, its purge running, or its purge failed. `expiresText` is the
 * formatted date the job is kept until. Null when the job is not deleted. */
export function deleteNotice(deleteState: DeleteState | null | undefined,
                             expiresText: string): { tone: "info" | "bad"; text: string } | null {
  if (!deleteState) return null;
  if (deleteState.state === "kept") {
    const daysLeft = deleteState.days_left ?? 0;
    const dueText = daysLeft <= 0 ? `Kept until ${expiresText}. Due for purge: the daily purge deletes it next.`
      : `Kept until ${expiresText}, ${daysLeftText(daysLeft)}.`;
    return { tone: "info", text: dueText };
  }
  if (deleteState.state === "deleting") {
    return { tone: "info", text: "Deleting" };
  }
  if (deleteState.state === "failed") {
    const why = deleteState.reason ? ` ${deleteState.reason}` : "";
    return { tone: "bad", text: `Delete failed.${why} Delete now runs it again.` };
  }
  return null;
}

/** Whether a request got no answer the app can rely on: the gateway gave up (504), or the connection dropped. The write
 * behind it may still have finished, so the page reads it as not confirmed, never as failed or done. */
export function isUnconfirmed(error: unknown): boolean {
  if ((error as { status?: number } | null)?.status === 504) return true;
  return error instanceof TypeError && /fetch|network/i.test(error.message);
}

/** A write sent as one request (a small one, not a task). It is pending until its answer, which comes once its tables
 * are written and the job's Databricks jobs are synced; an unconfirmed answer is a warning, not a failure. Null when the
 * page has nothing to say (the write is done, or it failed with its own message). */
export function requestWriteNotice(what: string, state: { pending: boolean; error: unknown }): { tone: "info" | "warn"; text: string } | null {
  if (state.pending) {
    return { tone: "info", text: what };
  }
  if (isUnconfirmed(state.error)) {
    return { tone: "warn", text: `${what}: not confirmed. The app did not answer in time, so the write may have finished. `
             + "Check the job's tables before sending it again." };
  }
  return null;
}

/** The wrapper jobs a save could not sync, as the page names them ("sales: job is locked"). Empty when all synced. */
export function syncFailureLines(results: { task_collection?: string | null; action?: string; detail?: string }[] | undefined): string[] {
  return (results ?? []).filter((r) => r.action === "error")
    .map((r) => (r.task_collection ? `${r.task_collection}: ${r.detail || "its Databricks job was not synced"}` : r.detail || "a Databricks job was not synced"));
}

/** The Edit view's line for a Save. Saved only once the table settings are written, the wrapper jobs are synced, and the
 * cluster copies run has ended; until then the page reads pending, and a failed copy or sync is said plainly. */
export function saveNotice(state: {
  pending: boolean;
  done: boolean;
  savedText: string;
  applying: boolean;
  applyTimeout?: string | null;
  applyError?: string | null;
  syncFailures?: string[];
  error?: unknown;
}): { tone: "info" | "ok" | "warn" | "bad"; text: string } | null {
  if (state.error) {
    return isUnconfirmed(state.error)
      ? { tone: "warn", text: "Saving: not confirmed. The app did not answer in time, so the save may have finished. "
          + "Reload the page to see what was saved." }
      : null;
  }
  if (state.pending) {
    return { tone: "info", text: "Saving" };
  }
  if (state.applying) {
    if (state.applyTimeout) {
      return { tone: "warn", text: `Saving: not confirmed yet, the job cluster is still being applied: ${state.applyTimeout}.` };
    }
    return { tone: "info", text: "Applying the job cluster" };
  }
  if (!state.done) return null;
  if (state.applyError) {
    return { tone: "bad", text: `Saved, but the job cluster was not applied: ${state.applyError}. The job's Databricks jobs `
             + "still use the cluster they had before. Change the cluster and save again to retry." };
  }
  const failures = state.syncFailures ?? [];
  if (failures.length) {
    return { tone: "bad", text: `Saved, but ${failures.length === 1 ? "a Databricks job did" : `${failures.length} Databricks jobs did`} `
             + `not sync: ${failures.join("; ")}. Save again to retry.` };
  }
  return { tone: "ok", text: state.savedText };
}

/** A re-discovery's progress as the server reports it (rediscovery.progress): pending while its discovery run goes,
 * adding while its tables are written, then done, failed, or a timeout that is not done. */
export interface RediscoveryProgress {
  state: "pending" | "adding" | "done" | "failed" | "timeout";
  found?: number;
  added?: number;
  note?: string | null;
  reason?: string | null;
  server?: string | null;
  url?: string | null;
}

/** Whether a re-discovery is still going: its request in flight, its discovery run or table writes not ended, or a
 * timeout (not confirmed, so not done). While it is, Run it now stays off and the page polls. */
export function rediscoveryBusy(progress: RediscoveryProgress | null | undefined, requesting: boolean): boolean {
  return requesting || progress?.state === "pending" || progress?.state === "adding" || progress?.state === "timeout";
}

/** The job page's Run it now for a re-discovery: off while one is going, with the reason in its title. */
export function rediscoveryButton(progress: RediscoveryProgress | null | undefined, requesting: boolean, refused?: string | null):
  { label: string; disabled: boolean; title: string | null } {
  if (requesting) return { label: "Starting…", disabled: true, title: "Starting the re-discovery" };
  if (progress?.state === "pending") return { label: "Running…", disabled: true, title: "Re-discovering" };
  if (progress?.state === "adding") return { label: "Adding tables…", disabled: true, title: "Adding tables" };
  if (progress?.state === "timeout") return { label: "Run it now", disabled: true, title: "Not confirmed yet: it stays off until the re-discovery ends" };
  return { label: "Run it now", disabled: !!refused, title: refused ?? null };
}

/** The re-discovery's line on the job page: what it is doing, or how it ended. Null when none has run. */
export function rediscoveryNotice(progress: RediscoveryProgress | null | undefined, requesting: boolean, error: unknown):
  { tone: "info" | "ok" | "warn" | "bad"; text: string } | null {
  if (requesting) return { tone: "info", text: "Starting the re-discovery" };
  if (error) return { tone: "bad", text: `Re-discovery did not start: ${error instanceof Error ? error.message : String(error)}.` };
  if (!progress) return null;
  switch (progress.state) {
    case "pending":
      return { tone: "info", text: `Re-discovering ${progress.server ?? "the job's servers"}` };
    case "adding":
      return { tone: "info", text: "Adding tables" };
    case "timeout":
      return { tone: "warn", text: `Re-discovery not confirmed: ${progress.reason || "Databricks has not reported it ended"}. `
               + "Run it now stays off until it ends." };
    case "failed":
      return { tone: "bad", text: `Re-discovery failed: ${progress.reason || "no reason was recorded"}.` };
    case "done":
      if (progress.note) return { tone: "ok", text: `Re-discovery done: ${progress.note}.` };
      return { tone: "ok", text: `Re-discovery done: found ${progress.found ?? 0} to add, added ${progress.added ?? 0}.` };
  }
}

/** A wait shows its elapsed time only once it has run this long, as Databricks' own progress lines do. */
export const LONG_WAIT_S = 20;

/** How long a wait has run ("25 s", "1 min 5 s") once past LONG_WAIT_S; null before that. */
export function longWait(since: Date, now: Date): string | null {
  const seconds = Math.max(0, Math.floor((now.getTime() - since.getTime()) / 1000));
  if (seconds < LONG_WAIT_S) return null;
  return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
}

/** A wait in one plain line: what is being done, and how long once it runs long ("Reading tables · 1 min 5 s"). */
export function waitLine(doing: string, since: Date, now: Date = new Date()): string {
  const long = longWait(since, now);
  return long ? `${doing} · ${long}` : doing;
}

/** Why Next is off on the connection step, said once beside it: a connection is chosen first, and while Next tests it,
 *  what the test is doing. Nothing once a connection is chosen: Next tests it, or moves on once a test has passed. */
export function connectionNextBlock(chosen: boolean, passed: boolean, testing: string | null): string | undefined {
  if (passed) return undefined;
  if (testing) return testing;
  if (!chosen) return "Choose a connection first";
  return undefined;
}

/** Next on the connection step: it runs the test itself, as Databricks' connection dialogs do, and reads so meanwhile. */
export const connectionNextLabel = (testing: boolean) => testing ? "Testing the connection…" : "Next: choose tables";

/** The codes a failed connection test names (the backend reads them from the message: source_catalog.error_codes). */
export interface ErrorCodes { error_class?: string | null; sql_server_error?: number | null; sqlstate?: string | null }

/** A failed connection test as the step shows it: its codes on one line ("FAILED_JDBC.CONNECTION · SQL Server error
 *  18456"), the source's message, and what to do next. */
export interface ConnectionFailure { code: string | null; message: string; hint: string }

const SQL_SERVER_HINTS: Record<number, string> = {
  18456: "The server refused the connection's login: check its user and password in Catalog Explorer (Connections, the "
         + "connection, Edit), and that the login exists on the server.",
  4060: "The login can't open master, where the server lists its databases: type the databases below, or give the login "
        + "a user in master (VIEW ANY DATABASE) and test again.",
  40615: "The server's firewall refuses Databricks: allow serverless compute on the server (a network connectivity "
         + "configuration's private endpoint or firewall rule), then test again.",
  40613: "The database isn't available right now (a paused serverless database takes a minute or so to resume): test again.",
};
const CLASS_HINTS: Record<string, string> = {
  "FAILED_JDBC.CONNECTION": "Databricks couldn't connect to the server: check the connection's host, port, user and password "
                            + "in Catalog Explorer, and that serverless compute has a network path to the server.",
};
const DEFAULT_HINT = "Check the connection in Catalog Explorer (Connections, the connection), then press Next to test again.";

/** A failed test's codes, message and hint: SQL Server's own error number says most, then Databricks' error class, then
 *  a SQLSTATE of class 28 (a login refused) or 08 (no connection); else a hint that fits any failure. */
export function connectionFailure(message: string, codes?: ErrorCodes | null): ConnectionFailure {
  const number = codes?.sql_server_error ?? null;
  const errorClass = codes?.error_class ?? null;
  const state = codes?.sqlstate ?? null;
  const code = [errorClass, number != null ? `SQL Server error ${number}` : null, state ? `SQLSTATE ${state}` : null]
    .filter(Boolean).join(" · ");
  const hint = (number != null ? SQL_SERVER_HINTS[number] : undefined) ?? (errorClass ? CLASS_HINTS[errorClass] : undefined)
    ?? (state?.startsWith("28") ? SQL_SERVER_HINTS[18456] : undefined)
    ?? (state?.startsWith("08") ? CLASS_HINTS["FAILED_JDBC.CONNECTION"] : undefined) ?? DEFAULT_HINT;
  return { code: code || null, message, hint };
}

/** How far a read of the chosen databases' tables is, for a progress bar: the databases ended (read or failed) of all. */
export function readsDone(answer: { databases?: number; done?: number; failed?: number }): { done: number; total: number } {
  return { done: Number(answer.done ?? 0) + Number(answer.failed ?? 0), total: Number(answer.databases ?? 0) };
}

/** Where a draft's read stands, as a preview answers it (main._draft_reading): its databases, those still to read,
 *  read, failed and discarded, the tables read so far, those a whole-server read left out, and the failures by name. */
export interface DraftReading {
  databases?: number; pending?: number; done?: number; failed?: number; discarded?: number; tables?: number;
  left_out?: number; failed_databases?: [string, string][];
}

const count = (value: unknown) => Number(value ?? 0).toLocaleString("en-US");
const word = (n: unknown, one: string) => `${one}${Number(n ?? 0) === 1 ? "" : "s"}`;

/** How far New job's chosen tables are read, for its progress line, while some are still to read; null once all are. */
export function draftReadProgress(reading: DraftReading | null | undefined): { done: number; total: number } | null {
  if (!reading || !Number(reading.pending)) return null;
  return readsDone(reading);
}

/** Why Create can't take the tables chosen as they are read: databases that couldn't be read (by name), or a draft
 *  discarded meanwhile. Null when nothing stops it. */
export function draftCreateBlock(reading: DraftReading | null | undefined): string | null {
  if (!reading) return null;
  if (Number(reading.discarded)) return "The tables read for this choice were discarded: add them again";
  const failed = reading.failed_databases ?? [];
  if (!Number(reading.failed) && !failed.length) return null;
  const names = failed.slice(0, 3).map(([name]) => name).join(", ");
  const more = Number(reading.failed) > 3 ? ` and ${count(Number(reading.failed) - 3)} more` : "";
  return `${count(reading.failed)} ${word(reading.failed, "database")} couldn't be read (${names}${more}): choose without them, or add them again`;
}
