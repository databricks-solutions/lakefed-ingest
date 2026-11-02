import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { PageHead, ErrorBox, Section, ToastOn } from "../components";
import { groupsRefusal, usePermissions } from "../permissions";

// The values in words, as the Edit view names them.
const OPTION_LABELS: Record<string, string> = { sqlserver: "SQL Server", oracle: "Oracle", postgresql: "PostgreSQL", mysql: "MySQL",
  ct: "Change tracking", cursor: "Cursor column", full: "Full reload", merge: "Merge: current state", append: "Append: change log" };

type Field = { key: string; label: string; required?: boolean; help?: string; type?: "text" | "number" | "checkbox"; options?: string[]; initial?: any };

const FIELDS: Field[] = [
  { key: "task_collection", label: "Ingestion group", required: true },
  { key: "src_type", label: "Source type", options: ["sqlserver", "oracle", "postgresql", "mysql"], initial: "sqlserver" },
  { key: "load_type", label: "Load type", options: ["ct", "cursor", "full"], initial: "ct" },
  { key: "use_remote_query", label: "Read through the UC connection", type: "checkbox", initial: false,
    help: "read through a Unity Catalog connection instead of Spark JDBC" },
  { key: "src_connection", label: "UC connection", help: "required when reading through the UC connection" },
  { key: "src_host", label: "Server", required: true },
  { key: "src_port", label: "Port", type: "number", help: "blank = engine default" },
  { key: "src_database", label: "Source database", required: true },
  { key: "secret_key", label: "Secret key", required: true, help: "secret holding JSON {user, password}" },
  { key: "src_schema", label: "Source schema", required: true, initial: "dbo" },
  { key: "src_table", label: "Source table", required: true },
  { key: "primary_key", label: "Primary key", help: "comma-separated; required for ct and cursor" },
  { key: "select_list", label: "Columns", required: true, help: "explicit, comma-separated columns" },
  { key: "sink_catalog", label: "Target catalog", required: true },
  { key: "sink_schema", label: "Target schema", required: true },
  { key: "sink_table", label: "Target table", required: true },
  { key: "client_id", label: "Client ID", help: "set to share one sink across databases" },
  { key: "write_mode", label: "Write mode", options: ["merge", "append"], initial: "merge",
    help: "append: the sink is a change log (ct and full)" },
  { key: "client_attrs", label: "Identity columns", help: 'JSON object, e.g. {"tenant_name": "Acme"}' },
  { key: "cursor_column", label: "Cursor column", help: "cursor only (rowversion recommended)" },
  { key: "load_partitioned", label: "Partitioned first load", type: "checkbox", initial: false },
  { key: "partition_col", label: "Partition column" },
  { key: "partition_size_mb", label: "Partition size (MB)", type: "number" },
  { key: "freshness_sla_minutes", label: "Freshness target (minutes)", type: "number", help: "most minutes since its last load; blank = no target" },
  { key: "jdbc_options", label: "JDBC options", help: 'JSON object, e.g. {"fetchsize": "50000"}' },
];
// The connection holds the credentials of a remote_query read: no host or secret then.
const JDBC_ONLY = ["src_host", "secret_key"];

export default function AddTable() {
  const initial = Object.fromEntries(FIELDS.map((f) => [f.key, f.initial ?? (f.type === "checkbox" ? false : "")]));
  const [row, setRow] = useState<Record<string, any>>(initial);
  const locked = groupsRefusal(usePermissions().data, [row.task_collection]);
  const qc = useQueryClient();
  const add = useMutation({
    mutationFn: () => api.post<{ id: number }>("/tables", {
      ...row,
      src_port: row.src_port === "" ? null : Number(row.src_port),
      partition_size_mb: row.partition_size_mb === "" ? null : Number(row.partition_size_mb),
      freshness_sla_minutes: row.freshness_sla_minutes === "" ? null : Number(row.freshness_sla_minutes),
    }),
    onSuccess: () => qc.invalidateQueries(),
  });
  return (
    <>
      <PageHead title="Add a table" lead="Adds one table to an ingestion group; the group's next run loads it. For many tables, use Import CSV or Bulk import." />
      <Section title="The table">
        <form className="form-grid" onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
          {FIELDS.map((f) => { const required = f.required && !(row.use_remote_query && JDBC_ONLY.includes(f.key)); return (
            <label key={f.key} className={f.key === "select_list" ? "wide" : ""}>
              {f.label}{required && " *"}
              {f.options ? (
                <select value={row[f.key]} onChange={(e) => setRow({ ...row, [f.key]: e.target.value })}>
                  {f.options.map((o) => <option key={o} value={o}>{OPTION_LABELS[o] ?? o}</option>)}
                </select>
              ) : f.type === "checkbox" ? (
                <input type="checkbox" checked={!!row[f.key]} onChange={(e) => setRow({ ...row, [f.key]: e.target.checked })} />
              ) : (
                <input type={f.type ?? "text"} value={row[f.key]} required={required}
                  onChange={(e) => setRow({ ...row, [f.key]: e.target.value })} />
              )}
              {f.help && <span className="muted small">{f.help}</span>}
            </label>
          ); })}
          {locked && <p className="wide muted small">{locked}.</p>}
          <div className="wide"><button className="primary" disabled={add.isPending || !!locked}>Add table</button></div>
        </form>
        <ErrorBox error={add.error} />
        <ToastOn text={add.data ? `Added control row ${add.data.id}` : null} />
      </Section>
    </>
  );
}
