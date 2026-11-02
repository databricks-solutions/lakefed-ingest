import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { type ColDef } from "ag-grid-community";
import { api, qs, type Row } from "../api";
import { ErrorBox, Grid, PageHead, RunStatus, Section, fmtDuration, fmtInt, fmtSpeed, isLiveOutcome, plural } from "../components";
import { JobRunsTable, pausedNotRun } from "./IngestionJob";

/** Tables that needed a read: the run's total less those found unchanged. */
const worked = (r: Row) => (r.total === null || r.total === undefined ? null : Number(r.total) - Number(r.skipped || 0));

const COLUMNS: ColDef[] = [
  { field: "run_id", headerName: "Run", width: 80, minWidth: 80, cellRenderer: (p: any) => <Link to={`/runs/${p.value}`}>#{p.value}</Link> },
  { field: "task_collection", headerName: "Ingestion group", minWidth: 150 },
  { field: "status", headerName: "Outcome", minWidth: 170, valueGetter: (p) => p.data?.outcome?.label ?? p.data?.status,
    cellRenderer: (p: any) => <RunStatus status={p.data?.status} outcome={p.data?.outcome} /> },
  { field: "started_at", headerName: "Started", minWidth: 110,
    valueFormatter: (p) => (p.value ? new Date(p.value).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "") },
  { field: "duration_s", headerName: "Took", type: "rightAligned", minWidth: 80,
    valueFormatter: (p) => (p.value === null || p.value === undefined ? "" : fmtDuration(p.value)) },
  { colId: "worked", headerName: "Worked", type: "rightAligned", minWidth: 90, valueGetter: (p) => worked(p.data), valueFormatter: (p) => fmtInt(p.value) },
  { field: "skipped", headerName: "Unchanged", minWidth: 110, type: "rightAligned", valueFormatter: (p) => fmtInt(p.value) },
  { field: "failed", headerName: "Failed", type: "rightAligned", minWidth: 80, valueFormatter: (p) => fmtInt(p.value),
    cellStyle: (p) => (Number(p.value) ? { color: "var(--bad)", fontWeight: 600 } : null) },
  { field: "rows_written", headerName: "Rows written", type: "rightAligned", minWidth: 120,
    valueFormatter: (p) => (p.data?.rows_partial ? `at least ${fmtInt(p.value)}` : fmtInt(p.value)) },
  { field: "speed_tables_per_min", headerName: "Tables/min", type: "rightAligned", minWidth: 105,
    valueFormatter: (p) => (p.value === null || p.value === undefined ? "" : fmtSpeed(p.value)) },
  { field: "parallelism", headerName: "Parallelism", type: "rightAligned", minWidth: 110 },
];

const PAGE = 25;

const fmtWhen = (v: unknown) => new Date(String(v)).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/** Ingestion runs of every job (or one), running first, then newest first, with outcome counts. "Not in any job" lists
 * those groups' group runs with the run columns. */
export default function Runs() {
  const [params, setParams] = useSearchParams();
  const job = params.get("job") || "";
  const [outcome, setOutcome] = useState("");
  const [pages, setPages] = useState<string[]>([]);
  const before = pages[pages.length - 1];
  const runs = useQuery({
    queryKey: ["job-runs", job, outcome, before],
    queryFn: () => api.get<Row>(`/job-runs${qs({ job, outcome, before, limit: PAGE })}`),
    refetchInterval: before ? false : 15_000,
  });
  const jobs = useQuery({ queryKey: ["job-runs-jobs"], queryFn: () => api.get<Row>(`/job-runs${qs({ limit: 1 })}`) });
  const d = runs.data;
  const list: Row[] = d?.job_runs ?? [];
  const running = list.filter((r) => isLiveOutcome(r.outcome?.label));
  // As the Databricks Runs list: a paused job's triggers that did not run are not finished runs; they show as one line.
  const pausedSlots = list.filter(pausedNotRun);
  const finished = list.filter((r) => r.outcome?.label !== "Running" && (outcome === "Not run" || !pausedNotRun(r)));
  const withFailed = finished.filter((r) => Number(r.failed_tables) > 0);
  const oldest = finished.length ? finished[finished.length - 1].slot : null;
  const choose = (value: string) => { setPages([]); setOutcome(""); setParams(value ? { job: value } : {}); };
  return (
    <>
      <PageHead title="Runs"
        lead={job === "none" ? "Group runs of the ingestion groups in no job."
          : d ? `${fmtInt(running.length)} running; ${fmtInt(withFailed.length)} finished with failed tables${oldest ? ` since ${fmtWhen(oldest)}` : ""}. `
            + "An ingestion run is one trigger of an ingestion job, with one group run per ingestion group."
          : "An ingestion run is one trigger of an ingestion job, with one group run per ingestion group."}
        right={<label className="toolbar" style={{ margin: 0 }}>Ingestion job
          <select value={job} onChange={(e) => choose(e.target.value)}>
            <option value="">(all jobs)</option>
            {(jobs.data?.jobs ?? []).map((j: Row) => <option key={j.id} value={String(j.id)}>{j.name}</option>)}
            <option value="none">Not in any job</option>
          </select>
        </label>} />
      <ErrorBox error={runs.error} />
      {job === "none" ? (
        <Section title="Group runs of groups in no job" note={<>Started outside any ingestion job: by hand in Databricks, or by the Databricks job's own schedule.
          Speed: tables loaded (failed ones not counted) a minute, from the first table's start to the last one's finish. Took is blank for a run another
          run closed: it ended without finishing. Rows written: "at least" when some loads recorded none.</>}>
          <Grid rows={d?.group_runs} columns={COLUMNS.map((c) => ({ ...c, filter: false }))} getRowId={(r) => String(r.run_id)} options={{ autoSizeStrategy: { type: "fitGridWidth" } }} />
        </Section>
      ) : (
        <>
          <div className="toolbar"><span className="chips">
            <button className={`chip${!outcome ? " active" : ""}`} onClick={() => { setPages([]); setOutcome(""); }}>All</button>
            {Object.entries(d?.counts ?? {}).map(([label, count]) => (
              <button key={label} className={`chip${outcome === label ? " active" : ""}`} onClick={() => { setPages([]); setOutcome(label); }}>
                {label} <b>{fmtInt(count)}</b></button>
            ))}
          </span></div>
          {running.length > 0 && (
            <Section title={`Running now: ${fmtInt(running.length)}`}>
              <JobRunsTable runs={running} showJob />
            </Section>
          )}
          <Section title="Finished, newest first" note="Tables/min: tables loaded a minute over the ingestion run's work span, so planning and change detection are left out; failed tables are not counted, and show under Failed."
            actions={<span className="pager-buttons">
              <button className="ghost" disabled={!pages.length} onClick={() => setPages(pages.slice(0, -1))}>Newer</button>
              <button className="ghost" disabled={!d?.next_before} onClick={() => setPages([...pages, d!.next_before])}>Older</button>
            </span>}>
            {d && <JobRunsTable runs={finished} showJob />}
            {pausedSlots.length > 0 && outcome !== "Not run" && <p className="muted small">Not listed: {fmtInt(pausedSlots.length)}{" "}
              {plural(pausedSlots.length, "trigger")} of paused jobs that did not run.</p>}
          </Section>
        </>
      )}
    </>
  );
}
