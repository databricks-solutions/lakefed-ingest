import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, type Row } from "../api";
import { valueName,
  Crumbs, ErrorBox, FillBox, PageHead, ProgressBar, ProgressKey, Section, Stamp, StateBar, StateKey, Tag, databasePath,
  fmtAgo, fmtClock, fmtDuration, fmtInt, progressOf, progressText, type Progress, plural
} from "../components";

interface CollectionData { collection: string; databases: Row[]; running: Row | null; batches: Row[]; jobs: Row[]; read_at: string; cache_s: number }

/** What a database's first loads wrote: per-table counts, a batched append's total, or "at least". */
function firstLoadRows(d: Row) {
  const counted = d.rows_first_loads === null || d.rows_first_loads === undefined ? null : Number(d.rows_first_loads);
  const batched = (d.batches ?? []).reduce((a: number, b: Row) => a + Number(b.rows || 0), 0);
  const uncounted = Number(d.first_loads_uncounted || 0);
  if (!Number(d.first_loads)) return <span className="muted">no first load yet</span>;
  if (batched) {
    return <>{fmtInt((counted ?? 0) + batched)}<span className="sub">{counted ? `${fmtInt(counted)} per table + ` : ""}
      {fmtInt(batched)} in batches (run {(d.batches as Row[]).map((b) => `#${b.run_id}`).join(", ")})</span></>;
  }
  if (uncounted) return <>{counted ? `at least ${fmtInt(counted)}` : "not recorded"}<span className="sub">{fmtInt(uncounted)} loads with no count</span></>;
  return fmtInt(counted);
}

export default function CollectionPage() {
  const { name = "" } = useParams();
  const data = useQuery({
    queryKey: ["collection", name],
    queryFn: () => api.get<CollectionData>(`/collections/${encodeURIComponent(name)}/databases`),
    refetchInterval: 15_000,
  });
  const dbs = data.data?.databases ?? [];
  const run = data.data?.running ?? null;
  const p = run ? progressOf(run) : null;
  const tables = dbs.reduce((a, d) => a + Number(d.tables || 0), 0);
  const servers = [...new Set(dbs.map((d) => d.server).filter(Boolean))];
  const totalFirst = dbs.reduce((a, d) => a + Number(d.rows_first_loads || 0)
    + (d.batches ?? []).reduce((x: number, b: Row) => x + Number(b.rows || 0), 0), 0);
  const unattributed = (data.data?.batches ?? []).filter((b) => (b.keys ?? []).length !== 1);
  return (
    <>
      <PageHead crumb={<Crumbs steps={[["Ingestion jobs", "/ingestion-jobs"],
          ...(data.data?.jobs?.length ? (data.data.jobs as Row[]).map((j): [string, string] => [j.name, `/ingestion-jobs/${j.id}`])
            : data.data ? [["Not in any job", "/ingestion-jobs/not-in-job"] as [string, string]] : []),
          ["Ingestion group"]]} />} title={name}
        lead={<><strong>{fmtInt(dbs.length)}</strong> {plural(dbs.length, "database")}, <strong>{fmtInt(tables)}</strong> enabled {plural(tables, "table")}
          {servers.length ? <> on <strong>{servers.join(", ")}</strong></> : null}.{" "}
          <Link to={`/tables?collection=${encodeURIComponent(name)}`}>Its tables</Link> ·{" "}
          <Link to="/schedules">Schedules</Link></>}
        right={<Stamp at={data.data?.read_at} every={run ? 15 : 60} error={data.error} />} />
      <ErrorBox error={data.error} />

      {run && p && (
        <Section title={<><Link to={`/runs/${run.run_id}`}>Group run {fmtInt(run.run_id)}</Link> <Tag tone="run">Running</Tag></>}
          actions={<div className="kv"><span>Started <b>{fmtClock(run.started_at)}</b></span>
            <span>Running <b>{fmtDuration(run.elapsed_s)}</b>{run.p50_s ? <> (typical <b>{fmtDuration(run.p50_s)}</b>)</> : null}</span></div>}>
          <div className="count">{progressText(p)}</div><ProgressBar p={p} big /><ProgressKey p={p} />
        </Section>
      )}

      <Section title="Databases" actions={<button className="ghost" onClick={() => download(name, dbs)}>Download CSV</button>}
        note={<>Rows in first loads are what each table's first load wrote, which is the source's row count at that moment, so the
          total can be checked against a count taken at the source. Rows in changes are written since, in the last 24 hours.</>}>
        <FillBox>
          <table className="data">
            <thead><tr><th>Database</th>{run && <th>This run</th>}{run && <th className="num">Checked</th>}{run && <th className="num">In flight</th>}
              <th>State now</th><th className="num">Failing</th><th className="num">First load</th><th className="num">Oldest success</th>
              <th className="num">Rows in first loads</th><th className="num">Rows in changes, 24 h</th></tr></thead>
            <tbody>
              {dbs.map((d) => {
                const tablesHere = Number(d.tables || 0);
                const checked = Number(d.checked_this_run || 0);
                const loaded = Number(d.loaded_this_run || 0);
                const dp: Progress = { total: tablesHere, unchanged: Math.max(0, checked - loaded), loaded,
                                       inFlight: Number(d.in_flight || 0), failed: Number(d.failed_this_run || 0) };
                return (
                  <tr key={`${d.server}\u001f${d.src_database}`}>
                    <td className="name"><Link to={databasePath(name, d.server, d.src_database)}><b>{d.src_database || "(none)"}</b></Link>
                      {servers.length > 1 && <span className="sub">{d.server || "(no server)"}</span>}</td>
                    {run && <td><ProgressBar p={dp} /></td>}
                    {run && <td className="num">{fmtInt(checked)} of {fmtInt(tablesHere)}</td>}
                    {run && <td className="num">{fmtInt(d.in_flight || 0)}</td>}
                    <td><StateBar row={d} /></td>
                    <td className="num" style={{ color: Number(d.state_failing) ? "var(--bad)" : undefined }}>{fmtInt(d.state_failing)}</td>
                    <td className="num">{fmtInt(d.state_first_load)}</td>
                    <td className="num">{fmtAgo(d.oldest_success_at)}</td>
                    <td className="num">{firstLoadRows(d)}</td>
                    <td className="num">{fmtInt(d.rows_changes_24h || 0)}</td>
                  </tr>
                );
              })}
              {dbs.length > 1 && (
                <tr className="total"><td>{fmtInt(dbs.length)} {plural(dbs.length, "database")}</td>{run && <td />}
                  {run && <td className="num">{fmtInt(dbs.reduce((a, d) => a + Number(d.checked_this_run || 0), 0))} of {fmtInt(tables)}</td>}
                  {run && <td className="num">{fmtInt(dbs.reduce((a, d) => a + Number(d.in_flight || 0), 0))}</td>}
                  <td /><td className="num">{fmtInt(dbs.reduce((a, d) => a + Number(d.state_failing || 0), 0))}</td>
                  <td className="num">{fmtInt(dbs.reduce((a, d) => a + Number(d.state_first_load || 0), 0))}</td><td />
                  <td className="num">{fmtInt(totalFirst)}</td>
                  <td className="num">{fmtInt(dbs.reduce((a, d) => a + Number(d.rows_changes_24h || 0), 0))}</td></tr>
              )}
            </tbody>
          </table>
        </FillBox>
        {run ? <ProgressKey /> : <StateKey />}
        {unattributed.length > 0 && <p className="small muted" style={{ marginTop: 8 }}>Batched appends over several databases wrote{" "}
          {unattributed.map((b) => `${fmtInt(b.rows)} rows in run #${b.run_id}`).join(", ")}; batches record one total, so these
          are not shown per database.</p>}
      </Section>
      <GroupSettings name={name} />
    </>
  );
}

const SETTING_LABELS: Record<string, string> = {
  src_type: "Source type", use_remote_query: "Read through the UC connection", load_type: "Load type",
  write_mode: "Write mode", fan_in: "Fan-in to shared tables", sink_catalog: "Target catalog", sink_schema: "Target schema",
  sink_table: "Target table", freshness_sla_minutes: "Freshness target (minutes)", jdbc_options: "Read options",
};

/** The group's settings as its rows share them (no table of groups), and its tables as the import CSV. */
function GroupSettings({ name }: { name: string }) {
  const settings = useQuery({
    queryKey: ["collection-settings", name],
    queryFn: () => api.get<{ tables: number; settings: Row[] }>(`/collections/${encodeURIComponent(name)}/settings`),
  });
  const show = (v: unknown) => (v === null || v === undefined || v === "" ? <span className="muted">none</span>
    : typeof v === "boolean" ? (v ? "yes" : "no") : String(v));
  return (
    <Section title="Settings" note="The values every table of the group shares; mixed where they differ. An import into the group takes them."
      actions={<span className="pager-buttons"><Link to={`/import?group=${encodeURIComponent(name)}`}>Import CSV</Link>
        <a href={`/api/collections/${encodeURIComponent(name)}/export.csv`}>Export CSV</a></span>}>
      <ErrorBox error={settings.error} />
      <div className="table-wrap" style={{ maxWidth: 760 }}><table className="data dense">
        <thead><tr><th>Setting</th><th>Value</th></tr></thead>
        <tbody>
          {(settings.data?.settings ?? []).map((s) => (
            <tr key={s.setting}>
              <td>{SETTING_LABELS[s.setting] ?? s.setting}</td>
              <td>{s.mixed ? <>mixed: {Object.entries(s.mixed as Record<string, number>).map(([v, n]) =>
                `${valueName(s.setting, v) || "blank"} (${fmtInt(n)} ${plural(n, "table")})`).join(", ")}</>
                : show(typeof s.value === "string" ? valueName(s.setting, s.value) : s.value)}</td>
            </tr>
          ))}
        </tbody>
      </table></div>
    </Section>
  );
}

function download(name: string, rows: Row[]) {
  const head = ["server", "database", "tables", "current", "late", "first_load", "failing", "oldest_success_at", "rows_first_loads",
                "first_loads_uncounted", "rows_in_batches", "rows_changes_24h"];
  const lines = rows.map((d) => [d.server, d.src_database, d.tables, d.state_current, d.state_late, d.state_first_load, d.state_failing,
    d.oldest_success_at ?? "", d.rows_first_loads ?? "", d.first_loads_uncounted ?? "",
    (d.batches ?? []).reduce((a: number, b: Row) => a + Number(b.rows || 0), 0), d.rows_changes_24h ?? ""].join(","));
  const url = URL.createObjectURL(new Blob([[head.join(","), ...lines].join("\n")], { type: "text/csv" }));
  const a = document.createElement("a");
  a.href = url; a.download = `${name}-databases.csv`; a.click();
  URL.revokeObjectURL(url);
}
