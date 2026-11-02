import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Row } from "../api";
import { Badge, ErrorBox, Menu, Notice, PageHead, Section, fmtTime, readAt, statusName, usePageStamp } from "../components";
import { usePermissions } from "../permissions";

// Schedules: when each ingestion job runs, as set in its Edit view (When it runs), one row per job and no groups, as
// the Databricks Jobs list shows them. Run history is on Runs. A schedule is changed in the job's Edit view.

function next(job: Row): string {
  if (job.paused) return "paused";
  const times = (job.members as Row[]).map((m) => m.next_run).filter(Boolean).sort();
  return times.length ? fmtTime(times[0]) : "not synced";
}

function last(job: Row): Row | null {
  return (job.members as Row[]).filter((m) => m.last_started).sort((a, b) => String(b.last_started).localeCompare(String(a.last_started)))[0] ?? null;
}

function JobRow({ job }: { job: Row }) {
  const qc = useQueryClient();
  const act = useMutation({
    mutationFn: (path: string) => api.post<Row>(`/schedules/${job.id}/${path}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["schedules"] }),
  });
  const check = useMutation({ mutationFn: () => api.get<Row>(`/schedules/${job.id}/check`) });
  const locked = usePermissions().data?.jobs[String(job.id)]?.change ?? null;
  const recent = last(job);
  const unsynced = (job.members as Row[]).filter((m) => m.sync_error || !m.job_id).length;
  const drifted = check.data ? Object.values(check.data).filter((c: any) => c?.status && c.status !== "in_sync").length : 0;
  return (
    <tr>
      <td className="name"><Link to={`/ingestion-jobs/${job.id}`}><b>{job.name}</b></Link></td>
      <td>{job.description}</td>
      <td>{job.paused ? <Badge value="paused" /> : <Badge value="ok" />}</td>
      <td>{next(job)}</td>
      <td>{recent ? <>{statusName(recent.last_status)}<span className="sub">{fmtTime(recent.last_started)}</span></> : <span className="muted">never run</span>}</td>
      <td>{unsynced ? <span className="tag warn">{unsynced} not synced</span> : check.data
        ? (drifted ? <span className="tag warn">changed in the Jobs UI</span> : <span className="muted">in sync</span>) : <span className="muted">synced</span>}
        <ErrorBox error={act.error || check.error} /></td>
      <td className="card-actions">
        <Link to={`/ingestion-jobs/${job.id}/edit`}>Edit schedule</Link>
        <Menu label="⋮" title="More actions">
          <button onClick={() => check.mutate()} disabled={check.isPending}>Check its Databricks jobs</button>
          <button onClick={() => act.mutate("sync")} disabled={act.isPending || !!locked} title={locked ?? undefined}>Sync its Databricks jobs</button>
        </Menu>
      </td>
    </tr>
  );
}

export default function Schedules() {
  const list = useQuery({ queryKey: ["schedules"], queryFn: () => api.get<Row[]>("/schedules"), refetchInterval: 15_000 });
  usePageStamp(readAt(list), 30, list.error);
  return (
    <>
      <PageHead title="Schedules" lead="When each ingestion job runs, as set in its Edit view." />
      <ErrorBox error={list.error} />
      <Section title="Ingestion jobs" note="Each one's schedule, as set in its Edit view (When it runs).">
        {list.data && list.data.length === 0 ? <Notice>No ingestion jobs yet: New ingestion job, on Ingestion jobs, makes one.</Notice> : (
          <div className="table-wrap"><table className="data">
            <thead><tr><th>Ingestion job</th><th>Schedule</th><th>Status</th><th>Next run</th><th>Last run</th><th>Its Databricks jobs</th><th></th></tr></thead>
            <tbody>{(list.data ?? []).map((job) => <JobRow key={job.id} job={job} />)}</tbody>
          </table></div>
        )}
      </Section>
    </>
  );
}
