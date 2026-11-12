// The one line a "Run now" or "Retry now" confirmation shows: what the run loads, in plain words and without a word
// on cost, as the Databricks UI's own confirmations name what they act on and say no more. Pure, so the Node tests
// read it as it is (tests/js/run_dialog.test.mjs).

const NUMBER = new Intl.NumberFormat("en-US");

/** What the server's run preview says one run loads (ingestion_job_run_preview): the enabled tables of the job, the
 * servers and databases they are on, the first few table names, and the server and database by name when there is one. */
export interface RunPreview {
  tables: number;
  servers: number;
  databases: number;
  server: string | null;
  database: string | null;
  names: string[];
}

const counted = (count: number, word: string) => `${NUMBER.format(count)} ${count === 1 ? word : `${word}s`}`;

/** Table names as a sentence list: all of them when they are all there ("a, b and c"), else the first few and how many
 * more ("a, b, c and 999 more"). */
export function tableList(names: string[], total: number): string {
  if (!names.length) return "";
  if (total > names.length) return `${names.join(", ")} and ${NUMBER.format(total - names.length)} more`;
  if (names.length === 1) return names[0];
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** Where the tables are: "server / database" for one database, else the databases on the one server, else on how many
 * servers. */
export function sourceOf(preview: RunPreview): string {
  if (preview.databases <= 1 && preview.servers <= 1) {
    return [preview.server, preview.database].filter(Boolean).join(" / ");
  }
  if (preview.servers === 1) return `${counted(preview.databases, "database")} on ${preview.server}`;
  return `${counted(preview.databases, "database")} on ${counted(preview.servers, "server")}`;
}

/** Run now's line from the run preview. A check job (`check`) first looks at its sources for changes, so it says it
 * loads the tables that changed. */
export function runNowLine(preview: RunPreview, check: boolean): string {
  if (!preview.tables) return "No table is enabled, so it loads nothing.";
  const from = sourceOf(preview);
  const tables = tableList(preview.names, preview.tables);
  const where = from ? ` from ${from}` : "";
  return check
    ? `It checks ${counted(preview.tables, "table")}${where} for changes and loads those that changed: ${tables}.`
    : `It loads ${counted(preview.tables, "table")}${where}: ${tables}.`;
}

/** Run now's line when the preview could not be read: the page's own count and the names of the job's groups. */
export function runNowFallbackLine(tables: number, groups: string[], check: boolean): string {
  const shown = groups.slice(0, 3);
  const where = `${counted(groups.length, "ingestion group")}${shown.length ? `: ${tableList(shown, groups.length)}` : ""}`;
  return check
    ? `It checks ${counted(tables, "table")} for changes and loads those that changed, in ${where}.`
    : `It loads ${counted(tables, "table")} in ${where}.`;
}

/** Retry now's line: one database, which tables load again from where they stopped. */
export function retryLine(database: string, server: string): string {
  return `It loads the enabled tables of ${database} on ${server} again, from their checkpoints.`;
}
