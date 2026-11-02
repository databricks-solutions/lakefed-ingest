import { Fragment, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, qs, type ApiError, type Row } from "../api";
import { Refused } from "../permissions";
import { changeInProgress, deleteNotice, rediscoveryBusy, rediscoveryButton, rediscoveryNotice, runButton, runTimeoutNotice,
  type RediscoveryProgress } from "../pending";
import { Tree } from "./JobEdit";
import { TargetTables } from "./TargetTables";
import { FailingTables } from "./FailingTables";
import { RunningTasks } from "./tasks";
import { Busy, Info, Spinner, OutLink, Crumbs, Badge, ErrorBox, Menu, Modal, Name, Notice, PageHead, StatusLine, toast, ProgressBar, RunStatus, Section, StateSummary, fmtClock, fmtDuration, fmtInt, fmtShort, fmtSlot, fmtSpeed, fmtTime, jobRunPath, plural, progressFrom, useConfirm, type ConfirmRequest, Twisty } from "../components";

const PAGE = 25;

/** An ingestion run's rows written: "at least" when some loads recorded no count. */
export const rowsText = (r: Row) => (r.rows_partial ? `at least ${fmtInt(r.rows_written)}` : fmtInt(r.rows_written));

/** A trigger that did not run because its job is paused: the Databricks Runs list shows no row for it. */
export const pausedNotRun = (r: Row) => r.outcome?.label === "Not run" && !!r.outcome?.paused;

/** The runs as table rows, each stretch of a paused job's triggers that did not run folded into one quiet line. */
function foldPaused(runs: Row[]): (Row | { folded: Row[] })[] {
  const out: (Row | { folded: Row[] })[] = [];
  for (const r of runs) {
    const last = out[out.length - 1] as { folded?: Row[] } | undefined;
    if (pausedNotRun(r) && last?.folded && last.folded[0].job_id === r.job_id) last.folded.push(r);
    else out.push(pausedNotRun(r) ? { folded: [r] } : r);
  }
  return out;
}

/** One row per ingestion run: outcome, group runs, took, tables loaded, speed, rows written, parallelism, clusters, failed. */
export function JobRunsTable({ runs, showJob }: { runs: Row[]; showJob?: boolean }) {
  const columns = showJob ? 11 : 10;
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="table-wrap"><table className="data dense fit wrap-head">
      <thead><tr><th>Ingestion run</th>{showJob && <th>Ingestion job</th>}<th>Outcome</th><th className="num">Group runs</th>
        <th className="num">Took</th><th className="num">Tables loaded</th><th className="num">Tables/min</th>
        <th className="num">Rows written</th><th className="num">Parallelism</th><th className="num">Clusters</th>
        <th className="num">Failed</th></tr></thead>
      <tbody>
        {foldPaused(runs).map((r) => "folded" in r ? (
          <tr key={`paused-${r.folded[0].job_id}-${r.folded[0].slot}`}>
            <td colSpan={columns} className="muted">Paused: {fmtInt(r.folded.length)} {plural(r.folded.length, "trigger")} did not run,{" "}
              {r.folded.length > 1 ? `${fmtSlot(r.folded[r.folded.length - 1].slot)} to ${fmtSlot(r.folded[0].slot)}` : fmtSlot(r.folded[0].slot)}
              {showJob && <>{" "}(<Link to={`/ingestion-jobs/${r.folded[0].job_id}`}>{r.folded[0].job_name}</Link>)</>}</td>
          </tr>
        ) : (
          <Fragment key={`${r.job_id}-${r.slot}`}><tr>
            <td className="name"><Twisty open={open === `${r.job_id}-${r.slot}`} label="Its group runs"
              onToggle={() => setOpen(open === `${r.job_id}-${r.slot}` ? null : `${r.job_id}-${r.slot}`)} />
              <Link to={jobRunPath(r.job_id, r.slot)}>{new Date(r.slot).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</Link>
              {r.outside && <span className="sub">outside its schedule: Run now or started by hand</span>}</td>
            {showJob && <td className="name"><Link to={`/ingestion-jobs/${r.job_id}`}><Name>{r.job_name}</Name></Link></td>}
            <td><RunStatus status={null} outcome={r.outcome} />{r.outcome?.note && <span className="sub">{r.outcome.note}</span>}</td>
            <td className="num">{fmtInt(r.ran)} of {fmtInt(r.members)}{Number(r.extra) > 0 && <span className="sub">{fmtInt(r.extra)} extra</span>}</td>
            <td className="num">{r.took_s != null ? fmtDuration(r.took_s) : r.running_s != null ? <>{fmtDuration(r.running_s)}<span className="sub">so far</span></> : ""}</td>
            <td className="num">{fmtInt(r.loaded)}</td>
            <td className="num">{r.speed_tables_per_min != null ? fmtSpeed(r.speed_tables_per_min) : ""}</td>
            <td className="num">{rowsText(r)}</td>
            <td className="num">{(r.parallelism ?? []).join(", ")}</td><td className="num">{fmtInt(r.clusters)}</td>
            <td className="num" style={{ color: Number(r.failed_tables) ? "var(--bad)" : undefined }}>{fmtInt(r.failed_tables)}</td>
          </tr>
          {open === `${r.job_id}-${r.slot}` && <tr className="l2"><td colSpan={columns}><RunGroups jobId={r.job_id} slot={r.slot} /></td></tr>}
          </Fragment>
        ))}
        {!runs.length && <tr><td colSpan={columns} className="muted">No ingestion runs in this window.</td></tr>}
      </tbody>
    </table></div>
  );
}

/** An ingestion run's group runs, as a Databricks job run lists its tasks (a for-each task's iterations): the groups its
 * check started this run, each with its outcome, start, time, tables and its run. */
function RunGroups({ jobId, slot }: { jobId: unknown; slot: string }) {
  const run = useQuery({ queryKey: ["job-run", String(jobId), slot],
    queryFn: () => api.get<Row>(`/ingestion-jobs/${encodeURIComponent(String(jobId))}/runs/${encodeURIComponent(slot)}`) });
  if (run.error) return <ErrorBox error={run.error} />;
  if (!run.data) return <span className="muted">Loading its group runs…</span>;
  const lines: Row[] = run.data.group_runs ?? [];
  const groups = lines.filter((g) => !g.check && g.run_id);
  return (
    <div className="run-groups">
      <p className="muted small">{lines.some((g) => g.check) ? `Its check started ${fmtInt(groups.length)} of the job's ${fmtInt(run.data.members)} ${plural(run.data.members, "group")}: those with changes.`
        : `${fmtInt(groups.length)} of the job's ${fmtInt(run.data.members)} ${plural(run.data.members, "group")} ran.`}</p>
      <table className="data dense">
        <thead><tr><th>Ingestion group</th><th>Status</th><th>Started</th><th className="num">Took</th><th>Tables</th><th className="num">Failed</th></tr></thead>
        <tbody>
          {lines.filter((g) => g.check || g.run_id).map((g) => (
            <tr key={`${g.check ? "check-" : ""}${g.task_collection}-${g.run_id ?? g.state}`}>
              <td className="name">{g.check ? <span className="muted">check</span> : <Link to={`/collections/${encodeURIComponent(g.task_collection)}`}>{g.task_collection}</Link>}</td>
              <td>{g.run_id ? <Link to={`/runs/${g.run_id}`}><RunStatus status={null} outcome={g.status} /></Link> : <RunStatus status={null} outcome={g.status} />}</td>
              <td>{g.started_at ? fmtClock(g.started_at) : ""}</td>
              <td className="num">{g.duration_s != null ? fmtDuration(g.duration_s) : ""}</td>
              <td style={{ minWidth: 140 }}>{g.check ? "" : <ProgressBar p={progressFrom(g.progress)} />}</td>
              <td className="num" style={{ color: Number(g.failed) ? "var(--bad)" : undefined }}>{g.check ? "" : fmtInt(g.failed)}</td>
            </tr>
          ))}
          {!lines.some((g) => g.check || g.run_id) && <tr><td colSpan={6} className="muted">No group run started in this ingestion run.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

/** The ingestion run going now, read from its Databricks runs every few seconds, in the order it runs, as a Databricks job
 * run's page lists its tasks: the job's run as the heading, then step 1 the check and what it found, then step 2 the
 * group runs the check starts (a job without the check has only the group runs). Shown only while something is going. */
function RunningNow({ live }: { live: Row }) {
  const check: Row | null = live.check;
  const found: Row | null = live.found;
  const runs: Row[] = live.group_runs ?? [];
  const going = runs.filter((g) => g.active).length;
  const checking = check && (check.status?.label === "Running" || check.status?.label === "Starting");
  return (
    <Section title="Running now" note={`Updated ${fmtClock(live.read_at)}`}>
      {(live.wrappers ?? []).map((w: Row) => (
        <div className="run-head" key={`w-${w.run_id}`}>
          <b>The job's run</b><RunStatus status={null} outcome={w.status} /><span className="muted">started {fmtClock(w.started)}</span>
          {w.url && <OutLink href={w.url}>Open run</OutLink>}
        </div>
      ))}
      <ol className="run-steps">
        {check && (
          <li>
            <h3><span className="step-number">1</span>Check<Info label="About the check">Looks at each database for changes, then starts
              a group run for each group with work.</Info></h3>
            <div className="table-wrap"><table className="data dense">
              <thead><tr><th>Status</th><th>Started</th><th>What it found</th><th></th></tr></thead>
              <tbody><tr>
                <td><RunStatus status={null} outcome={check.status} /></td><td>{fmtClock(check.started)}</td>
                <td>{found && !checking
                  ? <>{fmtInt(found.databases_with_work)} {plural(found.databases_with_work, "database")} changed
                    {found.unchanged_databases != null && <>, {fmtInt(found.unchanged_databases)} unchanged and stamped</>};{" "}
                    started {fmtInt(found.started)} of {fmtInt(found.groups)} {plural(found.groups, "group")}</>
                  : <Busy label="Checking the databases" />}</td>
                <td>{check.url && <OutLink href={check.url}>Open run</OutLink>}</td>
              </tr></tbody>
            </table></div>
          </li>
        )}
        <li>
          <h3>{check && <span className="step-number">2</span>}Group runs: {fmtInt(runs.length)}{runs.length ? `, ${fmtInt(going)} going` : ""}</h3>
          {check && !runs.length
            ? <p className="muted small">{checking || !found ? "Start when the check ends." : "No group had changes."}</p>
            : <div className="table-wrap"><table className="data dense">
              <thead><tr><th>Ingestion group</th><th>Status</th><th>Started</th><th>Progress</th><th></th></tr></thead>
              <tbody>
                {runs.map((g) => {
                  const p = g.progress;
                  return (
                    <tr key={g.run_id}>
                      <td className="name"><Link to={`/collections/${encodeURIComponent(g.task_collection)}`}>{g.task_collection}</Link></td>
                      <td><RunStatus status={null} outcome={g.status} />{g.status?.note && <span className="sub">{g.status.note}</span>}</td>
                      <td>{fmtClock(g.started)}</td>
                      <td style={{ minWidth: 220 }}>{p ? <ProgressBar p={progressFrom({ total: p.planned, loaded: p.done, failed: p.failed })} />
                        : g.active ? <Spinner label="Starting" /> : null}</td>
                      <td>{g.url && <OutLink href={g.url}>Open run</OutLink>}</td>
                    </tr>
                  );
                })}
                {!runs.length && <tr><td colSpan={5} className="muted">No group run yet.</td></tr>}
              </tbody>
            </table></div>}
        </li>
      </ol>
    </Section>
  );
}

export default function IngestionJob() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const [pages, setPages] = useState<string[]>([]);
  const before = pages[pages.length - 1];
  const qc = useQueryClient();
  const confirm = useConfirm();
  // Run now and Cancel run wait on the server's run phase (the poller's state of the job's Databricks runs), not on a
  // timer: "Starting…" until Databricks reports a run going, "Cancelling…" until it reports every run ended.
  const refresh = () => ["ingestion-job", "ingestion-job-live", "ingestion-job-runs"].forEach((k) => qc.invalidateQueries({ queryKey: [k, id] }));
  const runNow = useMutation({
    mutationFn: () => api.post<Row>(`/ingestion-jobs/${encodeURIComponent(id)}/run`, {}),
    onSuccess: () => { toast("Run started"); refresh(); },
  });
  const stop = useMutation({
    mutationFn: () => api.post<Row>(`/ingestion-jobs/${encodeURIComponent(id)}/stop`, {}),
    onSuccess: (r) => { if (!r.errors?.length) toast("Cancelling the run"); refresh(); },
  });
  const [deleting, setDeleting] = useState(false);
  const remove = useMutation({
    mutationFn: (dropTargets: boolean) => api.post<Row>(`/schedules/${encodeURIComponent(id)}/delete`, { drop_targets: dropTargets }),
    onSuccess: () => { toast("Deleting the job"); qc.invalidateQueries(); },
  });
  // A deleted job: Restore brings it back paused, and Delete now runs its purge at once.
  const restore = useMutation({
    mutationFn: () => api.post<Row>(`/schedules/${encodeURIComponent(id)}/restore`),
    onSuccess: () => { toast("Restored paused"); qc.invalidateQueries(); },
  });
  const purge = useMutation({
    mutationFn: () => api.post<Row>(`/schedules/${encodeURIComponent(id)}/purge`),
    onSuccess: () => { qc.invalidateQueries(); },
  });
  // Run it now for the daily re-discovery: its request, then the server's progress (the discovery run, then the tables).
  const rediscover = useMutation({
    mutationFn: () => api.post<Row>(`/ingestion-jobs/${encodeURIComponent(id)}/rediscover`, {}),
    onSuccess: () => refresh(),
  });
  const job = useQuery({ queryKey: ["ingestion-job", id], queryFn: () => api.get<Row>(`/ingestion-jobs/${encodeURIComponent(id)}`),
                         refetchInterval: (q) => {
                           const row = q.state.data as Row | undefined;
                           if (changeInProgress(row?.run_now, runNow.isPending || stop.isPending)) return 3_000;
                           return rediscoveryBusy(row?.rediscovery?.progress, rediscover.isPending) ? 5_000 : 15_000;
                         } });
  const busy = changeInProgress(job.data?.run_now, runNow.isPending || stop.isPending);
  const live = useQuery({ queryKey: ["ingestion-job-live", id], queryFn: () => api.get<Row>(`/ingestion-jobs/${encodeURIComponent(id)}/live`),
                          refetchInterval: (q) => (busy ? 3_000 : (q.state.data as Row | undefined)?.active ? 5_000 : 15_000) });
  const refusal = (runNow.error as ApiError | null)?.detail;
  const runs = useQuery({
    queryKey: ["ingestion-job-runs", id, before],
    queryFn: () => api.get<Row>(`/ingestion-jobs/${encodeURIComponent(id)}/runs${qs({ before, limit: PAGE })}`),
    refetchInterval: before ? false : 15_000,
  });
  const j = job.data;
  const last = j?.last_job_run;
  const button = runButton(j?.run_now, { starting: runNow.isPending, cancelling: stop.isPending });
  const timeout = runTimeoutNotice(j?.run_now);
  const deletion = deleteNotice(j?.delete, fmtShort(j?.delete?.expires_at));
  return (
    <>
      <PageHead crumb={<Crumbs steps={[["Ingestion jobs", "/ingestion-jobs"]]} />} tab={j?.name}
        title={j ? <>{j.name} {j.paused && <Badge value="Paused" />}</> : "Ingestion job"}
        lead={!j ? (job.error ? undefined : <span className="muted">Loading…</span>) : <>{j.description}{j.stagger_to ? `, its groups starting up to ${j.stagger_to} minutes after that` : ""};{" "}
          <strong>{fmtInt(j.groups?.length)}</strong> {plural(j.groups?.length, "ingestion group")}, <strong>{fmtInt(j.databases)}</strong>{" "}
          {plural(j.databases, "database")}, <strong>{fmtInt(j.enabled_tables)}</strong> {plural(j.enabled_tables, "table")} on{" "}
          <strong>{fmtInt(j.servers)}</strong> {plural(j.servers, "server")}
          {j.parameters?.parallelism && <>, parallelism <strong>{j.parameters.parallelism}</strong></>}
          {j.job_variant === "check" ? <>; each run checks its sources for changes first</>
            : j.job_variant && j.job_variant !== "default" ? <>, Databricks job variant <strong>{j.job_variant}</strong></> : null}
.</>}
        right={j && <span className="card-actions">
          {/* As a Databricks job page: Delete in the more-actions menu, Edit beside Run now, Run now the primary action,
              which becomes Cancel run while a run of the job is going. */}
          {j.delete ? <>
              <button disabled={!!j.delete_refusal || restore.isPending} title={j.delete_refusal ?? undefined}
                onClick={() => restore.mutate()}>{restore.isPending ? "Restoring…" : "Restore"}</button>
              <button className="danger" disabled={!!j.delete_refusal || purge.isPending} title={j.delete_refusal ?? undefined}
                onClick={() => confirm.ask(deleteNowRequest(j.name)).then((ok) => ok && purge.mutate())}>
                {purge.isPending ? "Starting the delete…" : "Delete now"}</button>
            </>
            : <>
                <Menu label="⋮" title="More actions">
                  <button className="danger-text" disabled={!!j.delete_blocked || remove.isPending} title={j.delete_blocked ?? undefined}
                    onClick={() => setDeleting(true)}>{remove.isPending ? "Deleting…" : "Delete job"}</button>
                </Menu>
                <button disabled={!!j.change_blocked} title={j.change_blocked ?? undefined}
                  onClick={() => navigate(`/ingestion-jobs/${id}/edit`)}>Edit</button>
                <button className={button.tone} disabled={button.disabled} title={button.title ?? undefined}
                  onClick={() => (button.action === "cancel"
                    ? confirm.ask(cancelRunRequest(j, live.data)).then((ok) => ok && stop.mutate())
                    : button.action === "run" && confirm.ask(runNowRequest(j)).then((ok) => ok && runNow.mutate()))}>{button.label}</button>
              </>}</span>} />
      {j && <p className="muted small">Created by {j.created_by ?? "unknown"}{j.created_at ? `, ${fmtTime(j.created_at)}` : ""} ·
        Last updated by {j.updated_by ?? "unknown"}{j.updated_at ? `, ${fmtTime(j.updated_at)}` : ""}</p>}
      {confirm.dialog}
      <RunningTasks jobId={id} />
      {deleting && j && <DeleteJobDialog job={j} onClose={(drop) => { setDeleting(false); if (drop !== null) remove.mutate(drop); }} />}
      {deletion && (j?.delete?.state === "deleting" ? <Busy label={deletion.text} />
        : <Notice tone={deletion.tone}>{j?.deleted_at && `Deleted on ${fmtShort(j.deleted_at)}. `}{deletion.text}
          {j?.delete?.run?.url && <> <OutLink href={j.delete.run.url}>Open in Databricks</OutLink>.</>}</Notice>)}
      {timeout && <Notice tone="warn">{timeout}</Notice>}
      {restore.data?.cluster_error && <Notice tone="warn">Its cluster was not set back: {restore.data.cluster_error}</Notice>}
      {purge.data?.delete_run?.error && <Notice tone="warn">The delete job did not start: {purge.data.delete_run.error}</Notice>}
      <ErrorBox error={restore.error} />
      <ErrorBox error={purge.error} />
      <Refused action="Run now" refusal={j?.run_now?.blocked} />
      <Refused action="Edit and Delete" refusal={j?.change_blocked} />
      <ErrorBox error={remove.error} />
      {refusal?.run_url ? <Notice tone="warn">Not started: a run of this job is already going (<OutLink href={refusal.run_url}>open it</OutLink>),
        and Databricks would skip a second one. Running now below follows it; cancel it, or Run now is open again when it ends.</Notice>
        : <ErrorBox error={runNow.error} />}
      <ErrorBox error={stop.error} />
      {stop.isSuccess && stop.data.errors?.length > 0 && <Notice tone="warn">Not cancelled: {stop.data.errors.join("; ")}.</Notice>}
      <ErrorBox error={job.error} />
      {j && (
        <div className="metrics">
          <div className="metric"><div className="metric-label">Tables now <Info>Loaded counts late tables too. A database is late when one of its tables is past its freshness target.</Info></div>
            <StateSummary row={j} looksGood lateDatabases /></div>
          <div className="metric"><div className="metric-label">Last ingestion run</div>
            <div className="metric-value small-value">{last ? <Link to={jobRunPath(j.id, last.slot)}><RunStatus status={null} outcome={last.outcome} /></Link> : "Never run"}</div>
            <div className="metric-sub">{last ? `${fmtSlot(last.slot)}${last.took_s != null ? ` · took ${fmtDuration(last.took_s)}` : ""}` : ""}</div></div>
          <div className="metric"><div className="metric-label">Next trigger {j.paused ? <Info>No trigger fires until it is resumed.</Info>
              : j.next_start && <Info>Its first group starts then.</Info>}</div>
            <div className={`metric-value small-value${j.paused || j.next_start ? "" : " muted"}`}>
              {j.paused ? "Paused" : j.next_start ? fmtSlot(j.next_start) : "Not scheduled"}</div></div>
          <div className="metric"><div className="metric-label">Speed {j.median_speed_tables_per_min != null
              ? <Info>{`Tables a minute: the median of the last ${fmtInt(j.speed_job_runs)} ingestion ${plural(j.speed_job_runs, "run")} that loaded any table, from the last 7 days.`}</Info>
              : <Info>No ingestion run loaded tables in 7 days.</Info>}</div>
            {j.median_speed_tables_per_min != null ? <>
              <div className="metric-value">{fmtSpeed(j.median_speed_tables_per_min)}<small>tables/min</small></div>
              <div className="metric-sub">Median of the last {fmtInt(j.speed_job_runs)} {plural(j.speed_job_runs, "run")}</div></>
              : <div className="metric-value small-value muted">Not yet</div>}</div>
          <div className="metric"><div className="metric-label">Ingestion run time {j.p95_took_s != null
              ? <Info>95% of ingestion runs took this long or less, over the last 7 days.</Info>
              : <Info>Shows once an ingestion run has finished.</Info>}</div>
            {j.p95_took_s != null ? <div className="metric-value">{fmtDuration(j.p95_took_s)}</div>
              : <div className="metric-value small-value muted">Not yet</div>}</div>
        </div>
      )}
      {live.data?.active && <RunningNow live={live.data} />}
      {j && <RediscoveryRecord rediscovery={j.rediscovery} requesting={rediscover.isPending} error={rediscover.error}
        refused={j.change_blocked} onRunNow={() => rediscover.mutate()} />}
      {j && <FailingTables jobId={id} runRefused={j.run_now?.refused} changeRefused={j.change_blocked}
        removeRefused={j.delete_blocked} />}
      <Section title="Ingestion runs" note="Newest first; one row per trigger. Open a run for the groups it ran."
        actions={<span className="pager-buttons">
          <button className="ghost" disabled={!pages.length} onClick={() => setPages(pages.slice(0, -1))}>Newer</button>
          <button className="ghost" disabled={!runs.data?.next_before} onClick={() => setPages([...pages, runs.data!.next_before])}>Older</button>
        </span>}>
        <ErrorBox error={runs.error} />
        {runs.data && <JobRunsTable runs={runs.data.job_runs} />}
      </Section>
      {j && <Section title={`What it ingests: ${fmtInt(j.enabled_tables)} ${plural(j.enabled_tables, "table")}`}
        note={<>From each server, its databases and their tables, for information. Edit changes them. Split into{" "}
          <b>{fmtInt(j.groups?.length)}</b> {plural(j.groups?.length, "ingestion group")}, which run side by side; each ingestion run lists the groups it started.</>}>
        <Tree jobId={id} locked={null} readOnly />
      </Section>}
      {j && <TargetTables jobId={id} />}
    </>
  );
}

const REDISCOVERY_STATUS: Record<string, string> = { running: "Running", done: "Done", deferred: "Deferred", failed: "Failed" };

/** The job's daily look for new databases and tables: whether it is on (and when), what the last one is doing or did, and
 * Run it now, which stays off until the re-discovery has ended. */
function RediscoveryRecord({ rediscovery, requesting, error, refused, onRunNow }: { rediscovery?: Row; requesting: boolean;
  error: unknown; refused?: string | null; onRunNow: () => void }) {
  const last = rediscovery?.last as Row | null | undefined;
  const progress = rediscovery?.progress as RediscoveryProgress | null | undefined;
  const hour = rediscovery?.hour;
  if (hour == null && !last) return null;
  const note = hour == null ? "Off. Turn it on from Edit, under When it runs."
    : `Daily at ${String(hour).padStart(2, "0")}:00 UTC, set from Edit under When it runs.`;
  const button = rediscoveryButton(progress, requesting, refused);
  const line = rediscoveryNotice(progress, requesting, error);
  return (
    <Section title="Adding new databases and tables" note={note}
      actions={<button disabled={button.disabled} title={button.title ?? undefined} onClick={onRunNow}>{button.label}</button>}>
      <RediscoveryLine line={line} busy={rediscoveryBusy(progress, requesting)} />
      {last ? <RediscoveryLast last={last} /> : <p className="muted">It has not run yet.</p>}
    </Section>
  );
}

/** The re-discovery's line: going, a plain line with a spinner; ended while the page watched, a toast; failed, a notice. */
function RediscoveryLine({ line, busy }: { line: { tone: "info" | "ok" | "warn" | "bad"; text: string } | null; busy: boolean }) {
  const watched = useRef(false);
  if (busy) watched.current = true;
  return <StatusLine line={line?.tone === "ok" && !watched.current ? null : line} />;
}

function RediscoveryLast({ last }: { last: Row }) {
  const vanished = last.vanished as Row | undefined;
  const vanishedTables: string[] = vanished?.tables ?? [];
  const errors: string[] = last.errors ?? [];
  const skipped: Row[] = last.skipped_tables ?? [];
  return (
    <div>
      <p>
        <b>{REDISCOVERY_STATUS[last.status] ?? String(last.status)}</b>
        {last.started_at && <>, started {fmtTime(last.started_at)}</>}
        {last.finished_at && <>, finished {fmtTime(last.finished_at)}</>}
      </p>
      {last.status === "deferred" && <p className="muted small">A group of the job was running, so nothing was added. The next
        attempt is the next day's.</p>}
      {last.status === "done" && !last.note && <ul>
        <li>Found <b>{fmtInt(last.found)}</b> {plural(last.found, "table")} to add: added <b>{fmtInt(last.added)}</b>, skipped{" "}
          <b>{fmtInt(last.skipped)}</b> <span className="muted small">(their target was already written by another table)</span></li>
        <li>New databases: {last.new_databases?.length ? last.new_databases.join(", ") : "none"}</li>
        <li>Vanished from the source and kept: <b>{fmtInt(vanished?.count)}</b>
          {vanishedTables.length ? <>: {vanishedTables.join(", ")}{vanishedTables.length < Number(vanished?.count) ? ", and more" : ""}</> : null}</li>
        {errors.length > 0 && <li>Could not be read: {errors.join("; ")}</li>}
        {skipped.length > 0 && <li>Not added: {skipped.map((s) => `${s.table} (${(s.errors ?? []).join(", ")})`).join("; ")}</li>}
      </ul>}
    </div>
  );
}

// What Run now starts, for its dialog: one run of every group, each on its own cluster, which costs money.
function runNowRequest(j: Row): ConfirmRequest {
  const groups = Number(j.run_now?.groups ?? 0);
  const starts = j.run_now?.check
    ? `the check job over its ${fmtInt(groups)} ${plural(groups, "group")}, which starts an ingest run, each on its own job cluster, for every group with changes (up to ${fmtInt(groups)} ${plural(groups, "cluster")})`
    : `an ingest run for each of its ${fmtInt(groups)} ${plural(groups, "group")}, each on its own job cluster (${fmtInt(groups)} ${plural(groups, "cluster")})`;
  return { title: `Run ${j.name} now?`, action: "Run now",
    body: <><p>This starts {starts}, over its {fmtInt(j.enabled_tables)} {plural(j.enabled_tables, "table")}.</p>
      <p>Clusters and the sources' load cost money while they run.</p></> };
}

/** Delete's dialog: what goes (the job, its Databricks jobs, its groups' tables and their state), what stays (run
 * history and the change log, and any group another job also runs), and the tick box that also drops the target
 * tables only this job writes. Read from the delete preview, so the counts are those the delete job acts on. */
function DeleteJobDialog({ job, onClose }: { job: Row; onClose: (dropTargets: boolean | null) => void }) {
  const [drop, setDrop] = useState(false);
  const preview = useQuery({ queryKey: ["delete-preview", job.id],
    queryFn: () => api.get<Row>(`/ingestion-jobs/${encodeURIComponent(job.id)}/delete-preview`) });
  const p = preview.data;
  const disabled = preview.error ? "Can't read what Delete removes" : !p ? "Reading what Delete removes…" : p.blocked ?? undefined;
  return (
    <Modal title={`Delete ${job.name}?`} action="Delete" danger actionDisabled={disabled} onClose={(ok) => onClose(ok ? drop : null)}>
      <ErrorBox error={preview.error} />
      {!p && !preview.error && <p className="muted">Reading what Delete removes…</p>}
      {p && <>
        {p.blocked && <Notice tone="warn">{p.blocked}</Notice>}
        {p.retention_days != null && <p>The job is kept for {fmtInt(p.retention_days)} {plural(p.retention_days, "day")}, then deleted
          for good. Restore brings it back with its state, so nothing reloads.</p>}
        {deleteJobText(p).map((line) => <p key={line}>{line}</p>)}
        {Number(p.targets) + Number(p.kept) > 0 && <>
          <label className="checkbox"><input type="checkbox" checked={drop} onChange={(e) => setDrop(e.target.checked)} />{" "}
            Also delete the target tables</label>
          <p className="muted small">{targetTablesText(p, drop)}</p>
        </>}
      </>}
    </Modal>
  );
}

/** What Delete removes now, what the purge deletes later, and what stays, line by line, from the delete preview. */
export function deleteJobText(p: Row): string[] {
  const groups = Number(p.groups?.length ?? 0), tables = Number(p.tables ?? 0), kept = p.groups_kept ?? [];
  return [
    "Its schedule and its Databricks jobs are removed now, and it stops running.",
    ...(groups ? [`Its ${fmtInt(groups)} ${plural(groups, "group")} and their ${fmtInt(tables)} ${plural(tables, "table")} are deleted for good `
      + "at the purge, with their checkpoints. Run history and the change log stay."] : []),
    ...(kept.length ? [`${fmtInt(kept.length)} ${plural(kept.length, "group")} another job also runs ${kept.length === 1 ? "stays" : "stay"}, `
      + `with ${kept.length === 1 ? "its" : "their"} tables: ${kept.join(", ")}.`] : []),
  ];
}

/** The tick box's note: how many target tables it drops, and the ones another job also writes, which it keeps. */
export function targetTablesText(p: Row, drop: boolean): string {
  const targets = Number(p.targets ?? 0), kept = Number(p.kept ?? 0), names: string[] = p.kept_names ?? [];
  const more = kept > names.length ? `, and ${fmtInt(kept - names.length)} more` : "";
  const keeps = kept ? ` ${fmtInt(kept)} ${plural(kept, "target table")} another job also writes ${kept === 1 ? "is" : "are"} kept: `
    + `${names.join(", ")}${more}.` : "";
  return (drop
    ? `${fmtInt(targets)} ${plural(targets, "target table")} only this job writes ${targets === 1 ? "is" : "are"} dropped at the purge, `
      + "with their data, after its tables are deleted. This can't be undone; schemas stay."
    : `Unticked, its ${fmtInt(targets)} ${plural(targets, "target table")} ${targets === 1 ? "stays" : "stay"} in Unity Catalog, with `
      + "their data.") + keeps;
}

/** Delete now's confirmation: the purge runs at once, so the job's state goes for good, with any target tables it was set
 * to drop (those no other job writes). */
export function deleteNowRequest(name: string): ConfirmRequest {
  return { title: `Delete ${name} now?`, action: "Delete now", danger: true,
    body: <p>It is deleted for good at once, by the delete job: its state goes, and any target tables it was set to drop that
      no other job writes go with it. It can't be restored after this.</p> };
}

// What Cancel run stops, for its dialog, named from the live view: the job's run, its check and its group runs going.
function cancelRunRequest(j: Row, live: Row | undefined): ConfirmRequest {
  const groups: Row[] = (live?.group_runs ?? []).filter((g: Row) => g.active);
  const parts = [
    ...(live?.wrappers?.length ? [`the job's run (${(live.wrappers as Row[]).map((w) => `run ${w.run_id}`).join(", ")})`] : []),
    ...(["Queued", "Starting", "Running"].includes(live?.check?.status?.label) ? [`its check (run ${live!.check.run_id})`] : []),
    ...(groups.length ? [`${fmtInt(groups.length)} ${plural(groups.length, "group run")} (${groups.map((g) => g.task_collection).join(", ")})`] : []),
  ];
  return { title: `Cancel the run of ${j.name}?`, action: "Cancel run", danger: true,
    body: <><p>This cancels {parts.length ? parts.join(", ") : "the run of this job going now"} in Databricks.</p>
      <p>Tables loading now stop part way. The next run loads them again from their last checkpoint.</p></> };
}
