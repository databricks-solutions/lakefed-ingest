import { useMemo, useState, type ReactNode } from "react";
import { AgGridReact } from "ag-grid-react";
import { themeQuartz, type ColDef, type GridOptions } from "ag-grid-community";
import { ApiError, type Row } from "./api";

const theme = themeQuartz.withParams({ fontSize: 13, headerFontSize: 13, spacing: 5, wrapperBorderRadius: 6 });

export function fmtTime(v: unknown): string {
  if (!v) return "";
  const d = new Date(String(v));
  return isNaN(d.getTime()) ? String(v) : d.toLocaleString();
}

/** Column defs from the first row's keys (with per-column overrides), so pages stay short. */
export function autoColumns(rows: Row[], overrides: Record<string, Partial<ColDef>> = {}, hide: string[] = []): ColDef[] {
  const keys = rows.length ? Object.keys(rows[0]) : Object.keys(overrides);
  return keys.filter((k) => !hide.includes(k)).map((k) => ({
    field: k,
    headerName: k,
    ...(/(_at|started|finished|ended)$/.test(k)
      ? { valueFormatter: (p: any) => fmtTime(p.value) } : {}),
    ...overrides[k],
  }));
}

export function Grid(props: {
  rows: Row[] | undefined;
  columns?: ColDef[];
  height?: number | string;
  selection?: "single" | "multiple";
  onSelect?: (rows: Row[]) => void;
  options?: GridOptions;
  getRowId?: (r: Row) => string;
}) {
  const cols = useMemo(() => props.columns ?? autoColumns(props.rows ?? []), [props.columns, props.rows]);
  return (
    <div style={{ height: props.height ?? 420, width: "100%" }}>
      <AgGridReact
        theme={theme}
        rowData={props.rows ?? []}
        columnDefs={cols}
        defaultColDef={{ sortable: true, filter: true, resizable: true, minWidth: 90 }}
        autoSizeStrategy={{ type: "fitCellContents" }}
        rowSelection={props.selection
          ? { mode: props.selection === "single" ? "singleRow" : "multiRow", enableClickSelection: props.selection === "single" }
          : undefined}
        onSelectionChanged={(e) => props.onSelect?.(e.api.getSelectedRows())}
        getRowId={props.getRowId ? (p) => props.getRowId!(p.data) : undefined}
        loading={props.rows === undefined}
        {...props.options}
      />
    </div>
  );
}

export function Metric({ label, value, tone }: { label: string; value: ReactNode; tone?: "bad" | "warn" | "ok" }) {
  return (
    <div className={`metric ${tone ?? ""}`}>
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value}</div>
    </div>
  );
}

export function Badge({ value }: { value: unknown }) {
  const v = String(value ?? "");
  const tone = ["failed", "FAILED", "invalid", "INTERNAL_ERROR"].includes(v) ? "bad"
    : ["running", "RUNNING", "PENDING", "seeding", "skipped", "breached"].includes(v) ? "warn"
    : ["ok", "completed", "SUCCESS", "registered", "valid", "incremental", "loaded"].includes(v) ? "ok" : "";
  return <span className={`badge ${tone}`}>{v}</span>;
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error as ApiError;
  return (
    <div className="alert bad">
      <strong>Error:</strong> {e.message ?? String(error)}
      {e.hint && <div className="hint">{e.hint}</div>}
    </div>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "ok" | "warn" | "bad"; children: ReactNode }) {
  return <div className={`alert ${tone}`}>{children}</div>;
}

/** A button that asks for a second click before running a write. */
export function ConfirmButton(props: {
  label: string;
  confirmLabel?: string;
  onConfirm: () => unknown;
  disabled?: boolean;
  danger?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!armed)
    return (
      <button className={props.danger ? "danger" : "primary"} disabled={props.disabled || busy} onClick={() => setArmed(true)}>
        {props.label}
      </button>
    );
  return (
    <span className="confirm">
      <button
        className={props.danger ? "danger" : "primary"}
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try { await props.onConfirm(); } finally { setBusy(false); setArmed(false); }
        }}
      >
        {busy ? "Working…" : props.confirmLabel ?? `Confirm: ${props.label}`}
      </button>
      <button className="ghost" disabled={busy} onClick={() => setArmed(false)}>Cancel</button>
    </span>
  );
}

export function Section({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      <div className="card-head">
        <h2>{title}</h2>
        <div className="card-actions">{actions}</div>
      </div>
      {children}
    </section>
  );
}
