import { Fragment, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, type Row } from "../api";
import { Busy, StatusLine, Crumbs, ErrorBox, Info, Menu, Modal, Notice, PageHead, Section, Twisty, fmtInt, plural, useConfirm, Chevron } from "../components";
import { CADENCES, IdentityFields, ParameterField, ParameterFields, RunFields, WritesFields } from "./jobForm";
import { DiscoverDialog, discardDrafts } from "./Discover";
import { serverRowCells, targetBlock } from "./previewRows";
import { ClusterFields, TablesPerGroup, ownCluster } from "./JobEdit";
import { SourcePick, nothingPicked, pickedAny, pickedText, type Picked } from "./SourcePick";
import { TaskProgress, finished, newRequestId, resumableWrite } from "./tasks";
import { draftCreateBlock, draftReadProgress, isUnconfirmed, requestWriteNotice } from "../pending";
import { ChosenDatabases } from "./ChosenTree";
import { defaultJobName, nameTaken } from "../jobName";

// New ingestion job: the Edit view, empty, with Create in place of Save. Its tables are added in What it ingests
// (Add tables: a CSV uploaded once, or a discovery), the rest is set as in Edit, and nothing is written until Create,
// which makes the job, its tables and its groups in one go (POST /api/ingestion-jobs). The page holds only what was
// chosen (an upload's or a discovery's id and the servers, databases or tables picked), never the tables: the preview
// and Create name it, and the server reads the tables from Lakebase. A large Create is written by a background task,
// whose progress the page shows before it opens the job.

// Removing at least this many chosen tables asks first.
export const REMOVE_CONFIRM_TABLES = 100;
const LOAD_TYPES: [string, string][] = [["ct", "Change tracking"], ["cursor", "Cursor column"], ["full", "Full reload"]];
const CSV_EXAMPLE = "src_database,src_schema,src_table,select_list,primary_key\nsales_01,dbo,orders,\"id, amount\",id";
// One line per table, and one per database: every table of a database line is found by name when the job is created.
const CSV_TEMPLATE = "src_database,src_schema,src_table,select_list,primary_key,cursor_column,client_id,load_type\n"
  + "sales_01,dbo,orders,\"id, amount\",id,,,ct\n" + "sales_02,,,,,,,\n";
// How many of the file's first lines the import's preview shows, under its header.
const PREVIEW_LINES = 5;

/** Tables chosen by reference: what Create sends as its source, and how the page names it. Chosen by Discover through
 *  the SQL warehouse, their reads may still be going (drafts): the preview keeps them going, Create waits for them, and
 *  leaving the page (or choosing again) before Create discards them. */
export type Chosen = { source: Row; label: string; drafts?: string[] };

/** The value once it has stayed the same for `ms`, so typing in a field doesn't ask for a preview per key. */
function useSettled<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => { const timer = setTimeout(() => setSettled(value), ms); return () => clearTimeout(timer); }, [value, ms]);
  return settled;
}

export default function NewJob() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const options = useQuery({ queryKey: ["schedule-options"], queryFn: () => api.get<Row>("/schedules/options") });
  // As the Databricks Jobs editor starts a new job: a default name with the time, typed over in the title if wanted.
  const [defaultName, setDefaultName] = useState(defaultJobName);
  const [name, setName] = useState(defaultName);
  const [chosen, setChosenNow] = useState<Chosen | null>(null);
  // The drafts of the tables chosen, discarded when they are replaced or the page is left, unless a Create took them.
  const created = useRef(false);
  const drafts = useRef<string[]>([]);
  drafts.current = chosen?.drafts ?? [];
  const setChosen = (next: Chosen | null) => {
    if (!created.current) discardDrafts(drafts.current);
    setChosenNow(next);
  };
  useEffect(() => {
    const leave = () => { if (!created.current) discardDrafts(drafts.current, true); };
    window.addEventListener("pagehide", leave);
    return () => { window.removeEventListener("pagehide", leave); leave(); };
  }, []);
  // Made when the page opens; a Create sent again (a retry after a timeout) finds the task the first one made.
  const [requestId, setRequestId] = useState(newRequestId);
  const [task, setTask] = useState<Row | null>(null);
  const [loadType, setLoadType] = useState("ct");
  // A null client_id or identity is sent as null: the server gives a new fan-in job its key and readable columns.
  const [w, setW] = useState<Row>({ fan_in: false, write_mode_changes: "merge", write_mode_full: "merge", sink_catalog: "",
                                    sink_schema: "", sink_table: "{table}", client_id: null,
                                    freshness_sla_minutes: "" });
  const [identity, setIdentity] = useState<[string, string][] | null>(null);
  const [run, setRun] = useState<Row>({ cadence_minutes: 60, start: "00:00", timezone: "Europe/London", paused: true, notify_emails: "" });
  const [params, setParams] = useState<Record<string, string>>({});
  // Unchanged, the job follows the default size (null); the field shows that default.
  const [tablesPerGroup, setTablesPerGroup] = useState<number | null>(null);
  const [importing, setImporting] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  // The job's own cluster, as in Edit's Job cluster: at the Databricks job's values unless changed here.
  const clusterInfo = useQuery({ queryKey: ["cluster-options"], queryFn: () => api.get<Row>("/cluster-options") });
  const [clusterChoice, setCluster] = useState<Row | null>(null);
  const cluster = clusterChoice ?? clusterInfo.data?.defaults ?? null;
  const own = clusterInfo.data && cluster ? ownCluster(cluster, clusterInfo.data.defaults) : null;
  const variants: string[] = options.data?.variants ?? [];
  // Runs start through the check job where there is one, as every job has been set up; there is no choice on the page.
  const jobVariant = variants.includes("check") ? "check" : "default";
  // Until the user changes the columns, a fan-in job shows the server's readable columns and sends none of its own.
  const sharedIdentity = Object.entries(options.data?.shared_identity ?? {}) as [string, string][];
  const identityRows = identity ?? (w.fan_in === true ? sharedIdentity : []);

  const body = (dryRun: boolean) => {
    const cadence = Number(run.cadence_minutes);
    const [h, m] = String(run.start || "00:00").split(":").map(Number);
    return {
      name: name.trim(), source: chosen?.source, ...(tablesPerGroup ? { tables_per_group: tablesPerGroup } : {}), dry_run: dryRun,
      request_id: requestId,
      settings: { ...w, load_type: loadType, freshness_sla_minutes: w.freshness_sla_minutes === "" ? null : Number(w.freshness_sla_minutes),
                  identity: identity === null ? null : Object.fromEntries(identity.filter(([n, t]) => n.trim() && t.trim()).map(([n, t]) => [n.trim(), t.trim()])) },
      run: { cadence_minutes: cadence, timezone: cadence <= 60 ? "UTC" : run.timezone || "Europe/London", paused: run.paused,
             notify_emails: run.notify_emails || null, job_variant: jobVariant,
             options: { ...(cadence > 60 ? { start_minute: (h || 0) * 60 + (m || 0) } : {}), ...(own ? { cluster: own } : {}) },
             parameters: Object.fromEntries(Object.entries(params).filter(([, v]) => v.trim() !== "").map(([k, v]) => [k, v.trim()])) },
    };
  };
  // The preview follows every change, so the page always says what Create would write; it is asked for only once the
  // changes have settled, and it names the tables by reference, so it stays small whatever their number.
  const previewBody = body(true);
  const current = JSON.stringify({ ...previewBody, request_id: null });
  const settled = useSettled(current, 600);
  const preview = useQuery({
    // Shown as soon as tables are chosen: a blank name or target only holds Create back, never the preview.
    queryKey: ["new-job-preview", settled], enabled: !!chosen, retry: false,
    queryFn: () => api.post<Row>("/ingestion-jobs", previewBody),
    // A file's database-only lines are discovered by name before the preview can count them, and a draft's tables are
    // read as they were chosen: asked again until they are (each preview also does a step of the draft's read).
    refetchInterval: (query) => (query.state.data?.discovering ? 3000 : query.state.data?.reading?.pending ? 2000 : false),
  });
  // The body is the variables, so a Create that stopped part way is finished by sending the same body again.
  const create = useMutation({
    // Once sent, the Create takes the drafts (it waits for their reads, on the server), so leaving no longer discards them.
    mutationFn: async (sent: Row) => { created.current = true; setTask(null); return finished(await api.post<Row>("/ingestion-jobs", sent), setTask); },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["ingestion-jobs"] }); qc.invalidateQueries({ queryKey: ["schedules"] });
      if (r.created) navigate(`/ingestion-jobs/${r.id}`);
    },
    // A Create that failed is a new request when it is sent again, unless it stopped part way: its databases are kept
    // and the same request finishes it.
    onError: (error) => { if (!resumableWrite(error)) { created.current = false; setRequestId(newRequestId()); } },
  });
  const note = requestWriteNotice("Creating the job", { pending: create.isPending && !task, error: create.error });
  const summary: Row | undefined = preview.data?.summary;
  const discoveringDatabases: number | null = preview.data?.discovering ? Number(preview.data.databases) : null;
  const reading = preview.data?.reading ?? null;
  const readProgress = draftReadProgress(reading);
  const [openServer, setOpenServer] = useState<string | null>(null);
  // A default name another job's groups already use (made the same second) is made again; a typed one is said taken.
  const taken = nameTaken(summary?.invalid);
  useEffect(() => {
    if (taken && name === defaultName) { const next = defaultJobName(); setDefaultName(next); setName(next); }
  }, [taken, name, defaultName]);
  const blocked = !name.trim() ? "Name the job first" : !chosen ? "Add its tables first"
    : discoveringDatabases !== null ? "Reading tables"
    : (preview.isFetching && preview.data === undefined) || settled !== current ? "Checking the tables"
    : preview.error ? "Fix what the check found first" : draftCreateBlock(reading)
    ?? (summary?.invalid?.length ? "Fix the invalid lines first" : targetBlock(w));
  const confirm = useConfirm();
  // As Databricks asks before a bulk delete: removing many tables is confirmed, a few go at once.
  const removeTables = () => {
    const tables = Number(summary?.tables ?? 0);
    if (tables < REMOVE_CONFIRM_TABLES) return setChosen(null);
    confirm.ask({ title: `Remove ${fmtInt(tables)} ${plural(tables, "table")}?`, action: "Remove", danger: true,
                  body: "They leave this new job. Add tables chooses them again." }).then((ok) => ok && setChosen(null));
  };
  const cancel = () => {
    if ((!name || name === defaultName) && !chosen) return navigate("/ingestion-jobs");
    confirm.ask({ title: "Leave without creating the job?", action: "Leave", danger: true, body: "Nothing of it is saved." })
      .then((ok) => ok && navigate("/ingestion-jobs"));
  };

  // The two ways to add tables: the Add tables menu and the empty state both offer them.
  const addTableChoices = (<>
    <button onClick={() => setDiscovering(true)}>Discover on a source</button>
    <button onClick={() => setImporting(true)}>Import a CSV</button>
  </>);

  return (
    <>
      <PageHead crumb={<Crumbs steps={[["Ingestion jobs", "/ingestion-jobs"], ["New"]]} />} tab="New ingestion job"
        title={<span className="title-edit-row">
          <input className="title-edit" aria-label="Job name" value={name} readOnly={create.isPending} onChange={(e) => setName(e.target.value)}
            onBlur={() => { if (!name.trim()) setName(defaultName); }} />
          {taken && name !== defaultName && <span className="title-note">Another job uses this name</span>}</span>}
        lead={summary ? <><strong>{fmtInt(summary.tables)}</strong> {plural(summary.tables, "table")} in{" "}
          <strong>{fmtInt(summary.groups.length)}</strong> {plural(summary.groups.length, "group")}.</>
          : discoveringDatabases !== null ? "Reading tables…"
          : "No tables yet: add them in What it ingests."}
        right={<span className="card-actions">
          <button onClick={cancel} disabled={create.isPending}>Cancel</button>
          <button className="primary" disabled={!!blocked || create.isPending} title={blocked ?? undefined}
            onClick={() => create.mutate(body(false))}>{create.isPending ? "Creating…" : "Create"}</button></span>} />
      {confirm.dialog}
      <StatusLine line={note} />
      {create.isPending && <TaskProgress task={task} />}
      {!create.isPending && resumableWrite(create.error)
        ? <TaskProgress task={task} onResume={() => create.mutate(create.variables as Row)} />
        : <ErrorBox error={isUnconfirmed(create.error) ? null : create.error} />}
      {create.data && !create.data.created && <Notice tone="warn">Not created: fix the invalid lines below first.</Notice>}

      <fieldset className="create-lock" disabled={create.isPending}>
        <Section title="What it ingests"
          actions={<Menu label={<>Add tables <Chevron open /></>} buttonClass="ghost" title="Add tables">{addTableChoices}</Menu>}>
          {!chosen ? <div className="empty-drop">
            <p>No tables yet. Add them with <b>Add tables</b>.</p>
            <div className="empty-choices">{addTableChoices}</div>
          </div> : (
            <>
              <p className="muted">From {chosen.label}.</p>
              {readProgress && <Busy label="Reading tables" {...readProgress} />}
              {draftCreateBlock(reading) && <Notice tone="bad">{draftCreateBlock(reading)}:<ul>
                {(reading?.failed_databases ?? []).slice(0, 20).map(([d, why]: [string, string]) => <li key={d}><b>{d}</b>: {why}</li>)}</ul></Notice>}
              {Number(reading?.left_out) > 0 && <p className="muted">The whole server leaves out {fmtInt(reading.left_out)}{" "}
                {plural(reading.left_out, "database")} the connection's login can't open.</p>}
              {discoveringDatabases !== null && <Busy label="Reading tables" />}
              <div className="table-wrap"><table className="data tree">
                <thead><tr><th>Server › database › table</th><th>Read through · load type</th><th className="num">Databases · tables</th></tr></thead>
                <tbody>
                  {(Array.isArray(summary?.servers) ? summary.servers : []).map((s: Row) => {
                    const cells = serverRowCells(s, LOAD_TYPES);
                    const open = openServer === cells.server;
                    return <Fragment key={cells.server}>
                      <tr><td className="name"><Twisty open={open} onToggle={() => setOpenServer(open ? null : cells.server)}><b>{cells.server}</b></Twisty></td>
                        <td>{cells.readThrough} · {cells.loadTypes}</td>
                        <td className="num"><span className="row-end">{cells.counts}
                          <button className="ghost icon remove" aria-label="Remove the tables" title="Remove the tables" onClick={removeTables}>×</button></span></td></tr>
                      {open && <ChosenDatabases source={chosen.source} server={cells.server} />}
                    </Fragment>;
                  })}
                </tbody>
              </table></div>
              {summary && Number(summary.discovered) > 0 && <p className="muted">{fmtInt(summary.discovered)} {plural(summary.discovered, "table")}
                found by name for the file's database lines{Number(summary.covered) > 0
                  ? `; ${fmtInt(summary.covered)} the file names in a row of its own, whose settings are used` : ""}.</p>}
              {(summary?.invalid ?? []).length > 0 && <Notice tone="bad">{fmtInt(summary!.invalid.length)} {plural(summary!.invalid.length, "line")} can't be
                added:<ul>{summary!.invalid.slice(0, 20).map((l: Row) => <li key={l.line}>line {l.line}, {l.table}: {l.errors.join("; ")}</li>)}</ul>
                {summary!.invalid.length > 20 && <span className="muted">and {fmtInt(summary!.invalid.length - 20)} more.</span>}</Notice>}
              <ErrorBox error={preview.error} />
            </>
          )}
        </Section>
        {discovering && <DiscoverDialog drafts onClose={(pick) => { setDiscovering(false); if (pick) setChosen(pick); }} />}
        {importing && <ImportCsvDialog loadType={loadType} onClose={(next) => {
          setImporting(false); if (next) { setChosen(next); setLoadType(next.loadType); } }} />}

        <Section title="How it writes"><WritesFields w={w} setW={setW}
          reseed={<ParameterField name="reseed_on_new_columns" params={params} setParams={setParams} options={options.data} />} /></Section>
        <Section title={<>Identity columns<Info label="About identity columns">Written on every row next to __client_id__, so the shared
          tables say whose rows they are.</Info></>}>
          <IdentityFields w={w} setW={setW} identity={identityRows} setIdentity={setIdentity} newJob
                          sharedKey={options.data?.shared_client_id} />
        </Section>
        <Section title="When it runs" note="A new job starts paused: resume it from its page once you have checked it.">
          <RunFields run={run} setRun={setRun} cadences={options.data?.cadences ?? CADENCES} />
        </Section>

        <details className="card advanced">
          <summary><b>Advanced configuration</b></summary>
          <h3>Compute</h3>
          <div className="form-grid"><TablesPerGroup value={tablesPerGroup} onChange={setTablesPerGroup} /></div>
          <ClusterFields info={clusterInfo.data} error={clusterInfo.error} cluster={cluster} setCluster={setCluster} locked={null} after="Create" />
          <h3>Tuning</h3>
          <ParameterFields params={params} setParams={setParams} options={options.data} />
        </details>
      </fieldset>
    </>
  );
}

/** A template for the CSV: its header, and one line per table and one per database. */
function downloadTemplate() {
  const url = URL.createObjectURL(new Blob([CSV_TEMPLATE], { type: "text/csv" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "tables-template.csv";
  link.click();
  URL.revokeObjectURL(url);
}

/** The first lines of an uploaded file as the file gave them: the header's columns, then each line's values. */
function FirstLines({ columns, lines }: { columns: string[]; lines: Row[] }) {
  return (
    <div className="table-wrap"><table className="data dense">
      <thead><tr><th className="num">Line</th>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead>
      <tbody>
        {lines.map((line) => (
          <tr key={line.line}><td className="num">{line.line}</td>
            {columns.map((column) => <td key={column} className="name">{String(line.fields?.[column] ?? "")}</td>)}</tr>
        ))}
      </tbody>
    </table></div>
  );
}

/** The Unity Catalog connection a file is read through, chosen before the file: the connections the app can read. */
function ConnectionPick({ value, onChange }: { value: string; onChange: (name: string) => void }) {
  const list = useQuery({ queryKey: ["connections"], queryFn: () => api.get<Row>("/connections") });
  const found: Row[] = (list.data?.connections ?? []).filter((c: Row) => c.discoverable);
  return (
    <>
      <label><span>Connection <Info>Every line of the file is read through it. A connection made in Catalog Explorer
        shows after Refresh on the Discover page, once the app has USE CONNECTION on it.</Info></span>
        <select value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">Choose a connection</option>
          {found.map((c: Row) => <option key={c.name} value={c.name}>{c.name}</option>)}
        </select>
</label>
      {list.data?.error && <div className="wide"><Notice tone="warn">The app can't list connections: {list.data.error}</Notice></div>}
      <div className="wide"><ErrorBox error={list.error} /></div>
    </>
  );
}

/** Import a CSV in three steps: the connection, the file (dropped, picked or pasted), then a preview of what the file holds
 *  and the lines that can't be taken, and the tables chosen from it (SourcePick). The file is uploaded once and kept
 *  (POST /api/uploads). New and Edit both use it. */
export function ImportCsvDialog({ loadType, onClose }: { loadType: string;
                         onClose: (next: (Chosen & { loadType: string }) | null) => void }) {
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [connection, setConnection] = useState("");
  const [text, setText] = useState("");
  const [fileName, setFileName] = useState<string | null>(null);
  const [pasting, setPasting] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const [load, setLoad] = useState(loadType);
  const [upload, setUpload] = useState<Row | null>(null);
  const [picked, setPicked] = useState<Picked>(nothingPicked);
  const send = useMutation({
    mutationFn: () => api.post<Row>("/uploads", { csv: text, file_name: fileName, connection }),
    onSuccess: (r) => { setUpload(r); setText(""); setStep(3); },
  });
  const firstLines = useQuery({
    queryKey: ["upload-lines", upload?.upload_id], enabled: !!upload,
    queryFn: () => api.get<Row>(`/uploads/${upload!.upload_id}/lines?limit=${PREVIEW_LINES}`),
  });
  const databases = Number(upload?.databases ?? 0);
  const tables = Number(upload?.tables ?? 0);
  const invalid = Number(upload?.invalid ?? 0);
  const readFile = async (file: File) => { setText(await file.text()); setFileName(file.name); };
  const blocked = step === 1 ? (connection ? undefined : "Choose the connection first")
    : step === 2 ? (send.isPending ? "Uploading the file" : !connection ? "Choose the connection first"
      : text.trim() ? undefined : "Choose a file or paste its lines first")
    : pickedAny(picked) || databases > 0 ? undefined : "Choose the server, databases or tables first";
  const action = step === 1 ? "Next: the file" : step === 2 ? (send.isPending ? "Uploading…" : "Upload and preview") : "Add the tables chosen";
  return (
    <Modal title={`Import a CSV · Step ${step} of 3: ${step === 1 ? "the connection" : step === 2 ? "the file" : "preview and choose tables"}`}
      action={action} actionDisabled={blocked}
      onClose={(ok) => {
        if (!ok) return onClose(null);
        if (step === 1) return setStep(2);
        if (step === 2) return send.mutate();
        const databaseWords = databases ? `${fmtInt(databases)} ${plural(databases, "database")} discovered at Create` : "";
        const chosenWords = [pickedAny(picked) ? pickedText(picked) : "", databaseWords].filter(Boolean).join(", ");
        onClose({ source: { upload_id: upload!.upload_id, ...picked }, loadType: load,
                  label: `${upload!.file_name ?? "the uploaded file"}: ${chosenWords}` });
      }}>
      {step === 1 && (
        <div className="form-grid">
          <ConnectionPick value={connection} onChange={setConnection} />
          <label>How its tables load<select value={load} onChange={(e) => setLoad(e.target.value)}>
            {LOAD_TYPES.map(([k, label]) => <option key={k} value={k}>{label}</option>)}</select>
            <span className="help">A line's own load_type wins. Change a table's load type later in Edit.</span></label>
        </div>
      )}
      {step === 2 && (
        <>
          <div className="toolbar">
            <button className="ghost" onClick={downloadTemplate}>Download template</button>
            <Info label="About the CSV's columns">One line per table or per database. Columns: <span className="mono">src_database</span>{" "}
              (required); <span className="mono">src_schema</span> and <span className="mono">src_table</span> together for one table, or neither
              for every table of that database, found by name when the job is created. Then, optionally, <span className="mono">select_list</span>,{" "}
              <span className="mono">primary_key</span>, <span className="mono">cursor_column</span>, <span className="mono">client_id</span> and{" "}
              <span className="mono">load_type</span>, and <span className="mono">attr:&lt;column&gt;</span> identity values. The file has no
              connection column: a row that names another connection refuses the file.</Info>
          </div>
          {pasting ? (
            <label>Paste the CSV<textarea rows={6} value={text.length > 20000 ? `${text.slice(0, 20000)}\n…` : text} placeholder={CSV_EXAMPLE}
              readOnly={text.length > 20000} onChange={(e) => { setText(e.target.value); setFileName(null); }} /></label>
          ) : (
            <div className={`drop-zone${dragging ? " over" : ""}`}
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => { e.preventDefault(); setDragging(false); const file = e.dataTransfer.files[0]; if (file) readFile(file); }}>
              <p>{fileName ? <>Chosen: <span className="mono">{fileName}</span></> : "Drop a CSV file here"}</p>
              <button className="ghost" onClick={() => fileInput.current?.click()}>Choose a file</button>
              <input ref={fileInput} type="file" accept=".csv,text/csv" hidden
                onChange={(e) => { const file = e.target.files?.[0]; if (file) readFile(file); }} />
            </div>
          )}
          <p className="toolbar">
            <button className="ghost link" onClick={() => setPasting(!pasting)}>{pasting ? "Choose a file instead" : "Paste CSV instead"}</button>
            <button className="ghost link" onClick={() => setStep(1)}>Back to the connection</button>
          </p>
          <ErrorBox error={send.error} />
        </>
      )}
      {step === 3 && upload && (
        <div className="discover">
          <p className="muted">Uploaded {upload.file_name ?? "the file"}: {fmtInt(tables)} {plural(tables, "table")}, {fmtInt(databases)}{" "}
            {plural(databases, "database")} (discovered at Create), {fmtInt(invalid)} {plural(invalid, "line")} that can't be taken.</p>
          {(upload.columns ?? []).length > 0 && <p className="muted">Columns found: <span className="mono">{(upload.columns as string[]).join(", ")}</span>.</p>}
          {(firstLines.data?.lines ?? []).length > 0 && <>
            <p className="muted">The first {fmtInt(firstLines.data!.lines.length)} of {fmtInt(Number(upload.lines))} lines:</p>
            <FirstLines columns={firstLines.data!.columns} lines={firstLines.data!.lines} />
          </>}
          {invalid > 0 && <Notice tone="warn">These lines can't be taken, and are left out:<ul>
            {(upload.invalid_lines ?? []).map((l: Row) => <li key={l.line}>line {l.line}{l.table ? `, ${l.table}` : ""}: {l.errors.join("; ")}</li>)}
          </ul>{invalid > (upload.invalid_lines ?? []).length && <p className="muted">Showing the first {fmtInt((upload.invalid_lines ?? []).length)} of {fmtInt(invalid)}.</p>}</Notice>}
          {databases > 0 && <p className="muted">Its database lines are always taken: each database's tables are found by name through{" "}
            {upload.connection} when the job is created. The table lines are taken as chosen below.</p>}
          <SourcePick kind="upload" sourceId={upload.upload_id} picked={picked} setPicked={setPicked} />
          <p className="muted">Chosen: {pickedText(picked)}.</p>
          <p className="toolbar"><button className="ghost link" onClick={() => { setUpload(null); setStep(2); }}>Upload a different file</button></p>
        </div>
      )}
    </Modal>
  );
}
