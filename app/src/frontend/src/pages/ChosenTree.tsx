import { Fragment, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, qs, type Row } from "../api";
import { ErrorBox, Pager, Twisty, fmtInt } from "../components";

// New job's What it ingests, below a server row: the databases chosen on it, a page at a time, and a database's tables
// read only once it is opened. It uses the reads the app has already (GET /api/sources/{kind}/{id} for the databases of
// a whole-server choice, GET /api/sources/{kind}/{id}/tables for a database's tables); the databases and tables chosen
// one by one are in the choice itself. A database typed by hand in a CSV file shows once Create's preview has found its
// tables by name.

const PAGE = 25;

/** The source a choice was made from, as /api/sources names it: an upload, or the discoveries it took tables from. */
export function sourceOf(source: Row): { kind: "upload" | "discovery"; id: string } {
  return source.upload_id ? { kind: "upload", id: String(source.upload_id) }
    : { kind: "discovery", id: [source.discovery_id, ...(source.discovery_ids ?? [])].filter(Boolean).join(",") };
}

/** The databases a choice names on a server outside a whole-server read: whole ones, and those with tables picked. */
export function namedDatabases(source: Row, server: string): { database: string; tables: string[] | null }[] {
  const same = (s: unknown) => String(s ?? "").toLowerCase() === server.toLowerCase();
  const whole = (source.databases ?? []).filter((d: Row) => same(d.server)).map((d: Row) => ({ database: String(d.database), tables: null }));
  const picked = (source.tables ?? []).filter((t: Row) => same(t.server)).map((t: Row) => ({ database: String(t.database), tables: t.tables as string[] }));
  return [...whole, ...picked];
}

function SourceTables({ source, server, database }: { source: Row; server: string; database: string }) {
  const [offset, setOffset] = useState(0);
  const { kind, id } = sourceOf(source);
  const page = useQuery({
    queryKey: ["chosen-tables", kind, id, server, database, offset], placeholderData: keepPreviousData,
    queryFn: () => api.get<Row>(`/sources/${kind}/${encodeURIComponent(id)}/tables${qs({ server, database, offset, limit: PAGE })}`),
  });
  return (
    <>
      {(page.data?.tables ?? []).map((t: Row) => (
        <tr key={`${t.src_schema}.${t.src_table}`} className="l3"><td className="name" colSpan={3}>{t.src_schema}.{t.src_table}</td></tr>
      ))}
      {(page.data?.total ?? 0) > PAGE && <tr className="l3"><td colSpan={3}>
        <Pager offset={offset} limit={PAGE} total={page.data!.total} noun="table" onPage={setOffset} /></td></tr>}
      {page.data && !page.data.tables.length && <tr className="l3"><td colSpan={3} className="muted">No tables read yet.</td></tr>}
      {page.error && <tr className="l3"><td colSpan={3}><ErrorBox error={page.error} /></td></tr>}
    </>
  );
}

function DatabaseRow({ source, server, database, tables, picked }: { source: Row; server: string; database: string;
                       tables?: number | null; picked: string[] | null }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr className="l2">
        <td className="name"><Twisty open={open} onToggle={() => setOpen(!open)}>{database}</Twisty></td>
        <td></td>
        <td className="num">{picked ? fmtInt(picked.length) : tables != null ? fmtInt(tables) : ""}</td>
      </tr>
      {open && (picked
        ? picked.map((name) => <tr key={name} className="l3"><td className="name" colSpan={3}>{name}</td></tr>)
        : <SourceTables source={source} server={server} database={database} />)}
    </>
  );
}

/** The databases chosen on a server, as rows under its row in What it ingests. */
export function ChosenDatabases({ source, server }: { source: Row; server: string }) {
  const [offset, setOffset] = useState(0);
  const [namedOffset, setNamedOffset] = useState(0);
  const { kind, id } = sourceOf(source);
  const whole = useQuery({
    queryKey: ["chosen-databases", kind, id, source.filter ?? "", offset], enabled: !!source.whole_server,
    placeholderData: keepPreviousData,
    queryFn: () => api.get<Row>(`/sources/${kind}/${encodeURIComponent(id)}${qs({ filter: source.filter ?? "", offset, limit: PAGE })}`),
  });
  const wholeRows = (whole.data?.databases ?? []).filter((d: Row) => String(d.server).toLowerCase() === server.toLowerCase());
  const covered = (database: string) => !!source.whole_server && database.toLowerCase().includes(String(source.filter ?? "").toLowerCase());
  const named = namedDatabases(source, server).filter((d) => !covered(d.database));
  return (
    <>
      {wholeRows.map((d: Row) => <DatabaseRow key={`w-${d.src_database}`} source={source} server={server} database={d.src_database}
        tables={d.tables} picked={null} />)}
      {(whole.data?.matched?.databases ?? 0) > PAGE && <tr className="l2"><td colSpan={3}>
        <Pager offset={offset} limit={PAGE} total={whole.data!.matched.databases} noun="database" onPage={setOffset} /></td></tr>}
      {named.slice(namedOffset, namedOffset + PAGE).map((d) => (
        <Fragment key={`n-${d.database}`}>
          <DatabaseRow source={source} server={server} database={d.database} picked={d.tables} />
        </Fragment>
      ))}
      {named.length > PAGE && <tr className="l2"><td colSpan={3}>
        <Pager offset={namedOffset} limit={PAGE} total={named.length} noun="database" onPage={setNamedOffset} /></td></tr>}
      {whole.error && <tr className="l2"><td colSpan={3}><ErrorBox error={whole.error} /></td></tr>}
    </>
  );
}
