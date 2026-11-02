import { isLiveOutcome } from "../components";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { useSettings } from "../App";
import { api, type Row } from "../api";
import { OutLink, clusterUrl,
  Crumbs, ErrorBox, PageHead, Pager, ProgressBar, ProgressKey, RunStatus, Section, Stamp, Tag, fmtClock, fmtDuration, fmtInt,
  fmtSpeed, jobRunName, plural, progressFrom, progressText, type Tone
} from "../components";
import { rowsText } from "./IngestionJob";

const DATABASES_PAGE = 20;

function Figure({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return <div className="metric"><div className="metric-label">{label}</div><div className="metric-value">{value || "–"}</div>
    {sub && <div className="metric-sub">{sub}</div>}</div>;
}

/** One ingestion run: progress over all the job's tables, its group runs (waiting ones with their start), its databases
 * failing first, and its failures by cause. */
export default function JobRun() {
  const { config } = useSettings();
  const { id = "", slot = "" } = useParams();
  const [dbOffset, setDbOffset] = useState(0);
  const run = useQuery({
    queryKey: ["job-run", id, slot],
    queryFn: () => api.get<Row>(`/ingestion-jobs/${encodeURIComponent(id)}/runs/${encodeURIComponent(slot)}`),
    refetchInterval: (q) => (isLiveOutcome((q.state.data as Row | undefined)?.outcome?.label) ? 15_000 : false),
  });
  const r = run.data;
  const live = isLiveOutcome(r?.outcome?.label);
  const p = progressFrom(r?.progress);
  const checkedPct = p.total ? Math.floor((100 * (p.unchanged + p.loaded)) / p.total) : 0;
  const notStarted = (r?.group_runs ?? []).filter((g: Row) => g.state === "waiting" || g.state === "starting")
    .reduce((a: number, g: Row) => a + Number(g.progress?.total || 0), 0);
  // A finished run says what it checked in the groups that ran, and which groups did not run; nothing is "to go".
  const groups: Row[] = (r?.group_runs ?? []).filter((g: Row) => !g.check);
  const notRunGroups = groups.filter((g) => Number(g.progress?.not_run || 0) > 0);
  const finishedText = `${fmtInt(p.unchanged + p.loaded)} ${plural(p.unchanged + p.loaded, "table")} checked in the `
    + `${fmtInt(groups.length - notRunGroups.length)} ${plural(groups.length - notRunGroups.length, "group")} that ran`
    + (notRunGroups.length ? `; ${fmtInt(notRunGroups.length)} ${plural(notRunGroups.length, "group")} `
      + `(${fmtInt(p.notRun)} ${plural(p.notRun, "table")}) did not run` : "")
    + (p.failed ? `; ${fmtInt(p.failed)} failed` : "");
  const dbs: Row[] = r?.databases ?? [];
  const manyServers = new Set(dbs.map((d) => d.server)).size > 1;
  return (
    <>
      <PageHead crumb={<Crumbs steps={[["Ingestion jobs", "/ingestion-jobs"], [r?.job_name ?? "Ingestion job", `/ingestion-jobs/${id}`], ["Ingestion run"]]} />}
        tab={r ? jobRunName(r.job_name, r.slot) : "Ingestion run"}
        title={r ? <>{jobRunName(r.job_name, r.slot)}{live ? `: ${checkedPct}% checked${r.expected_end ? `, ends about ${fmtClock(r.expected_end)}` : ""}` : ""}{" "}
          <RunStatus status={null} outcome={r.outcome} /></> : "Ingestion run"}
        lead={r && <>{r.outcome?.note ?? `${fmtInt(r.ran)} of ${fmtInt(r.members)} group ${plural(r.members, "run")}`}
          {live && <>; {fmtInt(r.running_group_runs)} running, {fmtInt(r.waiting)} waiting to start</>}. Group runs the check job started together are one ingestion run; others are placed by the job's current schedule.</>}
        right={live || run.error ? <Stamp at={r?.read_at} every={15} error={run.error} /> : undefined} />
      <ErrorBox error={run.error} />
      {r && (
        <>
          <Section title={live ? "Progress now" : "What the ingestion run did"}
            note={notStarted ? `${fmtInt(notStarted)} of the tables to go belong to groups that have not started yet.` : undefined}>
            <div className="count">{live ? progressText(p) : finishedText}</div>
            <ProgressBar p={p} big /><ProgressKey p={p} finished={!live} />
          </Section>
          <div className="metrics">
            {live ? <>
              <Figure label="Speed now" value={r.speed_now ? `${fmtSpeed(r.speed_now.tables_per_min)} tables/min` : ""}
                sub={r.speed_now ? `tables loaded, last 5 minutes; ${fmtInt(Math.round(r.speed_now.rows_per_min))} rows/min` : "shown after 3 minutes of loading"} />
              <Figure label="Expected end" value={r.expected_end ? fmtClock(r.expected_end) : ""} sub="its last group's start plus its typical run" />
            </> : <>
              <Figure label="Took" value={r.took_s != null ? fmtDuration(r.took_s) : ""} sub="first group run start to last finish" />
              <Figure label="Speed" value={r.speed_tables_per_min != null ? `${fmtSpeed(r.speed_tables_per_min)} tables/min` : ""} sub="tables loaded, failed ones not counted" />
            </>}
            <Figure label="Clusters" value={fmtInt(r.clusters)} sub="each group run starts its own" />
            <Figure label="Failed tables" value={fmtInt(r.failed_tables)} sub={Number(r.failed_before) ? `the ${fmtInt(r.failed_before)} job ${plural(r.failed_before, "run")} before also had failed tables` : undefined} />
            <Figure label="Rows written" value={rowsText(r)} sub={live ? "so far" : undefined} />
          </div>
          <Section title={`Group runs: ${fmtInt(r.group_runs.filter((g: Row) => !g.check).length)}`}
            note={r.group_runs.some((g: Row) => g.check) ? "The check job's checks first, then running, then waiting, then finished."
              : "Running first, then waiting, then finished."}>
            <div className="table-wrap"><table className="data">
              <thead><tr><th>Ingestion group</th><th>Status</th><th>Started</th><th className="num">Took</th><th>Progress</th>
                <th className="num">Tables/min</th><th className="num">Parallelism</th><th>Cluster</th><th className="num">Failed</th></tr></thead>
              <tbody>
                {r.group_runs.map((g: Row) => {
                  const gp = progressFrom(g.progress);
                  const waiting = g.state === "waiting";
                  return (
                    <tr key={`${g.task_collection}-${g.check ? "check-" : ""}${g.run_id ?? g.state}`}>
                      <td className="name"><Link to={`/collections/${encodeURIComponent(g.task_collection)}`}><b>{g.task_collection}</b></Link>
                        {g.extra && <span className="sub">extra run, likely started by hand</span>}
                        {g.check && <span className="sub">check</span>}</td>
                      <td>{waiting ? <Tag tone={"" as Tone}>Waiting, starts {fmtClock(g.fire_at)}</Tag>
                        : g.state === "starting" ? <Tag tone="run">Starting, triggered {fmtClock(g.fire_at)}</Tag>
                        : g.run_id ? <Link to={`/runs/${g.run_id}`}><RunStatus status={null} outcome={g.status} /></Link>
                        : <RunStatus status={null} outcome={g.status} />}</td>
                      <td>{g.started_at ? fmtClock(g.started_at) : ""}</td>
                      <td className="num">{g.duration_s != null ? fmtDuration(g.duration_s) : ""}</td>
                      <td style={{ minWidth: 160 }}>{g.check ? "" : <ProgressBar p={gp} />}</td>
                      <td className="num">{g.speed_tables_per_min != null ? fmtSpeed(g.speed_tables_per_min) : ""}</td>
                      <td className="num">{g.parallelism ?? ""}</td><td className="mono">{g.cluster_id ? <OutLink href={clusterUrl(config?.workspace_url, g.cluster_id)}>{g.cluster_id}</OutLink> : ""}</td>
                      <td className="num" style={{ color: Number(g.failed) ? "var(--bad)" : undefined }}>{g.check ? "" : fmtInt(g.failed)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table></div>
          </Section>
          <Section title={`Databases: ${fmtInt(dbs.length)}`} note="Every database with table events in this ingestion run: failed first, then in flight, then done.">
            <div className="table-wrap"><table className="data">
              <thead><tr><th>Database</th><th>Ingestion group</th><th className="num">Loaded</th><th className="num">In flight</th>
                <th className="num">Failed</th><th className="num">Rows written</th></tr></thead>
              <tbody>
                {dbs.slice(dbOffset, dbOffset + DATABASES_PAGE).map((d) => (
                  <tr key={`${d.task_collection}\u001f${d.server}\u001f${d.src_database}`}>
                    <td className="name"><b>{d.src_database || "(none)"}</b>{manyServers && <span className="sub">{d.server || "(no server)"}</span>}</td>
                    <td>{d.task_collection}</td><td className="num">{fmtInt(d.loaded)}</td><td className="num">{fmtInt(d.in_flight)}</td>
                    <td className="num" style={{ color: Number(d.failed) ? "var(--bad)" : undefined }}>{fmtInt(d.failed)}</td>
                    <td className="num">{Number(d.uncounted) ? `at least ${fmtInt(d.rows_written || 0)}` : fmtInt(d.rows_written || 0)}</td>
                  </tr>
                ))}
                {!dbs.length && <tr><td colSpan={6} className="muted">No table events: every table was unchanged, or nothing has started.</td></tr>}
              </tbody>
            </table></div>
            <Pager offset={dbOffset} limit={DATABASES_PAGE} total={dbs.length} noun="database" onPage={setDbOffset} />
          </Section>
          {r.failures.length > 0 && (
            <Section title={`Failures: ${fmtInt(r.failed_tables)} ${plural(r.failed_tables, "table")}`} note="Grouped by cause.">
              {r.failures.map((f: Row) => (
                <div key={f.signature} className="attention-row">
                  <div className="title">{f.label} <span className="muted">{fmtInt(f.tables)} {plural(f.tables, "table")} in{" "}
                    {fmtInt(f.databases.length)} {plural(f.databases.length, "database")}, {fmtClock(f.first_at)} to {fmtClock(f.last_at)}</span></div>
                  <div className="kv"><span className="mono example" title={f.example}>{f.example}</span></div>
                </div>
              ))}
            </Section>
          )}
        </>
      )}
    </>
  );
}
