import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, type Row } from "../api";
import { Badge, ErrorBox, Grid, Metric, Notice, Section, autoColumns } from "../components";

export default function Overview() {
  const nav = useNavigate();
  const ov = useQuery({
    queryKey: ["overview"],
    queryFn: () => api.get<{ collections: Row[]; running: Row[] }>("/overview"),
    refetchInterval: 30_000,
  });
  const errors = useQuery({ queryKey: ["errors"], queryFn: () => api.get("/errors?hours=24") });
  const cols = ov.data?.collections ?? [];
  const sum = (k: string) => cols.reduce((a, r) => a + Number(r[k] || 0), 0);
  return (
    <>
      <h1>Overview</h1>
      <ErrorBox error={ov.error} />
      <div className="metrics">
        <Metric label="Collections" value={cols.length} />
        <Metric label="Enabled tables" value={sum("enabled_tables")} />
        <Metric label="Failing tables" value={sum("failing_tables")} tone={sum("failing_tables") ? "bad" : "ok"} />
        <Metric label="SLA breached" value={sum("sla_breached_tables")} tone={sum("sla_breached_tables") ? "warn" : "ok"} />
        <Metric label="Seeding" value={sum("seeding_tables")} />
        <Metric label="Runs in progress" value={ov.data?.running.length ?? "…"} />
      </div>
      {sum("no_sla_tables") > 0 && (
        <Notice tone="warn">{sum("no_sla_tables")} enabled tables have no freshness SLA, so they are never flagged as late.
          Set <code>freshness_sla_minutes</code> on the Tables page (bulk edit).</Notice>
      )}
      <Section title="Collections">
        <p className="muted small">Click a collection to see its tables. A table breaches its SLA when its last
          success is older than its <code>freshness_sla_minutes</code>.</p>
        <Grid rows={ov.data?.collections} height={360}
          columns={autoColumns(cols, { last_run_status: { cellRenderer: (p: any) => <Badge value={p.value} /> } })}
          options={{ onRowClicked: (e) => nav(`/tables?collection=${encodeURIComponent(e.data.task_collection)}`) }} />
      </Section>
      {!!ov.data?.running.length && (
        <Section title="Runs in progress">
          <p className="muted small">Tables counts include only tables that needed work (unchanged tables are skipped in bulk).
            A run is flagged when it has taken more than twice its collection's typical time.</p>
          <Grid rows={ov.data.running.map((r) => ({
            collection: r.task_collection, run_id: r.run_id, started: r.started_at,
            elapsed: `${Math.round(r.elapsed_s / 6) / 10} min`,
            typical: r.typical_s ? `${Math.round(r.typical_s / 6) / 10} min` : "",
            status: r.typical_s && r.elapsed_s > 2 * Math.max(r.typical_s, 60) ? "longer than usual" : "running",
            tables_done: r.tables_ok, tables_failed: r.tables_failed, tables_in_progress: r.tables_running,
            job_run_id: r.job_run_id,
          }))} height={200} columns={autoColumns([{ collection: 1, run_id: 1, started: 1, elapsed: 1, typical: 1, status: 1,
            tables_done: 1, tables_failed: 1, tables_in_progress: 1, job_run_id: 1 }], {
            status: { cellRenderer: (p: any) => <Badge value={p.value === "running" ? "running" : "failed"} /> },
          })} />
        </Section>
      )}
      <Section title="Failed tables in the last 24 hours">
        <ErrorBox error={errors.error} />
        {errors.data && errors.data.length === 0 ? <Notice tone="ok">No failed tables in the last 24 hours.</Notice>
          : <Grid rows={errors.data} height={320} columns={autoColumns(errors.data ?? [], { error: { flex: 1, minWidth: 400, tooltipField: "error" } })} />}
      </Section>
    </>
  );
}
