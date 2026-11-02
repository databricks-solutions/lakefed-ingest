// The cells of one server row in a job's 'What it ingests' preview. Every create path sends the same fields per server
// (by_database.summary_of and new_job.summary), but each field is read as it comes, so a missing or differently shaped
// one shows as such instead of throwing, which would leave the page white.

export type Row = Record<string, any>;

const NUMBER = new Intl.NumberFormat("en-US");
const count = (value: unknown) => NUMBER.format(Number(value ?? 0) || 0);

export function serverRowCells(server: Row, labels: [string, string][]) {
  const loadTypes = server.load_types && typeof server.load_types === "object" ? Object.entries(server.load_types as Row) : [];
  const named = loadTypes.map(([key, n]) => `${labels.find(([k]) => k === key)?.[1] ?? key}${loadTypes.length > 1 ? ` ${count(n)}` : ""}`);
  const groups = Array.isArray(server.groups) ? server.groups.length : Number(server.groups ?? 0) || 0;
  return {
    server: String(server.server ?? ""),
    readThrough: String(server.read_through ?? ""),
    loadTypes: named.length ? named.join(", ") : "load types set when the tables are written",
    counts: `${count(server.databases)} · ${count(server.tables)}`,
    groups: count(groups),
  };
}

const TARGET_FIELDS: [string, string][] = [["sink_catalog", "catalog"], ["sink_schema", "schema"], ["sink_table", "table"]];

/** Why Create is off while target fields are blank ("Set the target catalog and schema to create the job"), or null.
 *  Only Create waits on them: the preview of what the job takes shows whatever they are. */
export function targetBlock(settings: Row): string | null {
  const blank = TARGET_FIELDS.filter(([key]) => !String(settings[key] ?? "").trim()).map(([, word]) => word);
  if (!blank.length) return null;
  const words = blank.length === 1 ? blank[0] : `${blank.slice(0, -1).join(", ")} and ${blank[blank.length - 1]}`;
  return `Set the target ${words} to create the job`;
}
