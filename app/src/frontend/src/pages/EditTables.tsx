import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Row } from "../api";
import { Badge, ConfirmButton, ErrorBox, Grid, Notice, autoColumns, fmtTime, plural } from "../components";

// Every edit runs through the server's plan first (preview): what changes, which tables reseed and
// why, warnings, and validation errors. Apply is only offered for a clean preview of the same edit.

export const EDIT_FIELDS: { key: string; label?: string; type?: "number" | "checkbox" | "select"; options?: string[]; help?: string }[] = [
  { key: "task_collection", label: "Ingestion group (task_collection)" }, { key: "task_enabled", type: "checkbox" }, { key: "tier" }, { key: "priority", type: "number" },
  { key: "freshness_sla_minutes", label: "Freshness target (freshness_sla_minutes)", type: "number", help: "minutes; blank = no target" },
  { key: "src_type", type: "select", options: ["sqlserver", "oracle", "postgresql", "mysql"] },
  { key: "use_remote_query", type: "checkbox", help: "read through the UC connection in src_connection" },
  { key: "src_connection" }, { key: "src_host" },
  { key: "src_port", type: "number" }, { key: "secret_key" }, { key: "src_database" }, { key: "src_schema" }, { key: "src_table" },
  { key: "select_list" }, { key: "primary_key" }, { key: "load_type", type: "select", options: ["ct", "cursor", "full"] },
  { key: "cursor_column" }, { key: "sink_catalog" }, { key: "sink_schema" }, { key: "sink_table" }, { key: "client_id" },
  { key: "write_mode", type: "select", options: ["merge", "append"], help: "changing it needs a new sink" },
  { key: "client_attrs", help: "JSON object {column: value}" },
  { key: "load_partitioned", type: "checkbox" }, { key: "partition_col" }, { key: "partition_size_mb", type: "number" },
  { key: "jdbc_options", help: "JSON object" },
];
export const BULK_FIELDS = ["task_collection", "task_enabled", "tier", "priority", "freshness_sla_minutes", "load_partitioned",
  "partition_col", "partition_size_mb", "jdbc_options", "sink_catalog", "sink_schema", "src_connection", "use_remote_query"];

interface PlanResult { summary: Row; plans: Row[] }

function PlanView({ result }: { result: PlanResult }) {
  const s = result.summary;
  const rows = result.plans.map((p) => ({
    table: p.table,
    changes: Object.entries(p.changes as Record<string, [any, any]>).map(([f, [o, n]]) => `${f}: ${o ?? "∅"} → ${n ?? "∅"}`).join("; ") || "(no change)",
    reseed: p.reseed ?? "",
    notes: (p.notes as string[]).join(" · "),
    errors: (p.errors as string[]).join("; "),
  }));
  return (
    <>
      <Notice tone={s.errors ? "bad" : s.reseed ? "warn" : "ok"}>
        {s.applied ? "Applied" : "Preview"}: {s.changed} of {s.rows} {plural(s.rows, "table")} change
        {s.reseed ? `, ${s.reseed} will reseed on their next run` : ""}{s.errors ? `, ${s.errors} have errors (nothing can be applied)` : ""}.
      </Notice>
      <Grid rows={rows} height={Math.min(80 + rows.length * 42, 320)}
        columns={autoColumns(rows, { changes: { minWidth: 320 }, notes: { flex: 1, minWidth: 320, tooltipField: "notes" },
          errors: { minWidth: 240, cellStyle: { color: "var(--bad)" }, tooltipField: "errors" },
          reseed: { cellRenderer: (p: any) => p.value ? <Badge value="seeding" /> : null } })} />
    </>
  );
}

function useEdit(ids: number[], onDone: () => void) {
  const qc = useQueryClient();
  const [preview, setPreview] = useState<PlanResult | null>(null);
  const [previewKey, setPreviewKey] = useState("");
  const run = useMutation({
    mutationFn: ({ changes, dry_run }: { changes: Row; dry_run: boolean }) =>
      api.post<PlanResult>("/tables/edit", { ids, changes, dry_run }),
    onSuccess: (r, v) => {
      setPreview(r);
      if (v.dry_run) setPreviewKey(JSON.stringify(v.changes));
      else { qc.invalidateQueries(); onDone(); }
    },
  });
  return { preview, previewKey, run, reset: () => { setPreview(null); setPreviewKey(""); run.reset(); } };
}

function ApplyBar({ changes, edit }: { changes: Row; edit: ReturnType<typeof useEdit> }) {
  const key = JSON.stringify(changes);
  const ready = edit.preview && !edit.preview.summary.applied && edit.previewKey === key
    && !edit.preview.summary.errors && edit.preview.summary.changed > 0;
  return (
    <div className="toolbar">
      <button className="primary" disabled={!Object.keys(changes).length || edit.run.isPending}
        onClick={() => edit.run.mutate({ changes, dry_run: true })}>Preview changes</button>
      <ConfirmButton label="Apply" disabled={!ready}
        confirmLabel={`Confirm: apply to ${edit.preview?.summary.changed ?? 0} ${plural(edit.preview?.summary.changed ?? 0, "table")}`}
        onConfirm={() => edit.run.mutateAsync({ changes, dry_run: false })} />
      {edit.preview && edit.previewKey !== key && !edit.preview.summary.applied && <span className="muted">Edited since the preview: preview again.</span>}
    </div>
  );
}

export function EditOne({ id, onClose }: { id: number; onClose: () => void }) {
  const detail = useQuery({ queryKey: ["table", id], queryFn: () => api.get<Row>(`/tables/${id}`) });
  const [form, setForm] = useState<Row>({});
  useEffect(() => { if (detail.data) setForm({ ...detail.data.control }); }, [detail.data]);
  const changes = useMemo(() => {
    const c: Row = {};
    const orig = detail.data?.control ?? {};
    for (const f of EDIT_FIELDS) {
      const a = orig[f.key] ?? null, b = form[f.key] === "" ? null : form[f.key] ?? null;
      if (String(a) !== String(b)) c[f.key] = b;
    }
    return c;
  }, [form, detail.data]);
  const edit = useEdit([id], () => detail.refetch());
  const cp = detail.data?.checkpoint;
  return (
    <div className="drawer">
      <div className="drawer-head">
        <h2>Edit table {id}</h2>
        <button className="ghost" onClick={onClose}>Close</button>
      </div>
      <ErrorBox error={detail.error} />
      {cp && <p className="muted small">Checkpoint: <Badge value={cp.phase} /> <Badge value={cp.status} /> last success {fmtTime(cp.last_success_at) || "never"}
        {cp.detail ? ` · ${cp.detail}` : ""}</p>}
      <div className="form-grid">
        {EDIT_FIELDS.map((f) => (
          <label key={f.key} className={changes[f.key] !== undefined ? "changed" : ""}>
            {f.label ?? f.key}
            {f.type === "checkbox" ? (
              <input type="checkbox" checked={!!form[f.key]} onChange={(e) => setForm({ ...form, [f.key]: e.target.checked })} />
            ) : f.type === "select" ? (
              <select value={form[f.key] ?? ""} onChange={(e) => setForm({ ...form, [f.key]: e.target.value })}>
                {f.options!.map((o) => <option key={o}>{o}</option>)}
              </select>
            ) : (
              <input type={f.type ?? "text"} value={form[f.key] ?? ""} onChange={(e) => setForm({ ...form, [f.key]: e.target.value })} />
            )}
            {f.help && <span className="muted small">{f.help}</span>}
          </label>
        ))}
      </div>
      <ApplyBar changes={changes} edit={edit} />
      <ErrorBox error={edit.run.error} />
      {edit.preview && <PlanView result={edit.preview} />}
      <h3>History</h3>
      {detail.data?.changes?.length ? <Grid rows={detail.data.changes} height={220} />
        : <p className="muted small">No changes recorded for this table.</p>}
    </div>
  );
}

export function EditBulk({ ids, onClose }: { ids: number[]; onClose: () => void }) {
  const [values, setValues] = useState<Row>({});
  const [field, setField] = useState(BULK_FIELDS[0]);
  const edit = useEdit(ids, () => setValues({}));
  const meta = EDIT_FIELDS.find((f) => f.key === field)!;
  return (
    <div className="drawer">
      <div className="drawer-head">
        <h2>Bulk edit {ids.length} {plural(ids.length, "table")}</h2>
        <button className="ghost" onClick={onClose}>Close</button>
      </div>
      <p className="muted small">Bulk edits are limited to fields that are safe across many tables. Source, key and sink
        table name changes are edited one table at a time.</p>
      <div className="toolbar">
        <label>Field<select value={field} onChange={(e) => setField(e.target.value)}>{BULK_FIELDS.map((f) => <option key={f}>{f}</option>)}</select></label>
        <label>Value
          {meta.type === "checkbox"
            ? <select value={String(values[field] ?? "")} onChange={(e) => setValues({ ...values, [field]: e.target.value === "true" })}>
                <option value="">(choose)</option><option value="true">true</option><option value="false">false</option></select>
            : <input type={meta.type === "number" ? "number" : "text"} value={values[field] ?? ""}
                onChange={(e) => setValues({ ...values, [field]: e.target.value })} />}
        </label>
        <button className="ghost" onClick={() => { const v = { ...values }; delete v[field]; setValues(v); }}>Clear field</button>
      </div>
      {Object.keys(values).length > 0 && <p className="small">Setting: {Object.entries(values).map(([k, v]) => `${k} = ${v === "" ? "∅" : String(v)}`).join(", ")}</p>}
      <ApplyBar changes={values} edit={edit} />
      <ErrorBox error={edit.run.error} />
      {edit.preview && <PlanView result={edit.preview} />}
    </div>
  );
}
