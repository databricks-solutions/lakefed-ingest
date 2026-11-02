import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Row } from "../api";
import { Crumbs, ErrorBox, PageHead, RunStatus, Section, StateBar, StateKey, fmtClock, fmtDuration, fmtInt, plural } from "../components";

// The groups in no ingestion job, as a page of their own, so a deployment with no jobs (a read-only share) still
// opens its groups and, from each group, its tables.
export default function NotInJob() {
  const page = useQuery({ queryKey: ["not-in-job"], queryFn: () => api.get<Row>("/ingestion-jobs/not-in-job") });
  const d = page.data;
  return (
    <>
      <PageHead crumb={<Crumbs steps={[["Ingestion jobs", "/ingestion-jobs"]]} />}
        title="Not in any job"
        lead={d && <><strong>{fmtInt(d.groups_detail.length)}</strong> {plural(d.groups_detail.length, "ingestion group")},{" "}
          <strong>{fmtInt(d.databases)}</strong> {plural(d.databases, "database")}, <strong>{fmtInt(d.enabled_tables)}</strong>{" "}
          {plural(d.enabled_tables, "table")} in no ingestion job: they run only when started by hand or by the Databricks job.
          Open a group to see its tables.</>} />
      <ErrorBox error={page.error} />
      {d && (
        <Section title={`Ingestion groups: ${fmtInt(d.groups_detail.length)}`} actions={<StateKey />}>
          <div className="table-wrap"><table className="data">
            <thead><tr><th>Ingestion group</th><th className="num">Databases</th><th className="num">Tables</th><th>State now</th>
              <th className="num">Failing</th><th>Last group run</th><th className="num">p95 run time</th></tr></thead>
            <tbody>
              {d.groups_detail.map((g: Row) => (
                <tr key={g.task_collection}>
                  <td className="name"><Link to={`/collections/${encodeURIComponent(g.task_collection)}`}><b>{g.task_collection}</b></Link></td>
                  <td className="num">{fmtInt(g.databases)}</td><td className="num">{fmtInt(g.enabled_tables)}</td><td><StateBar row={g} /></td>
                  <td className="num" style={{ color: Number(g.state_failing) ? "var(--bad)" : undefined }}>{fmtInt(g.state_failing)}</td>
                  <td>{g.last_run ? <><Link to={`/runs/${g.last_run.run_id}`}><RunStatus status={g.last_run.status} outcome={g.last_run.outcome} /></Link>
                    <span className="sub">group run {fmtInt(g.last_run.run_id)} at {fmtClock(g.last_run.started_at)}</span></> : <span className="muted">never run</span>}</td>
                  <td className="num">{g.p95_s != null ? fmtDuration(g.p95_s) : ""}</td>
                </tr>
              ))}
              {!d.groups_detail.length && <tr><td colSpan={7} className="muted">Every group is in an ingestion job.</td></tr>}
            </tbody>
          </table></div>
        </Section>
      )}
    </>
  );
}
