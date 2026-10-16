import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Row } from "../api";
import { Badge, ConfirmButton, ErrorBox, Grid, Notice, Section, fmtTime } from "../components";

// A schedule runs one or more collections at a cadence. The app turns each (schedule, collection)
// into a small Databricks job that starts the ingestion job; editing here keeps those jobs in sync.

interface Options { cadences: number[]; parameters: string[]; variants: string[]; collections: string[];
  scheduled: Record<string, string[]>; view_group: string | null }

const CADENCE_LABEL = (m: number) => (m < 60 ? `every ${m} minutes` : m === 60 ? "hourly" : m === 1440 ? "daily" : `every ${m / 60} hours`);
const mins = (s: unknown) => (s === null || s === undefined ? "" : `${Math.round((Number(s) / 60) * 10) / 10} min`);

function Editor({ initial, options, onDone }: { initial?: Row; options: Options; onDone: () => void }) {
  const [f, setF] = useState<Row>(initial ? {
    name: initial.name, cadence_minutes: initial.cadence_minutes, timezone: initial.timezone, stagger: initial.stagger,
    paused: initial.paused, notify_emails: initial.notify_emails ?? "", job_variant: initial.job_variant,
    parallelism: initial.parameters?.parallelism ?? "", members: initial.members.map((m: Row) => m.task_collection),
  } : { name: "", cadence_minutes: 15, timezone: "UTC", stagger: true, paused: false, notify_emails: "",
        job_variant: "default", parallelism: "", members: [] });
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: () => {
      const body = { name: f.name, cadence_minutes: Number(f.cadence_minutes), timezone: f.timezone, stagger: f.stagger,
        paused: f.paused, notify_emails: f.notify_emails || null, job_variant: f.job_variant, members: f.members,
        parameters: f.parallelism ? { parallelism: String(f.parallelism) } : {} };
      return initial ? api.post(`/schedules/${initial.id}`, body) : api.post("/schedules", body);
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["schedules"] }); onDone(); },
  });
  const toggle = (c: string) => setF({ ...f, members: f.members.includes(c) ? f.members.filter((x: string) => x !== c) : [...f.members, c] });
  return (
    <div className="drawer">
      <div className="drawer-head">
        <h2>{initial ? `Edit schedule: ${initial.name}` : "New schedule"}</h2>
        <button className="ghost" onClick={onDone}>Close</button>
      </div>
      <form className="form-grid" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <label>Name *<input value={f.name} required onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
        <label>Cadence
          <select value={f.cadence_minutes} onChange={(e) => setF({ ...f, cadence_minutes: Number(e.target.value) })}>
            {options.cadences.map((c) => <option key={c} value={c}>{CADENCE_LABEL(c)}</option>)}
          </select>
        </label>
        <label>Time zone<input value={f.timezone} onChange={(e) => setF({ ...f, timezone: e.target.value })} />
          <span className="muted small">IANA name; UTC avoids daylight-saving gaps for 24/7 schedules</span></label>
        <label>Parallelism<input type="number" min={1} max={256} value={f.parallelism} placeholder="job default"
          onChange={(e) => setF({ ...f, parallelism: e.target.value })} /></label>
        {options.variants.length > 1 && (
          <label>Ingestion job
            <select value={f.job_variant} onChange={(e) => setF({ ...f, job_variant: e.target.value })}>
              {options.variants.map((v) => <option key={v}>{v}</option>)}
            </select>
          </label>
        )}
        <label>Failure emails<input value={f.notify_emails} placeholder="a@example.com, b@example.com"
          onChange={(e) => setF({ ...f, notify_emails: e.target.value })} /></label>
        <label className="checkbox"><input type="checkbox" checked={f.stagger} onChange={(e) => setF({ ...f, stagger: e.target.checked })} />
          Stagger start times across the cadence</label>
        <label className="checkbox"><input type="checkbox" checked={f.paused} onChange={(e) => setF({ ...f, paused: e.target.checked })} />
          Paused</label>
        <div className="wide">
          <div className="muted small">Collections * ({f.members.length} selected). Each gets its own job, so one slow
            collection never delays the others.</div>
          <div className="checklist">
            {options.collections.map((c) => {
              const elsewhere = (options.scheduled[c] ?? []).filter((n) => n !== initial?.name);
              return (
                <label key={c} className="checkbox">
                  <input type="checkbox" checked={f.members.includes(c)} onChange={() => toggle(c)} />
                  {c}{elsewhere.length > 0 && <span className="muted small">also in {elsewhere.join(", ")}</span>}
                </label>
              );
            })}
          </div>
        </div>
        <div className="wide"><button className="primary" disabled={save.isPending || !f.members.length}>
          {save.isPending ? "Saving and syncing jobs…" : initial ? "Save and sync jobs" : "Create schedule and jobs"}</button></div>
      </form>
      <ErrorBox error={save.error} />
    </div>
  );
}

function SyncResult({ result }: { result?: Row }) {
  if (!result?.sync) return null;
  const errors = (result.sync as Row[]).filter((r) => r.action === "error");
  const changed = (result.sync as Row[]).filter((r) => ["created", "updated", "deleted"].includes(r.action));
  return errors.length ? <Notice tone="bad">Job sync failed for {errors.map((e) => `${e.task_collection}: ${e.detail}`).join("; ")}</Notice>
    : <Notice tone="ok">Jobs in sync{changed.length ? `: ${changed.map((c) => `${c.task_collection} ${c.action}`).join(", ")}` : ""}.</Notice>;
}

function ScheduleCard({ s, onEdit }: { s: Row; onEdit: () => void }) {
  const qc = useQueryClient();
  const [drift, setDrift] = useState<Row | null>(null);
  const act = useMutation({
    mutationFn: (path: string) => api.post(`/schedules/${s.id}/${path}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["schedules"] }),
  });
  const check = useMutation({ mutationFn: () => api.get<Row>(`/schedules/${s.id}/check`), onSuccess: setDrift });
  const rows = (s.members as Row[]).map((m) => ({
    collection: m.task_collection, starts: m.when, next_run: s.paused ? "paused" : fmtTime(m.next_run),
    last_run: m.last_status, last_started: fmtTime(m.last_started), last_duration: mins(m.last_duration_s),
    p95_7d: mins(m.p95_s), runs_24h: m.runs_24h, failed_24h: m.failed_24h, skipped_24h: m.skipped_24h ?? "",
    strictest_sla: m.min_sla_minutes ? `${m.min_sla_minutes} min` : "", job: m.job_url, sync: m.sync_error ? "error" : m.job_id ? "ok" : "not synced",
    sync_drift: drift?.[m.task_collection]?.status ?? "", warnings: (m.warnings as string[]).join(" · "),
  }));
  const warned = rows.filter((r) => r.warnings).length;
  return (
    <Section title={`${s.name}: ${s.description}`} actions={<>
      {s.paused ? <Badge value="paused" /> : <Badge value="ok" />}
      <button onClick={onEdit}>Edit</button>
      <button onClick={() => act.mutate(s.paused ? "resume" : "pause")} disabled={act.isPending}>{s.paused ? "Resume" : "Pause"}</button>
      <button onClick={() => check.mutate()} disabled={check.isPending}>Check jobs</button>
      <button onClick={() => act.mutate("sync")} disabled={act.isPending}>Sync jobs</button>
      <ConfirmButton label="Delete" danger onConfirm={() => act.mutateAsync("delete")} />
    </>}>
      <p className="muted small">Time zone {s.timezone}{s.parameters?.parallelism ? ` · parallelism ${s.parameters.parallelism}` : ""}
        {s.notify_emails ? ` · failure emails to ${s.notify_emails}` : ""} · updated {fmtTime(s.updated_at)}{s.created_by ? ` · created by ${s.created_by}` : ""}</p>
      {warned > 0 && <Notice tone="warn">{warned} collection(s) have warnings: see the warnings column.</Notice>}
      <ErrorBox error={act.error || check.error} />
      <SyncResult result={act.data} />
      <Grid rows={rows} height={Math.min(90 + rows.length * 42, 380)} columns={[
        { field: "collection", pinned: "left" }, { field: "starts" }, { field: "next_run" },
        { field: "last_run", cellRenderer: (p: any) => <Badge value={p.value} /> }, { field: "last_started" }, { field: "last_duration" },
        { field: "p95_7d", headerName: "p95 (7 days)" }, { field: "runs_24h" }, { field: "failed_24h" }, { field: "skipped_24h" },
        { field: "strictest_sla" },
        { field: "job", cellRenderer: (p: any) => (p.value ? <a href={p.value} target="_blank" rel="noreferrer">open</a> : null) },
        { field: "sync", cellRenderer: (p: any) => <Badge value={p.value === "ok" ? "ok" : "failed"} /> },
        { field: "sync_drift", headerName: "check", cellRenderer: (p: any) => (p.value ? <Badge value={p.value === "in_sync" ? "ok" : "failed"} /> : null) },
        { field: "warnings", flex: 1, minWidth: 320, tooltipField: "warnings" },
      ]} />
    </Section>
  );
}

export default function Schedules() {
  const list = useQuery({ queryKey: ["schedules"], queryFn: () => api.get<Row[]>("/schedules"), refetchInterval: 30_000 });
  const options = useQuery({ queryKey: ["schedule-options"], queryFn: () => api.get<Options>("/schedules/options") });
  const [editing, setEditing] = useState<Row | "new" | null>(null);
  const qc = useQueryClient();
  const close = () => { setEditing(null); qc.invalidateQueries({ queryKey: ["schedule-options"] }); };
  return (
    <>
      <h1>Schedules</h1>
      <p className="muted">Each collection in a schedule gets its own Databricks job that starts the ingestion job at the
        schedule's cadence. A run still going at the next trigger makes Databricks skip that trigger (shown as skipped), so a
        slow collection never piles up runs. Edit schedules here: changes made in the Jobs UI are reported by "Check jobs" and
        undone by the next sync.</p>
      <ErrorBox error={list.error || options.error} />
      <div className="toolbar"><button className="primary" disabled={!options.data} onClick={() => setEditing("new")}>New schedule</button></div>
      {list.data && list.data.length === 0 && <Notice>No schedules yet.</Notice>}
      {list.data?.map((s) => <ScheduleCard key={s.id} s={s} onEdit={() => setEditing(s)} />)}
      {editing && options.data && (
        <Editor initial={editing === "new" ? undefined : editing} options={options.data} onDone={close} />
      )}
    </>
  );
}
