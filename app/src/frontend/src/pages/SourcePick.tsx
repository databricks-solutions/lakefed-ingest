import { Fragment, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, qs, type Row } from "../api";
import { ErrorBox, Notice, Pager, fmtInt, plural, Twisty } from "../components";
import { TriBox } from "./Discover";
import { serverRowPick, underFilter } from "./pickState";

// What to take from an upload or a discovery, chosen at its top level: its servers and databases, with their table
// counts and load types, a database filter, and a database's tables in pages only when it is opened. The tables stay in
// Lakebase; what is chosen is sent by reference (main.Selection): the whole server (the databases matching the filter),
// whole databases, or some tables of a database.

export type Picked = { whole_server: boolean; filter: string; databases: Row[]; tables: Row[] };
export const nothingPicked: Picked = { whole_server: false, filter: "", databases: [], tables: [] };
export const pickedAny = (p: Picked) => p.whole_server || p.databases.length > 0 || p.tables.length > 0;

const DATABASES = 100;
const TABLES = 50;
const same = (a: Row, server: string, database: string) =>
  String(a.server).toLowerCase() === server.toLowerCase() && String(a.database).toLowerCase() === database.toLowerCase();
export { underFilter };

export function loadTypes(r: Row): string {
  const parts = [["ct", "change tracking"], ["cursor", "cursor"], ["full", "full reload"], ["unset", "the job's"]]
    .filter(([k]) => Number(r[k]) > 0).map(([k, label]) => `${label} ${fmtInt(r[k])}`);
  return parts.join(", ");
}

/** What is chosen, in words: "the whole server (databases matching eng_0), 2 databases and tables from 1 more". */
export function pickedText(p: Picked): string {
  const parts = [p.whole_server ? `every database${p.filter ? ` matching "${p.filter}"` : ""}` : "",
    p.databases.length ? `${fmtInt(p.databases.length)} whole ${plural(p.databases.length, "database")}` : "",
    p.tables.length ? `${fmtInt(p.tables.reduce((n, t) => n + t.tables.length, 0))} tables from ${fmtInt(p.tables.length)} more ${plural(p.tables.length, "database")}` : ""];
  return parts.filter(Boolean).join(", ") || "nothing yet";
}

/** One database's tables in a source, a page at a time, with a search; ticks choose single tables. */
export function DatabaseTables({ kind, sourceId, server, database, whole, chosen, onToggle }: { kind: string; sourceId: string;
  server: string; database: string; whole: boolean; chosen: string[]; onToggle: (table: string) => void }) {
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const page = useQuery({
    queryKey: ["source-tables", kind, sourceId, server, database, search, offset], placeholderData: keepPreviousData,
    queryFn: () => api.get<Row>(`/sources/${kind}/${encodeURIComponent(sourceId)}/tables${qs({ server, database, search, offset, limit: TABLES })}`),
  });
  const rows: Row[] = page.data?.tables ?? [];
  return (
    <>
      <tr className="l3"><td colSpan={3} className="name" style={{ paddingLeft: 62 }}>
        <input className="search" placeholder={`Search ${database}'s tables`} value={search}
          onChange={(e) => { setSearch(e.target.value); setOffset(0); }} /></td></tr>
      {rows.map((t) => {
        const key = `${t.src_schema}.${t.src_table}`;
        const on = whole || chosen.some((c) => c.toLowerCase() === key.toLowerCase());
        return (
          <tr key={key} className="l3"><td className="name" style={{ paddingLeft: 62 }}>
            <TriBox pick={on ? "all" : "none"} label={key} onClick={() => !whole && onToggle(key)} /> {key}
            {t.notes && <span className="sub">{t.notes}</span>}</td>
            <td className="num">{t.row_count != null ? `${fmtInt(t.row_count)} rows` : ""}</td>
            <td>{t.load_type === "ct" ? "Change tracking" : t.load_type === "cursor" ? `Cursor column ${t.cursor_column ?? ""}`
              : t.load_type === "full" ? "Full reload" : <span className="muted">the job's</span>}</td></tr>
        );
      })}
      <tr className="l3"><td colSpan={3} style={{ paddingLeft: 62 }}>
        {page.isLoading ? <span className="muted">Reading its tables…</span>
          : <Pager offset={offset} limit={TABLES} total={Number(page.data?.total ?? 0)} noun="table" onPage={setOffset} />}
        <ErrorBox error={page.error} /></td></tr>
    </>
  );
}

/** An upload's or a discovery's top level, to choose from (see the comment above). */
export function SourcePick({ kind, sourceId, picked, setPicked }: { kind: "upload" | "discovery"; sourceId: string;
  picked: Picked; setPicked: (update: (p: Picked) => Picked) => void }) {
  const [filter, setFilter] = useState(picked.filter);
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const top = useQuery({
    queryKey: ["source", kind, sourceId, filter, offset], placeholderData: keepPreviousData,
    queryFn: () => api.get<Row>(`/sources/${kind}/${encodeURIComponent(sourceId)}${qs({ filter, offset, limit: DATABASES })}`),
  });
  const data = top.data;
  const servers: Row[] = data?.servers ?? [];
  const isWhole = (d: Row) => (picked.whole_server && underFilter(d.src_database, picked.filter))
    || picked.databases.some((p) => same(p, d.server, d.src_database));
  const tablesOf = (d: Row): string[] => picked.tables.find((p) => same(p, d.server, d.src_database))?.tables ?? [];
  const toggleDatabase = (d: Row) => setPicked((now) => {
    const there = now.databases.some((p) => same(p, d.server, d.src_database));
    return { ...now, databases: there ? now.databases.filter((p) => !same(p, d.server, d.src_database))
      : [...now.databases, { server: d.server, database: d.src_database }],
      tables: now.tables.filter((p) => !same(p, d.server, d.src_database)) };
  });
  // Ticking every table of a database makes it a whole database, so it reads as one ("1 whole database", not "2 chosen").
  const toggleTable = (d: Row, table: string) => setPicked((now) => {
    const current = now.tables.find((p) => same(p, d.server, d.src_database))?.tables ?? [];
    const next = current.includes(table) ? current.filter((t: string) => t !== table) : [...current, table];
    const others = now.tables.filter((p) => !same(p, d.server, d.src_database));
    if (next.length > 0 && next.length >= Number(d.tables)) {
      const whole = now.databases.some((p) => same(p, d.server, d.src_database)) ? now.databases
        : [...now.databases, { server: d.server, database: d.src_database }];
      return { ...now, databases: whole, tables: others };
    }
    return { ...now, tables: next.length ? [...others, { server: d.server, database: d.src_database, tables: next }] : others };
  });
  // Ticking the whole server takes over what it covers: the databases matching its filter lose their own picks, so the
  // choice never names one twice. Turning it off keeps the rest.
  const wholeServerTicked = serverRowPick({ on: picked.whole_server, filter: picked.filter }, filter,
    picked.databases.length > 0 || picked.tables.length > 0) === "all";
  const wholeServer = () => setPicked((now) => wholeServerTicked
    ? { ...now, whole_server: false } : { ...now, whole_server: true, filter,
      databases: now.databases.filter((p) => !underFilter(p.database, filter)),
      tables: now.tables.filter((p) => !underFilter(p.database, filter)) });
  const key = (d: Row) => `${d.server}\u001f${d.src_database}`;
  return (
    <>
      {data && <p className="muted">{fmtInt(data.totals.tables)} {plural(data.totals.tables, "table")} in{" "}
        {fmtInt(data.totals.databases)} {plural(data.totals.databases, "database")} on {fmtInt(data.totals.servers)}{" "}
        {plural(data.totals.servers, "server")}{kind === "upload" && data.upload?.file_name ? `, from ${data.upload.file_name}` : ""}.
        Tick the whole server, whole databases, or open a database for its tables.</p>}
      {data && data.invalid_lines?.length > 0 && <Notice tone="warn">{fmtInt(data.upload.invalid)} {plural(data.upload.invalid, "line")} of the
        file can't be taken:<ul>{data.invalid_lines.slice(0, 5).map((l: Row) => <li key={l.line}>line {l.line}, {l.table}: {(l.errors ?? []).join("; ")}</li>)}</ul></Notice>}
      <div className="toolbar"><input className="search" placeholder="Filter databases, e.g. eng_0" value={filter}
        onChange={(e) => { setFilter(e.target.value); setOffset(0); }} />
        {filter && data && <span className="muted">{fmtInt(data.matched.databases)} {plural(data.matched.databases, "database")},{" "}
          {fmtInt(data.matched.tables)} {plural(data.matched.tables, "table")} match</span>}</div>
      <div className="table-wrap tree-pick"><table className="data tree">
        <thead><tr><th>Server › database › schema.table</th><th className="num">Databases · tables</th><th>Load types</th></tr></thead>
        <tbody>
          {servers.map((s) => (
            <tr key={s.server}><td className="name">
              <TriBox pick={serverRowPick({ on: picked.whole_server, filter: picked.filter }, filter,
                picked.databases.length > 0 || picked.tables.length > 0)}
                label={`The whole server ${s.server}`} onClick={wholeServer} />{" "}
              <b>{s.server}</b>, {filter ? <>every database matching "{filter}"</> : "the whole server"}</td>
              <td className="num">{fmtInt(s.databases)} · {fmtInt(s.tables)}</td><td className="muted">{loadTypes(s)}</td></tr>
          ))}
          {(data?.databases ?? []).map((d: Row) => (
            <Fragment key={key(d)}>
              <tr className="l2"><td className="name" style={{ paddingLeft: 30 }}>
                <TriBox pick={isWhole(d) ? "all" : tablesOf(d).length ? "part" : "none"} label={`The whole database ${d.src_database}`}
                  onClick={() => !(picked.whole_server && underFilter(d.src_database, picked.filter)) && toggleDatabase(d)} />{" "}
                <Twisty open={open === key(d)} onToggle={() => setOpen(open === key(d) ? null : key(d))}>{d.src_database}</Twisty>
                {servers.length > 1 && <span className="sub">{d.server}</span>}</td>
                <td className="num">{fmtInt(d.tables)}{tablesOf(d).length > 0 && <span className="sub">{fmtInt(tablesOf(d).length)} chosen</span>}</td>
                <td className="muted">{loadTypes(d)}</td></tr>
              {open === key(d) && <DatabaseTables kind={kind} sourceId={sourceId} server={d.server} database={d.src_database}
                whole={isWhole(d)} chosen={tablesOf(d)} onToggle={(t) => toggleTable(d, t)} />}
            </Fragment>
          ))}
        </tbody>
      </table></div>
      {data && <Pager offset={offset} limit={DATABASES} total={Number(data.matched.databases)} noun="database" onPage={setOffset} />}
      <ErrorBox error={top.error} />
    </>
  );
}
