import { useState } from "react";
import { Navigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type Row } from "../api";
import { Cols, Cut, CutCopy, ErrorBox, Metric, Notice, OutLink, PageHead, Section, fmtTime, oneLine, plural } from "../components";
import { usePermissions } from "../permissions";
import { useSettings } from "../App";
import {
  exportHref, schemaNote, warehouseChosenBy, warehouseKind, type SchemaState, type WarehouseInUse,
} from "./manageHelpers";

// Manage the app: for those with Can Manage on the app (its Databricks permission). The app's version, the SQL
// warehouse its queries run on, the export of the ingestion jobs' tables as the CSV Create reads, and the Databricks
// jobs the app runs them with.

function Export() {
  const list = useQuery({ queryKey: ["schedules"], queryFn: () => api.get<Row[]>("/schedules") });
  const [picked, setPicked] = useState<number[]>([]);
  const toggle = (id: number) => setPicked((now) => now.includes(id) ? now.filter((x) => x !== id) : [...now, id]);
  return (
    <Section title="Export as CSV">
      <ErrorBox error={list.error} />
      <p className="muted small">The tables of the ingestion jobs ticked, or of every one, in the columns Create from one CSV
        reads, each line naming its job. Each job's lines import on their own as a new ingestion job.</p>
      <div className="toolbar">
        {(list.data ?? []).map((job) => (
          <label key={job.id} className="checkbox">
            <input type="checkbox" checked={picked.includes(Number(job.id))} onChange={() => toggle(Number(job.id))} /> {job.name}
          </label>
        ))}
      </div>
      <button className="primary" onClick={() => { window.location.href = exportHref(picked); }}>
        {picked.length ? `Export ${picked.length} ${plural(picked.length, "ingestion job")}` : "Export all ingestion jobs"}
      </button>
    </Section>
  );
}

/** The Databricks jobs list as the Jobs list keeps its columns: the job takes the spare width, the id has its own. */
const JOB_WIDTHS = [220, null, 190] as const;
const WAREHOUSE_WIDTHS = [null, 220, 220, 110, 240] as const;

function DatabricksJobs() {
  const list = useQuery({ queryKey: ["manage-jobs"], queryFn: () => api.get<Row[]>("/manage/jobs") });
  return (
    <Section title="Databricks jobs">
      <ErrorBox error={list.error} />
      <div className="table-wrap"><table {...oneLine(JOB_WIDTHS)}>
        <Cols widths={JOB_WIDTHS} />
        <thead><tr><th>Used for</th><th>Databricks job</th><th>Job ID</th></tr></thead>
        <tbody>{(list.data ?? []).map((job) => (
          <tr key={job.job_id}>
            <td><Cut text={job.role} tipOnlyWhenCut /></td>
            <td>{job.url ? <OutLink href={job.url}>{job.name ?? `Job ${job.job_id}`}</OutLink> : <Cut text={job.name ?? `Job ${job.job_id}`} tipOnlyWhenCut />}
              {job.error && <Cut text={job.error} className="cut sub" />}</td>
            <td className="mono"><CutCopy text={String(job.job_id)} label={`Copy the job ID ${job.job_id}`} /></td>
          </tr>
        ))}</tbody>
      </table></div>
    </Section>
  );
}

interface AppVersion {
  app: string | null; version: string | null; deployment: Row; error: string | null; schema: SchemaState;
  warehouse?: WarehouseInUse;
}

const versionQuery = { queryKey: ["manage-version"], queryFn: () => api.get<AppVersion>("/manage/version") };

function Version() {
  // Read again while the setup job upgrades the state database, so the page shows when it is done.
  const version = useQuery({ ...versionQuery,
    refetchInterval: (q) => (q.state.data as AppVersion | undefined)?.schema?.run ? 15_000 : false });
  const { config } = useSettings();
  const v = version.data, deployment = v?.deployment ?? {}, schema = v?.schema, note = schemaNote(schema);
  return (
    <Section title="Version">
      <ErrorBox error={version.error} />
      {v && <div className="metrics">
        <Metric label="App" value={<span className="mono">{v.app ?? "–"}</span>} />
        <Metric label="Deployed from" value={v.version ? <span className="mono">{v.version.slice(0, 12)}</span> : "Not recorded"} />
        <Metric label="Deployed" value={deployment.created ? fmtTime(deployment.created) : "–"} />
        <Metric label="Deployed by" value={deployment.creator ?? "–"} />
        <Metric label="State database schema" tone={schema?.database != null && schema.database < schema.app ? "warn" : undefined}
          value={schema?.database != null ? `${schema.database} (the app's: ${schema.app})` : "–"} />
      </div>}
      {note && <Notice tone={note.tone}>{note.text}{schema?.run?.url && <> <OutLink href={schema.run.url}>View run</OutLink></>}</Notice>}
      {v?.error && <p className="muted small">The app's deployment can't be read: {v.error}.</p>}
      {config && <p className="muted small">State database: Lakebase instance <span className="mono">{config.lakebase_instance}</span>,
        database <span className="mono">{config.lakebase_database}</span>.</p>}
    </Section>
  );
}

// Read only: the warehouse is set in the bundle (app_warehouse_id), not here.
function Warehouse() {
  const warehouse = useQuery(versionQuery).data?.warehouse;
  if (!warehouse) return null;
  return (
    <Section title="SQL warehouse">
      <p className="muted small">Where the app runs its queries on the sources: the connection test, the database list,
        the access checks and the table reads. Set in the bundle, by the app_warehouse_id variable.</p>
      <div className="table-wrap"><table {...oneLine(WAREHOUSE_WIDTHS)}>
        <Cols widths={WAREHOUSE_WIDTHS} />
        <thead><tr><th>SQL warehouse</th><th>Warehouse ID</th><th>Type</th><th>State</th><th>Set by</th></tr></thead>
        <tbody><tr>
          <td><Cut text={warehouse.name ?? "Not known"} tipOnlyWhenCut /></td>
          <td className="mono">{warehouse.id ? <CutCopy text={String(warehouse.id)} label="Copy the warehouse ID" /> : <span className="muted">Not known</span>}</td>
          <td><Cut text={warehouseKind(warehouse)} tipOnlyWhenCut /></td>
          <td>{warehouse.state ?? "Not known"}</td>
          <td><Cut text={warehouseChosenBy(warehouse)} tipOnlyWhenCut /></td>
        </tr></tbody>
      </table></div>
      {warehouse.problem && <Notice tone="warn">{warehouse.problem}</Notice>}
    </Section>
  );
}

export default function Manage() {
  const permissions = usePermissions();
  if (permissions.isPending) return null;
  // Only those who manage the app see it; anyone else who follows a link lands on Overview.
  if (permissions.data?.manage !== null) return <Navigate to="/overview" replace />;
  return (
    <>
      <PageHead title="Manage the app" />
      <Version />
      <Warehouse />
      <Export />
      <DatabricksJobs />
    </>
  );
}
