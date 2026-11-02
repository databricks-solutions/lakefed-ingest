import { type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Row } from "../api";
import { type BarPart, type Tone } from "../bars";
import { CountBar, ErrorBox, FillBox, Info, JobStatus, Name, Notice, PageHead, Pager, RunStatus, Section, StateBar, StateSummary, fmtDuration, fmtInt, fmtShort, fmtSlot, fmtTime, jobRunPath, plural, usePageStamp } from "../components";

interface Overview {
  jobs: Row[]; jobs_total: number; offset: number; limit: number; not_in_job: Row | null;
  running_job_runs: Row[]; running_total: number; running_not_in_job: Row[];
  attention: Row[]; attention_total: number; totals: Row; read_at: string; cache_s: number;
  next_triggers: Row[]; paused_jobs: number; rows_24h: { rows: number; partial: boolean };
}

/** One headline tile. empty replaces the headline and the sub line when a card has nothing to count yet. */
interface Tile {
  label: string; to: string; value: ReactNode; sub?: ReactNode; info?: ReactNode; empty?: string;
}

const jobPath = (id: unknown) => `/ingestion-jobs/${encodeURIComponent(String(id))}`;

/** A job's last ingestion run that ran, however long ago: its outcome, when, failed tables and how long it took. A
 * trigger that did not run is never shown here; the job's own page lists every trigger. */
function LastJobRun({ job }: { job: Row }) {
  const last = job.last_job_run;
  if (!last) return <span className="muted">Never run</span>;
  return (
    <>
      <Link to={jobRunPath(job.id, last.slot)}><RunStatus status={null} outcome={last.outcome} /></Link>
      <span className="sub">{fmtSlot(last.slot)}{last.took_s != null && `, took ${fmtDuration(last.took_s)}`}
        {Number(last.failed_tables) > 0 && `, ${fmtInt(last.failed_tables)} failed`}</span>
    </>
  );
}

/** Every ingestion job, failing first, with its tables by state and its Status; "Not in any job" last. */
/** The Ingestion jobs list's sort: a time column and its direction, or none for the list's own order. */
export interface JobSort { by: "" | "created_at" | "updated_at"; descending: boolean }

export function JobsTable({ jobs, notInJob, total, offset, limit, onPage, link, fill, sort, onSort }: {
  jobs: Row[]; notInJob?: Row | null; total: number; offset: number; limit: number; onPage: (offset: number) => void;
  link?: boolean; fill?: boolean; sort?: JobSort; onSort?: (sort: JobSort) => void }) {
  // As the Databricks Jobs list: who created each job, and its last change, sortable by time.
  const audit = !!onSort;
  const sortHead = (by: JobSort["by"], label: string) => {
    const on = sort?.by === by;
    return <th><button className="th-sort" aria-sort={on ? (sort!.descending ? "descending" : "ascending") : "none"}
      onClick={() => onSort!({ by, descending: on ? !sort!.descending : true })}>{label}{on ? (sort!.descending ? " ↓" : " ↑") : ""}</button></th>;
  };
  // The user name before the @, the full address on hover, so both columns fit as the Jobs list's Created by does.
  const who = (by: unknown, at: unknown) => <>{by ? <span title={String(by)}>{String(by).split("@")[0]}</span> : <span className="muted">unknown</span>}
    {at ? <span className="sub" title={fmtTime(at)}>{fmtShort(at)}</span> : null}</>;
  const table = (
      <div className="table-wrap"><table className={audit ? "data fit" : "data"}>
        <thead><tr><th>Ingestion job</th><th className="num">Groups</th>{!audit && <th className="num">Databases</th>}
          <th className="num">Tables</th><th>State now</th><th className="num">Failing</th><th className="num">Late databases</th>
          <th>Last ingestion run</th><th>Status</th>
          {audit && <>{sortHead("created_at", "Created")}{sortHead("updated_at", "Last updated")}</>}</tr></thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id ?? "none"}>
              <td className={audit ? "name capped" : "name"}>{j.id != null ? <Link to={jobPath(j.id)}><b><Name>{j.name}</Name></b></Link> : <b><Name>{j.name}</Name></b>}
                <span className="sub">{j.id != null ? j.description : `${fmtInt(j.groups?.length)} ${plural(j.groups?.length, "group")}`}</span></td>
              <td className="num">{fmtInt(j.groups?.length)}</td>{!audit && <td className="num">{fmtInt(j.databases)}</td>}
              <td className="num">{fmtInt(j.enabled_tables)}</td><td><StateBar row={j} saysAllDone={false} /></td>
              <td className="num" style={{ color: Number(j.state_failing) ? "var(--bad)" : undefined }}>{fmtInt(j.state_failing)}</td>
              <td className="num">{fmtInt(j.late_databases)}</td>
              <td>{j.id != null ? <LastJobRun job={j} /> : <Link to="/runs?job=none">its group runs</Link>}</td>
              <td><JobStatus status={j.status} /></td>
              {audit && <><td>{who(j.created_by, j.created_at)}</td><td>{who(j.updated_by, j.updated_at)}</td></>}
            </tr>
          ))}
          {notInJob && (
            <tr className="total">
              <td className="name"><Link to="/ingestion-jobs/not-in-job"><b>Not in any job</b></Link><span className="sub">{notInJob.groups.join(", ")}</span></td>
              <td className="num">{fmtInt(notInJob.groups.length)}</td>{!audit && <td className="num">{fmtInt(notInJob.databases)}</td>}
              <td className="num">{fmtInt(notInJob.enabled_tables)}</td><td><StateBar row={notInJob} saysAllDone={false} /></td>
              <td className="num" style={{ color: Number(notInJob.state_failing) ? "var(--bad)" : undefined }}>{fmtInt(notInJob.state_failing)}</td>
              <td className="num">{fmtInt(notInJob.late_databases)}</td>
              <td><Link to="/runs?job=none">its group runs</Link></td><td><span className="muted">run by hand or by the Databricks job</span></td>
              {audit && <><td></td><td></td></>}
            </tr>
          )}
          {!jobs.length && !notInJob && <tr><td colSpan={audit ? 10 : 9} className="muted">No ingestion jobs here.</td></tr>}
        </tbody>
      </table></div>
  );
  const pager = <Pager offset={offset} limit={limit} total={total} noun="ingestion job" onPage={onPage} />;
  // With no job to list, one short row and no pager: no tall empty box.
  if (!jobs.length && !notInJob) return table;
  if (fill) return <><FillBox min={240} reserve={44}>{table}</FillBox>{pager}</>;
  return (
    <>
      {table}
      {pager}
      {link && <p className="muted small"><Link to="/ingestion-jobs">All ingestion jobs</Link>, with filters.</p>}
    </>
  );
}

export function RunningNow({ total, notInJob }: { total: number; notInJob: Row[] }) {
  return (
    <Section title={<>Running now <Info>Ingestion runs going now, and runs of groups that belong to no job.</Info></>}
      actions={<Link to="/runs">All runs</Link>}>
      <div className="metric-value small-value">{total ? <Link to="/runs">{fmtInt(total)} {plural(total, "ingestion run")}</Link>
        : <>0 ingestion runs</>}</div>
      {!total && !notInJob.length && <p className="muted">Nothing is running.</p>}
      {notInJob.length > 0 && <p className="muted small"><Link to="/runs?job=none">{fmtInt(notInJob.length)} {plural(notInJob.length, "group")} in no job</Link>, running now.</p>}
    </Section>
  );
}

export function NeedsAttention({ jobs, total }: { jobs: Row[]; total: number }) {
  const attentionTo = total === 1 ? jobPath(jobs[0].id) : "/ingestion-jobs?filter=attention";
  return (
    <Section title={<>Needs attention <Info>Jobs with tables failing now, or whose last ingestion run ended without finishing.
      Late data is counted on each job instead.</Info></>}>
      <div className="metric-value small-value">{total ? <Link to={attentionTo}>{fmtInt(total)} {plural(total, "job")}</Link>
        : <>0 jobs</>}</div>
      {!total && <p className="muted">No job needs attention.</p>}
    </Section>
  );
}

/** Every enabled table by state, each state linking to the tables in it. */
function Freshness({ totals }: { totals: Row }) {
  return (
    <Section title={<>Freshness <Info>Every enabled table of every job, by its state now. A database is late when one of its
      tables is past its freshness target; its database page lists those tables. Loaded counts late tables too.</Info></>}>
      <StateSummary row={totals} looksGood links={{ loaded: "/tables", waiting: "/tables?status=seeding", failing: "/tables?status=failing" }} />
    </Section>
  );
}

/** The overview lists at most this many triggers, so a full list may have more behind it. */
const SHOWN_TRIGGERS = 5;

function NextScheduled({ triggers, paused }: { triggers: Row[]; paused: number }) {
  const shown = triggers.length;
  const headline = shown >= SHOWN_TRIGGERS ? `${fmtInt(shown)}+` : fmtInt(shown);
  return (
    <Section title={<>Next scheduled{shown > 0 && paused > 0 && <> <Info>{fmtInt(paused)} paused {plural(paused, "job")}
      {paused === 1 ? "is" : "are"} not shown.</Info></>}</>} actions={<Link to="/schedules">Schedules</Link>}>
      <div className="metric-value small-value"><Link to="/schedules">{headline} {plural(shown, "trigger")}</Link></div>
      {!shown && <p className="muted">No trigger scheduled.</p>}
      {!shown && paused > 0 && <p className="muted small">{fmtInt(paused)} {plural(paused, "job")} paused.</p>}
    </Section>
  );
}

/** Each job's last ingestion run that ran, however long ago, newest first, each opening its ingestion run, as the Jobs list
 * shows each job's last run. A trigger that did not run is never shown here; the job's own page lists every trigger. */
/** How many of the newest runs the card lists and counts. */
const SHOWN_RECENT_RUNS = 6;
/** Each run outcome's tone (queries.run_outcome) as its bar segment: succeeded, with failures, failed, running, other. */
const OUTCOME_TONES: Record<string, Tone> = { ok: "loaded", warn: "late", bad: "failing", run: "running" };

function RecentRuns({ jobs }: { jobs: Row[] }) {
  const withRuns = jobs.filter((j) => j.last_job_run);
  const last = withRuns.length;
  const countTone = (tone: Tone) => withRuns.slice(0, SHOWN_RECENT_RUNS)
    .filter((j) => (OUTCOME_TONES[j.last_job_run.outcome?.tone] ?? "notrun") === tone).length;
  const parts: BarPart[] = [
    { count: countTone("loaded"), word: "succeeded", tone: "loaded" },
    { count: countTone("late"), word: "succeeded with failures", tone: "late" },
    { count: countTone("failing"), word: "failed", tone: "failing" },
    { count: countTone("running"), word: "running", tone: "running" },
    { count: countTone("notrun"), word: "skipped", tone: "notrun" },
  ];
  const shown = Math.min(last, SHOWN_RECENT_RUNS);
  const headline = last > shown ? `${fmtInt(shown)}+` : fmtInt(shown);
  return (
    <Section title={<>Recent runs <Info>Each job's last ingestion run.</Info></>} actions={<Link to="/runs">All runs</Link>}>
      {!last && <p className="muted">No ingestion run has finished yet.</p>}
      {last > 0 && <>
        <div className="metric-value small-value"><Link to="/runs">{headline} {plural(shown, "run")}</Link></div>
        <CountBar parts={parts} legend={countTone("late") + countTone("failing") > 0} />
      </>}
    </Section>
  );
}

/** A status summary, as the Databricks home and Jobs pages open: totals, what runs now, what needs a look, how fresh
 * the tables are, what runs next and what ran last, each item linking to its job or run. The jobs themselves are
 * listed on Ingestion jobs. */
/** The info icon on a tile that is itself a link: a click, Enter or Space on the icon shows its text and stays on the page.
 *  The event stops here, before the tile's link can act on it. */
function TileInfo({ children }: { children: ReactNode }) {
  const keepOnPage = (event: { preventDefault: () => void; stopPropagation: () => void }) => {
    event.preventDefault();
    event.stopPropagation();
  };
  return (
    <span onClick={keepOnPage} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") keepOnPage(event); }}>
      <Info>{children}</Info>
    </span>
  );
}

export default function OverviewPage() {
  const ov = useQuery({ queryKey: ["overview"], queryFn: () => api.get<Overview>("/overview"), refetchInterval: 15_000 });
  const d = ov.data;
  const t = d?.totals ?? {};
  const enabled = Number(t.enabled_tables || 0);
  const pct = enabled ? ((100 * Number(t.state_loaded || 0)) / enabled).toFixed(1) : "0.0";
  const jobs = Number(t.jobs || 0);
  const unscheduled = d?.not_in_job?.groups?.length ?? 0;
  const running = Number(t.running_jobs || 0);
  const paused = Number(d?.paused_jobs || 0);
  const idle = Math.max(0, jobs - running - paused);
  const databases = Number(t.databases || 0);
  const lateDatabases = Number(t.late_databases || 0);
  const notLateDatabases = Math.max(0, databases - lateDatabases);
  const tiles: Tile[] = d ? [
    { label: "Ingestion jobs", to: "/ingestion-jobs", value: <>{fmtInt(jobs)}<small>{plural(jobs, "job")}</small></>,
      empty: jobs ? undefined : "No ingestion jobs yet",
      sub: <CountBar parts={[{ count: running, word: "running", tone: "running" }, { count: paused, word: "paused", tone: "notrun" },
        { count: idle, word: "idle", tone: "unchanged" }]} />,
      info: unscheduled ? <>{fmtInt(unscheduled)} {plural(unscheduled, "group")} {unscheduled === 1 ? "runs" : "run"} in no job.
        The Ingestion jobs page lists {unscheduled === 1 ? "it" : "them"}.</> : undefined },
    { label: "Databases", to: "/sources", value: <>{fmtInt(databases)}<small>{plural(databases, "database")}</small></>,
      empty: databases ? undefined : "No databases yet",
      info: databases ? <>On {fmtInt(t.servers)} {plural(t.servers, "server")}. A database is late when one of its tables is past
        its freshness target.</> : undefined,
      sub: <CountBar parts={[{ count: notLateDatabases, word: "not late", tone: "loaded" },
        { count: lateDatabases, word: "late", tone: "late" }]} /> },
    { label: "Tables", to: "/tables", value: <>{fmtInt(enabled)}<small>enabled</small></>,
      empty: enabled ? undefined : "No tables yet", sub: <>{pct}% loaded</> },
    { label: "Rows written, 24 hours", to: "/runs",
      value: <>{d.rows_24h.partial ? "at least " : ""}{fmtInt(d.rows_24h.rows)}<small>rows</small></>,
      info: <>Rows every job's ingestion runs wrote in the last 24 hours.
        {d.rows_24h.partial && <> "At least" means some loads did not record a count.</>}</> },
  ] : [];
  usePageStamp(d?.read_at, 30, ov.error);
  return (
    <>
      <PageHead title="Overview"
        lead={d && (enabled ? <>{pct}% of <strong>{fmtInt(enabled)}</strong> enabled {plural(enabled, "table")} {enabled === 1 ? "is" : "are"} loaded,
          in <strong>{fmtInt(jobs)}</strong> {plural(jobs, "ingestion job")}.</> : "No enabled tables yet.")} />
      <ErrorBox error={ov.error} />
      {d && !enabled && <Notice tone="info">No tables are set up yet. <Link to="/ingestion-jobs/new">New ingestion job</Link> makes a job with its tables.</Notice>}
      {/* Only once freshness targets are in use: until then no table can be late, and there is nothing to warn about. */}
      {Number(t.no_sla_tables) > 0 && Number(t.no_sla_tables) < enabled && (
        <Notice tone="warn">{fmtInt(t.no_sla_tables)} enabled {plural(t.no_sla_tables, "table")} {Number(t.no_sla_tables) === 1 ? "has" : "have"} no
          freshness target, so {Number(t.no_sla_tables) === 1 ? "it is" : "they are"} never flagged as late. To set one: Tables, select the
          tables, Bulk edit, then Freshness target.</Notice>
      )}
      {/* Until the first read, the tiles show a dash, never zeros that look like real counts. */}
      <div className="metrics">
        {d ? tiles.map((tile) => (
          <Link key={tile.label} to={tile.to} className="metric metric-link">
            <div className="metric-label">{tile.label}{tile.info && <TileInfo>{tile.info}</TileInfo>}</div>
            {tile.empty ? <div className="metric-value small-value muted">{tile.empty}</div> : <>
              <div className="metric-value">{tile.value}</div>{tile.sub && <div className="metric-sub">{tile.sub}</div>}</>}
          </Link>
        )) :["Ingestion jobs", "Databases", "Tables", "Rows written, 24 hours"].map((label) => (
          <div key={label} className="metric"><div className="metric-label">{label}</div><div className="metric-value muted">–</div></div>))}
      </div>
      {d && <RunningNow total={d.running_total} notInJob={d.running_not_in_job} />}
      {d && <div className="split">
        <div>
          <NeedsAttention jobs={d.attention} total={d.attention_total} />
          <Freshness totals={t} />
        </div>
        <div>
          <NextScheduled triggers={d.next_triggers} paused={d.paused_jobs} />
          <RecentRuns jobs={d.jobs} />
        </div>
      </div>}
    </>
  );
}
