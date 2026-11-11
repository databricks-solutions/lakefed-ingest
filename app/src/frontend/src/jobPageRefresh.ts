// The job page's one refresh. While a run of the job is going, every 15 seconds the page re-reads the sections that have
// no timer of their own (What it ingests, Target tables, a run opened in the runs list); when the run ends it re-reads
// every section once, so the page settles on the final numbers. It reads only what can still change, and the cards,
// Running now, the newest runs page and Failing tables keep their own timers. Plain code with no JSX: the Node tests in
// app/tests/js read this file as it is, with a real QueryClient and fake timers.
import type { Query, QueryClient } from "@tanstack/react-query";
import type { Row } from "./api";

export const PAGE_REFRESH_MS = 15_000;

/** The job run outcomes the server calls live (queries.RUNNING_OUTCOMES). */
const LIVE_OUTCOMES = ["Running", "Cancelling"];

/** What the page needs of the live read (GET .../live): whether a run is going and which groups have a group run going. */
export interface LiveRead {
  active?: boolean;
  group_runs?: { task_collection: string; active?: boolean }[];
}

/** The groups that have a group run going in a live read. */
export function activeGroups(live: LiveRead | undefined): Set<string> {
  return new Set((live?.group_runs ?? []).filter((run) => run.active).map((run) => run.task_collection));
}

/** Whether an opened run row's job run has ended, so what it shows can no longer change. A row not read yet is not final. */
export function isRunRowFinal(runRow: Row | undefined): boolean {
  return runRow !== undefined && !LIVE_OUTCOMES.includes(runRow.outcome?.label);
}

type TreeLevel = "servers" | "databases" | "tables";

/** Which level of What it ingests a tree read is, from its key: ["job-tree", job] is the servers,
 * ["job-tree", job, server, "", search, offset] a server's databases and ["job-tree", job, server, database, offset] a
 * database's tables. */
function treeLevel(key: readonly unknown[]): TreeLevel {
  return key.length <= 2 ? "servers" : key.length === 6 ? "databases" : "tables";
}

/** The ingestion groups a database's tables are in, as the opened server's databases list says, or null when that list is
 * not in the page. */
function groupsOfDatabase(client: QueryClient, job: string, server: string, database: string): string[] | null {
  for (const query of client.getQueryCache().findAll({ queryKey: ["job-tree", job, server, ""] })) {
    if (query.queryKey.length !== 6) continue;
    const found = ((query.state.data as Row | undefined)?.items ?? []).find((item: Row) => item.database === database);
    if (found?.groups) return found.groups;
  }
  return null;
}

export class JobPageRefresh {
  private timer: ReturnType<typeof setInterval> | null = null;
  private going = false;
  private latest: LiveRead | undefined;
  /** The groups that had a group run going at any live read since the last tick, so a group that began and ended between
   * two ticks still gets its rows read. */
  private groupsSinceTick = new Set<string>();

  private readonly client: QueryClient;
  private readonly job: string;
  private readonly onRead: (at: number) => void;
  private readonly everyMs: number;

  /** `onRead` is told the time each read lands (the page's Updated stamp). Written without parameter properties so Node
   * can run this file as it is in the tests. */
  constructor(client: QueryClient, job: string, onRead: (at: number) => void = () => {}, everyMs = PAGE_REFRESH_MS) {
    this.client = client;
    this.job = job;
    this.onRead = onRead;
    this.everyMs = everyMs;
  }

  /** Tell it what the latest live read said. A run going starts the timer; a run that has ended stops it and reads every
   * section once. A page opened with nothing going reads nothing extra. */
  update(live: LiveRead | undefined): void {
    if (!live) return;
    this.latest = live;
    const active = Boolean(live.active);
    if (active) {
      activeGroups(live).forEach((group) => this.groupsSinceTick.add(group));
      this.timer ??= setInterval(() => void this.tick(), this.everyMs);
    } else if (this.going) {
      this.stop();
      this.groupsSinceTick.clear();
      void this.read((query) => this.isFinalRead(query));
    }
    this.going = active;
  }

  /** Stop the timer, when the page closes or the run has ended. */
  stop(): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
  }

  private async tick(): Promise<void> {
    const covered = this.groupsSinceTick;
    this.groupsSinceTick = activeGroups(this.latest);
    await this.read((query) => this.isDueOnTick(query, covered));
  }

  private async read(wanted: (query: Query) => boolean): Promise<void> {
    await this.client.refetchQueries({ type: "active", predicate: (query) => this.isOfThisJob(query) && wanted(query) });
    this.onRead(Date.now());
  }

  private isOfThisJob(query: Query): boolean {
    return query.queryKey[1] === this.job;
  }

  /** The sections with no timer of their own, read on every tick while a run is going. */
  private isDueOnTick(query: Query, coveredGroups: Set<string>): boolean {
    const [section] = query.queryKey;
    if (section === "job-targets" || section === "job-target-sources") return true;
    if (section === "job-run") return !isRunRowFinal(query.state.data as Row | undefined);
    if (section !== "job-tree") return false;
    if (treeLevel(query.queryKey) !== "tables") return true;
    const [, , server, database] = query.queryKey as string[];
    const groups = groupsOfDatabase(this.client, this.job, server, database);
    return groups === null || groups.some((group) => coveredGroups.has(group));
  }

  /** The final read: those sections, and the ones that poll on their own, once more. The live read just said the run
   * ended, so it is not read again; the background writes, the delete preview and the app frame are not the page's. */
  private isFinalRead(query: Query): boolean {
    const [section] = query.queryKey;
    if (section === "ingestion-job" || section === "ingestion-job-failing") return true;
    if (section === "ingestion-job-runs") return query.queryKey[2] === undefined;
    if (section === "job-tree") return true;
    return this.isDueOnTick(query, new Set());
  }
}
