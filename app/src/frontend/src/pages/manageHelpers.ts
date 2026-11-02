/** Manage's export link: the ingestion jobs ticked, or every job when none is. */
export function exportHref(ids: number[]): string {
  return ids.length ? `/api/manage/export.csv?jobs=${ids.join(",")}` : "/api/manage/export.csv";
}

/** The state database's schema as GET /api/manage/version reports it (main._schema_state). */
export interface SchemaState {
  database: number | null;
  app: number;
  check: { state?: string; error?: string | null; at?: string };
  run: { run_id: number; url?: string | null } | null;
  error: string | null;
}

/** What Version says of the state database's schema: upgrading while the setup job runs, else behind, missing, or the
 * last check's failure. Null when the schema is current and there is nothing to say. */
export function schemaNote(schema: SchemaState | undefined): { tone: "info" | "warn" | "bad"; text: string } | null {
  if (!schema) return null;
  if (schema.run) return { tone: "info", text: "Upgrading the state database." };
  if (schema.error) return { tone: "bad", text: `${schema.error}.` };
  if (schema.database === 0) {
    return { tone: "warn", text: "The state database has no schema yet: run the setup job once, as the deploy steps say." };
  }
  if (schema.database != null && schema.database < schema.app) {
    const why = schema.check.state === "failed" && schema.check.error
      ? ` The app could not start the setup job: ${schema.check.error}.` : "";
    return { tone: "warn", text: `The state database is behind (version ${schema.database}, the app's is ${schema.app}). `
             + `The next ingestion or check run upgrades it, or the app when it next starts.${why}` };
  }
  return null;
}
