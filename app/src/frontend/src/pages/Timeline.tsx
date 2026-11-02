import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, qs, type Row } from "../api";
import { PageHead, readAt, ErrorBox, Notice, fmtDuration, fmtTime, isLiveOutcome, jobRunName, jobRunPath, plural, statusName, useWidth, usePageStamp } from "../components";

// One lane per ingestion job, as the Jobs UI's run timeline: each ingestion run a bar from its first group run's start to
// its last one's finish, coloured by its outcome, opening the ingestion run; its groups on hover. Planned triggers are
// ticks, and triggers Databricks skipped (the previous run still going) red markers. Groups in no job get one lane.

interface Lane { job_id: number; name: string; description: string; paused: boolean; runs: Row[]; planned: string[]; skipped: string[] }
interface Timeline { start: string; end: string; jobs: Lane[]; not_in_job: { groups: string[]; group_runs: Row[] } | null }

const LABEL_W = 260, MIN_CHART_W = 600, ROW_H = 40, AXIS_H = 26, MIN_TICK_W = 64;
const TONE: Record<string, string> = { ok: "var(--s-loaded)", warn: "var(--s-late)", bad: "var(--s-failing)", run: "var(--s-running)" };
const GROUP_RUN: Record<string, string> = { completed: TONE.ok, failed: TONE.bad, running: TONE.run, skipped: TONE.warn };

const groupsText = (run: Row) => (run.groups ?? []).map((g: Row) => `${g.group}: ${g.status ? statusName(g.status) : "did not run"}`).join("\n");

function runTitle(lane: Lane, run: Row) {
  const span = run.started_at ? `${fmtTime(run.started_at)} – ${isLiveOutcome(run.outcome.label) ? "running" : fmtTime(run.ended_at)}` : "did not run";
  return [jobRunName(lane.name, run.slot), `${run.outcome.label}${run.outcome.note ? ` (${run.outcome.note})` : ""}`, span,
    run.took_s != null ? `took ${fmtDuration(run.took_s)}` : "",
    Number(run.failed_tables) > 0 ? `${run.failed_tables} ${plural(run.failed_tables, "table")} failed` : "",
    `${run.ran} of ${run.members} ${plural(run.members, "group")} ran`, groupsText(run), "Click to open the ingestion run"].filter(Boolean).join("\n");
}

export default function TimelinePage() {
  const [hours, setHours] = useState(24);
  const navigate = useNavigate();
  // The chart is drawn at the card's own width, so it uses the whole screen without scaling its text.
  const [box, boxWidth] = useWidth<HTMLDivElement>();
  const CHART_W = Math.max(MIN_CHART_W, boxWidth - LABEL_W - 26);
  const t = useQuery({ queryKey: ["timeline", hours], queryFn: () => api.get<Timeline>(`/timeline${qs({ hours })}`), refetchInterval: 60_000 });
  const d = t.data;
  const t0 = d ? new Date(d.start).getTime() : 0, t1 = d ? new Date(d.end).getTime() : 1;
  const x = (iso: string | number) => LABEL_W + ((new Date(iso).getTime() - t0) / (t1 - t0)) * CHART_W;
  const lanes = d?.jobs ?? [];
  const rows = lanes.length + (d?.not_in_job ? 1 : 0);
  const height = AXIS_H + rows * ROW_H + 8;
  // The fewest hours between gridlines that leaves room for each label; over a day or more they fall on local
  // midnights, labelled with the day.
  const step = [1, 2, 3, 6, 12, 24].find((h) => (CHART_W * h) / hours >= MIN_TICK_W) ?? 24;
  const ticks: number[] = [];
  if (d) {
    const tick = new Date(t0); tick.setMinutes(0, 0, 0);
    do tick.setHours(tick.getHours() + 1); while (tick.getHours() % step);
    for (; tick.getTime() < t1; tick.setHours(tick.getHours() + step)) ticks.push(tick.getTime());
  }
  const tickLabel = (ms: number) => {
    const at = new Date(ms);
    return at.getHours() === 0 && step >= 6 ? at.toLocaleDateString([], { weekday: "short", day: "numeric" })
      : at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  };
  const runs = lanes.flatMap((l) => l.runs);
  const failed = runs.filter((r) => r.outcome.tone === "bad").length;
  const skipped = lanes.reduce((n, l) => n + l.skipped.length, 0);
  const band = (i: number) => <rect x={0} y={AXIS_H + i * ROW_H} width={LABEL_W + CHART_W + 10} height={ROW_H}
    style={{ fill: i % 2 ? "var(--db-surface-subtle)" : "var(--db-surface-base)" }} />;
  usePageStamp(readAt(t), 60, t.error);
  return (
    <>
      <PageHead title="Timeline" lead="The ingestion runs of every ingestion job in the window, one lane per job. Click an ingestion run to open it." />
      <div className="toolbar">
        <label>Window
          <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            {[6, 12, 24, 48, 168].map((h) => <option key={h} value={h}>last {h < 48 ? `${h} hours` : `${h / 24} days`}</option>)}
          </select>
        </label>
        <span className="legend">
          <i style={{ background: TONE.ok }} /> Succeeded <i style={{ background: TONE.warn }} /> Succeeded with failures
          <i style={{ background: TONE.bad }} /> Failed <i style={{ background: TONE.run }} /> Running
          <i className="hollow" /> Not run <i className="tick" /> planned trigger <b className="skipmark">▼</b> trigger skipped by Databricks
        </span>
      </div>
      <ErrorBox error={t.error} />
      {d && <p className="muted small">{runs.length} {plural(runs.length, "ingestion run")}, {failed} failed, {skipped} skipped {plural(skipped, "trigger")},
        {" "}{fmtTime(d.start)} – {fmtTime(d.end)}. Hover an ingestion run for its groups.</p>}
      {d && rows === 0 && <Notice>No ingestion jobs yet.</Notice>}
      {d && rows > 0 && (
        <div className="card timeline" ref={box}>
          <svg viewBox={`0 0 ${LABEL_W + CHART_W + 10} ${height}`} width={LABEL_W + CHART_W + 10} height={height} role="img"
            aria-label="Ingestion runs per ingestion job over time">
            {ticks.map((ms) => (
              <g key={ms}>
                <line x1={x(ms)} x2={x(ms)} y1={AXIS_H - 4} y2={height} style={{ stroke: "var(--border)" }} />
                <text x={x(ms)} y={14} fontSize={11} textAnchor="middle" style={{ fill: "var(--muted)" }}>
                  {tickLabel(ms)}
                </text>
              </g>
            ))}
            {lanes.map((lane, i) => {
              const y = AXIS_H + i * ROW_H;
              return (
                <g key={lane.job_id}>
                  {band(i)}
                  <text x={6} y={y + 17} fontSize={12.5} className="lane-link" onClick={() => navigate(`/ingestion-jobs/${lane.job_id}`)}>
                    {lane.name.length > 34 ? lane.name.slice(0, 33) + "…" : lane.name}<title>{`${lane.name}\nOpen the job and its past runs`}</title>
                  </text>
                  <text x={6} y={y + 32} fontSize={11} style={{ fill: "var(--muted)" }}>
                    {(lane.paused ? "Paused · " : "") + (lane.description.length > 40 ? lane.description.slice(0, 39) + "…" : lane.description)}
                  </text>
                  {lane.planned.map((p) => <line key={p} x1={x(p)} x2={x(p)} y1={y + 6} y2={y + ROW_H - 6} strokeWidth={1}
                    style={{ stroke: "var(--db-border-strong, #9aa5b1)" }} />)}
                  {lane.runs.map((run) => {
                    const open = () => navigate(jobRunPath(lane.job_id, run.slot));
                    if (!run.started_at && !isLiveOutcome(run.outcome.label)) {
                      return <circle key={run.slot} cx={Math.max(LABEL_W + 4, x(run.slot))} cy={y + ROW_H / 2} r={4} className="hollow-mark"
                        onClick={open}><title>{runTitle(lane, run)}</title></circle>;
                    }
                    const sx = Math.max(LABEL_W, x(run.started_at ?? run.slot)), ex = x(run.ended_at ?? d.end);
                    return (
                      <rect key={run.slot} x={sx} y={y + 10} width={Math.max(3, ex - sx)} height={ROW_H - 20} rx={2} className="run-bar"
                        style={{ fill: TONE[run.outcome.tone] ?? "var(--muted)" }} onClick={open}>
                        <title>{runTitle(lane, run)}</title>
                      </rect>
                    );
                  })}
                  {lane.skipped.map((s) => (
                    <text key={s} x={x(s)} y={y + 10} fontSize={11} textAnchor="middle" style={{ fill: "var(--bad)" }}>▼
                      <title>{`trigger skipped at ${fmtTime(s)}: the previous run was still going`}</title></text>
                  ))}
                </g>
              );
            })}
            {d.not_in_job && (() => {
              const i = lanes.length, y = AXIS_H + i * ROW_H;
              return (
                <g key="not-in-job">
                  {band(i)}
                  <text x={6} y={y + 17} fontSize={12.5} style={{ fill: "var(--text)" }}>Not in any job</text>
                  <text x={6} y={y + 32} fontSize={11} style={{ fill: "var(--muted)" }}>
                    {d.not_in_job.groups.length} {plural(d.not_in_job.groups.length, "group")}, group runs</text>
                  {d.not_in_job.group_runs.map((r) => {
                    const sx = Math.max(LABEL_W, x(r.started_at)), ex = x(r.finished_at ?? d.end);
                    return (
                      <rect key={r.run_id} x={sx} y={y + 10} width={Math.max(3, ex - sx)} height={ROW_H - 20} rx={2} className="run-bar"
                        style={{ fill: GROUP_RUN[r.status] ?? "var(--muted)" }} onClick={() => navigate(`/runs/${r.run_id}`)}>
                        <title>{`${r.task_collection}, group run ${r.run_id}: ${statusName(r.status)}\n${fmtTime(r.started_at)} – ${r.finished_at ? fmtTime(r.finished_at) : "running"}\nClick to open the group run`}</title>
                      </rect>
                    );
                  })}
                </g>
              );
            })()}
          </svg>
        </div>
      )}
    </>
  );
}
