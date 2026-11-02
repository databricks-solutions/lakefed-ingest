import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ColDef } from "ag-grid-community";
import { api, qs, type Row } from "../api";
import { PageHead, Badge, ConfirmButton, ErrorBox, Grid, Info, Section, autoColumns, fmtInt, plural, toast } from "../components";
import { EditBulk, EditOne } from "./EditTables";
import { groupsRefusal, usePermissions } from "../permissions";

/** Each table's freshness target right after whether it is enabled, so it reads with the table, not off to the right. */
function targetAfterEnabled(columns: ColDef[]): ColDef[] {
  const target = columns.find((c) => c.field === "sla_min");
  if (!target) return columns;
  const rest = columns.filter((c) => c !== target);
  const at = rest.findIndex((c) => c.field === "task_enabled");
  return [...rest.slice(0, at + 1), target, ...rest.slice(at + 1)];
}

const FILTERS = ["all", "failing", "sla_breached", "no_sla", "seeding", "disabled"];
const FILTER_LABELS: Record<string, string> = { all: "All tables", failing: "Failing", sla_breached: "Past freshness target",
  no_sla: "No freshness target", seeding: "Waiting for a first load", disabled: "Disabled" };

export default function Tables() {
  const [params, setParams] = useSearchParams();
  const collection = params.get("collection") ?? "";
  const job = params.get("job") ?? "";
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState(FILTERS.includes(params.get("status") ?? "") ? params.get("status")! : "all");
  const database = params.get("database") ?? undefined;
  const [pages, setPages] = useState<string[]>([]);
  const after = pages[pages.length - 1];
  const [selected, setSelected] = useState<Row[]>([]);
  const [editing, setEditing] = useState<"one" | "bulk" | null>(null);
  const qc = useQueryClient();
  // The user's unit is the ingestion job; a group is kept only as a column, and as the filter a group's page links with.
  const jobs = useQuery({ queryKey: ["schedules", "names"], queryFn: () => api.get<Row[]>("/schedules?details=false") });
  const tables = useQuery({
    queryKey: ["tables", job, collection, database, search, status, after],
    queryFn: () => api.get<{ tables: Row[]; limit: number; next_after: unknown[] | null }>(`/tables${qs({ job, collection, database, search, status, after })}`),
  });
  const act = useMutation({
    mutationFn: ({ path, body }: { path: string; body: Row }) => api.post<{ updated: number }>(path, body),
    onSuccess: (r, v) => {
      toast(`${v.path.endsWith("reseed") ? "Reseed requested for" : "Updated"} ${fmtInt(r.updated)} ${plural(r.updated, "table")}`);
      qc.invalidateQueries();
    },
  });
  const ids = selected.map((r) => Number(r.id));
  const locked = groupsRefusal(usePermissions().data, selected.map((r) => r.task_collection));
  return (
    <>
      <PageHead title="Tables" lead="Every table of every ingestion job." />
      <div className="toolbar">
        <label>Ingestion job
          <select value={job} onChange={(e) => { setPages([]); setParams(e.target.value ? { job: e.target.value } : {}); }}>
            <option value="">(all)</option>
            {(jobs.data ?? []).filter((j) => !j.deleted_at).sort((a, b) => String(a.name).localeCompare(String(b.name)))
              .map((j) => <option key={j.id} value={String(j.id)}>{j.name}</option>)}
          </select>
        </label>
        {collection && <span className="tag">group {collection}
          <button className="ghost" onClick={() => { setPages([]); setParams(job ? { job } : {}); }}>clear</button></span>}
        <label className="grow">Search
          <input placeholder="Database, table, target or client ID" value={search} onChange={(e) => { setPages([]); setSearch(e.target.value); }} />
        </label>
        <label>Show
          <select value={status} onChange={(e) => { setPages([]); setStatus(e.target.value); }}>{FILTERS.map((f) => <option key={f} value={f}>{FILTER_LABELS[f]}</option>)}</select>
        </label>
        {database !== undefined && <span className="tag">database {database || "(none)"}
          <button className="ghost" onClick={() => { setPages([]); setParams(collection ? { collection } : {}); }}>clear</button></span>}
      </div>
      <ErrorBox error={tables.error || act.error} />
      <Section title={tables.data ? `Page ${pages.length + 1}: ${tables.data.tables.length} ${plural(tables.data.tables.length, "table")}` : "Tables"}
        actions={<>
          <button className="ghost" disabled={!pages.length} onClick={() => setPages(pages.slice(0, -1))}>Previous page</button>
          <button className="ghost" disabled={!tables.data?.next_after} onClick={() => setPages([...pages, JSON.stringify(tables.data!.next_after)])}>Next page</button>
          {ids.length > 0 && <>
          <span className="muted">{ids.length} selected</span>
          <button className="primary" disabled={!!locked} title={locked ?? undefined} onClick={() => setEditing(ids.length === 1 ? "one" : "bulk")}>
            {ids.length === 1 ? "Edit" : "Bulk edit"}
          </button>
          <ConfirmButton label="Enable" plain disabled={!!locked} title={`Enable ${fmtInt(ids.length)} ${plural(ids.length, "table")}?`}
            message="Their ingestion jobs' next runs load them." onConfirm={() => act.mutateAsync({ path: "/tables/enable", body: { ids, enabled: true } })} />
          <ConfirmButton label="Disable" danger disabled={!!locked} title={`Disable ${fmtInt(ids.length)} ${plural(ids.length, "table")}?`}
            message="Runs stop loading them. Their target tables, checkpoints and history stay, so enabling them again carries on from where they were."
            onConfirm={() => act.mutateAsync({ path: "/tables/enable", body: { ids, enabled: false } })} />
          <ConfirmButton label="Force reseed" danger disabled={!!locked} title={`Reseed ${fmtInt(ids.length)} ${plural(ids.length, "table")}?`}
            message="The next run of each table's ingestion job copies the whole table again from its source. Large tables take a while and load the source."
            confirmLabel="Reseed" onConfirm={() => act.mutateAsync({ path: "/tables/reseed", body: { ids } })} />
          </>}
        </>}>
        <p className="muted small">Select one row to edit it, or several to bulk edit, enable, disable, or force a reseed.
          <Info label="About editing tables">Changes are refused while the table's ingestion job is running. 100 tables a page, by
            ingestion job, database and table; selections are per page.</Info></p>
        {locked && <p className="muted small">Edit, Enable, Disable and Force reseed: {locked}.</p>}
        <Grid rows={tables.data?.tables} filters={false} selection="multiple" onSelect={setSelected} getRowId={(r) => String(r.id)}
          columns={targetAfterEnabled(autoColumns(tables.data?.tables ?? [], {
            ingestion_job: { headerName: "Ingestion job" },
            task_collection: { headerName: "Group" },
            sla_min: { headerName: "Freshness target (min)", minWidth: 180, type: "rightAligned", valueFormatter: (p) => fmtInt(p.value) },
            sla_breached: { headerName: "Past target", cellRenderer: (p: any) => p.value ? <Badge value="breached" /> : null },
            status: { cellRenderer: (p: any) => <Badge value={p.value} /> },
            phase: { cellRenderer: (p: any) => <Badge value={p.value} /> },
            detail: { flex: 1, minWidth: 300, tooltipField: "detail" },
          }))} />
      </Section>
      {editing === "one" && ids.length === 1 && <EditOne id={ids[0]} onClose={() => setEditing(null)} />}
      {editing === "bulk" && ids.length > 0 && <EditBulk ids={ids} onClose={() => setEditing(null)} />}
    </>
  );
}
