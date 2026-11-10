// Small pure helpers for table cells that stay on one line, so the Node tests read them as they are
// (tests/js/cells.test.mjs): the display name of a database type, a host with its port, and whether a cell's text is cut.

/** The databases the app reads, by their lower-case source type, in the words Catalog Explorer uses for them. */
export const SOURCE_TYPE_NAMES: Record<string, string> = {
  sqlserver: "SQL Server", oracle: "Oracle", postgresql: "PostgreSQL", mysql: "MySQL",
};

/** A Unity Catalog connection's type for people: "SQLSERVER" -> "SQL Server". A type with no name here shows as it came
 * (a connection type of a kind the app does not read still says what it is), and none shows as nothing. */
export function connectionTypeName(type: unknown): string {
  const raw = type === null || type === undefined ? "" : String(type);
  return SOURCE_TYPE_NAMES[raw.toLowerCase()] ?? raw;
}

/** A host as it is copied and shown in full: with its port when it has one ("db.example.com:1433"). */
export function hostLabel(host: string, port?: unknown): string {
  return port ? `${host}:${port}` : host;
}

/** Whether text was cut off by the ellipsis, from its box's scroll and client widths; one pixel of rounding is no cut. */
export function isCut(scrollWidth: number, clientWidth: number): boolean {
  return scrollWidth > clientWidth + 1;
}

/** Whether a click on text with a tooltip stays with the tooltip, which it shows or hides, instead of also reaching the
 * row, link or button around the text. Text with no tooltip has nothing to show, so its click always goes on to what
 * is around it; `passClick` lets a click on text that has one do both. */
export function tipKeepsClick(text: string, passClick: boolean): boolean {
  return text !== "" && !passClick;
}
