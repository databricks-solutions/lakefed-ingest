import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Row } from "../api";
import { ConfirmButton, ErrorBox, PageHead, Section, Tag, ToastOn, fmtInt, plural } from "../components";
import { groupsRefusal, usePermissions } from "../permissions";

/** The group settings the import fills each row with, in the order config_import.SETTINGS gives them. */
const SETTINGS: { key: string; label: string; options?: string[]; type?: "number" | "checkbox"; help?: string }[] = [
  { key: "src_type", label: "Source type", options: ["sqlserver", "oracle", "postgresql", "mysql"] },
  { key: "use_remote_query", label: "Read through the UC connection", type: "checkbox", help: "else Spark JDBC with a secret" },
  { key: "load_type", label: "Load type", options: ["ct", "cursor", "full"] },
  { key: "write_mode", label: "Write mode", options: ["merge", "append"] },
  { key: "fan_in", label: "Fan-in to shared tables", type: "checkbox", help: "each line gives its client_id" },
  { key: "sink_catalog", label: "Target catalog", help: "{database}, {schema} and {table} are filled per line" },
  { key: "sink_schema", label: "Target schema" },
  { key: "sink_table", label: "Target table" },
  { key: "freshness_sla_minutes", label: "Freshness target (minutes)", type: "number", help: "blank = none" },
  { key: "jdbc_options", label: "Read options", help: "JSON object; blank = none" },
];
const TONE: Record<string, "ok" | "bad" | "" | "first"> = { valid: "ok", imported: "ok", invalid: "bad", registered: "", skipped: "" };

interface Setting { setting: string; value?: unknown; mixed?: Record<string, number> }

/** Import a CSV of tables into one ingestion group: the CSV says where each table is, the group's settings say how
 * to ingest it. Preview every line first; Apply writes only when no line is invalid. */
export default function ImportCsv() {
  const [params] = useSearchParams();
  const [group, setGroup] = useState(params.get("group") ?? "");     // an ingestion group's page links here with it
  const [given, setGiven] = useState<Record<string, unknown>>({});
  const [csv, setCsv] = useState("");
  const qc = useQueryClient();
  const settings = useQuery({
    queryKey: ["collection-settings", group],
    queryFn: () => api.get<{ tables: number; settings: Setting[] }>(`/collections/${encodeURIComponent(group.trim())}/settings`),
    enabled: !!group.trim(),
  });
  const shared = Object.fromEntries((settings.data?.settings ?? []).map((s) => [s.setting, s]));
  // Import sends exactly the request that was previewed; any change asks for a new preview.
  const [previewed, setPreviewed] = useState<Row | null>(null);
  const preview = useMutation({
    mutationFn: () => {
      const request = { task_collection: group.trim(), settings: given, csv, dry_run: true };
      setPreviewed(request);
      return api.post<Row>("/import", request);
    },
  });
  const apply = useMutation({
    mutationFn: () => api.post<Row>("/import", { ...previewed, dry_run: false }),
    onSuccess: () => { qc.invalidateQueries(); preview.reset(); },
  });
  const changed = () => { preview.reset(); apply.reset(); setPreviewed(null); };
  const result = apply.data ?? preview.data;
  const counts: Record<string, number> = result?.counts ?? {};
  const locked = groupsRefusal(usePermissions().data, [group.trim()]);
  const set = (key: string, value: unknown) => { setGiven({ ...given, [key]: value }); changed(); };
  const shown = (key: string) => (key in given ? given[key] : shared[key]?.value ?? "");
  const isNew = settings.data && !settings.data.tables;
  return (
    <>
      <PageHead title="Import a CSV of tables"
        lead={<>Each line says where a table is; the ingestion group's settings say how to ingest it. Columns:
          src_connection (or src_host and secret_key), src_database, src_schema, src_table, select_list, primary_key,
          cursor_column, client_id and attr:&lt;column&gt; for a fan-in. An ingestion group's page exports the same CSV.</>} />
      <Section title="Ingestion group">
        <div className="toolbar">
          <label>Ingestion group <input value={group} placeholder="new or existing" onChange={(e) => { setGroup(e.target.value); setGiven({}); changed(); }} /></label>
          {settings.data && (isNew ? <span className="muted">A new group: set its settings below.</span>
            : <span className="muted">{fmtInt(settings.data.tables)} {plural(settings.data.tables, "table")} now; its shared settings are filled in.
              {" "}<Link to={`/collections/${encodeURIComponent(group.trim())}`}>Open the group</Link></span>)}
        </div>
        <ErrorBox error={settings.error} />
        {group.trim() && (
          <form className="form-grid" onSubmit={(e) => e.preventDefault()}>
            {SETTINGS.map((f) => {
              const mixed = shared[f.key]?.mixed;
              return (
                <label key={f.key}>
                  {f.label}
                  {f.options ? (
                    <select value={String(shown(f.key) ?? "")} onChange={(e) => set(f.key, e.target.value)}>
                      <option value="">(choose)</option>
                      {f.options.map((o) => <option key={o}>{o}</option>)}
                    </select>
                  ) : f.type === "checkbox" ? (
                    <input type="checkbox" checked={!!shown(f.key)} onChange={(e) => set(f.key, e.target.checked)} />
                  ) : (
                    <input type={f.type ?? "text"} value={String(shown(f.key) ?? "")}
                      onChange={(e) => set(f.key, f.type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value)} />
                  )}
                  {mixed && !(f.key in given)
                    ? <span className="small" style={{ color: "var(--warn)" }}>mixed: {Object.entries(mixed).map(([v, n]) => `${v || "blank"} (${fmtInt(n)})`).join(", ")}; set it for this import</span>
                    : f.help && <span className="muted small">{f.help}</span>}
                </label>
              );
            })}
          </form>
        )}
      </Section>
      <Section title="CSV">
        <div className="toolbar">
          <input type="file" accept=".csv,text/csv" onChange={async (e) => { const f = e.target.files?.[0]; if (f) { setCsv(await f.text()); changed(); } }} />
        </div>
        <textarea className="csv-input" rows={10} value={csv} spellCheck={false} placeholder="src_connection,src_database,src_schema,src_table,select_list,primary_key"
          onChange={(e) => { setCsv(e.target.value); changed(); }} />
        <div className="toolbar">
          <button className="primary" disabled={!group.trim() || !csv.trim() || preview.isPending} onClick={() => preview.mutate()}>Preview</button>
          {preview.data && previewed && (
            <ConfirmButton label={`Import ${fmtInt(counts.valid ?? 0)} ${plural(counts.valid ?? 0, "table")}`}
              disabled={!!counts.invalid || !counts.valid || !!locked} onConfirm={() => apply.mutateAsync()} />
          )}
          {preview.data && counts.invalid ? <span className="muted">Fix the invalid lines, then preview again.</span> : null}
        </div>
        {locked && <p className="muted small">Import: {locked}.</p>}
        <ErrorBox error={preview.error ?? apply.error} />
        <ToastOn text={apply.data ? `Imported ${fmtInt(counts.imported ?? 0)} ${plural(counts.imported ?? 0, "table")} into ${apply.data.task_collection}`
          + (counts.skipped ? `; ${fmtInt(counts.skipped)} skipped (already registered)` : "") : null} />
      </Section>
      {result && (
        <Section title={`Lines: ${fmtInt((result.lines ?? []).length)}`}
          note={Object.entries(counts).map(([s, n]) => `${fmtInt(n)} ${s}`).join(", ")}>
          <div className="table-wrap"><table className="data dense">
            <thead><tr><th className="num">Line</th><th>Table</th><th>Sink</th><th>Status</th><th>Problems</th></tr></thead>
            <tbody>
              {(result.lines ?? []).map((line: Row) => (
                <tr key={line.line}>
                  <td className="num">{line.line}</td><td className="name">{line.table}</td><td className="name">{line.sink}</td>
                  <td><Tag tone={TONE[line.status] ?? ""}>{line.status}</Tag></td>
                  <td>{(line.errors ?? []).join("; ")}</td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </Section>
      )}
    </>
  );
}
