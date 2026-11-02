import { Fragment, useEffect, useRef, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, qs, type Row } from "../api";
import { Busy, ErrorBox, Info, Modal, Notice, Pager, Spinner, Tag, Tip, fmtInt, plural, Twisty, useNow } from "../components";
import { useSettings } from "../App";
import { DatabaseTables } from "./SourcePick";
import { CreateConnectionChoices } from "./CreateConnectionChoices";
import { accessOf, countsText, likeContaining, readable, selectionCounts, serverRowPick, underFilter, type Pick, type ServerCount } from "./pickState";
import { connectionFailure, connectionNextBlock, connectionNextLabel, readsDone, waitLine, type ErrorCodes } from "../pending";

// Add tables, Discover on a source: as Lakeflow Connect's Connection and Source steps. Step 1 is the Unity Catalog
// connection, tested by Next before anything is discovered; step 2 picks the whole server, whole databases or single tables in
// a tree, a row chosen in part showing a filled square. The tables found stay in Lakebase (discovered_table): a
// database's tables are shown a page at a time when it is opened, and what is picked is handed to New and Edit by
// reference (the discoveries, and the whole server, whole databases or tables chosen), never as the tables themselves.

export type { Pick };
const RUNNING = new Set(["PENDING", "QUEUED", "RUNNING", "TERMINATING", "BLOCKED", "WAITING_FOR_RETRY"]);
const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** What the dialog waits on, in one plain line: what it does, since when, and how far when that is known. */
type DialogWait = { asked: Date; reading: string; done?: number; total?: number };

/** A discovery that failed, with the codes its error names (source_catalog.error_codes) when the backend read any. */
class SourceError extends Error {
  codes: ErrorCodes | null;
  constructor(message: string, codes?: ErrorCodes | null) { super(message); this.codes = codes ?? null; }
}

/** For an Azure SQL server: a service credential with Reader on it and its resource id, so the discovery job lists the
 *  databases through Azure instead of master. Both or neither. */
const azureOf = (credential: string, resource: string): Row => credential.trim() && resource.trim()
  ? { azure_credential: credential.trim(), azure_resource_id: resource.trim() } : {};

/** A discovery that has ended: its id, whether it ran through the SQL warehouse, and for a whole-server read there how
 *  many databases it left out because the login can't open them. */
type Discovered = { id: string; via: string; leftOut: number };

/** Start a discovery through the connection and wait for it to end; its id, or the reason it failed. onProgress hears
 *  each step of a read through the warehouse (its databases read so far). */
async function discover(connection: string, request: Row, onProgress?: (answer: Row) => void): Promise<Discovered> {
  const started = await api.post<Row>("/discoveries/source", { src_connection: connection, ...request });
  if (started.via === "warehouse") {
    // Through the SQL warehouse: a listing is one statement, asked about until it ends; a read is a step at a time
    // (each database one statement), asked for until none is left.
    const id = String(started.discovery_id);
    let answer = started;
    for (;;) {
      if (answer.state === "SUCCEEDED") return { id, via: "warehouse", leftOut: Number(started.left_out ?? 0) };
      if (answer.state === "FAILED" || answer.error) throw new SourceError(String(answer.error ?? "the listing failed").split("\n")[0], answer.codes);
      const listing = answer.statement_id != null;
      if (!listing) onProgress?.(answer);
      await wait(1000);
      answer = listing
        ? await api.get<Row>(`/discoveries/${encodeURIComponent(id)}/warehouse/${encodeURIComponent(answer.statement_id)}${qs({ connection })}`)
        : await api.get<Row>(`/discoveries/${encodeURIComponent(id)}/reads${qs({ connection })}`);
    }
  }
  for (;;) {
    const run = await api.get<Row>(`/discoveries/runs/${started.run_id}`);
    if (!RUNNING.has(run.state)) {
      if (run.result && run.result !== "SUCCESS") {
        const rows = await api.get<Row[]>(`/discoveries/${started.discovery_id}/databases`);
        const failed = rows.find((r) => r.status === "failed");
        throw new SourceError(failed?.error ? String(failed.error).split("\n")[0] : run.message || `the discovery ended ${run.result}`,
                              failed?.codes);
      }
      return { id: String(started.discovery_id), via: "job", leftOut: 0 };
    }
    await wait(3000);
  }
}

/** Read the tables of some databases into Lakebase: the discovery's id, and each database that couldn't be read with
 *  the first line of why. A database the login can name but not open fails on its own; the others are still read. */
async function readTables(connection: string, request: Row, onProgress?: (answer: Row) => void):
    Promise<{ id: string; failed: [string, string][]; leftOut: number }> {
  const { id, leftOut } = await discover(connection, request, onProgress);
  const rows = await api.get<Row[]>(`/discoveries/${id}/databases`);
  return { id, leftOut,
           failed: rows.filter((r) => r.status === "failed").map((r) => [r.src_database, String(r.error ?? "").split("\n")[0]]) };
}

/** Discard drafts (reads started for a choice no job took): nothing more is read for them, and the delete job purges
 *  their rows. On leaving the page it goes by sendBeacon, which still goes out as the page closes. */
export function discardDrafts(ids: string[], beacon = false) {
  for (const id of ids) {
    const path = `/discoveries/${encodeURIComponent(id)}/discard`;
    if (beacon && typeof navigator !== "undefined" && navigator.sendBeacon) navigator.sendBeacon(`/api${path}`);
    else api.post(path, {}).catch(() => undefined);
  }
}

/** Keep drafts' reads going while the page is open: each one's last step (/reads, which also does the next) until it
 *  has ended. ``generation`` starts the asking again when a draft takes more databases. */
export function useDraftReads(connection: string, ids: string[], generation = 0): Record<string, Row> {
  const [steps, setSteps] = useState<Record<string, Row>>({});
  const key = ids.join(",");
  useEffect(() => {
    if (!ids.length) return;
    let stopped = false;
    (async () => {
      const ended = new Set<string>();
      while (!stopped && ended.size < ids.length) {
        for (const id of ids.filter((i) => !ended.has(i))) {
          try {
            const step = await api.get<Row>(`/discoveries/${encodeURIComponent(id)}/reads${qs({ connection })}`);
            if (stopped) return;
            setSteps((now) => ({ ...now, [id]: step }));
            if (step.state === "SUCCEEDED") ended.add(id);
          } catch { /* asked again on the next round */ }
        }
        if (ended.size < ids.length) await wait(1500);
      }
    })();
    return () => { stopped = true; };
  }, [connection, key, generation]);
  return steps;
}

/** The databases that couldn't be read, and why: often that the login can see the name but not open the database. */
function Unread({ failed, reading, onReadAgain }: { failed: [string, string][]; reading: boolean; onReadAgain: () => void }) {
  if (!failed.length) return null;
  const one = failed.length === 1;
  const named = failed.slice(0, 3).map(([d]) => d).join(", ");
  return <Notice tone="warn"><p>{fmtInt(failed.length)} {plural(failed.length, "database")} couldn't be read ({named}
    {failed.length > 3 ? `, and ${fmtInt(failed.length - 3)} more` : ""}), so none of {one ? "its" : "their"} tables are added.
    The login can see {one ? "its name" : "their names"} on the server but may not open {one ? "it" : "them"}: untick{" "}
    {one ? "it" : "them"}, or give the login access and read {one ? "it" : "them"} again.</p>
    <div className="toolbar"><button disabled={reading} onClick={onReadAgain}>{reading ? "Reading again…" : "Read them again"}</button></div>
    <details className="unread-all"><summary>Show all {fmtInt(failed.length)}, with why</summary>
      <ul>{failed.map(([d, why]) => <li key={d}><b>{d}</b>: {why}</li>)}</ul></details></Notice>;
}

/** A tick box with a third state: a filled square for a row chosen in part, a tick only when all of it is. */
export function TriBox({ pick, label, onClick }: { pick: Pick; label: string; onClick: () => void }) {
  return <button type="button" className={`tri-box ${pick}`} role="checkbox" aria-checked={pick === "all" ? true : pick === "part" ? "mixed" : false}
    aria-label={label} title={pick === "part" ? "Chosen in part" : undefined} onClick={onClick}>{pick === "all" ? "✓" : ""}</button>;
}

/** A test that failed because the login can't open master, where SQL Server lists its databases. */
const cantList = (error: unknown) => /FAILED_JDBC\.CONNECTION|Login failed|Cannot open database|HV000/i.test(String((error as Error)?.message ?? error));

/** A failed connection test: its codes, the source's message and what to do. When the login may not open master, the
 *  databases can still be typed below, or listed through Azure. */
function TestFailed({ error }: { error: unknown }) {
  const failure = connectionFailure(String((error as Error)?.message ?? error), (error as SourceError)?.codes);
  return <Notice tone="bad">
    <p><b>The connection failed its test</b>{failure.code && <>: <span className="mono">{failure.code}</span></>}</p>
    <p className="mono">{failure.message}</p>
    <p>{failure.hint}</p>
    {cantList(error) && failure.code !== "SQL Server error 4060" && <p>If the login can't open master, where the server lists its
      databases, they may still be readable through the connection: type them below. Or give the login a user in master (VIEW ANY
      DATABASE) and test again. On Azure SQL, Azure can list them instead: see Azure SQL below.</p>}</Notice>;
}

/** The databases typed by hand, for a login that can't list the server's. */
function TypedDatabases({ onUse }: { onUse: (names: string[]) => void }) {
  const [text, setText] = useState("");
  const names = [...new Set(text.split(/[\s,;]+/).map((n) => n.trim()).filter(Boolean))];
  return (
    <div className="form-grid">
      <label className="wide">Its databases<textarea rows={4} value={text} placeholder={"lfc_eng_001\nlfc_eng_002"}
          onChange={(e) => setText(e.target.value)} />
        <span className="help">One per line or separated by commas.</span></label>
      <div className="toolbar"><button disabled={!names.length} onClick={() => onUse(names)}>
        Use {names.length ? `these ${fmtInt(names.length)} ${plural(names.length, "database")}` : "these databases"}</button></div>
    </div>
  );
}

function Step1({ chosen, setChosen, tested, setTested, testing, error, setError, azureCredential, setAzureCredential,
                 azureResource, setAzureResource }: { chosen: string; setChosen: (c: string) => void; tested: Row | null;
                 setTested: (t: Row | null) => void; testing: boolean; error: unknown; setError: (e: unknown) => void;
                 azureCredential: string; setAzureCredential: (c: string) => void; azureResource: string;
                 setAzureResource: (r: string) => void }) {
  const { config } = useSettings();
  const [how, setHow] = useState<"existing" | "create">("existing");
  const [search, setSearch] = useState("");
  const azure = azureOf(azureCredential, azureResource);
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["connections"], queryFn: () => api.get<Row>("/connections") });
  const [refreshing, setRefreshing] = useState(false);
  // Lists the connections again now, so one just made in Catalog Explorer shows up without leaving the form.
  const refresh = async () => {
    setRefreshing(true);
    try { qc.setQueryData(["connections"], await api.get<Row>("/connections?fresh=true")); }
    finally { setRefreshing(false); }
  };
  const found: Row[] = (list.data?.connections ?? []).filter((c: Row) => c.discoverable && c.name.toLowerCase().includes(search.toLowerCase()));
  return (
    <>
      <p className="muted">The Unity Catalog connection the tables are read through. <Info>Next tests it before anything is
        discovered, listing every database on the server (you filter them on the next step), so a source that cannot be
        reached is found here, not in the first run. Discover is the first choice: it reads the tables through the
        connection. A CSV upload is the fallback, for a list Discover can't find.</Info></p>
      <label className="radio-row"><input type="radio" checked={how === "existing"} onChange={() => setHow("existing")} /> Use an existing connection</label>
      {how === "existing" && (
        <div className="radio-body">
          <div className="toolbar"><input className="search" placeholder="Search connections" value={search} onChange={(e) => setSearch(e.target.value)} />
            <button className="ghost" disabled={refreshing} onClick={refresh}>{refreshing ? "Refreshing…" : "Refresh"}</button>
            <span className="muted">A connection made in Catalog Explorer shows here after Refresh, once this app has USE CONNECTION on it.</span></div>
          <ErrorBox error={list.error} />
          {list.data?.error && <Notice tone="warn">The app can't list connections: {list.data.error}</Notice>}
          <div className="table-wrap"><table className="data">
            <thead><tr><th></th><th>Name</th><th>Type</th><th>Host</th><th>Owner</th></tr></thead>
            <tbody>
              {found.map((c) => (
                <tr key={c.name} className="pick-row" onClick={() => { if (!testing) setChosen(c.name); }}>
                  <td><input type="radio" checked={chosen === c.name} readOnly aria-label={`Use ${c.name}`} /></td>
                  <td className="name"><b>{c.name}</b></td><td>{c.connection_type}</td><td className="mono">{c.host}</td><td>{c.owner}</td>
                </tr>
              ))}
              {list.data && !found.length && <tr><td colSpan={5} className="muted">No SQL Server connection {search ? "matches" : "the app can see"}.
                {!search && <> The app sees only connections its service principal{config?.app_principal ? <> (<span className="mono">{config.app_principal}</span>)</> : ""} has
                  USE CONNECTION on: the connection's owner grants it in Catalog Explorer, Connections, the connection, Permissions. Then Refresh.</>}</td></tr>}
            </tbody>
          </table></div>
          {tested && <div className="toolbar"><span><Tag tone="ok">{tested.typed ? "Typed" : "Connected"}</Tag> <span className="muted">
            {tested.typed ? "uses" : "reads"} {fmtInt(tested.databases.length)} {plural(tested.databases.length, "database")} through {chosen}
          </span></span></div>}
          {error != null && <TestFailed error={error} />}
          <details className="advanced">
            <summary><b>Azure SQL</b> <span className="muted">list the databases through Azure instead of master
              {azure.azure_credential ? <>: on, with {azure.azure_credential}</> : ", for a login that can't open master"}</span></summary>
            <p className="muted">Azure SQL Database and Managed Instance only. <Info>The server's databases are listed through
              Azure with a Unity Catalog service credential (Catalog Explorer, External data, Credentials) whose managed identity
              has Reader on the server, then each database is read through the connection. Whoever adds the tables needs ACCESS
              on the credential.</Info></p>
            <div className="toolbar">
              <label className="inline">Service credential<input className="search" placeholder="e.g. sql_reader" value={azureCredential}
                disabled={testing} onChange={(e) => { setAzureCredential(e.target.value); setTested(null); setError(null); }} /></label>
              <label className="inline">Server resource id<input className="search" size={60}
                placeholder="/subscriptions/…/resourceGroups/…/providers/Microsoft.Sql/servers/…" value={azureResource}
                disabled={testing} onChange={(e) => { setAzureResource(e.target.value); setTested(null); setError(null); }} /></label>
            </div>
            {Boolean(azureCredential.trim()) !== Boolean(azureResource.trim()) &&
              <span className="muted">Both are needed; with one, the databases are listed through master.</span>}
          </details>
          {error != null && cantList(error) && <TypedDatabases onUse={(names) => { setError(null); setTested({ discovery_id: null, databases: names, typed: true }); }} />}
        </div>
      )}
      <label className="radio-row"><input type="radio" checked={how === "create"} onChange={() => setHow("create")} /> Create a connection
        <Info label="What a new connection needs">A connection is made in Catalog Explorer, with its name, type, host, port, user and
          password. Making it needs CREATE CONNECTION on the metastore, and this app needs USE CONNECTION on it.</Info></label>
      {how === "create" && (
        <CreateConnectionChoices workspaceUrl={config?.workspace_url} refreshing={refreshing} onRefresh={() => { refresh(); setHow("existing"); }} />
      )}
    </>
  );
}

type Picks = Record<string, Pick | string[]>;
/** The whole server, as a flag: on, and the filter it was ticked under (it covers the databases whose names contain it). */
type WholeServer = { on: boolean; filter: string };
const DATABASES_PER_PAGE = 100;
/** Whether a whole-server tick covers a database: the same rule as SourcePick's, so the two pages never disagree. */
const coveredByServer = (server: WholeServer, database: string) => server.on && underFilter(database, server.filter);

/** What a choice takes, as Step 2 counts it: shown at its top, in its last line and in the job's Chosen summary. */
type Counts = ReturnType<typeof selectionCounts> & { listed: number };

function Step2({ connection, discoveryId, checksOn, typedNames, picks, setPicks, wholeServer, setWholeServer, read, setRead, failed,
                 setFailed, onCounts }: {
  connection: string; discoveryId: string | null; checksOn: boolean; typedNames: string[]; picks: Picks;
  setPicks: (update: (p: Picks) => Picks) => void; wholeServer: WholeServer; setWholeServer: (next: WholeServer) => void;
  read: Record<string, string>; setRead: (update: (t: Record<string, string>) => Record<string, string>) => void;
  failed: [string, string][]; setFailed: (update: (f: [string, string][]) => [string, string][]) => void;
  onCounts: (counts: Counts) => void }) {
  const [filter, setFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const [reading, setReading] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  // A discovery's databases come a page at a time from Lakebase, filtered there; typed names are a short list kept here.
  const listing = useQuery({
    queryKey: ["discovery-listing", discoveryId, filter, offset], placeholderData: keepPreviousData, enabled: discoveryId != null,
    queryFn: () => api.get<Row>(`/discoveries/${encodeURIComponent(String(discoveryId))}/listing${qs({ filter, offset, limit: DATABASES_PER_PAGE })}`),
  });
  const typedShown = typedNames.filter((d) => d.toLowerCase().includes(filter.toLowerCase()));
  const shown: string[] = discoveryId != null ? (listing.data?.databases ?? []).map((d: Row) => d.src_database) : typedShown;
  const total = discoveryId != null ? Number(listing.data?.total ?? 0) : typedNames.length;
  // Each listed database's check (can the login open it, and its tables), as the listing pages have shown them.
  const [seen, setSeen] = useState<Record<string, Row>>({});
  useEffect(() => {
    const rows: Row[] = listing.data?.databases ?? [];
    if (rows.length) setSeen((now) => ({ ...now, ...Object.fromEntries(rows.map((r) => [r.src_database, r])) }));
    // A database found closed after it was picked leaves the choice, so nothing reads it.
    const shut = rows.filter((r) => accessOf(r) === "closed").map((r) => String(r.src_database));
    if (shut.some((d) => picks[d] !== undefined)) {
      setPicks((now) => Object.fromEntries(Object.entries(now).filter(([d]) => !shut.includes(d))));
    }
  }, [listing.data]);
  const closed = (d: string) => accessOf(seen[d]) === "closed";
  // Through the SQL warehouse, the databases the page shows are checked, and only those: another page or filter checks
  // its own. The answers stay on the listing in Lakebase, so a database is checked once.
  const qc = useQueryClient();
  const [checking, setChecking] = useState(false);
  const shownKey = shown.join("\n");
  useEffect(() => {
    if (!checksOn || discoveryId == null) return;
    let stopped = false;
    (async () => {
      setChecking(true);
      try {
        for (;;) {
          const step = await api.post<Row>(`/discoveries/${encodeURIComponent(discoveryId)}/access${qs({ connection })}`,
                                           { first: shownKey ? shownKey.split("\n") : [] });
          if (stopped) return;
          await qc.invalidateQueries({ queryKey: ["discovery-listing", discoveryId] });
          if (step.error) throw new Error(String(step.error));
          if (!Number(step.pending_shown ?? step.pending)) return;
          await wait(1500);
          if (stopped) return;
        }
      } catch (e) { if (!stopped) setError(e); } finally { if (!stopped) setChecking(false); }
    })();
    return () => { stopped = true; };
  }, [checksOn, discoveryId, connection, shownKey, qc]);
  // The whole server's part of the counts: the databases its filter keeps, less those the login can't open.
  const serverTotals = useQuery({
    queryKey: ["discovery-listing", discoveryId, wholeServer.filter, "totals"], placeholderData: keepPreviousData,
    enabled: discoveryId != null && wholeServer.on,
    queryFn: () => api.get<Row>(`/discoveries/${encodeURIComponent(String(discoveryId))}/listing${qs({ filter: wholeServer.filter, offset: 0, limit: 1 })}`),
  });
  const covered = (d: string) => coveredByServer(wholeServer, d) && !closed(d);
  const pickIn = (all: Picks, d: string): Pick => {
    const p = all[d];
    if (Array.isArray(p)) return p.length === 0 ? "none" : "part";
    return p ?? "none";
  };
  const pickOf = (d: string): Pick => (covered(d) ? "all" : pickIn(picks, d));
  const typedServer: Pick = typedNames.length && typedNames.every((d) => pickOf(d) === "all") ? "all"
    : typedNames.some((d) => pickOf(d) !== "none") ? "part" : "none";
  const anyChosen = Object.values(picks).some((p) => (Array.isArray(p) ? p.length > 0 : p === "all" || p === "part"));
  const server: Pick = discoveryId == null ? typedServer : serverRowPick(wholeServer, filter, anyChosen);
  // Each change starts from the choices as they are then, so quick clicks never undo each other. A database the login
  // can't open is never chosen.
  const setAll = (names: string[], pick: Pick) => setPicks((now) => ({
    ...now, ...Object.fromEntries(names.filter((d) => pick === "none" || !closed(d)).map((d) => [d, pick])) }));
  const access = serverTotals.data?.access;
  const serverCount: ServerCount | null = discoveryId != null && wholeServer.on
    ? { matched: Number(serverTotals.data?.matched ?? total), closed: Number(access?.matched_closed ?? 0),
        unknown: checksOn ? Number(access?.matched_unknown ?? 0) : 1, tables: Number(access?.matched_tables ?? 0) }
    : null;
  const wholeNames = Object.keys(picks).filter((d) => picks[d] === "all" && !covered(d));
  const tablesOf = (d: string): number | null => (accessOf(seen[d]) === "open" && seen[d]?.tables != null ? Number(seen[d].tables) : null);
  const counts: Counts = {
    ...selectionCounts(serverCount, wholeNames.map(tablesOf),
                       Object.entries(picks).filter(([d, p]) => Array.isArray(p) && p.length > 0 && !covered(d)).map(([, p]) => (p as string[]).length)),
    listed: total };
  const countsKey = JSON.stringify(counts);
  useEffect(() => { onCounts(JSON.parse(countsKey)); }, [countsKey]);
  // A whole-server tick takes over what it covers: the picks of the databases matching its filter go. A discovery keeps
  // just the flag; typed names are each ticked, as before.
  const tickServer = () => {
    if (discoveryId == null) return setAll(typedNames, server === "all" ? "none" : "all");
    const turningOff = server === "all";
    setWholeServer({ on: !turningOff, filter });
    if (!turningOff) setPicks((now) => Object.fromEntries(Object.entries(now).filter(([d]) => !underFilter(d, filter))));
  };
  const openDatabase = async (d: string) => {
    if (open === d) return setOpen(null);
    if (!readable([d], seen).length) return;
    setOpen(d);
    if (read[d]) return;
    setReading(d); setError(null);
    try {
      const got = await readTables(connection, { databases: [d] });
      if (got.failed.length) setFailed((now) => [...now.filter(([name]) => name !== d), ...got.failed]);
      else setRead((now) => ({ ...now, [d]: got.id }));
    } catch (e) { setError(e); } finally { setReading(null); }
  };
  // Read the databases that failed once more, by name: the ones that open now are kept, the rest stay listed.
  const [readingAgain, setReadingAgain] = useState(false);
  const readAgain = async () => {
    const names = failed.map(([d]) => d);
    setReadingAgain(true); setError(null);
    try {
      const got = await readTables(connection, { databases: names });
      const still = new Set(got.failed.map(([d]) => d));
      setRead((now) => ({ ...now, ...Object.fromEntries(names.filter((d) => !still.has(d)).map((d) => [d, got.id])) }));
      setFailed(() => got.failed);
    } catch (e) { setError(e); } finally { setReadingAgain(false); }
  };
  const toggleTable = (d: string, key: string) => setPicks((now) => {
    const chosen = Array.isArray(now[d]) ? (now[d] as string[]) : [];
    return { ...now, [d]: chosen.includes(key) ? chosen.filter((k) => k !== key) : [...chosen, key] };
  });
  return (
    <>
      <p className="muted">Tick the whole server, whole databases, or open a database for its tables.
        {checksOn && <>{" "}<Info>A database the connection's login can't open is greyed out; hover on it for why.</Info></>}</p>
      <p className="pick-counts"><b>{countsText(counts, total)}</b>{counts.counting && <> <Spinner label="Counting tables" /></>}
        {counts.leftOut > 0 && <span className="muted"> · the whole server leaves out {fmtInt(counts.leftOut)} {plural(counts.leftOut, "database")} the
          login can't open</span>}
        {checking && <> <Spinner label="Checking access" /></>}</p>
      <div className="toolbar"><input className="search" placeholder="Filter databases, e.g. eng_0" value={filter}
        onChange={(e) => { setFilter(e.target.value); setOffset(0); }} />
        <button className="ghost" onClick={() => setAll(shown.filter((d) => !covered(d) && !closed(d)), "all")}>Select all shown</button></div>
      <div className="table-wrap tree-pick"><table className="data tree">
        <thead><tr><th>Server › database › schema.table</th><th className="num">Tables</th><th>Load type</th></tr></thead>
        <tbody>
          <tr><td className="name"><TriBox pick={server} label={`The whole server ${connection}`} onClick={tickServer} />{" "}
            <b>{connection}</b>, {discoveryId != null && wholeServer.on && wholeServer.filter
              ? <>every database matching "{wholeServer.filter}"</> : "the whole server"}</td>
            <td className="num">{fmtInt(total)} {plural(total, "database")}</td>
            <td className="muted">{server === "part" ? "chosen in part" : ""}</td></tr>
          {shown.slice(0, 500).map((d) => (
            <Fragment key={d}>
              <tr className={`l2${closed(d) ? " closed" : ""}`}><td className="name" style={{ paddingLeft: 30 }}>
                <TriBox pick={pickOf(d)} label={`The whole database ${d}`} onClick={() => !covered(d) && setAll([d], pickOf(d) === "all" ? "none" : "all")} />{" "}
                <Twisty open={open === d} onToggle={() => openDatabase(d)}
                  closed={closed(d) ? `${d} can't be opened: ${String(seen[d]?.why ?? "the connection's login can't open this database")}` : undefined}>{d}</Twisty></td>
                <td className="num">{reading === d ? "reading…" : ""}</td>
                <td className="muted">{closed(d) ? <Tip text={String(seen[d]?.why ?? "the connection's login can't open this database")} focusable>
                  can't open: hover for why</Tip>
                  : pickOf(d) === "part" ? `${fmtInt((picks[d] as string[]).length)} chosen`
                  : tablesOf(d) != null ? `${fmtInt(tablesOf(d))} ${plural(tablesOf(d), "table")}`
                  : checksOn && accessOf(seen[d]) === "unknown" ? "checking…" : ""}</td></tr>
              {open === d && read[d] && <DatabaseTables kind="discovery" sourceId={read[d]} server={connection} database={d}
                whole={pickOf(d) === "all"} chosen={Array.isArray(picks[d]) ? (picks[d] as string[]) : []} onToggle={(key) => toggleTable(d, key)} />}
            </Fragment>
          ))}
          {discoveryId == null && shown.length > 500 && <tr className="l2"><td colSpan={3} className="muted" style={{ paddingLeft: 30 }}>
            and {fmtInt(shown.length - 500)} more: filter to find one, or tick the whole server</td></tr>}
          {discoveryId == null && typedNames.length > shown.length && <tr className="l2"><td colSpan={3} className="muted" style={{ paddingLeft: 30 }}>
            {fmtInt(typedNames.length - shown.length)} more {plural(typedNames.length - shown.length, "database")} hidden by the filter</td></tr>}
        </tbody>
      </table></div>
      {discoveryId != null && <Pager offset={offset} limit={DATABASES_PER_PAGE} total={Number(listing.data?.matched ?? 0)} noun="database"
        onPage={setOffset} />}
      <Unread failed={failed} reading={readingAgain} onReadAgain={readAgain} />
      <ErrorBox error={listing.error ?? error} />
    </>
  );
}

/** Beside Next after a failed test: that it failed, with its codes; the message and what to do are above. */
const testFailedNote = (error: unknown) => {
  const { code } = connectionFailure(String((error as Error)?.message ?? error), (error as SourceError)?.codes);
  return `The test failed${code ? `: ${code}` : ""}. Next tests it again`;
};

/** What Discover hands to New and Edit: the discoveries the tables are in, and what of them was chosen (main.Selection). */
export type DiscoveredPick = { source: Row; label: string; connection?: string; drafts?: string[] };

/** Discover on a source: the two steps, then what was chosen by reference (onClose(pick)), or nothing on Cancel. With
 *  ``drafts`` (New job), through the SQL warehouse, the tables of what is chosen whole start being read as it is
 *  chosen, and Add hands the reads over still going (pick.drafts): New job keeps them going, and Create waits for what
 *  is left. Without it (Edit's Add tables), Add waits for the reads, as before. */
export function DiscoverDialog({ onClose, drafts = false }: { onClose: (pick: DiscoveredPick | null) => void; drafts?: boolean }) {
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [connection, setConnection] = useState("");
  const [tested, setTested] = useState<Row | null>(null);
  const [testing, setTesting] = useState(false);
  const [testError, setTestError] = useState<unknown>(null);
  const [azureCredential, setAzureCredential] = useState("");
  const [azureResource, setAzureResource] = useState("");
  const [picks, setPicks] = useState<Picks>({});
  const [wholeServer, setWholeServer] = useState<WholeServer>({ on: false, filter: "" });
  // The whole server's discovery, read once (under its filter, through Azure if tested so); its databases aren't listed here.
  const [serverRead, setServerRead] = useState<string | null>(null);
  const [read, setRead] = useState<Record<string, string>>({});
  const [failed, setFailed] = useState<[string, string][]>([]);
  const [error, setError] = useState<unknown>(null);
  // What a discovery run the dialog waits on is doing, and since when: the connection test, or the reading on Add.
  const [waiting, setWaiting] = useState<DialogWait | null>(null);
  const now = useNow(waiting !== null);
  const waitText = waiting ? waitLine(waiting.reading, waiting.asked, now) : null;
  const discoveryId: string | null = tested?.discovery_id ?? null;
  const throughWarehouse = tested?.via === "warehouse";
  const [counts, setCounts] = useState<Counts | null>(null);
  const choose = (name: string) => { setConnection(name); setTested(null); setTestError(null); };
  // Next on the connection step: the test (the server's databases listed), then step 2 once it passes. A test that
  // passed for the connection chosen isn't run again; choosing another clears it.
  const testThenNext = async () => {
    if (tested) return setStep(2);
    const azure = azureOf(azureCredential, azureResource);
    setTesting(true); setTestError(null);
    const asked = new Date();
    setWaiting({ asked, reading: "Testing the connection" });
    try {
      const { id, via } = await discover(connection, { kind: "list", ...azure });
      const rows = await api.get<Row[]>(`/discoveries/${id}/databases`);
      setTested({ discovery_id: id, via, databases: rows.filter((r) => r.status === "listed").map((r) => r.src_database), azure });
      setStep(2);
    } catch (e) { setTestError(e); } finally { setTesting(false); setWaiting(null); }
  };
  const countsLine = counts ? countsText(counts, counts.listed) : "";
  const typedNames: string[] = discoveryId == null ? (tested?.databases ?? []) : [];
  const whole = Object.keys(picks).filter((d) => picks[d] === "all" && !coveredByServer(wholeServer, d));
  const handPicked = Object.entries(picks).filter(([d, p]) => Array.isArray(p) && p.length > 0 && !coveredByServer(wholeServer, d)) as [string, string[]][];
  const chosen = (wholeServer.on ? 1 : 0) + whole.length + handPicked.length;
  const chosenWords = [wholeServer.on ? (wholeServer.filter ? `every database matching "${wholeServer.filter}"` : "the whole server") : "",
    whole.length ? `${fmtInt(whole.length)} whole ${plural(whole.length, "database")}` : "",
    handPicked.length ? `tables from ${fmtInt(handPicked.length)} more` : ""].filter(Boolean).join(", ");
  // Databases chosen whole whose tables aren't read yet are discovered on Add. The whole server is read once, only the
  // databases its filter keeps (likeContaining), and the filter also goes with the job's selection. The databases picked
  // one by one outside that filter are read by name.
  // The drafts of this choice: one for the databases chosen whole (it grows as more are chosen), one for the whole
  // server under its filter (made again when the filter changes). Their ids are kept in a ref, so the calls that make
  // them run one after another on the choice as it is when each runs.
  const drafting = drafts && throughWarehouse && discoveryId != null;
  const latest = useRef({ whole, read, wholeServer });
  latest.current = { whole, read, wholeServer };
  const draftState = useRef<{ named: string | null; names: Set<string>; server: { id: string; filter: string } | null }>(
    { named: null, names: new Set(), server: null });
  const syncing = useRef<Promise<void>>(Promise.resolve());
  const handed = useRef(false);
  const [draftIds, setDraftIds] = useState<string[]>([]);
  const [draftGeneration, setDraftGeneration] = useState(0);
  const syncDrafts = () => {
    syncing.current = syncing.current.then(async () => {
      const d = draftState.current;
      const { whole: wholeNow, read: readNow, wholeServer: serverNow } = latest.current;
      const fresh = wholeNow.filter((name) => !d.names.has(name) && !readNow[name]);
      if (fresh.length) {
        const got = await api.post<Row>("/discoveries/source", { src_connection: connection, kind: "tables", databases: fresh,
                                                                  ...(d.named ? { draft_id: d.named } : { draft: true }) });
        d.named = String(got.discovery_id);
        fresh.forEach((name) => d.names.add(name));
      }
      const want = serverNow.on ? serverNow.filter : null;
      if (d.server && d.server.filter !== want) { discardDrafts([d.server.id]); d.server = null; }
      if (want !== null && !d.server) {
        const got = await api.post<Row>("/discoveries/source", { src_connection: connection, kind: "tables", whole_server: true,
                                                                  listing_id: discoveryId, filter: want, draft: true });
        d.server = { id: String(got.discovery_id), filter: want };
      }
      setDraftIds([d.server?.id, d.named].filter((id): id is string => Boolean(id)));
      setDraftGeneration((g) => g + 1);
    }).catch((e) => setError(e));
    return syncing.current;
  };
  const wholeKey = whole.join("\n");
  useEffect(() => { if (drafting) syncDrafts(); }, [drafting, wholeKey, wholeServer.on, wholeServer.filter]);
  const draftSteps = useDraftReads(connection, draftIds, draftGeneration);
  const allDrafts = () => [draftState.current.server?.id, draftState.current.named].filter((id): id is string => Boolean(id));
  // Leaving the page with the dialog open, or closing it any way but Add, discards what it started reading.
  useEffect(() => {
    const leave = () => { if (!handed.current) discardDrafts(allDrafts(), true); };
    window.addEventListener("pagehide", leave);
    return () => { window.removeEventListener("pagehide", leave); leave(); };
  }, []);
  const draftProgress = Object.values(draftSteps).reduce((sum: Row, step: Row) => ({
    databases: sum.databases + Number(step.databases ?? 0), done: sum.done + Number(step.done ?? 0),
    failed: sum.failed + Number(step.failed ?? 0), tables: sum.tables + Number(step.tables ?? 0),
    pending: sum.pending + Number(step.pending ?? 0) }), { databases: 0, done: 0, failed: 0, tables: 0, pending: 0 });
  const label = () => (wholeServer.on ? `${connection}, ${chosenWords}` : `${chosenWords} on ${connection}`)
    + (counts ? ` (${countsLine.replace(/^Chosen: /, "")})` : "");
  const finish = async () => {
    if (drafting) {
      // The reads go on from New job's page; what isn't read yet is waited for at Create.
      setError(null);
      await syncDrafts();
      const ids = new Set([...allDrafts(), ...Object.values(read)]);
      const tables = handPicked.map(([d, keys]) => ({ server: connection, database: d, tables: keys }));
      const source = { discovery_ids: [...ids], whole_server: wholeServer.on, filter: wholeServer.on ? wholeServer.filter : "",
                       databases: whole.map((d) => ({ server: connection, database: d })), tables };
      handed.current = true;
      onClose({ source, label: label(), connection, drafts: allDrafts() });
      return;
    }
    setStep(3); setError(null);
    const asked = new Date();
    const onProgress = (answer: Row) => setWaiting((now) => now && { ...now, ...readsDone(answer) });
    setWaiting({ asked, reading: "Reading tables" });
    try {
      let readServer = serverRead;
      if (wholeServer.on && !readServer) {
        // Through the warehouse, the whole server is the test's listing under the filter, less the databases the login
        // can't open; through the discovery job, it is listed again under the filter's LIKE pattern.
        const pattern = likeContaining(wholeServer.filter);
        const request = throughWarehouse ? { whole_server: true, listing_id: discoveryId, filter: wholeServer.filter }
          : { whole_server: true, ...(pattern ? { pattern } : {}), ...(tested?.azure ?? {}) };
        const got = await readTables(connection, request, onProgress);
        if (got.failed.length) { setFailed(() => got.failed); setStep(2); return; }
        readServer = got.id; setServerRead(got.id);
      }
      const unread = whole.filter((d) => !read[d]);
      const ids = new Set([...(readServer ? [readServer] : []), ...Object.values(read)]);
      if (unread.length) {
        // Databases typed by hand are read by name.
        const got = await readTables(connection, { databases: unread }, onProgress);
        ids.add(got.id);
        if (got.failed.length) {
          // Keep what was read, and say which databases weren't, before anything is added.
          const bad = new Set(got.failed.map(([d]) => d));
          setRead((now) => ({ ...now, ...Object.fromEntries(unread.filter((d) => !bad.has(d)).map((d) => [d, got.id])) }));
          setFailed(() => got.failed); setStep(2);
          return;
        }
      }
      const tables = handPicked.map(([d, keys]) => ({ server: connection, database: d, tables: keys }));
      const source = { discovery_ids: [...ids], whole_server: wholeServer.on, filter: wholeServer.on ? wholeServer.filter : "",
                       databases: whole.map((d) => ({ server: connection, database: d })), tables };
      onClose({ source, label: label() });
    } catch (e) { setError(e); setStep(2); } finally { setWaiting(null); }
  };
  const title = step === 1 ? "Add tables · Step 1 of 2: the connection" : `Add tables · Step 2 of 2: choose on ${connection}`;
  const blocked = step === 1 ? connectionNextBlock(Boolean(connection), Boolean(tested), testing ? waitText ?? "starting" : null)
    : step === 3 ? waitText ?? "Reading tables" : chosen ? undefined : "Choose the server, databases or tables first";
  return (
    <Modal title={title} action={step === 1 ? connectionNextLabel(testing) : step === 3 ? "Reading…" : "Add the tables chosen"}
      actionDisabled={blocked} actionNote={step === 1 && testError != null ? testFailedNote(testError) : undefined} onClose={(ok) => {
        if (!ok) return onClose(null);
        if (step === 1) return testThenNext();
        finish();
      }}>
      <div className="discover">
        {step === 1 && <Step1 chosen={connection} setChosen={choose} tested={tested} setTested={setTested} testing={testing}
          error={testError} setError={setTestError} azureCredential={azureCredential} setAzureCredential={setAzureCredential}
          azureResource={azureResource} setAzureResource={setAzureResource} />}
        {step >= 2 && <Step2 connection={connection} discoveryId={discoveryId} checksOn={throughWarehouse} typedNames={typedNames}
          picks={picks} setPicks={setPicks} wholeServer={wholeServer} setWholeServer={setWholeServer} read={read} setRead={setRead}
          failed={failed} setFailed={setFailed} onCounts={setCounts} />}
        {waiting && <Busy label={waiting.reading} since={waiting.asked} done={waiting.done} total={waiting.total} />}
        {step === 2 && chosen > 0 && <p className="muted">{chosenWords} chosen. {countsLine}.</p>}
        {step === 2 && drafting && draftProgress.pending > 0 && <Busy label="Reading tables" {...readsDone(draftProgress)} />}
        <ErrorBox error={error} />
      </div>
    </Modal>
  );
}
