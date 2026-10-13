import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { ErrorBox, Notice, Section } from "../components";

type Field = { key: string; label: string; required?: boolean; help?: string; type?: "text" | "number" | "checkbox"; options?: string[]; initial?: any };

const FIELDS: Field[] = [
  { key: "task_collection", label: "task_collection", required: true },
  { key: "src_type", label: "src_type", options: ["sqlserver", "oracle", "postgresql", "mysql"], initial: "sqlserver" },
  { key: "load_type", label: "load_type", options: ["ct", "cursor", "full"], initial: "ct" },
  { key: "src_host", label: "src_host", required: true },
  { key: "src_port", label: "src_port", type: "number", help: "blank = engine default" },
  { key: "src_database", label: "src_database", required: true },
  { key: "secret_key", label: "secret_key", required: true, help: "secret holding JSON {user, password}" },
  { key: "src_schema", label: "src_schema", required: true, initial: "dbo" },
  { key: "src_table", label: "src_table", required: true },
  { key: "primary_key", label: "primary_key", help: "comma-separated; required for ct and cursor" },
  { key: "select_list", label: "select_list", required: true, help: "explicit, comma-separated columns" },
  { key: "sink_catalog", label: "sink_catalog", required: true },
  { key: "sink_schema", label: "sink_schema", required: true },
  { key: "sink_table", label: "sink_table", required: true },
  { key: "client_id", label: "client_id", help: "set to share one sink across databases" },
  { key: "cursor_column", label: "cursor_column", help: "cursor only (rowversion recommended)" },
  { key: "load_partitioned", label: "load_partitioned", type: "checkbox", initial: false },
  { key: "partition_col", label: "partition_col" },
  { key: "partition_size_mb", label: "partition_size_mb", type: "number" },
];

export default function AddTable() {
  const initial = Object.fromEntries(FIELDS.map((f) => [f.key, f.initial ?? (f.type === "checkbox" ? false : "")]));
  const [row, setRow] = useState<Record<string, any>>(initial);
  const qc = useQueryClient();
  const add = useMutation({
    mutationFn: () => api.post<{ id: number }>("/tables", {
      ...row,
      src_port: row.src_port === "" ? null : Number(row.src_port),
      partition_size_mb: row.partition_size_mb === "" ? null : Number(row.partition_size_mb),
    }),
    onSuccess: () => qc.invalidateQueries(),
  });
  return (
    <>
      <h1>Add a table</h1>
      <Section title="New control row">
        <p className="muted small">Adds one control row; the next run of its collection seeds it. For many tables use Bulk import.</p>
        <form className="form-grid" onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
          {FIELDS.map((f) => (
            <label key={f.key} className={f.key === "select_list" ? "wide" : ""}>
              {f.label}{f.required && " *"}
              {f.options ? (
                <select value={row[f.key]} onChange={(e) => setRow({ ...row, [f.key]: e.target.value })}>
                  {f.options.map((o) => <option key={o}>{o}</option>)}
                </select>
              ) : f.type === "checkbox" ? (
                <input type="checkbox" checked={!!row[f.key]} onChange={(e) => setRow({ ...row, [f.key]: e.target.checked })} />
              ) : (
                <input type={f.type ?? "text"} value={row[f.key]} required={f.required}
                  onChange={(e) => setRow({ ...row, [f.key]: e.target.value })} />
              )}
              {f.help && <span className="muted small">{f.help}</span>}
            </label>
          ))}
          <div className="wide"><button className="primary" disabled={add.isPending}>Add table</button></div>
        </form>
        <ErrorBox error={add.error} />
        {add.data && <Notice tone="ok">Added control row {add.data.id}.</Notice>}
      </Section>
    </>
  );
}
