import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AgGridReact } from "ag-grid-react";
import { themeQuartz, type ColDef } from "ag-grid-community";
import { api, type Row } from "../api";
import { useSettings } from "../App";
import { Badge, ConfirmButton, ErrorBox, Grid, Notice, Section, autoColumns, fmtTime } from "../components";

const EDITABLE = ["include", "load_type", "primary_key", "cursor_column", "load_partitioned", "partition_col",
  "partition_size_mb", "sink_catalog", "sink_schema", "sink_table", "client_id"];
const BULK_FIELDS = EDITABLE.filter((f) => f !== "include");
const key = (r: Row) => `${r.src_database}\u0000${r.src_schema}\u0000${r.src_table}`;
const theme = themeQuartz.withParams({ fontSize: 13, headerFontSize: 13, spacing: 5, wrapperBorderRadius: 6 });

interface Shared {
  sink_catalog: string; sink_schema: string; sink_table: string; client_id: string;
  partition_threshold_mb: number; partition_size_mb: number; include_default: boolean;
}

function DiscoverForm({ onStarted }: { onStarted: (id: string) => void }) {
  const [f, setF] = useState({ src_host: "", src_port: "", databases: "", secret_key_template: "{database}" });
  const start = useMutation({
    mutationFn: () => api.post<{ discovery_id: string; run_id: number; databases: number }>("/discoveries", {
      ...f, src_port: f.src_port ? Number(f.src_port) : null }),
    onSuccess: (r) => onStarted(r.discovery_id),
  });
  return (
    <Section title="1. Discover tables">
      <p className="muted small">Runs the discovery job on the ingestion network path; it reads catalog metadata only
        (no table data). SQL Server for now.</p>
      <form className="form-grid" onSubmit={(e) => { e.preventDefault(); start.mutate(); }}>
        <label>src_host *<input required value={f.src_host} onChange={(e) => setF({ ...f, src_host: e.target.value })} /></label>
        <label>src_port<input type="number" placeholder="1433" value={f.src_port} onChange={(e) => setF({ ...f, src_port: e.target.value })} /></label>
        <label>Secret key template *
          <input required value={f.secret_key_template} onChange={(e) => setF({ ...f, secret_key_template: e.target.value })} />
          <span className="muted small">secret with JSON {"{user, password}"}; <code>{"{database}"}</code> is replaced per database</span>
        </label>
        <label className="wide">Databases * (comma- or newline-separated)
          <textarea required rows={3} value={f.databases} onChange={(e) => setF({ ...f, databases: e.target.value })} />
        </label>
        <div className="wide"><button className="primary" disabled={start.isPending}>{start.isPending ? "Starting…" : "Discover"}</button></div>
      </form>
      <ErrorBox error={start.error} />
      {start.data && <Notice tone="ok">Discovery {start.data.discovery_id} started for {start.data.databases} databases
        (job run {start.data.run_id}). It appears below; results arrive in a few minutes.</Notice>}
    </Section>
  );
}

export default function BulkImport() {
  const { config } = useSettings();
  const qc = useQueryClient();
  const [discoveryId, setDiscoveryId] = useState<string>("");
  const discoveries = useQuery({
    queryKey: ["discoveries"],
    queryFn: () => api.get("/discoveries"),
    refetchInterval: (q) => ((q.state.data as Row[] | undefined)?.some((d) => Number(d.running) > 0) ? 10_000 : false),
  });
  const did = discoveryId || discoveries.data?.[0]?.discovery_id || "";
  const dbs = useQuery({
    queryKey: ["discovery-dbs", did],
    queryFn: () => api.get(`/discoveries/${did}/databases`),
    enabled: !!did,
    refetchInterval: (q) => ((q.state.data as Row[] | undefined)?.some((d) => d.status === "running") ? 10_000 : false),
  });
  const running = (dbs.data ?? []).some((d) => d.status === "running");

  const [collection, setCollection] = useState("");
  const [shared, setShared] = useState<Shared>({
    sink_catalog: "main", sink_schema: "bronze_{database}", sink_table: "{table}", client_id: "",
    partition_threshold_mb: config?.defaults.partition_threshold_mb ?? 1024,
    partition_size_mb: config?.defaults.partition_size_mb ?? 512, include_default: true,
  });
  const review = useQuery({
    queryKey: ["review", did, shared, running],
    queryFn: () => api.post<Row[]>(`/discoveries/${did}/review`, { shared }),
    enabled: !!did && !running,
  });
  const [rows, setRows] = useState<Row[]>([]);
  useEffect(() => { setRows(review.data ?? []); }, [review.data]);

  const [hideRegistered, setHideRegistered] = useState(true);
  const [quick, setQuick] = useState("");
  const [bulkField, setBulkField] = useState(BULK_FIELDS[0]);
  const [bulkValue, setBulkValue] = useState("");
  const [selected, setSelected] = useState<Row[]>([]);
  const gridRef = useRef<AgGridReact>(null);
  const shown = useMemo(() => rows.filter((r) => !(hideRegistered && r.registered_id)), [rows, hideRegistered]);

  const update = (pred: (r: Row) => boolean, patch: (r: Row) => Row) =>
    setRows((rs) => rs.map((r) => (pred(r) ? patch(r) : r)));
  const visibleKeys = () => {
    const ks = new Set<string>();
    gridRef.current?.api.forEachNodeAfterFilter((n) => n.data && ks.add(key(n.data)));
    return ks;
  };

  const columns: ColDef[] = useMemo(() => autoColumns(shown.length ? shown : [{}], {
    include: { editable: true, cellDataType: "boolean", pinned: "left", width: 90 },
    src_database: { pinned: "left" }, src_schema: { pinned: "left" }, src_table: { pinned: "left" },
    load_type: { editable: true, cellEditor: "agSelectCellEditor", cellEditorParams: { values: ["ct", "cursor", "full"] } },
    load_partitioned: { editable: true, cellDataType: "boolean", headerName: "partitioned" },
    size_mb: { valueFormatter: (p: any) => Number(p.value ?? 0).toFixed(1) },
    registered_id: { headerName: "registered" },
    notes: { minWidth: 300, tooltipField: "notes" },
    ...Object.fromEntries(["primary_key", "cursor_column", "partition_col", "partition_size_mb", "sink_catalog",
      "sink_schema", "sink_table", "client_id"].map((f) => [f, { editable: true }])),
  }), [shown]);

  const payload = (dry: boolean) => ({
    task_collection: collection, shared, dry_run: dry,
    edits: rows.map((r) => Object.fromEntries(["src_database", "src_schema", "src_table", ...EDITABLE].map((k) => [k, r[k]]))),
  });
  const validate = useMutation({ mutationFn: () => api.post(`/discoveries/${did}/register`, payload(true)) });
  const register = useMutation({
    mutationFn: () => api.post(`/discoveries/${did}/register`, payload(false)),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["review"] }); qc.invalidateQueries({ queryKey: ["collections"] }); },
  });
  const result = (register.data ?? validate.data) as Row | undefined;
  const nIncluded = rows.filter((r) => r.include).length;
  const nPartitioned = rows.filter((r) => r.include && r.load_partitioned).length;

  if (!config?.jobs.discover) return <><h1>Bulk import</h1><Notice tone="warn">DISCOVER_JOB_ID isn't configured for this app.</Notice></>;
  return (
    <>
      <h1>Bulk import</h1>
      <DiscoverForm onStarted={(id) => { setDiscoveryId(id); qc.invalidateQueries({ queryKey: ["discoveries"] }); }} />

      <Section title="2. Review">
        <ErrorBox error={discoveries.error || dbs.error} />
        <div className="toolbar">
          <label className="grow">Discovery
            <select value={did} onChange={(e) => setDiscoveryId(e.target.value)}>
              {discoveries.data?.map((d) => (
                <option key={d.discovery_id} value={d.discovery_id}>
                  {d.discovery_id}: {d.src_host}, {d.databases} databases, {d.tables} tables, {fmtTime(d.started_at)}
                  {Number(d.running) > 0 ? " (running)" : ""}{Number(d.failed) > 0 ? ` (${d.failed} failed)` : ""}
                </option>
              ))}
            </select>
          </label>
        </div>
        {!discoveries.data?.length && <p className="muted">No discoveries yet.</p>}
        {dbs.data && (
          <Grid rows={dbs.data} height={Math.min(60 + dbs.data.length * 42, 240)}
            columns={autoColumns(dbs.data, { status: { cellRenderer: (p: any) => <Badge value={p.value} /> },
              error: { flex: 1, minWidth: 300, tooltipField: "error" } })} />
        )}
        {running && <Notice tone="warn">Discovery is still running; this updates automatically.</Notice>}
      </Section>

      {did && !running && (
        <Section title="3. Shared fields and tables">
          <p className="muted small">Templates can use <code>{"{database}"}</code>, <code>{"{schema}"}</code> and
            <code>{" {table}"}</code>. Changing shared fields rebuilds the grid, so set them before editing rows.</p>
          <div className="form-grid">
            <label>task_collection *<input value={collection} onChange={(e) => setCollection(e.target.value)} /></label>
            <label>sink_catalog *<input value={shared.sink_catalog} onChange={(e) => setShared({ ...shared, sink_catalog: e.target.value })} /></label>
            <label>sink_schema *<input value={shared.sink_schema} onChange={(e) => setShared({ ...shared, sink_schema: e.target.value })} /></label>
            <label>sink_table *<input value={shared.sink_table} onChange={(e) => setShared({ ...shared, sink_table: e.target.value })} /></label>
            <label>client_id (consolidate)
              <input placeholder="e.g. {database}" value={shared.client_id} onChange={(e) => setShared({ ...shared, client_id: e.target.value })} />
              <span className="muted small">with a shared sink_schema, consolidates databases into one table each</span>
            </label>
            <label>Partition tables larger than (MB)
              <input type="number" min={1} value={shared.partition_threshold_mb}
                onChange={(e) => setShared({ ...shared, partition_threshold_mb: Number(e.target.value) || 1 })} />
            </label>
            <label>partition_size_mb
              <input type="number" min={1} value={shared.partition_size_mb}
                onChange={(e) => setShared({ ...shared, partition_size_mb: Number(e.target.value) || 1 })} />
            </label>
            <label className="checkbox"><input type="checkbox" checked={shared.include_default}
              onChange={(e) => setShared({ ...shared, include_default: e.target.checked })} /> Include new tables by default</label>
          </div>
          <ErrorBox error={review.error} />
          <div className="toolbar">
            <label className="grow">Filter<input placeholder="any column" value={quick} onChange={(e) => setQuick(e.target.value)} /></label>
            <label className="checkbox"><input type="checkbox" checked={hideRegistered} onChange={(e) => setHideRegistered(e.target.checked)} /> Hide registered</label>
            <button className="ghost" onClick={() => { const ks = visibleKeys(); update((r) => ks.has(key(r)), (r) => ({ ...r, include: true })); }}>Include shown</button>
            <button className="ghost" onClick={() => { const ks = visibleKeys(); update((r) => ks.has(key(r)), (r) => ({ ...r, include: false })); }}>Exclude shown</button>
          </div>
          <div className="toolbar">
            <span className="muted">{selected.length} selected rows:</span>
            <label>Set<select value={bulkField} onChange={(e) => setBulkField(e.target.value)}>{BULK_FIELDS.map((f) => <option key={f}>{f}</option>)}</select></label>
            <label>to<input value={bulkValue} placeholder={bulkField === "load_partitioned" ? "true / false" : ""} onChange={(e) => setBulkValue(e.target.value)} /></label>
            <button className="ghost" disabled={!selected.length} onClick={() => {
              const ks = new Set(selected.map(key));
              const v: any = bulkField === "load_partitioned" ? bulkValue.trim().toLowerCase() === "true"
                : bulkField === "partition_size_mb" ? (Number(bulkValue) || null) : bulkValue;
              update((r) => ks.has(key(r)), (r) => ({ ...r, [bulkField]: v }));
            }}>Apply</button>
          </div>
          <div style={{ height: 560 }}>
            <AgGridReact ref={gridRef} theme={theme} rowData={shown} columnDefs={columns} getRowId={(p) => key(p.data)}
              defaultColDef={{ sortable: true, filter: true, resizable: true, minWidth: 90 }}
              quickFilterText={quick} rowSelection={{ mode: "multiRow" }} selectionColumnDef={{ pinned: "left", width: 50 }}
              singleClickEdit stopEditingWhenCellsLoseFocus
              loading={review.isFetching}
              onSelectionChanged={(e) => setSelected(e.api.getSelectedRows())}
              onCellValueChanged={(e) => update((r) => key(r) === key(e.data), () => ({ ...e.data }))} />
          </div>
          <p className="muted small">{nIncluded} of {rows.length} tables included ({nPartitioned} partitioned).
            {rows.length - shown.length > 0 && ` ${rows.length - shown.length} already registered are hidden.`}</p>
        </Section>
      )}

      {did && !running && (
        <Section title="4. Register">
          <div className="toolbar">
            <button className="ghost" disabled={!nIncluded || !collection || validate.isPending} onClick={() => { register.reset(); validate.mutate(); }}>
              Validate {nIncluded} tables
            </button>
            <ConfirmButton label={`Register ${nIncluded} tables`} disabled={!nIncluded || !collection}
              confirmLabel={`Confirm: register ${nIncluded} tables in '${collection}'`}
              onConfirm={async () => { validate.reset(); await register.mutateAsync(); }} />
            {!collection && <span className="muted">Set task_collection first.</span>}
          </div>
          <ErrorBox error={validate.error || register.error} />
          {result && (
            <>
              <Notice tone={result.counts.invalid ? "warn" : "ok"}>
                {register.data ? "Registered" : "Validated"}: {Object.entries(result.counts).map(([k, v]) => `${v} ${k}`).join(", ") || "nothing selected"}.
                {register.data && result.counts.registered ? " Their first run seeds them." : ""}
              </Notice>
              {result.results.some((r: Row) => r.errors?.length) && (
                <Grid height={260} rows={result.results.filter((r: Row) => r.errors?.length).map((r: Row) => ({ ...r, errors: r.errors.join("; ") }))}
                  columns={autoColumns([{ table: "", status: "", errors: "" }], { status: { cellRenderer: (p: any) => <Badge value={p.value} /> }, errors: { flex: 1 } })} />
              )}
            </>
          )}
        </Section>
      )}
    </>
  );
}
