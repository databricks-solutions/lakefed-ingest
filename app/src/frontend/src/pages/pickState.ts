// The tick states of the Chosen pickers' top rows (SourcePick and Discover). Kept free of React so a test can run it
// with node (tests/frontend_pick_test.py).

export type Pick = "all" | "part" | "none";

/** Whether a database's name contains a filter, in any case: the rule a whole-server choice covers databases by. */
export const underFilter = (database: unknown, filter: string) => String(database).toLowerCase().includes(filter.toLowerCase());

/** The SQL Server LIKE pattern for the database names containing a filter, as underFilter matches them, so reading a
 *  whole server under a filter reads only those databases. Its wildcards (_, % and [) are taken literally. Null when
 *  there is no filter, or when it has a character the server's listing doesn't take: the whole server is read then. */
export function likeContaining(filter: string): string | null {
  if (!filter) return null;
  const pattern = `%${filter.replace(/\[/g, "[[]").replace(/_/g, "[_]").replace(/%/g, "[%]")}%`;
  return /^[A-Za-z0-9_%\-\[\].$#@]{1,128}$/.test(pattern) ? pattern : null;
}

/**
 * The server row's tick. It is ticked when the whole server is chosen under a filter that covers every database the page
 * shows (an empty filter covers them all; a filter covers the databases whose names contain it). It is in part when
 * anything else is chosen, and empty when nothing is.
 */
export function serverRowPick(whole: { on: boolean; filter: string }, pageFilter: string, anythingElseChosen: boolean): Pick {
  const covers = whole.on && (whole.filter === "" || underFilter(pageFilter, whole.filter));
  if (covers) return "all";
  return whole.on || anythingElseChosen ? "part" : "none";
}

/** What a database's check said: the login can open it, can't (it is greyed out and can't be chosen), or not known
 *  yet (still being checked, or listed without a check). */
export type Access = "open" | "closed" | "unknown";
export const accessOf = (row: { open?: boolean | null } | undefined): Access =>
  row?.open === true ? "open" : row?.open === false ? "closed" : "unknown";

/** The databases of these that may be read: every one but those the login can't open, which nothing reads (their
 *  rows are greyed out, their chevron is off and a whole-database choice leaves them out). */
export const readable = (names: string[], seen: Record<string, { open?: boolean | null } | undefined>): string[] =>
  names.filter((name) => accessOf(seen[name]) !== "closed");

/** The whole server's part of a choice: the databases its filter keeps, less those the login can't open; their
 *  tables as counted so far, and how many of them aren't checked yet. */
export type ServerCount = { matched: number; closed: number; unknown: number; tables: number };

/** How much a choice takes, counted as the step shows it at its top: databases chosen (whole or in part) and their
 *  tables. A whole database's tables are its check's count; one not counted yet makes the tables "counting". */
export function selectionCounts(server: ServerCount | null, wholeTables: (number | null)[], pickedTables: number[]):
    { databases: number; tables: number; counting: boolean; leftOut: number } {
  const serverDatabases = server ? Math.max(0, server.matched - server.closed) : 0;
  const known = wholeTables.filter((n): n is number => n != null);
  return {
    databases: serverDatabases + wholeTables.length + pickedTables.length,
    tables: (server?.tables ?? 0) + known.reduce((a, b) => a + b, 0) + pickedTables.reduce((a, b) => a + b, 0),
    counting: Boolean(server?.unknown) || known.length < wholeTables.length,
    leftOut: server?.closed ?? 0,
  };
}

const n = (value: number) => value.toLocaleString("en-US");

/** "Chosen: 3 of 201 databases, 3,000 tables", or just the databases while some tables aren't counted yet (the page
 *  shows a spinner for those). */
export function countsText(counts: { databases: number; tables: number; counting: boolean }, listed: number): string {
  const tables = counts.counting ? null : `${n(counts.tables)} ${counts.tables === 1 ? "table" : "tables"}`;
  return `Chosen: ${n(counts.databases)} of ${n(listed)} ${listed === 1 ? "database" : "databases"}${tables ? `, ${tables}` : ""}`;
}
