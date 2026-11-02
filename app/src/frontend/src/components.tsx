import { Fragment, createContext, useCallback, useContext, useEffect, useId, useLayoutEffect, useMemo, useRef, useState,
         useSyncExternalStore, type ReactNode } from "react";
import { type BarPart, type BarSummary, type StateCounts, progressParts, progressSummary, stateParts, stateSummary } from "./bars";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { AgGridReact } from "ag-grid-react";
import { themeQuartz, type ColDef, type GridApi, type GridOptions } from "ag-grid-community";
import { ApiError, type Row } from "./api";
import { longWait } from "./pending";

// AG Grid in the workspace look: the design system's font, border, header and accent colors (styles.css).
export const gridTheme = themeQuartz.withParams({
  fontFamily: "var(--db-font-family)", fontSize: 13, headerFontSize: 13, spacing: 5, wrapperBorderRadius: 8,
  borderColor: "var(--db-border-base)", headerBackgroundColor: "var(--db-surface-subtle)", headerFontWeight: 600,
  foregroundColor: "var(--db-text-base)", accentColor: "var(--db-accent-surface)", rowHoverColor: "var(--db-surface-hover)",
  selectedRowBackgroundColor: "var(--db-surface-selected)",
});
const theme = gridTheme;

/** The element's current width in pixels, kept up to date as the window resizes. A callback ref, so it also
 * measures elements that appear later (after data loads). */
// A job run the server reports as live: running, or cancelling until the run it cancelled has closed.
export const isLiveOutcome = (label: string | undefined) => label === "Running" || label === "Cancelling";

export function useWidth<T extends HTMLElement>(): [(element: T | null) => void, number] {
  const [element, setElement] = useState<T | null>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);
  return [setElement, width];
}

export function fmtTime(v: unknown): string {
  if (!v) return "";
  const d = new Date(String(v));
  return isNaN(d.getTime()) ? String(v) : d.toLocaleString();
}

/** "28 Oct, 16:01": a short date and time for a table cell, the full one on hover (fmtTime). */
export function fmtShort(v: unknown): string {
  if (!v) return "";
  const d = new Date(String(v));
  return isNaN(d.getTime()) ? String(v) : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

// Headings in words for the state database's column names, as the Databricks lists and Catalog Explorer show them.
const HEADINGS: Record<string, string> = {
  id: "Table ID", control_id: "Table ID", task_enabled: "Enabled", task_collection: "Ingestion group", src_type: "Source type",
  load_type: "Load type", src_database: "Source database", src_schema: "Source schema", src_table: "Source table",
  src_host: "Server", src_port: "Port", src_connection: "UC connection", sink_catalog: "Target catalog",
  sink_schema: "Target schema", sink_table: "Target table", sink_fqn: "Target table", primary_key: "Primary key",
  changed_at: "Changed at", changed_by: "Changed by", action: "Action", old_value: "Old value", new_value: "New value",
  run_id: "Run ID", job_id: "Job ID", sink: "Target table", state: "Status", result: "Result", url: "Link", started: "Started", ended: "Ended", trigger: "Trigger",
  schedule: "Schedule", freshness_sla_minutes: "Freshness target (minutes)", last_success_at: "Last loaded",
  status: "Status", phase: "Phase", created_at: "Created", updated_at: "Last updated", client_id: "Client ID",
  write_mode: "Write mode", cursor_column: "Cursor column", select_list: "Columns", secret_key: "Secret key",
  use_remote_query: "Read through UC connection", error: "Error", stage: "Stage", position: "Position",
};

// Coded values in words, the same on every page, as the Edit view names them and the Runs list names triggers.
const VALUE_WORDS: Record<string, Record<string, string>> = {
  load_type: { ct: "Change tracking", cursor: "Cursor column", full: "Full reload", cdc: "Change data capture" },
  write_mode: { merge: "Merge: current state", append: "Append: change log" },
  src_type: { sqlserver: "SQL Server", oracle: "Oracle", postgresql: "PostgreSQL", mysql: "MySQL" },
  trigger: { ONE_TIME: "Manual", PERIODIC: "Scheduled", RETRY: "Retry", RUN_JOB_TASK: "Started by a job",
             FILE_ARRIVAL: "File arrival", TABLE: "Table update", CONTINUOUS: "Continuous" },
};

/** A coded value in words for its column ("ct" in load_type -> "Change tracking"); other values as they are. */
export function valueName(key: string, value: unknown): string {
  const v = value === null || value === undefined ? "" : String(value);
  return VALUE_WORDS[key]?.[v] ?? v;
}

/** A column name in words: its heading, else the name with spaces and a capital ("rows_merged" -> "Rows merged"). */
export function heading(key: string): string {
  return HEADINGS[key] ?? (key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) || " ");
}

/** Column defs from the first row's keys (with per-column overrides), so pages stay short. */
export function autoColumns(rows: Row[], overrides: Record<string, Partial<ColDef>> = {}, hide: string[] = []): ColDef[] {
  const keys = rows.length ? Object.keys(rows[0]) : Object.keys(overrides);
  return keys.filter((k) => !hide.includes(k)).map((k) => ({
    field: k,
    headerName: heading(k),
    ...(/(_at|started|finished|ended)$/.test(k)
      ? { valueFormatter: (p: any) => fmtShort(p.value), tooltipValueGetter: (p: any) => fmtTime(p.value) }
      : VALUE_WORDS[k] ? { valueFormatter: (p: any) => valueName(k, p.value) } : {}),
    ...overrides[k],
  }));
}

/** Space kept free below a panel that fills the window: the card's padding and the page's bottom margin. */
const FILL_GAP = 64;

/**
 * The height that lets an element reach the bottom of the window from where it starts on the page, so a page's
 * main table uses the screen's height and scrolls inside itself. ``share`` takes a part of the space left (for a
 * page with two panels), ``reserve`` leaves room in pixels for a panel below, ``min`` keeps it usable on short
 * windows. Kept up to date as the window or page changes.
 */
export function useFillHeight<T extends HTMLElement>(min = 280, share = 1, reserve = 0): [(element: T | null) => void, number] {
  const [element, setElement] = useState<T | null>(null);
  const [height, setHeight] = useState(min);
  useEffect(() => {
    if (!element) return;
    const measure = () => {
      const top = element.getBoundingClientRect().top + window.scrollY;
      setHeight(Math.max(min, Math.floor((window.innerHeight - top - FILL_GAP - reserve) * share)));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(document.body);
    window.addEventListener("resize", measure);
    return () => { observer.disconnect(); window.removeEventListener("resize", measure); };
  }, [element, min, share, reserve]);
  return [setElement, height];
}

/** A box for a plain table that fills the window's height and scrolls inside, with its header kept in view. */
export function FillBox({ children, min, share, reserve }: { children: ReactNode; min?: number; share?: number; reserve?: number }) {
  const [ref, height] = useFillHeight<HTMLDivElement>(min, share, reserve);
  return <div ref={ref} className="table-wrap fill" style={{ maxHeight: height }}>{children}</div>;
}

const ROW_HEIGHT = 34, HEADER_HEIGHT = 38;

export function Grid(props: {
  rows: Row[] | undefined;
  columns?: ColDef[];
  /** A fixed height; by default the grid fills the window's height from where it starts. */
  height?: number | string;
  /** Size to the rows when they are few (a short list), never taller than the window allows. */
  fitRows?: boolean;
  /** Part of the window's remaining height to take, when the page has another panel below. */
  fillShare?: number;
  /** Pixels to leave below the grid for a small panel that follows it. */
  fillReserve?: number;
  selection?: "single" | "multiple";
  onSelect?: (rows: Row[]) => void;
  options?: GridOptions;
  getRowId?: (r: Row) => string;
  /** Per-column filter menus; off on pages with their own search or a short list, as the Databricks lists keep one search. */
  filters?: boolean;
}) {
  const cols = useMemo(() => props.columns ?? autoColumns(props.rows ?? []), [props.columns, props.rows]);
  const box = useRef<HTMLDivElement>(null);
  const [fillRef, fillHeight] = useFillHeight<HTMLDivElement>(props.fitRows ? 140 : 280, props.fillShare ?? 1, props.fillReserve ?? 0);
  // Rows and header at fixed heights, so a short list's box fits its rows with no blank space below them.
  const rowsHeight = HEADER_HEIGHT + (props.rows?.length ?? 0) * ROW_HEIGHT + 4;
  const height = props.height ?? (props.fitRows ? Math.min(rowsHeight, fillHeight) : fillHeight);
  const byContent = (props.options?.autoSizeStrategy?.type ?? "fitCellContents") === "fitCellContents";
  /** Columns sized to their content, then stretched when they leave the grid's width unused (wide screens). */
  const fill = (gridApi: GridApi) => {
    const width = box.current?.clientWidth ?? 0;
    if (!width || gridApi.isDestroyed()) return;
    if (byContent) gridApi.autoSizeAllColumns();
    const used = gridApi.getAllDisplayedColumns().reduce((sum, column) => sum + column.getActualWidth(), 0);
    if (used < width - 2) gridApi.sizeColumnsToFit();
  };
  return (
    <div ref={(element) => { box.current = element; fillRef(element); }} style={{ height, width: "100%" }}>
      <AgGridReact
        theme={theme}
        rowData={props.rows ?? []}
        columnDefs={cols}
        defaultColDef={{ sortable: true, filter: props.filters ?? true, resizable: true, minWidth: 90 }}
        rowHeight={ROW_HEIGHT} headerHeight={HEADER_HEIGHT}
        autoSizeStrategy={{ type: "fitCellContents" }}
        rowSelection={props.selection
          ? { mode: props.selection === "single" ? "singleRow" : "multiRow", enableClickSelection: props.selection === "single" }
          : undefined}
        onSelectionChanged={(e) => props.onSelect?.(e.api.getSelectedRows())}
        getRowId={props.getRowId ? (p) => props.getRowId!(p.data) : undefined}
        loading={props.rows === undefined}
        onFirstDataRendered={(e) => fill(e.api)}
        onRowDataUpdated={(e) => fill(e.api)}
        onGridSizeChanged={(e) => fill(e.api)}
        {...props.options}
      />
    </div>
  );
}

export function Metric({ label, value, tone }: { label: string; value: ReactNode; tone?: "bad" | "warn" | "ok" }) {
  return (
    <div className={`metric ${tone ?? ""}`}>
      <div className="metric-label">{label}</div>
      <div className="metric-value">{value}</div>
    </div>
  );
}

// Status names as the Databricks Jobs UI has them, in sentence case and the same on every page, for the engine's and
// the Jobs API's values alike.
const STATUS_NAMES: Record<string, string> = {
  completed: "Succeeded", success: "Succeeded", succeeded: "Succeeded", failed: "Failed", internal_error: "Failed",
  running: "Running", pending: "Pending", queued: "Queued", blocked: "Blocked", skipped: "Skipped", terminated: "Terminated",
  terminating: "Terminating", canceled: "Canceled", cancelled: "Canceled", timedout: "Timed out", timed_out: "Timed out",
  paused: "Paused", unpaused: "Active", ok: "Active", abandoned: "Ended without finishing", "no run": "Not run",
  success_with_failures: "Succeeded with failures", synced: "Synced", in_sync: "In sync", drifted: "Changed outside the app",
};

/** A status value in the Databricks UI's words ("SUCCESS" and "completed" -> "Succeeded"); other values capitalized. */
export function statusName(value: unknown): string {
  const v = String(value ?? "");
  return STATUS_NAMES[v.toLowerCase()] ?? (v ? v[0].toUpperCase() + v.slice(1) : "");
}

export function Badge({ value }: { value: unknown }) {
  const v = String(value ?? "").toLowerCase();
  const tone = ["failed", "invalid", "internal_error", "timedout"].includes(v) ? "bad"
    : ["running", "pending", "queued", "seeding"].includes(v) ? "run"
    : ["skipped", "breached", "drifted"].includes(v) ? "warn"
    : ["completed", "success", "succeeded", "registered", "valid", "incremental", "loaded", "synced", "in_sync"].includes(v) ? "ok" : "";
  return <span className={`badge ${tone}`}>{statusName(value)}</span>;
}

// ---- monitoring views --------------------------------------------------------------------------

const NUMBER = new Intl.NumberFormat("en-US");

/** 1234567 -> "1,234,567"; empty for null. */
export function fmtInt(v: unknown): string {
  return v === null || v === undefined || v === "" ? "" : NUMBER.format(Number(v));
}

const COMPACT = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

/** A count for a metric tile: in full below a billion, else short ("296.3B") with the full count on hover, so it fits. */
export function BigCount({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Math.abs(n) < 1e9 ? <>{fmtInt(n)}</> : <span title={fmtInt(n)}>{COMPACT.format(n)}</span>;
}

/** A host name that wraps only after a dot, never inside a name or the port. */
/** Text shown on hover, focus or tap in the page itself (data-tip), not the browser's title: drawn at the page's top
 * level, below its target or above it when there is no room, and always kept inside the window, so no table or
 * scrolling box can cut it off. It closes on scroll, resize, Escape or a tap elsewhere, so it never stays behind
 * at an old place. `focusable` makes it a tab stop (for text that is cut off, or a help note). */
export function Tip({ text, children, focusable = false }: { text: string; children: ReactNode; focusable?: boolean }) {
  const ref = useRef<HTMLSpanElement>(null);
  const id = useId();
  const [at, setAt] = useState<DOMRect | null>(null);
  const show = () => ref.current && setAt(ref.current.getBoundingClientRect());
  const hide = () => setAt(null);
  useEffect(() => {
    if (!at) return;
    const away = (e: Event) => { if (!ref.current?.contains(e.target as Node)) hide(); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") hide(); };
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", key);
    return () => {
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", key);
    };
  }, [at]);
  return (
    <span ref={ref} className="tip-target" data-tip={text} tabIndex={focusable ? 0 : undefined}
      aria-describedby={at ? id : undefined} onMouseEnter={show} onFocus={show} onMouseLeave={hide} onBlur={hide}
      onClick={(e) => { e.stopPropagation(); if (at) hide(); else show(); }}>
      {children}
      {at && createPortal(<TipPop id={id} text={text} target={at} />, document.body)}
    </span>
  );
}

function TipPop({ id, text, target }: { id: string; text: string; target: DOMRect }) {
  const ref = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState({ left: target.left, top: target.bottom + 6, ready: false });
  useLayoutEffect(() => {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    const gap = 6, edge = 4;
    const below = target.bottom + gap;
    const top = below + box.height <= window.innerHeight - edge ? below : Math.max(edge, target.top - gap - box.height);
    const left = Math.min(Math.max(edge, target.left), Math.max(edge, window.innerWidth - box.width - edge));
    setPlace({ left, top, ready: true });
  }, [target]);
  return <div ref={ref} id={id} className="tip-pop" role="tooltip"
    style={{ left: place.left, top: place.top, visibility: place.ready ? "visible" : "hidden" }}>{text}</div>;
}

/** A host name on one line: cut with an ellipsis when it does not fit, its full name (and port) on hover. */
export function Host({ host, port }: { host: string; port?: unknown }) {
  const full = port ? `${host}:${port}` : host;
  const ref = useRef<HTMLSpanElement>(null);
  const [cut, setCut] = useState(false);       // only a name that is cut off is a tab stop
  useLayoutEffect(() => {
    const measure = () => ref.current && setCut(ref.current.scrollWidth > ref.current.clientWidth + 1);
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [full]);
  return <Tip text={full} focusable={cut}><span ref={ref} className="host">{full}</span></Tip>;
}

/** "database" for one, "databases" for any other count. */
export function plural(count: unknown, word: string): string {
  return Number(count) === 1 ? word : `${word}s`;
}

/** Seconds -> "8m 12s", "1h 4m", "41s". */
export function fmtDuration(seconds: unknown): string {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/** A timestamp -> "4 s ago", "38 min ago", "2.0 h ago", "3 days ago"; "never" without one. */
export function fmtAgo(v: unknown, now = Date.now()): string {
  if (!v) return "never";
  const s = (now - new Date(String(v)).getTime()) / 1000;
  if (!isFinite(s)) return String(v);
  if (s < 60) return `${Math.max(0, Math.round(s))} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 48 * 3600) return `${(s / 3600).toFixed(1)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

/** Hours and minutes of a timestamp, local time. */
export function fmtClock(v: unknown): string {
  return v ? new Date(String(v)).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
}

export type Tone = "ok" | "warn" | "bad" | "run" | "first" | "";

/** A status tag with a dot and a label, never color alone. */
export function Tag({ tone = "", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`tag ${tone}`}><span className="dot" />{children}</span>;
}

export interface Outcome { label: string; tone: string; note?: string | null }

/** A run's status as a tag: its outcome (queries.run_outcome) when the server sent one, else its engine status. */
export function RunStatus({ status, outcome }: { status: unknown; outcome?: Outcome | null }) {
  if (outcome) {
    const tone = (["ok", "warn", "bad", "run"].includes(outcome.tone) ? outcome.tone : "") as Tone;
    return <span title={outcome.note ?? undefined}><Tag tone={tone}>{outcome.label}</Tag></span>;
  }
  const v = String(status ?? "");
  const tone: Tone = v === "completed" ? "ok" : v === "running" ? "run" : v === "failed" || v === "abandoned" ? "bad"
    : v === "skipped" ? "warn" : "";
  return <Tag tone={tone}>{statusName(v) || "Unknown"}</Tag>;
}

/** Tables a minute -> "12", "2.5", "0.4". */
export function fmtSpeed(perMinute: unknown): string {
  const v = Number(perMinute) || 0;
  return v >= 10 ? fmtInt(Math.round(v)) : v.toFixed(1);
}

/** Seconds left -> "about 7 min", "under a minute", "about 2.5 h". */
export function fmtLeft(seconds: unknown): string {
  const s = Math.max(0, Number(seconds) || 0);
  if (s < 60) return "under a minute";
  if (s < 5400) return `about ${Math.round(s / 60)} min`;
  return `about ${(s / 3600).toFixed(1)} h`;
}

export type { StateCounts } from "./bars";

/** The counts under a bar, in the bar's colours (bars.ts says what they are). */
function BarCountsLine({ summary, stacked }: { summary: BarSummary; stacked?: boolean }) {
  const className = `bar-counts${stacked ? " stacked" : ""}`;
  if (summary.allDone) return <span className={className}><span className="tone-loaded all-done"><Check /> {summary.allDone}</span></span>;
  return <span className={className}>{summary.items.map((entry) => <span key={entry.text} className={`tone-${entry.tone}`}>{entry.text}</span>)}</span>;
}

function Check() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <path d="M3 8.5 6.5 12 13 4.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** The segments of a bar, in the Databricks UI's status colours. */
function Segments({ parts, total, big }: { parts: BarPart[]; total: number; big?: boolean }) {
  const label = parts.map((p) => `${fmtInt(p.count)} ${p.word}`).join(", ");
  return (
    <div className={`bar${big ? " big" : ""}`} role="img" aria-label={label}>
      {total > 0 && parts.map((p) => p.count > 0 && <span key={p.word} className={`seg-${p.tone}`} style={{ width: `${(100 * p.count) / total}%` }} />)}
    </div>
  );
}

/** Each state now as one bar, with its counts written under it. full: a card's bar, every state counted. saysAllDone false
 * keeps "All N loaded" out (the jobs lists count "N of N loaded" instead). */
export function StateBar({ row, full, saysAllDone = true }: { row: StateCounts; full?: boolean; saysAllDone?: boolean }) {
  const { parts } = stateParts(row);
  return (
    <div className="state-bar">
      <Segments parts={parts} total={parts.reduce((a, p) => a + p.count, 0)} big={full} />
      <BarCountsLine summary={stateSummary(row, full, saysAllDone)} stacked={full} />
    </div>
  );
}

/** A count split into states: one bar, and under it a legend of the counts that are not zero. legend false leaves the
 * legend out when the bar and the headline already say it; note is a line in its place. links name a state's words. */
export function CountBar({ parts, links = {}, legend = true, note, whole }: { parts: BarPart[]; links?: Record<string, string>;
                           legend?: boolean; note?: ReactNode; whole?: number }) {
  const total = Math.max(whole ?? 0, parts.reduce((sum, part) => sum + part.count, 0));
  return (
    <div className="summary">
      <Segments parts={parts} total={total} />
      {(legend || note) && <div className="key">
        {legend && parts.filter((part) => part.count > 0).map((part) => (
          <span key={part.tone}><i className={`seg-${part.tone}`} /><b>{fmtInt(part.count)}</b>{" "}
            {links[part.tone] ? <Link to={links[part.tone]}>{part.word}</Link> : part.word}</span>))}
        {note}
      </div>}
    </div>
  );
}

/** A card's tables by state: the total, one bar and a legend of the counts that are not all loaded. A late table counts
 * as loaded. Once every table is loaded the legend is left out, as the total and the full bar say it; looksGood writes
 * "Looks good" in its place, and lateDatabases adds the late databases beneath, when there are any. */
export function StateSummary({ row, links = {}, looksGood, lateDatabases }: { row: StateCounts; links?: Record<string, string>;
                               looksGood?: boolean; lateDatabases?: boolean }) {
  const parts: BarPart[] = [
    { count: Number(row.state_loaded ?? row.state_current ?? 0) + Number(row.state_late ?? 0), word: "loaded", tone: "loaded" },
    { count: Number(row.state_first_load ?? 0), word: "waiting for a first load", tone: "waiting" },
    { count: Number(row.state_failing ?? 0), word: "failing", tone: "failing" },
  ];
  const total = parts.reduce((sum, part) => sum + part.count, 0);
  if (!total) return <div className="metric-value muted">No tables yet</div>;
  const allLoaded = parts[1].count === 0 && parts[2].count === 0;
  const late = Number(row.late_databases ?? 0);
  return (
    <>
      <div className="metric-value">{fmtInt(total)}<small>{plural(total, "table")}</small></div>
      <CountBar parts={parts} links={links} legend={!allLoaded}
        note={allLoaded && looksGood ? <span className="all-done"><Check /> Looks good</span> : undefined} />
      {lateDatabases && late > 0 && <div className="metric-sub">{fmtInt(late)} late {plural(late, "database")}</div>}
    </>
  );
}

export function StateKey() {
  return (
    <div className="key">
      <span><i className="seg-loaded" />Loaded</span><span><i className="seg-late" />Late</span>
      <span><i className="seg-waiting" />Waiting for a first load</span><span><i className="seg-failing" />Failing</span>
    </div>
  );
}

export interface Progress { unchanged: number; loaded: number; inFlight: number; failed: number; total: number; notRun?: number }

/** A run's progress over its collection: checked (unchanged + loaded), in flight, failed, to go. */
export function progressOf(run: Row): Progress {
  const total = Number(run.enabled_tables || 0);
  const loaded = Number(run.tables_ok || 0);
  const checked = Number(run.tables_checked || 0);
  return { total, loaded, unchanged: Math.max(0, checked - loaded), inFlight: Number(run.tables_running || 0),
           failed: Number(run.tables_failed || 0) };
}

/** A run's progress as one bar in the Databricks UI's status colours: unchanged and loaded (done), in flight, failed,
 *  not run, and the rest to go (the track). A table row writes its counts under it; a big bar has its key below. */
export function ProgressBar({ p, big }: { p: Progress; big?: boolean }) {
  const parts = progressParts(p);
  return (
    <div className="state-bar">
      <Segments parts={parts} total={Math.max(p.total, parts.reduce((a, part) => a + part.count, 0))} big={big} />
      {!big && <BarCountsLine summary={progressSummary(p)} />}
    </div>
  );
}

/** A run's tables for a running-now row: the count, then one bar with the states done, in flight and failed. The empty
 * part of the bar is still to go, so the legend leaves it out. */
export function RunBar({ p }: { p: Progress }) {
  return (
    <>
      <div className="count"><b>{fmtInt(p.total)}</b> {plural(p.total, "table")}</div>
      <CountBar parts={progressParts(p).filter((part) => part.tone !== "notrun")} whole={p.total} />
    </>
  );
}

/** The bar's key; a finished run has nothing to go, so it shows what did not run instead. */
export function ProgressKey({ p, finished }: { p?: Progress; finished?: boolean }) {
  const left = p ? Math.max(0, p.total - p.unchanged - p.loaded - p.inFlight - p.failed - (p.notRun ?? 0)) : undefined;
  const n = (v?: number) => (v === undefined ? null : <> <b>{fmtInt(v)}</b></>);
  return (
    <div className="key">
      <span><i className="seg-unchanged" />Unchanged{n(p?.unchanged)}</span><span><i className="seg-loaded" />Loaded{n(p?.loaded)}</span>
      {!finished && <span><i className="seg-running" />In flight{n(p?.inFlight)}</span>}<span><i className="seg-failing" />Failed{n(p?.failed)}</span>
      {finished ? <span><i className="seg-notrun" />Not run{n(p?.notRun ?? 0)}</span>
        : <span><i className="seg-track" />{p ? "To go" : "Not checked yet"}{n(left)}</span>}
    </div>
  );
}

/** "12,094 of 13,000 tables checked (93%), 96 in flight, 810 to go". */
export function progressText(p: Progress): string {
  const checked = p.unchanged + p.loaded;
  const left = Math.max(0, p.total - checked - p.inFlight - p.failed);
  const pct = p.total ? Math.floor((100 * checked) / p.total) : 0;
  return `${fmtInt(checked)} of ${fmtInt(p.total)} tables checked (${pct}%), ${fmtInt(p.inFlight)} in flight, ${fmtInt(left)} to go`;
}

/** When a query's data was read, for a Stamp: undefined until it has data. */
export const readAt = (query: { dataUpdatedAt: number }) => (query.dataUpdatedAt ? new Date(query.dataUpdatedAt).toISOString() : undefined);

/** When the shown data was read, and how often it refreshes. When the latest refresh failed, it says the page
 * shows older data and why, so a broken read never looks calm. */
export function Stamp({ at, every, error }: { at?: string; every?: number; error?: unknown }) {
  if (error && at) return <span className="stamp stale" title={(error as ApiError).message ?? String(error)}>
    Not updated since {fmtClock(at)}: {(error as ApiError).message ?? String(error)}</span>;
  if (!at) return null;
  return <span className="stamp">Updated {fmtAgo(at)}{every ? `, every ${every} s` : ""}</span>;
}

/** The Databricks UI's info icon beside a label: its explanation shows on hover, or on click or focus (keyboard and
 * touch). The one place the app keeps an explanation a user may want but needn't read. */
export function Info({ children, label = "More about this" }: { children: ReactNode; label?: string }) {
  return (
    <span className="info-tip" tabIndex={0} role="button" aria-label={label}>
      <span aria-hidden>ⓘ</span>
      <span className="info-pop" role="tooltip">{children}</span>
    </span>
  );
}

/** The mark of a field a job can't be created or saved without, as Databricks forms mark them. */
export function Required() {
  return <span className="required" aria-label="required">*</span>;
}

export interface PageStampValue { at?: string; every?: number; error?: unknown }

/** Where a page's "Updated ..." line goes when it is shown at the bottom of the left menu (App), not in the page head. */
export const PageStampContext = createContext<(stamp: PageStampValue | null) => void>(() => {});

/** Show this page's "Updated ..." line at the bottom of the left menu while the page is open. */
export function usePageStamp(at?: string, every?: number, error?: unknown) {
  const set = useContext(PageStampContext);
  useEffect(() => { set({ at, every, error }); }, [set, at, every, error]);
  useEffect(() => () => set(null), [set]);
}

/** The browser tab's title, as the workspace's "<page or object> - Databricks": the page or object first. */
export function useTabTitle(title?: string) {
  useEffect(() => { document.title = title ? `${title} · Lakefed Ingest` : "Lakefed Ingest"; }, [title]);
}

/** The steps above a page, as the workspace shows them: "Ingestion jobs › East hourly › Ingestion run". */
export function Crumbs({ steps }: { steps: [ReactNode, string?][] }) {
  return <span className="eyebrow">{steps.map(([label, to], i) => <Fragment key={i}>{i > 0 && " › "}
    {to ? <Link to={to}>{label}</Link> : label}</Fragment>)}</span>;
}

/** A page's header: its crumbs, title (also the browser tab's, or ``tab`` when the title is not text), lead and actions. */
export function PageHead({ title, lead, crumb, right, tab }: { title: ReactNode; lead?: ReactNode; crumb?: ReactNode; right?: ReactNode;
                           tab?: string }) {
  useTabTitle(tab ?? (typeof title === "string" ? title : undefined));
  return (
    <div className="page-head">
      <div>{crumb && <div className="crumb">{crumb}</div>}<h1>{title}</h1>{lead && <p className="lead">{lead}</p>}</div>
      {right && <div>{right}</div>}
    </div>
  );
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error as ApiError;
  return (
    <div className="alert bad">
      <strong>Error:</strong> {e.message ?? String(error)}
      {e.hint && <div className="hint">{e.hint}</div>}
      {Array.isArray(e.detail?.jobs) && e.detail.jobs.map((job: Row) => (
        <div className="hint" key={job.id}>
          Open ingestion job <Link to={`/ingestion-jobs/${job.id}`}>{job.name}</Link>
        </div>
      ))}
    </div>
  );
}

export function Notice({ tone = "info", children }: { tone?: "info" | "ok" | "warn" | "bad"; children: ReactNode }) {
  return <div className={`alert ${tone}`}>{children}</div>;
}

/** A dialog as the Databricks UI asks before a change: a title, what it does, then Cancel and the action at the right,
 * red when it destroys or disables something. Escape, a click outside and Cancel all cancel. Beside the action: why it
 * is off (actionDisabled), or else a note on what it did last (actionNote, such as a test that failed). */
export function Modal({ title, action, danger, actionDisabled, actionNote, onClose, children }: { title: string; action: string;
                        danger?: boolean; actionDisabled?: string; actionNote?: string; onClose: (confirmed: boolean) => void;
                        children?: ReactNode }) {
  const id = useId();
  const box = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  // As Databricks dialogs: Tab and Shift+Tab stay inside it, and focus goes back to what opened it when it closes.
  // The opener is read before Cancel takes focus (no autoFocus, which would move focus first).
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    cancel.current?.focus();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") return closeRef.current(false);
      if (e.key !== "Tab" || !box.current) return;
      const stops = [...box.current.querySelectorAll<HTMLElement>("button, a[href], input, select, textarea, [tabindex]")]
        .filter((el) => !el.hasAttribute("disabled") && el.tabIndex >= 0);
      if (!stops.length) return;
      const first = stops[0], last = stops[stops.length - 1];
      const inside = box.current.contains(document.activeElement);
      if (e.shiftKey && (document.activeElement === first || !inside)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (document.activeElement === last || !inside)) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("keydown", key); opener?.focus?.(); };
  }, []);
  return createPortal(
    <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(false); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={id} ref={box}>
        <h2 id={id}>{title}</h2>
        {children && <div className="modal-body">{children}</div>}
        <div className="modal-actions">
          {actionDisabled ? <span className="modal-why muted" role="status">{actionDisabled}</span>
            : actionNote && <span className="modal-why bad" role="alert">{actionNote}</span>}
          <button ref={cancel} onClick={() => onClose(false)}>Cancel</button>
          <button className={danger ? "danger" : "primary"} disabled={!!actionDisabled} title={actionDisabled}
            onClick={() => onClose(true)}>{action}</button>
        </div>
      </div>
    </div>, document.body);
}

export interface ConfirmRequest { title: string; body?: ReactNode; action: string; danger?: boolean }

/** Ask with a Modal from an event handler: `confirm.ask({...}).then((ok) => ok && act())`, with `confirm.dialog` rendered
 * on the page. */
export function useConfirm(): { ask: (request: ConfirmRequest) => Promise<boolean>; dialog: ReactNode } {
  const [request, setRequest] = useState<(ConfirmRequest & { resolve: (ok: boolean) => void }) | null>(null);
  const ask = useCallback((r: ConfirmRequest) => new Promise<boolean>((resolve) => setRequest({ ...r, resolve })), []);
  const close = useCallback((ok: boolean) => { setRequest((r) => { r?.resolve(ok); return null; }); }, []);
  return { ask, dialog: request && <Modal title={request.title} action={request.action} danger={request.danger}
    onClose={close}>{request.body}</Modal> };
}

/** A button that asks in a Modal before running a write: its title (default "<label>?"), what it does and its action. */
export function ConfirmButton(props: {
  label: string;
  confirmLabel?: string;
  title?: string;
  message?: ReactNode;
  onConfirm: () => unknown;
  disabled?: boolean;
  danger?: boolean;
  plain?: boolean;
}) {
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);
  const run = async () => {
    if (!(await confirm.ask({ title: props.title ?? `${props.label}?`, body: props.message,
                              action: props.confirmLabel ?? props.label, danger: props.danger }))) return;
    setBusy(true);
    try { await props.onConfirm(); } finally { setBusy(false); }
  };
  return (
    <>
      <button className={props.danger ? "danger" : props.plain ? "" : "primary"} disabled={props.disabled || busy} onClick={run}>
        {busy ? "Working…" : props.label}
      </button>
      {confirm.dialog}
    </>
  );
}

export function Section({ title, actions, note, children }: { title: ReactNode; actions?: ReactNode; note?: ReactNode;
                          children: ReactNode }) {
  return (
    <section className="card">
      <div className="card-head">
        <div>{title ? <h2>{title}</h2> : null}{note && <p>{note}</p>}</div>
        <div className="card-actions">{actions}</div>
      </div>
      {children}
    </section>
  );
}

/** The server's progress shape (ingestion_jobs._progress) as the progress bar's. */
export function progressFrom(p?: Row | null): Progress {
  return { total: Number(p?.total || 0), unchanged: Number(p?.unchanged || 0), loaded: Number(p?.loaded || 0),
           inFlight: Number(p?.in_flight || 0), failed: Number(p?.failed || 0), notRun: Number(p?.not_run || 0) };
}

/** An ingestion run's name: its job and its slot's start, "East hourly, 14:00 ingestion run" (with the day when not today). */
/** A time as a clock reading today, and with its day otherwise ("23 Oct, 21:00"). */
export function fmtSlot(slot: unknown): string {
  if (!slot) return "";
  const at = new Date(String(slot));
  const today = new Date().toDateString() === at.toDateString();
  return at.toLocaleString([], today ? { hour: "2-digit", minute: "2-digit" }
    : { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function jobRunName(jobName: unknown, slot: unknown): string {
  return `${jobName}, ${fmtSlot(slot)} ingestion run`;
}

export const jobRunPath = (jobId: unknown, slot: unknown) =>
  `/ingestion-jobs/${encodeURIComponent(String(jobId))}/runs/${encodeURIComponent(String(slot))}`;

/** A job's Status cell (ingestion_jobs.status): its state and issues together, never color alone. */
export function JobStatus({ status }: { status?: Row | null }) {
  if (!status) return null;
  const tone = (["ok", "warn", "bad", "run"].includes(status.tone) ? status.tone : "") as Tone;
  const detail: string[] = [...(status.detail ?? [])];
  if (status.paused_since) detail[detail.length - 1] = `paused since ${fmtClock(status.paused_since)}`;
  if (status.expected_end) detail.push(`ends about ${fmtClock(status.expected_end)}`);
  return (
    <div className="status-cell">
      <Tag tone={tone}>{status.label}</Tag>
      {detail.length > 0 && <span className="sub">{detail.join(" · ")}</span>}
      {status.cause && <span className="sub">{status.cause}</span>}
    </div>
  );
}

/** A link out to Databricks, named for what it opens and marked as leaving the app, as links out of a workspace page read. */
export function OutLink({ href, children }: { href?: string; children: ReactNode }) {
  return href ? <a href={href} target="_blank" rel="noreferrer">{children} ↗</a> : <>{children}</>;
}

/** A cluster's page in the workspace, or undefined without the workspace's address or an id. */
export const clusterUrl = (workspace: string | undefined, id: unknown) =>
  workspace && id ? `${workspace}/#setting/clusters/${encodeURIComponent(String(id))}/configuration` : undefined;

/** A Databricks job run's page, through the app, which asks the Jobs API where it is. */
export const databricksRunUrl = (runId: unknown) => `/api/databricks-runs/${encodeURIComponent(String(runId))}/open`;

/** "1 to 25 of 40 ingestion jobs", with previous and next. */
export function Pager({ offset, limit, total, noun, onPage }: { offset: number; limit: number; total: number; noun: string;
                        onPage: (offset: number) => void }) {
  const last = Math.min(offset + limit, total);
  return (
    <div className="pager">
      <span className="muted">{total ? `${fmtInt(offset + 1)} to ${fmtInt(last)} of ${fmtInt(total)}` : "0"} {plural(total, noun)}</span>
      {total > limit && (
        <span className="pager-buttons">
          <button className="ghost" disabled={offset === 0} onClick={() => onPage(Math.max(0, offset - limit))}>Previous page</button>
          <button className="ghost" disabled={last >= total} onClick={() => onPage(offset + limit)}>Next page</button>
        </span>
      )}
    </div>
  );
}

/** One database's page: its group, server ("-" when blank) and name. */
export const databasePath = (group: unknown, server: unknown, database: unknown) =>
  `/collections/${encodeURIComponent(String(group))}/databases/${encodeURIComponent(String(server || "-"))}/${encodeURIComponent(String(database ?? ""))}`;

/** A name that wraps only between words, so a hyphenated or underscored name never breaks mid-word. */
export function Name({ children }: { children: unknown }) {
  const words = String(children ?? "").split(" ");
  return <>{words.map((w, i) => <Fragment key={i}>{i > 0 && " "}<span className="word">{w}</span></Fragment>)}</>;
}

/** The Databricks UI's disclosure chevron, as Catalog Explorer's tree and a job page's collapsible panels draw it:
 * pointing right when closed and down when open. A menu's button shows it pointing down. */
export function Chevron({ open }: { open: boolean }) {
  return (
    <svg className={`chevron${open ? " open" : ""}`} width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <path d="M6 3.5 10.5 8 6 12.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** A row that opens to show what is under it: the chevron and the row's name in one button, so the whole name is the
 * hit area and Enter or Space opens it, with aria-expanded as the Databricks UI sets it. With no children it is the
 * chevron alone, for a row whose name is a link; give it a label then. closed: why the row can't open (its button is
 * off and its hover says so). */
export function Twisty({ open, onToggle, label, closed, children }: { open: boolean; onToggle: () => void; label?: string;
                        closed?: string; children?: ReactNode }) {
  return (
    <button type="button" className={`twisty${children ? " named" : ""}`} aria-expanded={closed ? undefined : open}
      aria-label={label} title={closed ?? label} disabled={!!closed} onClick={closed ? undefined : onToggle}>
      <Chevron open={open} />{children}
    </button>
  );
}

/** A menu behind a button, as the Databricks UI keeps a job's less common and destructive actions (the kebab) and the
    user's details (the avatar). It closes on a click outside or Escape. */
export function Menu({ label, title, buttonClass = "kebab", children }: { label: ReactNode; title: string;
                     buttonClass?: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !box.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", close); };
  }, [open]);
  return (
    <span className="menu" ref={box}>
      <button className={buttonClass} aria-haspopup="menu" aria-expanded={open} aria-label={title} title={title}
        onClick={() => setOpen(!open)}>{label}</button>
      {open && <div className="menu-items" role="menu" onClick={(e) => (e.target as HTMLElement).closest("button") && setOpen(false)}>{children}</div>}
    </span>
  );
}

/** Now, read again every second while `on`, so a wait's elapsed time keeps counting on the page. */
export function useNow(on: boolean): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (!on) return;
    setNow(new Date());
    const timer = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(timer);
  }, [on]);
  return now;
}

/** The small turning circle Databricks shows while something runs. */
export function Spinner({ label }: { label?: string }) {
  return <span className="spinner" role={label ? "status" : undefined} aria-label={label} aria-hidden={label ? undefined : true} />;
}

/** Something running, in one plain line: a spinner, what it does ("Reading tables"), the elapsed time once it runs long,
 * and a thin bar when how far it is is known (done of total). */
export function Busy({ label, since, done, total }: { label: string; since?: Date; done?: number; total?: number }) {
  const now = useNow(since != null);
  const long = since ? longWait(since, now) : null;
  return (
    <p className="busy" role="status">
      <Spinner />{label}{long && <span className="muted"> · {long}</span>}
      {total ? <progress className="busy-progress" max={total} value={Math.min(done ?? 0, total)} aria-label={`${label}: ${fmtInt(done ?? 0)} of ${fmtInt(total)}`} /> : null}
    </p>
  );
}

// Toasts: a brief line at the foot of the page that fades on its own, as Databricks confirms an action ("Run started").
// Errors and refusals stay as notices on the page.
export const TOAST_MS = 4000;
type ToastItem = { id: number; text: string };
let toastItems: ToastItem[] = [];
let nextToastId = 1;
const toastListeners = new Set<() => void>();
const emitToasts = () => toastListeners.forEach((listener) => listener());

/** Show a toast; it goes after TOAST_MS. */
export function toast(text: string) {
  const id = nextToastId++;
  toastItems = [...toastItems, { id, text }];
  emitToasts();
  setTimeout(() => { toastItems = toastItems.filter((item) => item.id !== id); emitToasts(); }, TOAST_MS);
}

/** Toast `text` each time it turns up (a result's confirmation), not again while it stays the same. */
export function useToastOn(text: string | null | undefined) {
  const last = useRef<string | null>(null);
  useEffect(() => {
    if (text && text !== last.current) toast(text);
    last.current = text ?? null;
  }, [text]);
}

/** Where the toasts show: once, in the app's shell. */
export function Toaster() {
  const items = useSyncExternalStore((listener) => { toastListeners.add(listener); return () => { toastListeners.delete(listener); }; },
                                     () => toastItems);
  return <div className="toasts" role="status" aria-live="polite">{items.map((item) => <div key={item.id} className="toast">{item.text}</div>)}</div>;
}

/** A write's line as the page shows it: going, a plain line with a spinner; done, a toast; not confirmed or failed, a
 * notice that stays. */
export function StatusLine({ line }: { line: { tone: "info" | "ok" | "warn" | "bad"; text: string } | null }) {
  useToastOn(line?.tone === "ok" ? line.text : null);
  if (!line || line.tone === "ok") return null;
  if (line.tone === "info") return <Busy label={line.text} />;
  return <Notice tone={line.tone}>{line.text}</Notice>;
}

/** useToastOn as an element, for a page that confirms a result where it would have shown it. */
export function ToastOn({ text }: { text: string | null }) {
  useToastOn(text);
  return null;
}
