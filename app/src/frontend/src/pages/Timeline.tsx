import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, qs, type Row } from "../api";
import { ErrorBox, Notice, fmtTime } from "../components";

// Runs per collection over a window: planned triggers (ticks), runs (bars by outcome) and triggers
// Databricks skipped because the previous run was still going (red markers).

interface Timeline { start: string; end: string; collections: { task_collection: string; runs: Row[]; planned: string[]; skipped: string[]; schedules: string[] }[] }

const LABEL_W = 230, CHART_W = 1000, ROW_H = 26, AXIS_H = 26;
const COLOR: Record<string, string> = { completed: "#0f7b4f", failed: "#b42318", running: "#2563eb", skipped: "#d4a017" };

export default function TimelinePage() {
  const [hours, setHours] = useState(24);
  const t = useQuery({ queryKey: ["timeline", hours], queryFn: () => api.get<Timeline>(`/timeline${qs({ hours })}`), refetchInterval: 60_000 });
  const d = t.data;
  const t0 = d ? new Date(d.start).getTime() : 0, t1 = d ? new Date(d.end).getTime() : 1;
  const x = (iso: string | number) => LABEL_W + ((new Date(iso).getTime() - t0) / (t1 - t0)) * CHART_W;
  const rows = d?.collections ?? [];
  const height = AXIS_H + rows.length * ROW_H + 8;
  const step = hours <= 6 ? 1 : hours <= 24 ? 2 : 6;
  const ticks: number[] = [];
  if (d) {
    const first = new Date(t0); first.setMinutes(0, 0, 0);
    for (let ms = first.getTime() + 3600_000; ms < t1; ms += step * 3600_000) ticks.push(ms);
  }
  const stats = rows.reduce((a, r) => ({ runs: a.runs + r.runs.length, failed: a.failed + r.runs.filter((x) => x.status === "failed").length,
    skipped: a.skipped + r.skipped.length }), { runs: 0, failed: 0, skipped: 0 });
  return (
    <>
      <h1>Timeline</h1>
      <div className="toolbar">
        <label>Window
          <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            {[6, 12, 24, 48, 168].map((h) => <option key={h} value={h}>last {h < 48 ? `${h} hours` : `${h / 24} days`}</option>)}
          </select>
        </label>
        <span className="legend">
          <i style={{ background: COLOR.completed }} /> completed <i style={{ background: COLOR.failed }} /> failed
          <i style={{ background: COLOR.running }} /> running <i style={{ background: COLOR.skipped }} /> skipped (collection already running)
          <i className="tick" /> planned trigger <b className="skipmark">▼</b> trigger skipped by Databricks
        </span>
      </div>
      <ErrorBox error={t.error} />
      {d && <p className="muted small">{stats.runs} runs, {stats.failed} failed, {stats.skipped} skipped triggers, {fmtTime(d.start)} – {fmtTime(d.end)}.
        Hover a bar for details.</p>}
      {d && rows.length === 0 && <Notice>No runs or schedules in this window.</Notice>}
      {d && rows.length > 0 && (
        <div className="card timeline">
          <svg viewBox={`0 0 ${LABEL_W + CHART_W + 10} ${height}`} width="100%" role="img" aria-label="Runs per collection over time">
            {ticks.map((ms) => (
              <g key={ms}>
                <line x1={x(ms)} x2={x(ms)} y1={AXIS_H - 4} y2={height} stroke="#e2e6eb" />
                <text x={x(ms)} y={14} fontSize={11} textAnchor="middle" fill="#5f6b7a">
                  {new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                </text>
              </g>
            ))}
            {rows.map((r, i) => {
              const y = AXIS_H + i * ROW_H;
              return (
                <g key={r.task_collection}>
                  <rect x={0} y={y} width={LABEL_W + CHART_W + 10} height={ROW_H} fill={i % 2 ? "#fafbfc" : "#fff"} />
                  <text x={6} y={y + 17} fontSize={12} fill="#1d2433">
                    {r.task_collection.length > 30 ? r.task_collection.slice(0, 29) + "…" : r.task_collection}
                    <title>{r.task_collection}{r.schedules.length ? `\n${r.schedules.join("\n")}` : "\nnot scheduled"}</title>
                  </text>
                  {r.planned.map((p) => <line key={p} x1={x(p)} x2={x(p)} y1={y + 3} y2={y + ROW_H - 3} stroke="#9aa5b1" strokeWidth={1} />)}
                  {r.runs.map((run) => {
                    const sx = Math.max(LABEL_W, x(run.started_at)), ex = x(run.finished_at ?? d.end);
                    return (
                      <rect key={run.run_id} x={sx} y={y + 6} width={Math.max(2, ex - sx)} height={ROW_H - 12} rx={2}
                        fill={COLOR[run.status] ?? "#5f6b7a"} opacity={0.85}>
                        <title>{`run ${run.run_id}: ${run.status}\n${fmtTime(run.started_at)} – ${run.finished_at ? fmtTime(run.finished_at) : "running"}`
                          + `\n${Math.round(((run.finished_at ? new Date(run.finished_at).getTime() : t1) - new Date(run.started_at).getTime()) / 6000) / 10} min`
                          + (run.ok !== null && run.ok !== undefined ? `\nok ${run.ok}, failed ${run.failed}, unchanged ${run.skipped}` : "")}</title>
                      </rect>
                    );
                  })}
                  {r.skipped.map((s) => (
                    <text key={s} x={x(s)} y={y + 10} fontSize={11} textAnchor="middle" fill="#b42318">▼<title>{`trigger skipped at ${fmtTime(s)}: previous run still going`}</title></text>
                  ))}
                </g>
              );
            })}
          </svg>
        </div>
      )}
    </>
  );
}
