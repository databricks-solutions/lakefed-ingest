// What a state bar or a progress bar shows: its parts (a count, a word and a status tone each) and the counts written
// under it, so a state never rests on colour alone. Pure, so the Node tests read it as it is (tests/js/bars.test.mjs);
// components.tsx draws it in the Databricks UI's status colours.

export type Tone = "loaded" | "unchanged" | "running" | "late" | "waiting" | "failing" | "notrun";

export interface BarPart { count: number; word: string; tone: Tone }

/** One count under a bar; a count of nothing is drawn muted. */
export interface CountItem { text: string; tone: Tone | "zero" }

/** The counts under a bar: "All 100,200 loaded" when everything is done, else each count in its tone. */
export interface BarSummary { allDone: string | null; items: CountItem[] }

export interface StateCounts {
  state_current?: unknown; state_loaded?: unknown; state_late?: unknown; state_first_load?: unknown; state_failing?: unknown;
  late_databases?: unknown;
}

export interface ProgressCounts { unchanged: number; loaded: number; inFlight: number; failed: number; total: number; notRun?: number }

const NUMBER = new Intl.NumberFormat("en-US");
const count = (n: number) => NUMBER.format(n);
const amount = (value: unknown) => Number(value || 0);
const item = (part: BarPart): CountItem => ({ text: `${count(part.count)} ${part.word}`, tone: part.count ? part.tone : "zero" });

/** A table row counts its tables: current, late, waiting, failing. A rollup row (a group, job, server or instance)
 * counts its loaded tables, late ones included, and its late databases, which are written beside the bar, not in it. */
export function stateParts(row: StateCounts): { parts: BarPart[]; lateDatabases?: number } {
  const rollup = row.state_current === undefined && row.state_loaded !== undefined;
  const parts: BarPart[] = [
    { count: amount(rollup ? row.state_loaded : row.state_current), word: "loaded", tone: "loaded" },
    ...(rollup ? [] : [{ count: amount(row.state_late), word: "late", tone: "late" } as BarPart]),
    { count: amount(row.state_first_load), word: "waiting", tone: "waiting" },
    { count: amount(row.state_failing), word: "failing", tone: "failing" }];
  return { parts, lateDatabases: rollup && row.late_databases !== undefined ? amount(row.late_databases) : undefined };
}

/** A table row's counts: "980 of 1,002 loaded · 12 late · 10 failing", only the states it has. A card's (full):
 * every state, "100,200 loaded · 0 late databases · 0 waiting · 0 failing". All loaded: "All 1,002 loaded", unless
 * saysAllDone is false (the jobs lists), which counts it as "1,002 of 1,002 loaded". */
export function stateSummary(row: StateCounts, full = false, saysAllDone = true): BarSummary {
  const { parts, lateDatabases } = stateParts(row);
  const total = parts.reduce((sum, part) => sum + part.count, 0);
  const [loaded, ...rest] = parts;
  if (saysAllDone && total > 0 && loaded.count === total && !lateDatabases) {
    return { allDone: `All ${count(total)} loaded`, items: [] };
  }
  const late: CountItem[] = lateDatabases === undefined || (!lateDatabases && !full) ? []
    : [{ text: `${count(lateDatabases)} late ${lateDatabases === 1 ? "database" : "databases"}`, tone: lateDatabases ? "late" : "zero" }];
  const head: CountItem = full ? item(loaded)
    : { text: `${count(loaded.count)} of ${count(total)} loaded`, tone: loaded.count ? "loaded" : "zero" };
  return { allDone: null, items: [head, ...late, ...rest.filter((part) => full || part.count > 0).map(item)] };
}

/** A run's parts: unchanged and loaded (done), in flight, failed, not run; the rest to go is the bar's track. */
export function progressParts(p: ProgressCounts): BarPart[] {
  return [{ count: p.unchanged, word: "unchanged", tone: "unchanged" }, { count: p.loaded, word: "loaded", tone: "loaded" },
          { count: p.inFlight, word: "in flight", tone: "running" }, { count: p.failed, word: "failed", tone: "failing" },
          { count: p.notRun ?? 0, word: "not run", tone: "notrun" }];
}

/** "812 of 1,002 done · 40 in flight · 2 failed"; "All 1,002 done" once every table is. */
export function progressSummary(p: ProgressCounts): BarSummary {
  const done = p.unchanged + p.loaded;
  if (p.total > 0 && done >= p.total) return { allDone: `All ${count(p.total)} done`, items: [] };
  return { allDone: null, items: [{ text: `${count(done)} of ${count(p.total)} done`, tone: done ? "loaded" : "zero" },
                                  ...progressParts(p).slice(2).filter((part) => part.count > 0).map(item)] };
}
