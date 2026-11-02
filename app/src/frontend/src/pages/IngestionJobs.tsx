import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, qs, type Row } from "../api";
import { ErrorBox, Info, Notice, OutLink, PageHead, Pager, RunStatus, Section, Twisty, fmtInt, fmtShort, fmtTime, toast, useConfirm,
         usePageStamp, type Outcome } from "../components";
import { daysLeftText } from "../pending";
import { deleteNowRequest } from "./IngestionJob";
import { JobsTable, type JobSort } from "./Overview";

const FILTERS = [["all", "All"], ["running", "Running"], ["failing", "Failing"], ["late", "Late"], ["paused", "Paused"], ["attention", "Needs attention"]] as const;
const PAGE = 25;

interface JobsPage { jobs: Row[]; total: number; offset: number; limit: number; not_in_job: Row | null; counts: Record<string, number>;
                   read_at: string }

interface DeletionsPage { deleted: Row[]; total: number; offset: number; limit: number; configured: boolean; retention_days: number }

/** Every ingestion job, failing first, filtered and searched, in a table that fills the page. */
export default function IngestionJobs() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const filter = params.get("filter") || "all";
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [sort, setSort] = useState<JobSort>({ by: "", descending: true });
  const jobs = useQuery({
    queryKey: ["ingestion-jobs", filter, search, offset, sort],
    queryFn: () => api.get<JobsPage>(`/ingestion-jobs${qs({ filter, search, offset, limit: PAGE, sort: sort.by, descending: sort.descending })}`),
    refetchInterval: 15_000,
  });
  const counts = jobs.data?.counts ?? {};
  usePageStamp(jobs.data?.read_at, 30, jobs.error);
  return (
    <>
      <PageHead title="Ingestion jobs" />
      <ErrorBox error={jobs.error} />
      <div className="toolbar">
        <span className="chips">
          {FILTERS.map(([key, label]) => (
            <button key={key} className={`chip${filter === key ? " active" : ""}`}
              onClick={() => { setOffset(0); setParams(key === "all" ? {} : { filter: key }); }}>
              {label} <b>{fmtInt(counts[key] ?? 0)}</b>
            </button>
          ))}
        </span>
        <input type="search" className="search-wide" placeholder="Search jobs and groups" value={search}
          onChange={(e) => { setOffset(0); setSearch(e.target.value); }} />
        <button className="primary toolbar-end" onClick={() => navigate("/ingestion-jobs/new")}>New ingestion job</button>
      </div>
      <Section title="Jobs">
        {jobs.data && <JobsTable jobs={jobs.data.jobs} notInJob={offset + PAGE >= jobs.data.total ? jobs.data.not_in_job : null}
          total={jobs.data.total} offset={offset} limit={PAGE} onPage={setOffset} fill
          sort={sort} onSort={(next) => { setOffset(0); setSort(next); }} />}
      </Section>
      <Deletions />
    </>
  );
}

const DELETED_PAGE = 10;

/** The deleted jobs kept for a Restore (not yet purged): closed until opened, its count in the header, a page at a time,
 * each with Restore and Delete now. Shown only when there is one. */
function Deletions() {
  const [open, setOpen] = useState(false);
  const [offset, setOffset] = useState(0);
  const qc = useQueryClient();
  const confirm = useConfirm();
  const page = useQuery({
    queryKey: ["ingestion-job-deletions", open, offset],
    queryFn: () => api.get<DeletionsPage>(`/ingestion-jobs/deletions${qs({ offset, limit: DELETED_PAGE, rows: open })}`),
    refetchInterval: 60_000,
  });
  const total = Number(page.data?.total ?? 0);
  const rows: Row[] = page.data?.deleted ?? [];
  // A restored or purged job leaves this list, and it changes the jobs list and the job's own page.
  const refresh = () => ["ingestion-job-deletions", "schedules", "ingestion-jobs", "ingestion-job"]
    .forEach((key) => qc.invalidateQueries({ queryKey: [key] }));
  const restore = useMutation({
    mutationFn: (id: string) => api.post<Row>(`/schedules/${encodeURIComponent(id)}/restore`),
    onSuccess: () => { toast("Restored paused"); setOffset(0); refresh(); },
  });
  const purge = useMutation({
    mutationFn: (id: string) => api.post<Row>(`/schedules/${encodeURIComponent(id)}/purge`),
    onSuccess: () => refresh(),
  });
  if (!total && !page.error) return null;
  const deleteNow = (d: Row) => confirm.ask(deleteNowRequest(d.name)).then((ok) => { if (ok) purge.mutate(d.id); });
  const title = <span className="section-toggle"><Twisty open={open} onToggle={() => setOpen(!open)}
    label={open ? "Close the deleted jobs" : "Open the deleted jobs"}>Deleted jobs ({fmtInt(total)})</Twisty>
    <Info label="About deleted jobs">Jobs you deleted are kept until their retention runs out, so Restore can bring them back.
      Delete now deletes one at once.</Info></span>;
  return (
    <Section title={title}>
      <ErrorBox error={page.error} />
      {open && <>
        {page.data && <p className="muted small">Kept for {page.data.retention_days} days after Delete, then deleted for good.
          Restore brings a job back paused, with its state, so nothing reloads.</p>}
        <ErrorBox error={restore.error} />
        <ErrorBox error={purge.error} />
        {purge.data?.delete_run?.error && <Notice tone="warn">The delete job did not start: {purge.data.delete_run.error}</Notice>}
        <div className="table-wrap"><table className="data">
          <thead><tr><th>Ingestion job</th><th>Deleted</th><th>Days left</th><th>State</th><th className="num">Tables left</th><th /></tr></thead>
          <tbody>
            {rows.map((d) => {
              const outcome = deletionOutcome(d);
              const refusal = d.delete_refusal ?? undefined;
              return (
                <tr key={d.id}>
                  <td className="name"><Link to={`/ingestion-jobs/${encodeURIComponent(d.id)}`}>{d.name}</Link></td>
                  <td title={fmtTime(d.deleted_at)}>{fmtShort(d.deleted_at)}</td>
                  <td>{daysLeftText(d.delete?.days_left ?? 0)}</td>
                  <td><OutLink href={d.delete?.run?.url}><RunStatus status={null} outcome={outcome} /></OutLink>
                    {outcome.note && <span className="sub">{outcome.note}</span>}</td>
                  <td className="num">{fmtInt(d.tables_left)}</td>
                  <td>
                    <button disabled={!!refusal || restore.isPending} title={refusal}
                      onClick={() => restore.mutate(d.id)}>{restore.isPending && restore.variables === d.id ? "Restoring…" : "Restore"}</button>{" "}
                    <button className="danger" disabled={!!refusal || purge.isPending} title={refusal}
                      onClick={() => deleteNow(d)}>Delete now</button>
                    {refusal && <span className="sub">{refusal}</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table></div>
        <Pager offset={offset} limit={DELETED_PAGE} total={total} noun="deleted job" onPage={setOffset} />
        {confirm.dialog}
      </>}
    </Section>
  );
}

/** A deleted job's state as a status: kept until the purge, its purge running, or its purge failed, with the reason. */
export function deletionOutcome(d: Row): Outcome {
  if (d.delete?.state === "deleting") return { label: "Deleting", tone: "run", note: null };
  if (d.delete?.state === "failed") return { label: "Delete failed", tone: "bad", note: d.delete.reason || null };
  return { label: "Deleted", tone: "neutral", note: null };
}
