import { useState } from "react";
import { Navigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type Row } from "../api";
import { ErrorBox, Metric, Notice, OutLink, PageHead, Section, fmtTime, plural } from "../components";
import { usePermissions } from "../permissions";
import { useSettings } from "../App";
import { exportHref, schemaNote, type SchemaState } from "./manageHelpers";

// Manage the app: for those with Can Manage on the app (its Databricks permission). The app's version, the export of
// the ingestion jobs' tables as the CSV Create reads, and the Databricks jobs the app runs them with.

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

function DatabricksJobs() {
  const list = useQuery({ queryKey: ["manage-jobs"], queryFn: () => api.get<Row[]>("/manage/jobs") });
  return (
    <Section title="Databricks jobs">
      <ErrorBox error={list.error} />
      <div className="table-wrap"><table className="data">
        <thead><tr><th>Used for</th><th>Databricks job</th><th>Job ID</th></tr></thead>
        <tbody>{(list.data ?? []).map((job) => (
          <tr key={job.job_id}>
            <td>{job.role}</td>
            <td>{job.url ? <OutLink href={job.url}>{job.name ?? `Job ${job.job_id}`}</OutLink> : job.name ?? `Job ${job.job_id}`}
              {job.error && <span className="sub">{job.error}</span>}</td>
            <td className="mono">{job.job_id}</td>
          </tr>
        ))}</tbody>
      </table></div>
    </Section>
  );
}

interface AppVersion { app: string | null; version: string | null; deployment: Row; error: string | null; schema: SchemaState }

function Version() {
  // Read again while the setup job upgrades the state database, so the page shows when it is done.
  const version = useQuery({ queryKey: ["manage-version"], queryFn: () => api.get<AppVersion>("/manage/version"),
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

export default function Manage() {
  const permissions = usePermissions();
  if (permissions.isPending) return null;
  // Only those who manage the app see it; anyone else who follows a link lands on Overview.
  if (permissions.data?.manage !== null) return <Navigate to="/overview" replace />;
  return (
    <>
      <PageHead title="Manage the app" />
      <Version />
      <Export />
      <DatabricksJobs />
    </>
  );
}
