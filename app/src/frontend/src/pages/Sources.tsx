import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type Row } from "../api";
import { Cols, CutCopy, Cut, ErrorBox, Notice, PageHead, Section, StateBar, StateKey, fmtAgo, fmtInt, oneLine, plural, usePageStamp } from "../components";
import { connectionTypeName, hostLabel } from "../cells";

/** How an instance is read, in a few words: its UC connections (name, type and the host each points at, or why the host is
 * not known) or its JDBC hosts. The tooltip of its cell has the same, with what a hidden host means. */
function readViaText(via: Row[]): string {
  return via.map((v) => v.kind === "uc"
    ? `UC connection ${v.name}${v.connection_type ? ` (${connectionTypeName(v.connection_type)})` : ""}, ${
      v.host ? hostLabel(v.host) : v.host_hidden ? "host not visible to this app" : "host not set on the connection"}`
    : `JDBC host ${v.host}`).join("; ");
}

const HIDDEN_HOST_NOTE = "A host not visible to this app: this app's identity cannot see the UC connection (it has no rights on it). The ingestion jobs read through it with their own identity.";

/** The Sources lists' columns, as Catalog Explorer's connections list keeps its own: the instance takes the room, how it
 * is read has a column, and the counts are narrow. */
const INSTANCE_WIDTHS = [null, 260, 56, 64, 84, 64, 170, 64, 112] as const;
const JOB_WIDTHS = [null, 64, 84, 64, 170, 300] as const;
const NOT_INGESTED_WIDTHS = [null, 220, 96, 112, 220, 120] as const;

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
        <div className="table-wrap"><table {...oneLine(INSTANCE_WIDTHS)}>
          <Cols widths={INSTANCE_WIDTHS} />
          <thead><tr><th>Instance</th><th>Read through</th><th className="num">Jobs</th><th className="num">Groups</th><th className="num">Databases</th>
            <th className="num">Tables</th><th>State now</th><th className="num">Failing</th><th className="num">Not ingested yet</th></tr></thead>
          <tbody>
            {instances.map((i) => (
              <tr key={i.server} className={pick?.server === i.server ? "selected" : ""} onClick={() => setChosen(i.server)} style={{ cursor: "pointer" }}>
                <td className="name">{i.server
                    ? <CutCopy text={i.name || i.server} label={`Copy the name of ${i.name || i.server}`}
                      shown={<b>{i.name || i.server}</b>} />
                    : <b>(no server)</b>}</td>
                <td><Cut text={[readViaText(i.read_via), (i.read_via as Row[]).some((v) => v.host_hidden) ? HIDDEN_HOST_NOTE : "",
                  (i.labels ?? []).length > 0 ? `One server, named differently on its rows' instance group: ${(i.labels as Row[]).map((l) =>
                    `${l.label ?? "no name"} (${fmtInt(l.tables)} ${plural(l.tables, "table")})`).join(", ")}` : ""].filter(Boolean).join(". ")}>
                  {readViaText(i.read_via)}</Cut></td>
                <td className="num">{fmtInt(i.jobs.filter((j: Row) => j.id != null).length)}</td><td className="num">{fmtInt(i.groups)}</td>
                <td className="num">{fmtInt(i.databases)}</td><td className="num">{fmtInt(i.tables)}</td>
                <td>{i.nothing_registered ? <span className="muted">nothing registered</span> : <StateBar row={i} />}</td>
                <td className="num" style={{ color: Number(i.state_failing) ? "var(--bad)" : undefined }}>{fmtInt(i.state_failing)}</td>
                <td className="num">{fmtInt(i.not_registered)}</td>
              </tr>
            ))}
            {d && !instances.length && <tr><td colSpan={9} className="muted">No instances: nothing is registered and no UC connection is readable.</td></tr>}
          </tbody>
        </table></div>
      </Section>
      {pick && (
        <>
          <Section title={`${pick.name || pick.server || "(no server)"}: its ingestion jobs`} note="Jobs only: a job's groups and their databases are on the job's own page.">
            <div className="table-wrap"><table {...oneLine(JOB_WIDTHS)}>
              <Cols widths={JOB_WIDTHS} />
              <thead><tr><th>Ingestion job</th><th className="num">Groups</th><th className="num">Databases</th><th className="num">Tables</th>
                <th>State now</th><th>Now</th></tr></thead>
              <tbody>
                {pick.jobs.map((j: Row) => (
                  <tr key={j.id ?? "none"}>
                    <td className="name">{j.id != null ? <Link to={`/ingestion-jobs/${j.id}`}><b><Cut text={j.name} tipOnlyWhenCut /></b></Link> : <b>Not in any job</b>}</td>
                    <td className="num">{fmtInt(j.groups.length)}</td><td className="num">{fmtInt(j.databases)}</td><td className="num">{fmtInt(j.enabled_tables)}</td>
                    <td><StateBar row={j} /></td>
                    <td><Cut tipOnlyWhenCut text={j.paused ? `paused${Number(j.late_databases) ? `, ${fmtInt(j.late_databases)} late databases` : ""}` : j.id == null ? "run by hand or by the Databricks job" : j.description ?? ""} /></td>
                  </tr>
                ))}
                {!pick.jobs.length && <tr><td colSpan={6} className="muted">No ingestion job reads this instance.</td></tr>}
              </tbody>
            </table></div>
          </Section>
          <Section title="Not ingested yet" note="Per database discovered on this instance's hosts: tables found by its latest discovery that have no control row. Databases never discovered can't be listed without querying the server."
            actions={<Link to="/bulk">Review on Bulk import</Link>}>
            <div className="table-wrap"><table {...oneLine(NOT_INGESTED_WIDTHS)}>
              <Cols widths={NOT_INGESTED_WIDTHS} />
              <thead><tr><th>Database</th><th>Server</th><th className="num">Discovered</th><th className="num">Not registered</th><th>Largest not registered</th><th>Discovered</th></tr></thead>
              <tbody>
                {pick.not_ingested.map((n: Row) => (
                  <tr key={`${n.src_host}\u001f${n.src_database}`}><td className="name"><b><Cut text={n.src_database} tipOnlyWhenCut /></b></td>
                    <td className="mono"><CutCopy text={n.src_host} label={`Copy the host ${n.src_host}`} /></td>
                    <td className="num">{fmtInt(n.discovered)}</td><td className="num">{fmtInt(n.not_registered)}</td>
                    <td><Cut tipOnlyWhenCut text={n.largest_not_registered ?? ""} /></td><td>{fmtAgo(n.discovered_at)}</td></tr>
                ))}
                {!pick.not_ingested.length && <tr><td colSpan={6} className="muted">No discovery on this instance's hosts.</td></tr>}
              </tbody>
            </table></div>
          </Section>
        </>
      )}
    </>
  );
}
