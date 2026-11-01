// What a job's page says while the job isn't Ready yet: where its preparation stands in one line, how far its bar is,
// why each database was set aside, and what its schedule does once it is Ready. Every count comes from the server
// (GET /api/ingestion-jobs/{id}/prepare, preparing.view). Plain functions: the Node tests in app/tests/js read this file
// as it is.

export interface SetAside {
  server: string; database: string; kind: string; why?: string | null; target?: string | null; job?: string | null;
  again?: unknown; replan?: unknown;
}

export interface PrepareView {
  state?: string; stage?: string | null; turn_on?: boolean; kept_paused?: string | null;
  reading?: { databases: number; read: number; tables: number; minutes_left?: number | null } | null;
  progress?: Record<string, unknown>; counts?: Record<string, number>; set_aside?: SetAside[];
}

const count = (n: unknown) => Number(n || 0).toLocaleString("en-US");
const words = (n: unknown, word: string) => `${count(n)} ${Number(n) === 1 ? word : `${word}s`}`;

/** "about 12 min left", "about 1 h 20 min left", or null while there is nothing to go on. */
export function minutesLeftText(minutes: number | null | undefined): string | null {
  if (minutes == null) return null;
  if (minutes < 60) return `about ${minutes} min left`;
  const hours = Math.floor(minutes / 60), rest = minutes % 60;
  return `about ${hours} h${rest ? ` ${rest} min` : ""} left`;
}

/** Where the job stands, in one line: "Preparing: reading tables, 1,240 of 3,000 databases (58,280 tables so far), about
 *  12 min left", then the write and the Databricks jobs, "Needs you: 3 of 3,000 databases couldn't be read", or Ready. */
export function preparingLine(view: PrepareView | null | undefined): string {
  if (!view || view.state === "ready" || !view.state) return "Ready";
  const progress = view.progress ?? {};
  if (view.state === "needs_you") {
    const counts = view.counts ?? {};
    const unreadable = Number(counts.waiting_unreadable || 0), clash = Number(counts.waiting_clash || 0);
    const of = view.reading?.databases ? ` of ${count(view.reading.databases)}` : "";
    if (unreadable && !clash) return `Needs you: ${count(unreadable)}${of} ${unreadable === 1 && !of ? "database" : "databases"} couldn't be read`;
    if (clash && !unreadable) return `Needs you: ${words(clash, "database")} not added`;
    return `Needs you: ${words(counts.waiting, "database")} set aside`;
  }
  if (view.stage === "writing") {
    const written = progress.tables_written, total = progress.tables_total;
    return total ? `Preparing: writing the job's tables, ${count(written)} of ${count(total)}`
      : "Preparing: writing the job's tables";
  }
  if (view.stage === "syncing") {
    return progress.jobs ? `Preparing: making the job's ${count(progress.jobs)} Databricks jobs`
      : "Preparing: making the job's Databricks jobs";
  }
  const reading = view.reading;
  if (!reading || !reading.databases) return "Preparing: reading tables";
  const left = minutesLeftText(reading.minutes_left);
  return `Preparing: reading tables, ${count(reading.read)} of ${words(reading.databases, "database")} `
    + `(${words(reading.tables, "table")} so far)${left ? `, ${left}` : ""}`;
}

/** How far the job's bar is ({done, total}), or null when the stage has no count (making its Databricks jobs). */
export function preparingBar(view: PrepareView | null | undefined): { done: number; total: number } | null {
  if (!view || view.state === "ready") return null;
  const progress = view.progress ?? {};
  if (view.stage === "writing" && Number(progress.tables_total)) {
    return { done: Number(progress.tables_written || 0), total: Number(progress.tables_total) };
  }
  if ((view.stage ?? "reading") === "reading" && view.reading?.databases) {
    return { done: Number(view.reading.read || 0), total: Number(view.reading.databases) };
  }
  return null;
}

/** Why one database was set aside, and what is to happen to it now. */
export function setAsideWhy(entry: SetAside): string {
  const why = entry.why ? `: ${entry.why}` : "";
  const reason = entry.kind === "unreadable" ? `couldn't be read${why}`
    : entry.kind === "clash" ? `not added${why}${entry.job ? ` (the job ${entry.job})` : ""}`
    : `left out${why}`;
  if (entry.again) return `${reason}; to be read again`;
  if (entry.replan) return `${reason}; to be planned again with the new target names`;
  return reason;
}

/** What the job's schedule does once it is Ready. */
export function scheduleWhenReady(view: PrepareView | null | undefined): string {
  return view?.turn_on ? "Schedule: on when ready" : "Schedule: stays paused when ready";
}

/** The page's note on Run now and the schedule while the job isn't Ready. */
export const AVAILABLE_WHEN_READY = "available when the job is ready";
