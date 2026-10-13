import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, qs, type Row } from "../api";
import { Badge, ErrorBox, Grid, Section, autoColumns } from "../components";

export default function Runs() {
  const [collection, setCollection] = useState("");
  const [run, setRun] = useState<Row | null>(null);
  const collections = useQuery({ queryKey: ["collections"], queryFn: () => api.get<string[]>("/collections") });
  const runs = useQuery({
    queryKey: ["runs", collection],
    queryFn: () => api.get(`/runs${qs({ collection, limit: 200 })}`),
    refetchInterval: 30_000,
  });
  const events = useQuery({
    queryKey: ["events", run?.run_id],
    queryFn: () => api.get(`/runs/${run!.run_id}/events`),
    enabled: !!run,
  });
  const failed = (events.data ?? []).filter((e) => e.status === "failed").length;
  const statusCol = { cellRenderer: (p: any) => <Badge value={p.value} /> };
  return (
    <>
      <h1>Runs</h1>
      <div className="toolbar">
        <label>Collection
          <select value={collection} onChange={(e) => { setCollection(e.target.value); setRun(null); }}>
            <option value="">(all)</option>
            {collections.data?.map((c) => <option key={c}>{c}</option>)}
          </select>
        </label>
      </div>
      <ErrorBox error={runs.error} />
      <Section title="Recent runs">
        <p className="muted small">Select a run to see its tables.</p>
        <Grid rows={runs.data} height={340} selection="single" onSelect={(r) => setRun(r[0] ?? null)}
          columns={autoColumns(runs.data ?? [], { status: statusCol })} getRowId={(r) => String(r.run_id)} />
      </Section>
      {run && (
        <Section title={`Run ${run.run_id}: ${events.data?.length ?? "…"} table events, ${failed} failed`}>
          <p className="muted small">Tables found unchanged by change detection are counted in the run's
            "skipped" without an event.</p>
          <ErrorBox error={events.error} />
          <Grid rows={events.data} height={420}
            columns={autoColumns(events.data ?? [], { status: statusCol, error: { flex: 1, minWidth: 400, tooltipField: "error" } })} />
        </Section>
      )}
    </>
  );
}
