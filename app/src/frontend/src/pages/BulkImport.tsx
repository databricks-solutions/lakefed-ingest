import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AgGridReact } from "ag-grid-react";
import { type ColDef } from "ag-grid-community";
import { api, type Row } from "../api";
import { useSettings } from "../App";
import { groupsRefusal, usePermissions } from "../permissions";
import { Badge, Busy, ConfirmButton, ErrorBox, FillBox, ToastOn, Grid, Notice, PageHead, Section, Tag, autoColumns, fmtAgo, fmtInt, fmtTime, gridTheme, Host, plural } from "../components";

const EDITABLE = ["include", "load_type", "primary_key", "cursor_column", "load_partitioned", "partition_col",
  "partition_size_mb", "sink_catalog", "sink_schema", "sink_table", "client_id", "freshness_sla_minutes"];
const NUMBER_FIELDS = ["partition_size_mb", "freshness_sla_minutes"];
const BULK_FIELDS = EDITABLE.filter((f) => f !== "include");
const key = (r: Row) => `${r.src_database}\u0000${r.src_schema}\u0000${r.src_table}`;
const theme = gridTheme;

interface Shared {
  sink_catalog: string; sink_schema: string; sink_table: string; client_id: string;
  freshness_sla_minutes: number | null; partition_threshold_mb: number; partition_size_mb: number; include_default: boolean;
}

interface Connection { name: string; connection_type: string; host?: string; port?: string; owner?: string; discoverable: boolean;
  tracked: { databases: number; tables: number; collections: number } }

/** The Unity Catalog connections the app can see, with what lakefed already tracks on each; picking one fills the
    discovery form's host and port. Discovery itself still reads over JDBC with a secret per database. */
function Connections({ picked, onPick }: { picked: Connection | null; onPick: (c: Connection) => void }) {
  const list = useQuery({ queryKey: ["connections"], queryFn: () => api.get<{ connections: Connection[]; error: string | null }>("/connections") });
  const tracked = useQuery({
    queryKey: ["connection-databases", picked?.name],
    queryFn: () => api.get(`/connections/${encodeURIComponent(picked!.name)}/databases`),
    enabled: !!picked,
  });
  const found = list.data?.connections ?? [];
  return (
    <Section title="1. Source" note="Unity Catalog connections this app can use, with the databases, tables and ingestion groups lakefed already tracks on each (matched by connection name or host).">
      <ErrorBox error={list.error} />
      {list.data?.error && <Notice tone="warn">Could not list connections: {list.data.error}. Discovery by host and secret below still works.</Notice>}
      {list.data && !list.data.error && !found.length && <div className="empty">No Unity Catalog connections visible to this app.</div>}
      <div className="server-cards">
        {found.map((c) => (
          <div key={c.name} className={`server-card ${c.discoverable ? "selectable" : "disabled"} ${picked?.name === c.name ? "selected" : ""}`}
            onClick={() => c.discoverable && onPick(c)} role="button" aria-pressed={picked?.name === c.name}>
            <div className="top"><div className="name"><h3 title={c.name}>{c.name}</h3>
              <div className="meta">{c.connection_type}{c.host && <> · <Host host={c.host} port={c.port} /></>}</div></div>
              {picked?.name === c.name ? <Tag tone="run">Selected</Tag> : c.tracked.tables ? <Tag tone="ok">Tracked</Tag> : <Tag>Not tracked</Tag>}</div>
            {c.discoverable ? (
              <div className="nums three"><div><b>{fmtInt(c.tracked.databases)}</b><span>databases tracked</span></div>
                <div><b>{fmtInt(c.tracked.tables)}</b><span>tables tracked</span></div>
                <div><b>{fmtInt(c.tracked.collections)}</b><span>ingestion groups</span></div></div>
            ) : <div className="meta">Discovery is SQL Server only for now.</div>}
          </div>
        ))}
      </div>
      {picked && (
        <div style={{ marginTop: 16 }}>
          <h3>Tracked on {picked.name}</h3>
          <p className="small muted">Databases lakefed already ingests from this source. Listing the server's other databases needs
            discovery through the connection, which is not built yet; add new ones in the discovery form below.</p>
          <ErrorBox error={tracked.error} />
          <FillBox min={200}>
            <table className="data">
              <thead><tr><th>Database</th><th>Ingestion groups</th><th className="num">Tables</th><th className="num">Last success</th></tr></thead>
              <tbody>
                {(tracked.data ?? []).map((d) => (
                  <tr key={d.src_database}><td><b>{d.src_database}</b></td><td>{d.collections}</td><td className="num">{fmtInt(d.tables)}</td>
                    <td className="num">{fmtAgo(d.newest_success_at)}</td></tr>
                ))}
                {tracked.data && !tracked.data.length && <tr><td colSpan={4} className="muted">Nothing tracked on this source yet.</td></tr>}
              </tbody>
            </table>
          </FillBox>
        </div>
      )}
    </Section>
  );
}

function DiscoverForm({ onStarted, prefill }: { onStarted: (id: string, runId: number) => void; prefill: Connection | null }) {
  const [f, setF] = useState({ src_host: "", src_port: "", databases: "", secret_key_template: "{database}" });
  useEffect(() => {
    if (prefill) setF((v) => ({ ...v, src_host: prefill.host ?? "", src_port: prefill.port ?? "" }));
  }, [prefill]);
  const discoveryRefusal = usePermissions().data?.run.discovery ?? null;
  const start = useMutation({
    mutationFn: () => api.post<{ discovery_id: string; run_id: number; databases: number }>("/discoveries", {
      ...f, src_port: f.src_port ? Number(f.src_port) : null }),
    onSuccess: (r) => onStarted(r.discovery_id, r.run_id),
  });
  return (
    <Section title="2. Discover tables">
      <p className="muted small">Runs the discovery job on the ingestion network path; it reads catalog metadata only
        (no table data). SQL Server for now.{prefill && <> Host and port come from <b>{prefill.name}</b>.</>}</p>
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
        {discoveryRefusal && <p className="wide muted small">Discover: {discoveryRefusal}.</p>}
        <div className="wide"><button className="primary" disabled={start.isPending || !!discoveryRefusal}>{start.isPending ? "Starting…" : "Discover"}</button></div>
      </form>
      <ErrorBox error={start.error} />
      <ToastOn text={start.data ? `Discovery started for ${fmtInt(start.data.databases)} ${plural(start.data.databases, "database")}` : null} />
    </Section>
  );
}

export default function BulkImport() {
  const { config } = useSettings();
  const qc = useQueryClient();
  const [discoveryId, setDiscoveryId] = useState<string>("");
  const [picked, setPicked] = useState<Connection | null>(null);
  const [startedRun, setStartedRun] = useState<{ did: string; runId: number } | null>(null);
  // A discovery just started has no rows until its job runs: keep polling until its databases appear
  // and finish (polling only while a row said "running" stopped before the first row existed).
  const discoveries = useQuery({
    queryKey: ["discoveries"],
    queryFn: () => api.get("/discoveries"),
    refetchInterval: (q) => {
      const ds = q.state.data as Row[] | undefined;
      return ds?.some((d) => Number(d.running) > 0) || (discoveryId && !ds?.some((d) => d.discovery_id === discoveryId))
        ? 5_000 : false;
    },
  });
  const did = discoveryId || discoveries.data?.[0]?.discovery_id || "";
  // The job run of a discovery started here: once it has ended, stop waiting for rows it won't write.
  const runId = startedRun && startedRun.did === did ? startedRun.runId : null;
  const run = useQuery({
    queryKey: ["discovery-run", runId],
    queryFn: () => api.get<Row>(`/discoveries/runs/${runId}`),
    enabled: !!runId,
    refetchInterval: (q) => (["TERMINATED", "INTERNAL_ERROR", "SKIPPED"].includes((q.state.data as Row | undefined)?.state)
      ? false : 10_000),
  });
  const jobEnded = ["TERMINATED", "INTERNAL_ERROR", "SKIPPED"].includes(run.data?.state);
  const dbs = useQuery({
    queryKey: ["discovery-dbs", did],
    queryFn: () => api.get(`/discoveries/${did}/databases`),
    enabled: !!did,
    refetchInterval: (q) => {
      const ds = q.state.data as Row[] | undefined;
      return !jobEnded && (!ds?.length || ds.some((d) => d.status === "running")) ? 5_000 : false;
    },
  });
  useEffect(() => { if (jobEnded) qc.invalidateQueries({ queryKey: ["discovery-dbs", did] }); }, [jobEnded, did, qc]);
  const noRows = !!dbs.data && dbs.data.length === 0;
  const waiting = noRows && !jobEnded;                           // the job hasn't started on it yet
  const running = waiting || (!jobEnded && (dbs.data ?? []).some((d) => d.status === "running"));
  // Re-read the review whenever a database finishes (its status or table count changes).
  const progress = (dbs.data ?? []).map((d) => `${d.src_database}:${d.status}:${d.tables}`).join("|");

  const [collection, setCollection] = useState("");
  const locked = groupsRefusal(usePermissions().data, [collection.trim()]);
  const [shared, setShared] = useState<Shared>({
    sink_catalog: "main", sink_schema: "bronze_{database}", sink_table: "{table}", client_id: "", freshness_sla_minutes: null,
    partition_threshold_mb: config?.defaults.partition_threshold_mb ?? 1024,
    partition_size_mb: config?.defaults.partition_size_mb ?? 512, include_default: true,
  });
  const review = useQuery({
    queryKey: ["review", did, shared, progress, collection.trim()],
    queryFn: () => api.post<Row[]>(`/discoveries/${did}/review`, { shared, task_collection: collection.trim() }),
    enabled: !!did && !!dbs.data && !running,
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
    freshness_sla_minutes: { editable: true, cellDataType: "number", headerName: "sla_min" },
  }), [shown]);

  const payload = (dry: boolean) => ({
    task_collection: collection, shared, dry_run: dry,
    edits: rows.map((r) => Object.fromEntries(["src_database", "src_schema", "src_table", ...EDITABLE].map((k) => [k, r[k]]))),
  });
  const validate = useMutation({ mutationFn: () => api.post(`/discoveries/${did}/register`, payload(true)) });
  const register = useMutation({
    mutationFn: () => api.post(`/discoveries/${did}/register`, payload(false)),
    // Registering changes what lakefed tracks on the connection, so its card and database list refresh too.
    onSuccess: () => {
      for (const key of ["review", "collections", "connections", "connection-databases", "overview", "tables"]) {
        qc.invalidateQueries({ queryKey: [key] });
      }
    },
  });
  const result = (register.data ?? validate.data) as Row | undefined;
  const nIncluded = rows.filter((r) => r.include).length;
  const nPartitioned = rows.filter((r) => r.include && r.load_partitioned).length;

  const head = <PageHead title="Bulk import" lead="Pick a source, see what lakefed already tracks on it, discover the rest, then review and register." />;
  if (!config?.jobs.discover) return <>{head}<Notice tone="warn">DISCOVER_JOB_ID isn't configured for this app.</Notice></>;
  return (
    <>
      {head}
      <Connections picked={picked} onPick={setPicked} />
      <DiscoverForm prefill={picked} onStarted={(id, runId) => {
        setDiscoveryId(id); setStartedRun({ did: id, runId }); qc.invalidateQueries({ queryKey: ["discoveries"] });
      }} />

      <Section title="3. Review">
        <ErrorBox error={discoveries.error || dbs.error} />
        <div className="toolbar">
          <label className="grow">Discovery
            <select value={did} onChange={(e) => setDiscoveryId(e.target.value)}>
              {discoveries.data?.map((d) => (
                <option key={d.discovery_id} value={d.discovery_id}>
                  {d.discovery_id}: {d.src_host}, {d.databases} {plural(d.databases, "database")}, {d.tables} {plural(d.tables, "table")}, {fmtTime(d.started_at)}
                  {Number(d.running) > 0 ? " (running)" : ""}{Number(d.failed) > 0 ? ` (${d.failed} failed)` : ""}
                </option>
              ))}
            </select>
          </label>
        </div>
        {!discoveries.data?.length && <p className="muted">No discoveries yet.</p>}
        {dbs.data && (
          <Grid rows={dbs.data} fitRows
            columns={autoColumns(dbs.data, { status: { cellRenderer: (p: any) => <Badge value={p.value} /> },
              error: { flex: 1, minWidth: 300, tooltipField: "error" } })} />
        )}
        {waiting && <Busy label="Starting the discovery" />}
        {jobEnded && (noRows || run.data?.result !== "SUCCESS") && (
          <Notice tone="bad">The discovery ended ({run.data?.result ?? run.data?.state}
            {run.data?.message ? `: ${run.data.message}` : ""}){noRows ? " without discovering any database" : ""}.{" "}
            {run.data?.url && <a href={run.data.url} target="_blank" rel="noreferrer">Open the run</a>}</Notice>
        )}
        {running && !waiting && <Notice tone="warn">Discovery is still running; this updates automatically.</Notice>}
      </Section>

      {did && !running && (
        <Section title="4. Shared fields and tables">
          <p className="muted small">Templates can use <code>{"{database}"}</code>, <code>{"{schema}"}</code> and
            <code>{" {table}"}</code>. Changing shared fields rebuilds the grid, so set them before editing rows.</p>
          <div className="form-grid">
            <label>Ingestion group (task_collection) *<input value={collection} onChange={(e) => setCollection(e.target.value)} /></label>
            <label>sink_catalog *<input value={shared.sink_catalog} onChange={(e) => setShared({ ...shared, sink_catalog: e.target.value })} /></label>
            <label>sink_schema *<input value={shared.sink_schema} onChange={(e) => setShared({ ...shared, sink_schema: e.target.value })} /></label>
            <label>sink_table *<input value={shared.sink_table} onChange={(e) => setShared({ ...shared, sink_table: e.target.value })} /></label>
            <label>client_id (consolidate)
              <input placeholder="e.g. {database}" value={shared.client_id} onChange={(e) => setShared({ ...shared, client_id: e.target.value })} />
              <span className="muted small">with a shared sink_schema, consolidates databases into one table each</span>
            </label>
            <label>Freshness target (freshness_sla_minutes)
              <input type="number" min={1} placeholder="blank = no target" value={shared.freshness_sla_minutes ?? ""}
                onChange={(e) => setShared({ ...shared, freshness_sla_minutes: Number(e.target.value) > 0 ? Number(e.target.value) : null })} />
              <span className="muted small">a table is late when its last success is older than this</span>
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
                : NUMBER_FIELDS.includes(bulkField) ? (Number(bulkValue) || null) : bulkValue;
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
          <p className="muted small">{nIncluded} of {rows.length} {plural(rows.length, "table")} included ({nPartitioned} partitioned).
            {rows.length - shown.length > 0 && ` ${rows.length - shown.length} already registered are hidden.`}</p>
        </Section>
      )}

      {did && !running && (
        <Section title="5. Register">
          <div className="toolbar">
            <button className="ghost" disabled={!nIncluded || !collection || validate.isPending} onClick={() => { register.reset(); validate.mutate(); }}>
              Validate {nIncluded} {plural(nIncluded, "table")}
            </button>
            <ConfirmButton label={`Register ${nIncluded} ${plural(nIncluded, "table")}`} disabled={!nIncluded || !collection || !!locked}
              confirmLabel={`Confirm: register ${nIncluded} ${plural(nIncluded, "table")} in '${collection}'`}
              onConfirm={async () => { validate.reset(); await register.mutateAsync(); }} />
            {!collection && <span className="muted">Set the ingestion group first.</span>}
          </div>
          {locked && <p className="muted small">Register: {locked}.</p>}
          <ErrorBox error={validate.error || register.error} />
          {result && (
            <>
              <Notice tone={result.counts.invalid ? "warn" : "ok"}>
                {register.data ? "Registered" : "Validated"}: {Object.entries(result.counts).map(([k, v]) => `${v} ${k}`).join(", ") || "nothing selected"}.
                {register.data && result.counts.registered ? " Their first run seeds them." : ""}
              </Notice>
              {result.results.some((r: Row) => r.errors?.length) && (
                <Grid fitRows rows={result.results.filter((r: Row) => r.errors?.length).map((r: Row) => ({ ...r, errors: r.errors.join("; ") }))}
                  columns={autoColumns([{ table: "", status: "", errors: "" }], { status: { cellRenderer: (p: any) => <Badge value={p.value} /> }, errors: { flex: 1 } })} />
              )}
            </>
          )}
        </Section>
      )}
    </>
  );
}
