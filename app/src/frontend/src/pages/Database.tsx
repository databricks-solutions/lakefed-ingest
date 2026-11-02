import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, qs, type Row } from "../api";
import {
  Crumbs, ErrorBox, FillBox, Info, PageHead, Pager, Section, Stamp, StateKey, StateSummary, Tag, fmtAgo, fmtDuration, fmtInt, plural, type Tone
} from "../components";

const STATE_TONE: Record<string, Tone> = { failing: "bad", "first load": "first", late: "warn", current: "ok", disabled: "" };

/** A table's part in the group run going now: in flight with its step and time, loaded with its rows, failed, to go,
 * or unchanged (stamped since the run started). */
function RunPart({ t, running }: { t: Row; running: boolean }) {
  if (!running) return null;
  if (t.run_status === "running") return <Tag tone="run">In flight: {t.run_stage || "starting"}, {fmtDuration(t.run_running_s)}</Tag>;
  if (t.run_status === "ok") return <Tag tone="ok">Loaded{t.run_rows != null ? `, ${fmtInt(t.run_rows)} rows` : ""}</Tag>;
  if (t.run_status === "failed") return <Tag tone="bad">Failed</Tag>;
  if (t.run_to_go) return <span className="muted">to go</span>;
  return <span className="muted">unchanged</span>;
}

export default function Database() {
  const { name = "", server = "-", database = "" } = useParams();
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const path = `/collections/${encodeURIComponent(name)}/databases/${encodeURIComponent(server)}/${encodeURIComponent(database)}`;
  const found = useQuery({
    queryKey: ["database", name, server, database, search, offset],
    queryFn: () => api.get<Row>(`${path}${qs({ search, offset })}`),
    refetchInterval: 15_000,
  });
  const d = found.data;
  const running = !!d?.running;
  return (
    <>
      <PageHead crumb={<Crumbs steps={[["Ingestion jobs", "/ingestion-jobs"],
          ...(d?.jobs ?? []).map((j: Row): [string, string] => [j.name, `/ingestion-jobs/${j.id}`]),
          [name, `/collections/${encodeURIComponent(name)}`], ["Database"]]} />}
        title={database || "(none)"}
        lead={d && <>On <strong>{d.server || "(no server)"}</strong>, in ingestion group <Link to={`/collections/${encodeURIComponent(name)}`}><strong>{name}</strong></Link>
          {d.jobs.length ? <> ({d.jobs.map((j: Row) => j.name).join(", ")})</> : <> (in no job)</>}: <strong>{fmtInt(d.enabled_tables)}</strong> enabled{" "}
          {plural(d.enabled_tables, "table")}{running && <>; <Link to={`/runs/${d.running.run_id}`}>group run {fmtInt(d.running.run_id)}</Link> is going now</>}.{" "}
          <Link to={`/tables${qs({ collection: name, database })}`}>Edit these tables</Link></>}
        right={<Stamp at={d?.read_at} every={running ? 15 : 60} error={found.error} />} />
      <ErrorBox error={found.error} />
      {d && (
        <div className="metrics">
          <div className="metric"><div className="metric-label">Tables now <Info>Loaded counts late tables too. A database is late when one of its tables is past its freshness target.</Info></div>
            <StateSummary row={d} looksGood /></div>
        </div>
      )}
      <Section title="Tables" note={running ? "Failing first, then in flight, then to go, then the rest. Its part in the group run going now." : "Failing first, then by name."}
        actions={<><StateKey /><input type="search" placeholder="Search schema.table" value={search}
          onChange={(e) => { setOffset(0); setSearch(e.target.value); }} /></>}>
        {d && <Pager offset={offset} limit={d.limit} total={d.total} noun="table" onPage={setOffset} />}
        <FillBox min={240}>
          <table className="data">
            <thead><tr><th>Table</th>{running && <th>In this group run</th>}<th>State now</th><th>Last success</th>
              <th className="num">Freshness target</th><th>Load</th></tr></thead>
            <tbody>
              {(d?.tables ?? []).map((t: Row) => (
                <tr key={t.id}>
                  <td className="name"><b>{t.src_schema}.{t.src_table}</b></td>
                  {running && <td><RunPart t={t} running={running} /></td>}
                  <td><Tag tone={STATE_TONE[t.state] ?? ""}>{t.state}</Tag></td>
                  <td>{t.last_success_at ? fmtAgo(t.last_success_at) : <span className="muted">never</span>}</td>
                  <td className="num">{t.sla_min != null ? `${fmtInt(t.sla_min)} min` : <span className="muted">none</span>}</td>
                  <td>{t.load_type}</td>
                </tr>
              ))}
              {d && !d.tables.length && <tr><td colSpan={running ? 6 : 5} className="muted">No tables match.</td></tr>}
            </tbody>
          </table>
        </FillBox>
      </Section>
    </>
  );
}
