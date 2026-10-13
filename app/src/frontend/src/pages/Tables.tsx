import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, qs, type Row } from "../api";
import { useSettings } from "../App";
import { Badge, ConfirmButton, ErrorBox, Grid, Notice, Section, autoColumns } from "../components";

const FILTERS = ["all", "failing", "stale", "seeding", "disabled"];

export default function Tables() {
  const { staleMinutes } = useSettings();
  const [params, setParams] = useSearchParams();
  const collection = params.get("collection") ?? "";
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [limit, setLimit] = useState(1000);
  const [selected, setSelected] = useState<Row[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const qc = useQueryClient();
  const collections = useQuery({ queryKey: ["collections"], queryFn: () => api.get<string[]>("/collections") });
  const tables = useQuery({
    queryKey: ["tables", collection, search, status, staleMinutes, limit],
    queryFn: () => api.get(`/tables${qs({ collection, search, status, stale_minutes: staleMinutes, limit })}`),
  });
  const act = useMutation({
    mutationFn: ({ path, body }: { path: string; body: Row }) => api.post<{ updated: number }>(path, body),
    onSuccess: (r, v) => {
      setMessage(`${v.path.endsWith("reseed") ? "Reseed requested for" : "Updated"} ${r.updated} tables.`);
      qc.invalidateQueries();
    },
  });
  const ids = selected.map((r) => Number(r.id));
  return (
    <>
      <h1>Tables</h1>
      <div className="toolbar">
        <label>Collection
          <select value={collection} onChange={(e) => setParams(e.target.value ? { collection: e.target.value } : {})}>
            <option value="">(all)</option>
            {collections.data?.map((c) => <option key={c}>{c}</option>)}
          </select>
        </label>
        <label className="grow">Search
          <input placeholder="database, table, sink or client_id" value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
        <label>Show
          <select value={status} onChange={(e) => setStatus(e.target.value)}>{FILTERS.map((f) => <option key={f}>{f}</option>)}</select>
        </label>
        <label>Max rows
          <input type="number" min={50} max={20000} step={50} value={limit} onChange={(e) => setLimit(Number(e.target.value) || 1000)} />
        </label>
      </div>
      <ErrorBox error={tables.error || act.error} />
      {message && <Notice tone="ok">{message}</Notice>}
      <Section title={`${tables.data?.length ?? "…"} tables`} actions={ids.length > 0 && (
        <>
          <span className="muted">{ids.length} selected</span>
          <ConfirmButton label="Enable" onConfirm={() => act.mutateAsync({ path: "/tables/enable", body: { ids, enabled: true } })} />
          <ConfirmButton label="Disable" onConfirm={() => act.mutateAsync({ path: "/tables/enable", body: { ids, enabled: false } })} />
          <ConfirmButton label="Force reseed" danger
            onConfirm={() => act.mutateAsync({ path: "/tables/reseed", body: { ids } })} />
        </>
      )}>
        <p className="muted small">Select rows to enable, disable, or force a reseed (the next run copies them again).</p>
        <Grid rows={tables.data} height={560} selection="multiple" onSelect={setSelected} getRowId={(r) => String(r.id)}
          columns={autoColumns(tables.data ?? [], {
            status: { cellRenderer: (p: any) => <Badge value={p.value} /> },
            phase: { cellRenderer: (p: any) => <Badge value={p.value} /> },
            detail: { flex: 1, minWidth: 300, tooltipField: "detail" },
          })} />
      </Section>
    </>
  );
}
