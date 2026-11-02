import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Row } from "../api";
import { ErrorBox, Host, Name, Notice, PageHead, Section, StateBar, StateKey, Tip, fmtAgo, fmtInt, plural, usePageStamp } from "../components";

/** How an instance is read: its UC connections (name, type and the host each points at, or that the app cannot see
 * it) or its JDBC hosts. */
function ReadVia({ via }: { via: Row[] }) {
  return <>{via.map((v, i) => <span key={i} className="sub">{v.kind === "uc"
    ? <>UC connection <b>{v.name}</b>{v.connection_type ? ` (${v.connection_type})` : ""},{" "}
        {v.host ? <Host host={v.host} />
          : v.host_hidden ? <Tip focusable text="This app's identity cannot see this UC connection (it has no rights on it), so its host is not known here. The ingestion jobs read through it with their own identity.">
            <span className="muted" data-host-hidden>host not visible to this app</span></Tip>
          : <span className="muted" data-host-hidden>host not set on the connection</span>}</>
    : <>JDBC host <Host host={v.host} /></>}</span>)}</>;
}

/** Source instances first; a chosen instance's ingestion jobs (jobs only: groups are on their job's page) and its
 * tables discovered and not registered. */
export default function Sources() {
  const found = useQuery({ queryKey: ["sources"], queryFn: () => api.get<Row>("/sources"), refetchInterval: 15_000 });
  const [chosen, setChosen] = useState<string | null>(null);
  const d = found.data;
  const instances: Row[] = d?.instances ?? [];
  const pick = instances.find((i) => i.server === chosen) ?? instances[0];
  usePageStamp(d?.read_at, 60, found.error);
  return (
    <>
      <PageHead title="Sources"
        lead={d && <>{fmtInt(d.ingested)} {plural(d.ingested, "instance")} ingested, {fmtInt(d.nothing_registered)} with nothing registered;{" "}
          {fmtInt(d.not_registered)} {plural(d.not_registered, "table")} discovered and not registered yet. An instance is the source
          server the tables' rows reach: their UC connection, or their host and port.</>} />
      <ErrorBox error={found.error} />
      {d?.connections_error && <Notice tone="warn">UC connections could not be listed, so connections with nothing registered are missing: {d.connections_error}</Notice>}
      <Section title={`Instances: ${fmtInt(instances.length)}`} actions={<StateKey />}>
        <div className="table-wrap"><table className="data">
          <thead><tr><th>Instance</th><th className="num">Jobs</th><th className="num">Groups</th><th className="num">Databases</th>
            <th className="num">Tables</th><th>State now</th><th className="num">Failing</th><th className="num">Not ingested yet</th></tr></thead>
          <tbody>
            {instances.map((i) => (
              <tr key={i.server} className={pick?.server === i.server ? "selected" : ""} onClick={() => setChosen(i.server)} style={{ cursor: "pointer" }}>
                <td className="name"><b>{i.name || i.server || "(no server)"}</b><ReadVia via={i.read_via} />
                  {(i.labels ?? []).length > 0 && <span className="sub" style={{ color: "var(--warn)" }}>
                    One server, named differently on its rows' instance group: {(i.labels as Row[]).map((l) =>
                      `${l.label ?? "no name"} (${fmtInt(l.tables)} ${plural(l.tables, "table")})`).join(", ")}</span>}</td>
                <td className="num">{fmtInt(i.jobs.filter((j: Row) => j.id != null).length)}</td><td className="num">{fmtInt(i.groups)}</td>
                <td className="num">{fmtInt(i.databases)}</td><td className="num">{fmtInt(i.tables)}</td>
                <td>{i.nothing_registered ? <span className="muted">nothing registered</span> : <StateBar row={i} />}</td>
                <td className="num" style={{ color: Number(i.state_failing) ? "var(--bad)" : undefined }}>{fmtInt(i.state_failing)}</td>
                <td className="num">{fmtInt(i.not_registered)}</td>
              </tr>
            ))}
            {d && !instances.length && <tr><td colSpan={8} className="muted">No instances: nothing is registered and no UC connection is readable.</td></tr>}
          </tbody>
        </table></div>
      </Section>
      {pick && (
        <>
          <Section title={`${pick.name || pick.server || "(no server)"}: its ingestion jobs`} note="Jobs only: a job's groups and their databases are on the job's own page.">
            <div className="table-wrap"><table className="data">
              <thead><tr><th>Ingestion job</th><th className="num">Groups</th><th className="num">Databases</th><th className="num">Tables</th>
                <th>State now</th><th>Now</th></tr></thead>
              <tbody>
                {pick.jobs.map((j: Row) => (
                  <tr key={j.id ?? "none"}>
                    <td className="name">{j.id != null ? <Link to={`/ingestion-jobs/${j.id}`}><b><Name>{j.name}</Name></b></Link> : <b>Not in any job</b>}</td>
                    <td className="num">{fmtInt(j.groups.length)}</td><td className="num">{fmtInt(j.databases)}</td><td className="num">{fmtInt(j.enabled_tables)}</td>
                    <td><StateBar row={j} /></td>
                    <td>{j.paused ? `paused${Number(j.late_databases) ? `, ${fmtInt(j.late_databases)} late databases` : ""}` : j.id == null ? "run by hand or by the Databricks job" : j.description ?? ""}</td>
                  </tr>
                ))}
                {!pick.jobs.length && <tr><td colSpan={6} className="muted">No ingestion job reads this instance.</td></tr>}
              </tbody>
            </table></div>
          </Section>
          <Section title="Not ingested yet" note="Per database discovered on this instance's hosts: tables found by its latest discovery that have no control row. Databases never discovered can't be listed without querying the server."
            actions={<Link to="/bulk">Review on Bulk import</Link>}>
            <div className="table-wrap"><table className="data">
              <thead><tr><th>Database</th><th className="num">Discovered</th><th className="num">Not registered</th><th>Largest not registered</th><th>Discovered</th></tr></thead>
              <tbody>
                {pick.not_ingested.map((n: Row) => (
                  <tr key={`${n.src_host}\u001f${n.src_database}`}><td className="name"><b>{n.src_database}</b><span className="sub"><Host host={n.src_host} /></span></td>
                    <td className="num">{fmtInt(n.discovered)}</td><td className="num">{fmtInt(n.not_registered)}</td>
                    <td>{n.largest_not_registered ?? ""}</td><td>{fmtAgo(n.discovered_at)}</td></tr>
                ))}
                {!pick.not_ingested.length && <tr><td colSpan={5} className="muted">No discovery on this instance's hosts.</td></tr>}
              </tbody>
            </table></div>
          </Section>
        </>
      )}
    </>
  );
}
