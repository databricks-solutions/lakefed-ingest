import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { JdbcSourceNotice } from "./JdbcConnectionForm";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api, qs, type Row } from "../api";
import { Crumbs, ConfirmButton, ErrorBox, Info, Menu, Notice, PageHead, Pager, Section, StateBar, fmtInt, plural, useConfirm, Twisty,
         Busy, Chevron, OutLink, Required, StatusLine, toast } from "../components";
import { usePermissions } from "../permissions";
import { CADENCES, IdentityFields, ParameterField, ParameterFields, RunFields, WritesFields } from "./jobForm";
import { ImportCsvDialog } from "./NewJob";
import { TaskProgress, finished, newRequestId, resumableWrite } from "./tasks";
import { DiscoverDialog } from "./Discover";
import { isUnconfirmed, requestWriteNotice, saveNotice, syncFailureLines } from "../pending";

// Edit an ingestion job: what it ingests (a tree from its servers), how it writes, its identity columns, when it
// runs and its advanced configuration. A change in the tree applies once its dialog is confirmed, as Catalog Explorer
// asks before a change to data; the rest is saved with Save.

const PAGE = 25;
const LOAD_TYPES: Record<string, string> = { ct: "Change tracking", cursor: "Cursor column", full: "Full reload" };

/** Why the signed-in user may not change this job (their Databricks permission), or null when they may. */
type Locked = string | null;

/** When it runs, its parameters and its name as the form shows them for a stored schedule: the baseline Save compares with. */
function scheduleForm(schedule: Row) {
  // A set-time schedule keeps its stored zone; Europe/London is only the default offered when a minute or
  // hourly schedule (always UTC) becomes a set-time one.
  const start = Number(schedule.options?.start_minute ?? 0);
  return {
    run: { cadence_minutes: schedule.cadence_minutes, start: `${String(Math.floor(start / 60)).padStart(2, "0")}:${String(start % 60).padStart(2, "0")}`,
           timezone: Number(schedule.cadence_minutes) > 60 ? schedule.timezone : "Europe/London",
           paused: schedule.paused, notify_emails: schedule.notify_emails ?? "",
           rediscover_hour: typeof schedule.options?.rediscover_hour === "number" ? schedule.options.rediscover_hour : null } as Row,
    params: Object.fromEntries(Object.entries(schedule.parameters ?? {}).map(([k, v]) => [k, String(v)])) as Record<string, string>,
    name: String(schedule.name),
  };
}

const startMinute = (hhmm: string) => { const [h, m] = String(hhmm || "00:00").split(":").map(Number); return (h || 0) * 60 + (m || 0); };
const value = (s?: Row) => (s && "value" in s ? s.value : undefined);
function TableRows({ jobId, server, database, locked, readOnly }: { jobId: string; server: string; database: string; locked: Locked;
                    readOnly?: boolean }) {
  const [offset, setOffset] = useState(0);
  const qc = useQueryClient();
  const tables = useQuery({
    queryKey: ["job-tree", jobId, server, database, offset],
    queryFn: () => api.get<Row>(`/ingestion-jobs/${jobId}/tree${qs({ server, database, offset, limit: PAGE })}`),
  });
  const confirm = useConfirm();
  // A load type is the same in every database of the job: the preview counts the tables it changes and reloads.
  const edit = useMutation({
    mutationFn: async ({ t, load_type }: { t: Row; load_type: string }) => {
      const body = { schema: t.schema, table: t.table, changes: { load_type } };
      const preview = (await api.post<Row>(`/ingestion-jobs/${jobId}/tables/edit`, { ...body, dry_run: true })).summary;
      const ok = await confirm.ask({ title: `Change the load type of ${t.schema}.${t.table}?`, action: "Change load type",
        body: <><p>It becomes {LOAD_TYPES[load_type] ?? load_type} in all {fmtInt(preview.changed)} {plural(preview.changed, "table")} of
          this name in the job's databases.</p>
          {Number(preview.reseed) > 0 && <p>The next run loads {fmtInt(preview.reseed)} of them again from their sources.</p>}</> });
      return ok ? api.post(`/ingestion-jobs/${jobId}/tables/edit`, { ...body, dry_run: false }) : null;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["job-tree", jobId] }),
  });
  const enable = useMutation({
    mutationFn: async (t: Row) => {
      const ok = await confirm.ask(t.enabled
        ? { title: `Disable ${t.schema}.${t.table} in ${database}?`, action: "Disable", danger: true,
            body: "Runs stop loading it. Its target table, checkpoint and history stay." }
        : { title: `Enable ${t.schema}.${t.table} in ${database}?`, action: "Enable", body: "Its group's next run loads it." });
      return ok ? api.post(`/ingestion-jobs/${jobId}/enable`, { server, database, schema: t.schema, table: t.table, enabled: !t.enabled }) : null;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["job-tree", jobId] }),
  });
  return (
    <>
      {(tables.data?.items ?? []).map((t: Row) => (
        <tr key={t.id} className="l3">
          <td className="name">{t.schema}.{t.table}</td>
          <td>{readOnly ? LOAD_TYPES[t.load_type] ?? t.load_type : <select value={t.load_type} disabled={edit.isPending || !!locked}
            title={locked ?? "Changes this table in every database of the job"} onChange={(e) => edit.mutate({ t, load_type: e.target.value })}>
            <option value="ct">Change tracking</option><option value="cursor">Cursor column</option>
            <option value="full">Full reload</option></select>}</td>
          <td className="mono">{t.primary_key}</td>
          <td className="num">{fmtInt(t.columns)} columns</td>
          <td className="mono">{t.target}</td>
          <td>{readOnly ? (t.enabled ? "on" : <span className="muted">off</span>) : <input type="checkbox" checked={!!t.enabled}
            disabled={enable.isPending || !!locked} title={locked ?? undefined} onChange={() => enable.mutate(t)} />}</td>
          <td><StateBar row={t} /></td>
        </tr>
      ))}
      {(tables.data?.total ?? 0) > PAGE && (
        <tr className="l3"><td colSpan={7}><Pager offset={offset} limit={PAGE} total={tables.data!.total} noun="table" onPage={setOffset} /></td></tr>
      )}
      {(edit.error || enable.error || tables.error) && <tr className="l3"><td colSpan={7}><ErrorBox error={edit.error || enable.error || tables.error} /></td></tr>}
      {confirm.dialog}
    </>
  );
}

function DatabaseRows({ jobId, server, locked, readOnly }: { jobId: string; server: string; locked: Locked; readOnly?: boolean }) {
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const qc = useQueryClient();
  const dbs = useQuery({
    queryKey: ["job-tree", jobId, server, "", search, offset],
    queryFn: () => api.get<Row>(`/ingestion-jobs/${jobId}/tree${qs({ server, search, offset, limit: PAGE })}`),
  });
  const confirm = useConfirm();
  const enable = useMutation({
    mutationFn: async (d: Row) => {
      const on = Number(d.enabled_tables) === 0;
      const ok = await confirm.ask(on
        ? { title: `Enable all ${fmtInt(d.tables)} ${plural(d.tables, "table")} of ${d.database}?`, action: "Enable",
            body: "Its group's next run loads them." }
        : { title: `Disable all ${fmtInt(d.enabled_tables)} enabled ${plural(d.enabled_tables, "table")} of ${d.database}?`,
            action: "Disable", danger: true, body: "Runs stop loading them. Their target tables, checkpoints and history stay." });
      return ok ? api.post(`/ingestion-jobs/${jobId}/enable`, { server, database: d.database, enabled: on }) : null;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["job-tree", jobId] }),
  });
  return (
    <>
      <tr className="l2"><td colSpan={7}><input className="search tree-search" placeholder={`Find a database on ${server}`} value={search}
        onChange={(e) => { setSearch(e.target.value); setOffset(0); }} /></td></tr>
      {(dbs.data?.items ?? []).map((d: Row) => (
        <Fragment key={d.database}>
          <tr className="l2">
            <td className="name"><Twisty open={open === d.database} onToggle={() => setOpen(open === d.database ? null : d.database)}>{d.database}</Twisty></td>
            <td className="muted">{d.client_id ? `client_id ${d.client_id}` : ""}</td>
            <td></td>
            <td className="num">{fmtInt(d.tables)} {Number(d.missing) > 0 && <span className="tag warn">{fmtInt(d.missing)} missing</span>}</td>
            <td></td>
            <td>{readOnly ? <span className="muted">{fmtInt(d.enabled_tables)} on</span> : <input type="checkbox"
              checked={Number(d.enabled_tables) > 0} disabled={enable.isPending || !!locked}
              title={locked ?? "Enables or disables every table of this database"} onChange={() => enable.mutate(d)} />}</td>
            <td>{Number(d.enabled_tables) > 0 ? <StateBar row={d} /> : <span className="muted">disabled</span>}</td>
          </tr>
          {open === d.database && <TableRows jobId={jobId} server={server} database={d.database} locked={locked} readOnly={readOnly} />}
        </Fragment>
      ))}
      {(dbs.data?.total ?? 0) > PAGE && (
        <tr className="l2"><td colSpan={7}><Pager offset={offset} limit={PAGE} total={dbs.data!.total} noun="database" onPage={setOffset} /></td></tr>
      )}
      {(dbs.error || enable.error) && <tr className="l2"><td colSpan={7}><ErrorBox error={dbs.error || enable.error} /></td></tr>}
      {confirm.dialog}
    </>
  );
}

/** What a job ingests, as a tree from its servers; read only on the job page, editable in Edit. */
export function Tree({ jobId, locked, readOnly }: { jobId: string; locked: Locked; readOnly?: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  const servers = useQuery({ queryKey: ["job-tree", jobId], queryFn: () => api.get<Row>(`/ingestion-jobs/${jobId}/tree${qs({ limit: 200 })}`) });
  return (
    <div className="table-wrap">
      {servers.data?.jdbc && <JdbcSourceNotice jdbc={servers.data.jdbc} />}
      <table className="data tree">
        <thead><tr><th>Server › database › table</th><th>Read through · load type</th><th>Key</th>
          <th className="num">Databases · tables</th><th>Target table</th><th>Enabled</th><th>State now</th></tr></thead>
        <tbody>
          {(servers.data?.items ?? []).map((s: Row) => (
            <Fragment key={s.server}>
              <tr>
                <td className="name"><Twisty open={open === s.server} onToggle={() => setOpen(open === s.server ? null : s.server)}><b>{s.server}</b></Twisty></td>
                <td>{s.src_connection ? `UC connection ${s.src_connection}` : `JDBC ${s.src_host}`}</td>
                <td></td>
                <td className="num">{fmtInt(s.databases)} · {fmtInt(s.tables)}</td>
                <td></td>
                <td className="muted">{fmtInt(s.enabled_tables)} on</td>
                <td><StateBar row={s} /></td>
              </tr>
              {open === s.server && <DatabaseRows jobId={jobId} server={s.server} locked={locked} readOnly={readOnly} />}
            </Fragment>
          ))}
          {servers.data && !servers.data.items.length && <tr><td colSpan={7} className="muted">No tables yet: Add tables adds them.</td></tr>}
        </tbody>
      </table>
      <ErrorBox error={servers.error} />
    </div>
  );
}

function AddDatabases({ jobId, servers }: { jobId: string; servers: string[] }) {
  const [server, setServer] = useState(servers[0] ?? "");
  const [names, setNames] = useState("");
  const [preview, setPreview] = useState<Row | null>(null);
  const [checked, setChecked] = useState<Row | null>(null);
  const qc = useQueryClient();
  const body = (dry_run: boolean): Row => ({
    databases: names.split(/[\n,]+/).map((n) => n.trim()).filter(Boolean).map((database) => ({ server, database })),
    full_list: false, dry_run });
  // The confirm applies exactly the request that was checked; any change to the inputs clears the check.
  const check = useMutation({
    mutationFn: () => { const b = body(false); return api.post<Row>(`/ingestion-jobs/${jobId}/databases`, { ...b, dry_run: true }).then((r) => ({ r, b })); },
    onSuccess: ({ r, b }) => { setPreview(r.summary); setChecked({ ...b, request_id: newRequestId() }); setTask(null); },
  });
  useEffect(() => { setPreview(null); setChecked(null); }, [server, names]);
  const [task, setTask] = useState<Row | null>(null);
  // One request id per checked request: applying it again (a retry after a timeout) finds the same task.
  const apply = useMutation({
    mutationFn: async () => finished(await api.post<Row>(`/ingestion-jobs/${jobId}/databases`, checked!), setTask),
    onSuccess: (r) => {
      setPreview(r.summary); qc.invalidateQueries({ queryKey: ["job-tree", jobId] });
      toast(`Added ${fmtInt(r.summary.new_tables)} ${plural(r.summary.new_tables, "table")}`);
    },
  });
  const note = requestWriteNotice("Adding databases", { pending: apply.isPending && !task, error: apply.error });
  return (
    <div className="add-databases">
      <div className="form-grid">
        <label><span>Server<Required /><Info label="About the server">A UC connection, or one of the job's servers. Each database
          gets every table of the job, with the job's target names and identity columns filled in.</Info></span>
          <input list="job-servers" value={server} onChange={(e) => setServer(e.target.value)} />
          <datalist id="job-servers">{servers.map((s) => <option key={s} value={s} />)}</datalist></label>
        <label className="wide"><span>Database names, one per line<Required /></span><textarea rows={4} value={names} onChange={(e) => setNames(e.target.value)} /></label>
      </div>
      <div className="toolbar">
        <button className="ghost" disabled={check.isPending} onClick={() => check.mutate()}>Check</button>
        {preview && checked && !preview.applied && <ConfirmButton label={`Add ${fmtInt(preview.new_tables)} ${plural(preview.new_tables, "table")}`}
          disabled={!preview.new_tables && !preview.enable && !preview.disable} onConfirm={() => apply.mutate()} />}
      </div>
      {preview && !preview.applied && <Notice>
        {"Would add: "}{fmtInt(preview.new_tables)} {plural(preview.new_tables, "table")} from {fmtInt(preview.databases)} {plural(preview.databases, "database")}
        {preview.skipped ? `, ${fmtInt(preview.skipped)} skipped (target taken)` : ""}; enable {fmtInt(preview.enable)}, disable {fmtInt(preview.disable)}
        {preview.new_groups?.length ? `; new groups ${preview.new_groups.join(", ")}` : ""}.</Notice>}
      <StatusLine line={note} />
      {apply.isPending && <TaskProgress task={task} />}
      {!apply.isPending && resumableWrite(apply.error) && <TaskProgress task={task} onResume={() => apply.mutate()} />}
      <ErrorBox error={check.error || (isUnconfirmed(apply.error) || resumableWrite(apply.error) ? null : apply.error)} />
    </div>
  );
}

/** Add tables from an uploaded CSV or a discovery, by reference: checked first, then added after a confirm, with the
 *  job's settings; a large add is written by a background task, whose progress shows here. */
/** A database-only file's tables are discovered by name before a check can count them: the check is asked again every few
 *  seconds until they are, and onDiscovering hears how many databases are still being read (null once none is). */
async function checkedWhenDiscovered(check: () => Promise<Row>, onDiscovering: (databases: number | null) => void): Promise<Row> {
  for (;;) {
    const answer = await check();
    if (!answer.discovering) { onDiscovering(null); return answer; }
    onDiscovering(Number(answer.databases));
    await new Promise((resolve) => setTimeout(resolve, 3000));
  }
}

function useAddTables(jobId: string) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [open, setOpen] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  const [found, setFound] = useState<Row | null>(null);
  const [discoveringDatabases, setDiscoveringDatabases] = useState<number | null>(null);
  const [task, setTask] = useState<Row | null>(null);
  // The body of the write sent last: a write that stopped part way is finished by sending it again, as it was.
  const [sent, setSent] = useState<Row | null>(null);
  const [writing, setWriting] = useState(false);
  const write = async (body: Row) => {
    setSent(body);
    setWriting(true);
    try {
      return await finished(await api.post<Row>(`/ingestion-jobs/${jobId}/tables`, { ...body, dry_run: false }), setTask);
    } finally {
      setWriting(false);
    }
  };
  const add = useMutation({
    mutationFn: async (request: { source: Row; loadType: string } | { resume: Row }) => {
      setTask(null);
      if ("resume" in request) return write(request.resume);
      const body = { source: request.source, load_type: request.loadType, request_id: newRequestId() };
      const preview = (await checkedWhenDiscovered(() => api.post<Row>(`/ingestion-jobs/${jobId}/tables`, { ...body, dry_run: true }),
        setDiscoveringDatabases)).summary;
      setFound(preview);
      if (preview.invalid.length || !preview.tables) return null;
      const servers = (preview.servers as Row[]).map((s) => `${fmtInt(s.tables)} on ${s.server}`).join(", ");
      const ok = await confirm.ask({ title: `Add ${fmtInt(preview.tables)} ${plural(preview.tables, "table")} to this job?`, action: "Add tables",
        body: <><p>{servers}. Each takes the job's settings: how it writes, its target names and identity columns.</p>
          {Number(preview.in_job) > 0 && <p>{fmtInt(preview.in_job)} more {plural(preview.in_job, "line")} the job already has {Number(preview.in_job) === 1 ? "is" : "are"} left as {Number(preview.in_job) === 1 ? "it is" : "they are"}.</p>}
          {preview.new_groups.length > 0 && <p>A new server gets {fmtInt(preview.new_groups.length)} new {plural(preview.new_groups.length, "group")}.</p>}
          <p>Their first loads run at the job's next run.</p></> });
      return ok ? write(body) : null;
    },
    onSuccess: (r) => { if (r) { setFound(r.summary); toast(`Added ${fmtInt(r.summary.tables)} ${plural(r.summary.tables, "table")}`);
      qc.invalidateQueries({ queryKey: ["job-tree", jobId] });
      qc.invalidateQueries({ queryKey: ["job-settings", jobId] }); qc.invalidateQueries({ queryKey: ["schedules"] }); } },
  });
  // Pending only while the write itself is sent: the checks and the confirm before it show their own state.
  const note = requestWriteNotice("Adding tables", { pending: add.isPending && writing && !task, error: add.error });
  const dialog = <>
    {open && <ImportCsvDialog loadType="ct" onClose={(next) => { setOpen(false); if (next) add.mutate(next); }} />}
    {discovering && <DiscoverDialog onClose={(pick) => { setDiscovering(false); if (pick) add.mutate({ source: pick.source, loadType: "ct" }); }} />}
    {confirm.dialog}
  </>;
  const result = <>
    <StatusLine line={note} />
    {discoveringDatabases !== null && <Busy label="Reading tables" />}
    {add.isPending && <TaskProgress task={task} />}
    {!add.isPending && resumableWrite(add.error)
      ? <TaskProgress task={task} onResume={() => add.mutate({ resume: sent as Row })} />
      : <ErrorBox error={isUnconfirmed(add.error) ? null : add.error} />}
    {found && found.invalid.length > 0 && <Notice tone="bad">Nothing was added: {fmtInt(found.invalid.length)} {plural(found.invalid.length, "line")} can't be
      added.<ul>{found.invalid.slice(0, 20).map((l: Row) => <li key={l.line}>line {l.line}, {l.table}: {l.errors.join("; ")}</li>)}</ul></Notice>}

  </>;
  return { open: () => setOpen(true), discover: () => setDiscovering(true), dialog, result };
}

export default function JobEdit() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["job-settings", id], queryFn: () => api.get<Row>(`/ingestion-jobs/${id}/settings`) });
  const schedules = useQuery({ queryKey: ["schedules"], queryFn: () => api.get<Row>("/schedules") });
  const options = useQuery({ queryKey: ["schedule-options"], queryFn: () => api.get<Row>("/schedules/options") });
  const servers = useQuery({ queryKey: ["job-tree", id], queryFn: () => api.get<Row>(`/ingestion-jobs/${id}/tree${qs({ limit: 200 })}`) });
  const schedule: Row | undefined = (schedules.data?.schedules ?? schedules.data ?? []).find?.((s: Row) => String(s.id) === id);
  const s = settings.data?.settings as Row | undefined;
  const [w, setW] = useState<Row>({});
  const [identity, setIdentity] = useState<[string, string][]>([]);
  // Identity columns whose values differ between tables: a value typed here sets the column for all of them.
  const [differing, setDiffering] = useState<Record<string, string>>({});
  const [run, setRun] = useState<Row>({});
  const [name, setName] = useState("");
  const [params, setParams] = useState<Record<string, string>>({});
  const [addOpen, setAddOpen] = useState(false);
  const addTables = useAddTables(id);
  // The settings form as it loaded, so "unsaved" means the user changed something, not that a default differs.
  const [loaded, setLoaded] = useState("");
  const locked: Locked = usePermissions().data?.jobs[id]?.change ?? null;
  // The cluster copies run a save started: from the save's answer until a read taken after it shows the run ended.
  const [applyStartedAt, setApplyStartedAt] = useState<number | null>(null);
  const [applyError, setApplyError] = useState<string | null>(null);
  const clusterInfo = useQuery({
    queryKey: ["job-cluster", id], queryFn: () => api.get<Row>(`/ingestion-jobs/${id}/cluster`),
    refetchInterval: (query) => (query.state.data?.applying || applyStartedAt != null ? 5000 : false),
  });
  const [cluster, setCluster] = useState<Row | null>(null);
  useEffect(() => {
    if (clusterInfo.data && cluster === null) setCluster({ ...clusterInfo.data.defaults, ...(clusterInfo.data.cluster ?? {}) });
  }, [clusterInfo.data, cluster]);

  useEffect(() => {
    if (!s) return;
    const form = { fan_in: value(s.fan_in), write_mode_changes: value(s.write_mode_changes), write_mode_full: value(s.write_mode_full),
      freshness_sla_minutes: value(s.freshness_sla_minutes) ?? "", sink_catalog: value(s.sink_catalog) ?? "",
      sink_schema: value(s.sink_schema) ?? "", sink_table: value(s.sink_table) ?? "",
      // A job turning fan-in on takes the key new fan-in jobs get (job_settings.SHARED_CLIENT_ID), with its server.
      client_id: value(s.client_id) ?? "{server}_{database}" };
    const ident = Object.entries((value(s.identity) as Record<string, string>) ?? {});
    const differs = Object.fromEntries((settings.data?.identity_differs ?? []).map((name: string) => [name, ""]));
    setW(form); setIdentity(ident); setDiffering(differs);
    setLoaded(JSON.stringify({ w: form, identity: ident, differing: differs }));
  }, [settings.data]);
  const baseline = useMemo(() => (schedule ? scheduleForm(schedule) : null), [schedule?.id, schedule?.updated_at]);
  useEffect(() => {
    if (!baseline) return;
    setRun(baseline.run); setParams(baseline.params); setName(baseline.name);
  }, [baseline]);

  // Only what differs from the job's rows is sent, so an untouched setting never rewrites a mixed one.
  const changed = (): Row => {
    if (!s) return {};
    const out: Row = {};
    for (const k of ["fan_in", "write_mode_changes", "write_mode_full", "sink_catalog", "sink_schema", "sink_table", "client_id"])
      if (w[k] !== undefined && w[k] !== value(s[k]) && !(w[k] === "" && value(s[k]) == null)) out[k] = w[k];
    if (String(w.freshness_sla_minutes ?? "") !== String(value(s.freshness_sla_minutes) ?? ""))
      out.freshness_sla_minutes = w.freshness_sla_minutes === "" ? null : Number(w.freshness_sla_minutes);
    const typed = Object.entries(differing).filter(([, t]) => t.trim());
    const ident = Object.fromEntries([...typed, ...identity].filter(([n, t]) => n.trim() && t.trim()).map(([n, t]) => [n.trim(), t.trim()]));
    if (typed.length || JSON.stringify(ident) !== JSON.stringify(value(s.identity) ?? {})) out.identity = ident;
    return out;
  };
  const writeModeMoves = s && ((w.write_mode_changes && w.write_mode_changes !== value(s.write_mode_changes))
    || (w.write_mode_full && w.write_mode_full !== value(s.write_mode_full)));
  const targetsChanged = s && ["sink_catalog", "sink_schema", "sink_table"].some((k) => w[k] !== value(s[k]));

  const save = useMutation({
    mutationFn: async () => {
      setApplyError(null);
      // A cluster read already in flight would answer from before this save, so it can't end the wait.
      await qc.cancelQueries({ queryKey: ["job-cluster", id] });
      const edits = changed();
      let result: Row | null = null;
      let scheduleResult: Row | null = null;
      if (Object.keys(edits).length) {
        // The preview refuses (422, with its reasons) anything the tables can't take, before any write.
        await api.post<Row>(`/ingestion-jobs/${id}/settings`, { settings: edits, dry_run: true });
        result = await api.post<Row>(`/ingestion-jobs/${id}/settings`, { settings: edits, dry_run: false });
      }
      if (schedule) {
        const cadence = Number(run.cadence_minutes);
        const { cluster: stored, rediscover_hour: _storedHour, ...kept } = schedule.options ?? {};
        // Until the cluster is read, keep what's stored; at the Databricks job's own values, the job has no cluster of its own.
        const own = !clusterInfo.data || !cluster ? stored : ownCluster(cluster, clusterInfo.data.defaults);
        scheduleResult = await api.post<Row>(`/schedules/${schedule.id}`, {
          name: name.trim() || schedule.name, cadence_minutes: cadence, timezone: cadence <= 60 ? "UTC" : run.timezone || "Europe/London",
          stagger: schedule.stagger, paused: run.paused, notify_emails: run.notify_emails || null, job_variant: schedule.job_variant,
          options: { ...kept, ...(own ? { cluster: own } : {}),
            ...(cadence > 60 ? { start_minute: startMinute(run.start) } : { start_minute: undefined }),
            ...(run.rediscover_hour == null ? {} : { rediscover_hour: Number(run.rediscover_hour) }) },
          members: (schedule.members as Row[]).map((m) => m.task_collection),
          parameters: Object.fromEntries(Object.entries(params).filter(([, v]) => v.trim() !== "").map(([k, v]) => [k, v.trim()])),
        }).catch((e: Error) => {
          throw new Error(result ? `The table settings were saved, but When it runs and Advanced configuration were not: ${e.message}` : e.message);
        });
      }
      return { settings: result, schedule: scheduleResult };
    },
    onSuccess: (data) => {
      if (data.schedule?.cluster_started) setApplyStartedAt(Date.now());
      if (data.schedule?.cluster_error) setApplyError(String(data.schedule.cluster_error));
      qc.invalidateQueries({ queryKey: ["job-settings", id] }); qc.invalidateQueries({ queryKey: ["schedules"] });
      qc.invalidateQueries({ queryKey: ["job-tree", id] }); qc.invalidateQueries({ queryKey: ["ingestion-job", id] });
      qc.invalidateQueries({ queryKey: ["job-cluster", id] }); qc.invalidateQueries({ queryKey: ["job-compute", id] }); },
  });

  useEffect(() => {
    const info = clusterInfo.data;
    if (applyStartedAt == null || !info || info.applying || clusterInfo.dataUpdatedAt < applyStartedAt) return;
    setApplyError(info.error ?? null);
    setApplyStartedAt(null);
  }, [clusterInfo.data, clusterInfo.dataUpdatedAt, applyStartedAt]);
  const saving = save.isPending || applyStartedAt != null;
  const savedSummary = save.data?.settings?.summary;
  const saveLine = saveNotice({
    pending: save.isPending, done: save.isSuccess, error: save.error, applying: applyStartedAt != null,
    applyTimeout: clusterInfo.data?.applying?.outcome?.state === "timeout" ? clusterInfo.data.applying.outcome.reason : null,
    applyError, syncFailures: syncFailureLines(save.data?.schedule?.sync),
    savedText: savedSummary ? `Saved: ${fmtInt(savedSummary.changed)} ${plural(savedSummary.changed, "table")} changed${savedSummary.reseed ? `, ${fmtInt(savedSummary.reseed)} to load again` : ""}.` : "Saved.",
  });
  // As the Databricks editors do: Save only with something to save, a sign of unsaved changes, and a question before
  // leaving with them (a link in the app, or closing the tab).
  const info = clusterInfo.data;
  const clusterChanged = !!info && !!cluster && JSON.stringify(ownCluster(cluster, info.defaults))
    !== JSON.stringify(info.cluster ? ownCluster({ ...info.defaults, ...info.cluster }, info.defaults) : null);
  const dirty = !!s && (JSON.stringify({ w, identity, differing }) !== loaded || clusterChanged
    || (!!baseline && JSON.stringify({ run, params, name }) !== JSON.stringify(baseline)));
  const confirm = useConfirm();
  const leaving = useRef(false);
  // navigate() takes a path or a history step, not a value that may be either.
  const go = (to: string | number) => { leaving.current = true; if (typeof to === "number") navigate(to); else navigate(to); };
  const leave = (to: string | number) => {
    if (!dirty || leaving.current) return go(to);
    confirm.ask({ title: "Leave without saving?", action: "Leave", danger: true,
                  body: "Your changes to this job's settings are not saved. Changes you confirmed in What it ingests are already applied." })
      .then((ok) => ok && go(to));
  };
  // The browser's Back button too: with unsaved changes a guard entry sits on top of this page's history entry, so Back
  // lands on it and asks; staying puts the guard back, leaving goes back past it. Once the form is clean again (saved,
  // or the edit undone) the guard comes off, so Back always goes back one page.
  useEffect(() => {
    if (!dirty) return;
    window.history.pushState({ unsavedGuard: true }, "", window.location.href);
    const back = () => {
      if (leaving.current) return;
      window.history.pushState({ unsavedGuard: true }, "", window.location.href);
      leave(-2);
    };
    window.addEventListener("popstate", back);
    return () => {
      window.removeEventListener("popstate", back);
      if (!leaving.current && window.history.state?.unsavedGuard) window.history.back();
    };
  }, [dirty]);
  useEffect(() => {
    if (!dirty) return;
    const unload = (e: BeforeUnloadEvent) => { e.preventDefault(); };
    const click = (e: MouseEvent) => {
      const link = (e.target as HTMLElement).closest?.("a[href]") as HTMLAnchorElement | null;
      if (!link || link.target === "_blank" || link.origin !== window.location.origin || e.defaultPrevented) return;
      e.preventDefault(); e.stopPropagation();
      leave(link.pathname + link.search);
    };
    window.addEventListener("beforeunload", unload);
    document.addEventListener("click", click, true);
    return () => { window.removeEventListener("beforeunload", unload); document.removeEventListener("click", click, true); };
  });

  const cadences: number[] = options.data?.cadences ?? CADENCES;
  return (
    <>
      <PageHead crumb={<Crumbs steps={[["Ingestion jobs", "/ingestion-jobs"], [schedule?.name ?? "Ingestion job", `/ingestion-jobs/${id}`], ["Edit"]]} />}
        title={`Edit ${schedule?.name ?? "ingestion job"}`}
        lead={settings.data && <><strong>{fmtInt(settings.data.tables)}</strong> {plural(settings.data.tables, "table")} in{" "}
          <strong>{fmtInt(settings.data.groups.length)}</strong> {plural(settings.data.groups.length, "group")}.</>}
        right={<span className="card-actions">{dirty && <span className="muted">Unsaved changes</span>}
          <button onClick={() => leave(`/ingestion-jobs/${id}`)}>Cancel</button>
          <button className="primary" disabled={saving || !s || !!locked || !dirty} title={locked ?? (!dirty ? "Nothing to save yet" : undefined)}
            onClick={() => save.mutate()}>{saving ? "Saving…" : "Save"}</button></span>} />
      {confirm.dialog}
      {locked && <Notice tone="info">You can see this job's settings but not change them: {locked}.</Notice>}
      <ErrorBox error={settings.error || (isUnconfirmed(save.error) ? null : save.error)} />
      <StatusLine line={saveLine} />

      {schedule && <Section title="Name">
        <div className="form-grid"><label><span>Job name<Required /></span><input value={name} onChange={(e) => setName(e.target.value)} /></label></div>
      </Section>}

      <Section title={<>What it ingests<Info label="About what it ingests">Open a server to see its databases, and a database to see
        its tables. A table's load type is the same in every database: change it and it changes in all of them.</Info></>}
        actions={locked ? <button className="ghost" disabled title={locked}>Add tables <Chevron open /></button> : <Menu label={<>Add tables <Chevron open /></>} buttonClass="ghost" title="Add tables">
          <button onClick={addTables.discover}>Discover on a source</button>
          <button onClick={addTables.open}>Import a CSV of tables</button>
          <button onClick={() => setAddOpen(true)}>Pick databases by hand</button>
        </Menu>}>
        <p className="impact">Changes here apply as soon as you confirm them; Save does not cover them.</p>
        {addTables.dialog}{addTables.result}
        {addOpen && <div className="toolbar"><b>Add databases</b><button className="ghost" onClick={() => setAddOpen(false)}>Close</button></div>}
        {addOpen && <AddDatabases jobId={id} servers={(servers.data?.items ?? []).map((x: Row) => x.server)} />}
        <Tree jobId={id} locked={locked} />
      </Section>

      {s && <Section title="How it writes">
        <WritesFields w={w} setW={setW} stored={s}
          reseed={<ParameterField name="reseed_on_new_columns" params={params} setParams={setParams} options={options.data} />}
          modeNote={writeModeMoves && <p className={targetsChanged ? "help" : "impact"}>A new write mode makes new target tables and loads every
            table again{targetsChanged ? "." : ": change the target names below too."}</p>}
          targetsNote={targetsChanged && <p className="impact">New target names make new tables and load their tables again. The old tables are left in place.</p>} />
      </Section>}

      {s && <Section title={<>Identity columns<Info label="About identity columns">Written on every row next to __client_id__, so the
        shared tables say whose rows they are.</Info></>}>
        <IdentityFields w={w} setW={setW} identity={identity} setIdentity={setIdentity} differing={differing}
          setDiffering={setDiffering} stored={s} tables={settings.data?.tables} />
      </Section>}

      {schedule && <Section title="When it runs">
        <RunFields run={run} setRun={setRun} cadences={cadences} />
        <div className="form-grid">
          <label><span>Add new databases and tables<Info label="About adding new databases and tables">Once a day the servers are
            read again and the job adds what its selection allows: every new database matching a whole server's filter, with all its
            tables, and the new tables of a whole database. Specific tables add nothing. Tables the source no longer has are kept and
            listed on the job page.</Info></span>
            <select value={run.rediscover_hour ?? ""}
              onChange={(e) => setRun({ ...run, rediscover_hour: e.target.value === "" ? null : Number(e.target.value) })}>
              <option value="">Off</option>
              {Array.from({ length: 24 }, (_, hour) => <option key={hour} value={hour}>{`Daily at ${String(hour).padStart(2, "0")}:00 UTC`}</option>)}
            </select></label>
        </div>
      </Section>}

      {/* Compute and the tuning keys are one closed section, at the defaults the job was tested with, as on the board. */}
      {schedule && <details className="card advanced">
        <summary><b>Advanced configuration</b></summary>
        <h3>Compute</h3>
        <ClusterFields info={clusterInfo.data} error={clusterInfo.error} cluster={cluster} setCluster={setCluster} locked={locked} />
        <Compute jobId={id} />
        <Groups jobId={id} locked={locked} saved={Number(schedule.options?.tables_per_group) || null} />
        <h3>Tuning</h3>
        <ParameterFields params={params} setParams={setParams} options={options.data} hostReads={!!settings.data?.reads_by_host} />
      </details>}
    </>
  );
}

const CLUSTER_KEYS = ["driver_node_type_id", "node_type_id", "num_workers"];

/** The cluster each run uses, in a few words for the closed Advanced configuration. */
export function clusterSummary(info: Row | undefined, cluster: Row | null) {
  const c = cluster ?? info?.defaults;
  if (!c) return "the Databricks job's cluster";
  const at = info?.defaults && !ownCluster(c, info.defaults) ? ", the tested defaults" : "";
  return `${c.driver_node_type_id} driver, ${c.node_type_id} workers, ${c.num_workers} ${Number(c.num_workers) === 1 ? "worker" : "workers"}${at}`;
}

/** The cluster to save as the job's own, or null when it is the Databricks job's (so the job runs that job itself). */
export function ownCluster(cluster: Row, defaults: Row): Row | null {
  if (CLUSTER_KEYS.every((k) => String(cluster[k] ?? "") === String(defaults[k] ?? ""))) return null;
  return { driver_node_type_id: cluster.driver_node_type_id || null, node_type_id: cluster.node_type_id || null,
    num_workers: cluster.num_workers === "" || cluster.num_workers == null ? null : Number(cluster.num_workers) };
}

// The job's ingestion cluster, set like a Databricks job cluster. A change gives the job its own copy of the Databricks
// job (made by the cluster copies job after Save), since a run of that job can't change its cluster.
export function ClusterFields({ info, error, cluster, setCluster, locked, after = "Save" }: { info?: Row; error: unknown;
                        cluster: Row | null; setCluster: (c: Row) => void; locked: Locked; after?: string }) {
  if (!info || !cluster) return <ErrorBox error={error} />;
  const d = info.defaults ?? {};
  const off = !!locked || !info.configured;
  const types: Row[] = info.node_types ?? [];
  // As the job cluster form's Driver type and Worker type: the workspace's node types with their memory and cores,
  // the Databricks job's own first and marked as the default.
  const typeSelect = (key: "driver_node_type_id" | "node_type_id") => {
    const value = String(cluster[key] ?? "");
    const label = (t: Row) => `${t.id}${t.id === d[key] ? " (default)" : ""} · ${t.memory_gb} GB, ${t.cores} cores`;
    const ordered = [...types.filter((t) => t.id === d[key]), ...types.filter((t) => t.id !== d[key])];
    return (
      <select value={value} disabled={off} onChange={(e) => setCluster({ ...cluster, [key]: e.target.value })}>
        {value && !types.some((t) => t.id === value) && <option value={value}>{value}</option>}
        {ordered.map((t) => <option key={t.id} value={t.id}>{label(t)}</option>)}
      </select>
    );
  };
  return (
    <div className="compute">
      <h3>Job cluster<Info label="About the job cluster">The driver and worker types and the number of workers each run uses. The
        defaults are the Databricks job's own: {d.driver_node_type_id} driver, {d.node_type_id} workers, {d.num_workers}{" "}
        {Number(d.num_workers) === 1 ? "worker" : "workers"}. A change gives this job its own copy of the Databricks job, made a
        minute or so after {after}.</Info></h3>
      <div className="form-grid">
        <label>Driver type{typeSelect("driver_node_type_id")}</label>
        <label>Worker type{typeSelect("node_type_id")}</label>
        <label><span>Workers<Info label="About workers">0 to {info.max_workers}; 0 runs on the driver alone.</Info></span><input type="number" min={0} max={info.max_workers} value={cluster.num_workers ?? ""} disabled={off}
          onChange={(e) => setCluster({ ...cluster, num_workers: e.target.value })} /></label>
      </div>
      <div className="toolbar">
        <span className="muted">{info.applying ? <>Applying: the cluster copies job is making this job's copy (<a href={info.applying.url}
          target="_blank" rel="noreferrer">run {info.applying.run_id}</a>).</>
          : info.copy_job_id ? <>Runs its own copy of the Databricks job. <OutLink href={info.copy_url}>Open in Databricks</OutLink></>
          : info.cluster ? "Saved; its copy of the Databricks job is not made yet." : "Runs the Databricks job's own cluster."}</span>
      </div>
      {!info.configured && <p className="muted small">This app has no cluster copies job, so the cluster can't be changed here.</p>}
      {info.error && <Notice tone="warn">{info.error}.</Notice>}
    </div>
  );
}

// What the job's runs execute on, read live from the Jobs API.
// The job cluster's own settings, shown once: in Job cluster above, not again in the bundle's table.
const CLUSTER_FIELDS = ["Driver node type", "Worker node type", "Workers"];

/** The job's tables per group: set a new size, preview what moves, then change it. Each server's databases are spread again, as few moving as can be and none split; it waits for any run
 * or write of the job to end. */
function Groups({ jobId, locked, saved }: { jobId: string; locked: Locked; saved?: number | null }) {
  const qc = useQueryClient();
  const [preview, setPreview] = useState<Row | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [size, setSize] = useState<number | null>(null);
  const ask = (dry_run: boolean) => api.post<Row>(`/ingestion-jobs/${jobId}/groups`,
    { ...(size ? { tables_per_group: size } : {}), dry_run, ...(dry_run ? {} : { request_id: newRequestId() }) });
  const look = async () => {
    setError(null);
    try { setPreview(await ask(true)); } catch (e) { setError(e); }
  };
  const change = async () => {
    setError(null);
    try {
      await ask(false);
      toast("Groups changed");
      setPreview(null);
      await qc.invalidateQueries();
    } catch (e) { setError(e); }
  };
  const summary: Row | undefined = preview?.summary;
  return (
    <div className="compute">
      <h3>Groups<Info label="About changing the groups">Changing the size spreads each server's databases again over its new
        number of groups, moving as few databases as it can and never splitting one. Each table keeps its progress, so nothing
        reloads.</Info></h3>
      <div className="form-grid">
        <TablesPerGroup value={size} saved={saved} onChange={(next) => { setSize(next); setPreview(null); }} />
      </div>
      <button className="ghost" onClick={look} disabled={!!locked} title={locked ?? undefined}>Preview the groups</button>
      <ErrorBox error={error} />
      {summary && (
        <>
          <table className="data dense">
            <thead><tr><th>Server</th><th className="num">Databases · tables</th><th className="num">Groups now</th>
              <th className="num">Groups after</th><th className="num">Databases that move</th></tr></thead>
            <tbody>{(summary.servers ?? []).map((line: Row) => (
              <tr key={line.server}><td><b>{line.server}</b></td><td className="num">{fmtInt(line.databases)} · {fmtInt(line.tables)}</td>
                <td className="num">{fmtInt(line.groups_before)}</td><td className="num">{fmtInt(line.groups_after)}</td>
                <td className="num">{line.groups_after === line.groups_before ? "none" : `${fmtInt(line.databases_moving)} (${fmtInt(line.tables_moving)} ${plural(line.tables_moving, "table")})`}</td></tr>
            ))}</tbody>
          </table>
          {summary.unchanged ? <p className="muted">Nothing changes: every server already has that many groups.</p>
            : preview?.blocked ? <Notice tone="warn">Not now: {preview.blocked}.</Notice>
            : <ConfirmButton label="Change the groups" title="Change the job's groups?" onConfirm={change} disabled={!!locked}
                message={<>{fmtInt(summary.databases_moving)} {plural(summary.databases_moving, "database")} ({fmtInt(summary.tables_moving)} {plural(summary.tables_moving, "table")}) move
                  {summary.new_groups?.length ? `; new groups: ${summary.new_groups.join(", ")}` : ""}
                  {summary.emptied_groups?.length ? `; groups removed: ${summary.emptied_groups.join(", ")}` : ""}.
</>} />}
        </>
      )}
    </div>
  );
}

// The size the app splits a server's databases by unless the job sets its own (the backend's default).
export const DEFAULT_TABLES_PER_GROUP = 10_000;
const MIN_TABLES_PER_GROUP = 100, MAX_TABLES_PER_GROUP = 1_000_000;

/** Tables per group, as New and Edit's Groups set it: the box shows the job's size (saved, else the default) until
 * changed; a value left at it is sent as nothing (null), so the job keeps its size. Reset goes back to the default. */
export function TablesPerGroup({ value, onChange, saved }: { value: number | null; onChange: (next: number | null) => void;
                                                              saved?: number | null }) {
  const baseline = saved || DEFAULT_TABLES_PER_GROUP;
  const [text, setText] = useState(String(value ?? baseline));
  const commit = (raw: string) => {
    const n = Math.round(Number(raw.replace(/[, ]/g, "")));
    const clamped = Number.isFinite(n) && raw.trim() ? Math.min(MAX_TABLES_PER_GROUP, Math.max(MIN_TABLES_PER_GROUP, n)) : baseline;
    setText(String(clamped));
    onChange(clamped === baseline ? null : clamped);
  };
  return (
    <label><span>Tables per group<Info label="About tables per group">Each server's databases are split into groups of about this many
      tables, each database whole in one group and spread evenly; a group runs on its own cluster.</Info>
      {Number(text) !== DEFAULT_TABLES_PER_GROUP && <button className="ghost link reset"
        onClick={() => commit(String(DEFAULT_TABLES_PER_GROUP))}>Reset</button>}</span>
      <input type="number" min={MIN_TABLES_PER_GROUP} max={MAX_TABLES_PER_GROUP} step={100} value={text}
        onChange={(e) => setText(e.target.value)} onBlur={(e) => commit(e.target.value)} /></label>
  );
}

function Compute({ jobId }: { jobId: string }) {
  const compute = useQuery({ queryKey: ["job-compute", jobId], queryFn: () => api.get<Row>(`/ingestion-jobs/${jobId}/compute`) });
  return (
    <div className="compute">
      <h3>Everything else its runs use</h3>
      <ErrorBox error={compute.error} />
      {(compute.data?.jobs ?? []).map((job: Row) => (
        <Fragment key={job.role}>
          <h4>{job.role === "check" ? "The check" : "Each group run"}</h4>
          {job.error && <p className="muted">{job.error}</p>}
          <table className="data">
            <thead><tr><th>Task</th><th>Setting</th><th>Value</th><th>Set by</th></tr></thead>
            <tbody>
              {job.compute.map((task: Row) => task.fields.filter((f: Row) => !(job.role !== "check" && CLUSTER_FIELDS.includes(f.name)))
                .map((f: Row, i: number) => (
                <tr key={`${task.task}-${f.name}`}>
                  <td>{i === 0 ? <><b>{task.task}</b> <span className="muted">{task.kind}</span></> : ""}</td>
                  <td>{f.name}</td><td className="mono">{f.value}</td>
                  <td className="muted">{f.source}</td>
                </tr>
              )))}
            </tbody>
          </table>
        </Fragment>
      ))}
    </div>
  );
}
