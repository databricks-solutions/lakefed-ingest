// Tests for the job page's one refresh (src/frontend/src/jobPageRefresh.ts), with a real QueryClient and a fake clock:
//   - What it ingests followed a run that loaded 1,002 tables while it still said 205 of 1,002, because nothing re-read it;
//   - when the run ends, every section is read once more and the timer stops;
//   - a run row that has ended is final, and the sections that poll on their own are not read twice.
// frontend_job_page_refresh_test.py runs this from pytest.
import { test, mock } from "node:test";
import assert from "node:assert/strict";
import { QueryClient, QueryObserver } from "../../src/node_modules/@tanstack/query-core/build/modern/index.js";
import { JobPageRefresh, PAGE_REFRESH_MS, isRunRowFinal } from "../../src/frontend/src/jobPageRefresh.ts";

const JOB = "48";
const RUNNING = { active: true, group_runs: [{ task_collection: "c1", active: true }] };
const ENDED = { active: false, group_runs: [{ task_collection: "c1", active: false }] };

/** Lets the reads a tick started finish; only the clock and Date are faked, so promises and immediates run for real. */
const settle = async () => { for (let turn = 0; turn < 10; turn++) await new Promise((resolve) => setImmediate(resolve)); };
const tick = async (milliseconds) => { mock.timers.tick(milliseconds); await settle(); };

/** A page: sections mounted as active observers (a section on screen), each counting the reads made of it. */
function mountPage() {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: 15_000, retry: false } } });
  const server = { loaded: 205, runOutcome: "Running", groups: ["c1"] };
  const reads = {};
  const mounted = [];
  const mount = (key, answer) => {
    const name = key.join("|");
    const observer = new QueryObserver(client, { queryKey: key, queryFn: async () => {
      reads[name] = (reads[name] ?? 0) + 1;
      return answer();
    } });
    mounted.push(observer.subscribe(() => {}));
    return name;
  };
  const sections = {
    servers: mount(["job-tree", JOB], () => ({ loaded: server.loaded, total: 1002 })),
    databases: mount(["job-tree", JOB, "sql_01", "", "", 0], () => ({ items: [{ database: "sales", groups: server.groups }] })),
    tables: mount(["job-tree", JOB, "sql_01", "sales", 0], () => ({ loaded: server.loaded })),
    otherGroupTables: mount(["job-tree", JOB, "sql_01", "hr", 0], () => ({ loaded: 10 })),
    targets: mount(["job-targets", JOB, "", 0], () => ({ loaded: server.loaded })),
    targetSources: mount(["job-target-sources", JOB, "orders", 0], () => ({ items: [] })),
    liveRun: mount(["job-run", JOB, "live"], () => ({ outcome: { label: server.runOutcome } })),
    endedRun: mount(["job-run", JOB, "ended"], () => ({ outcome: { label: "Succeeded" } })),
    card: mount(["ingestion-job", JOB], () => ({})),
    live: mount(["ingestion-job-live", JOB], () => ({})),
    newestRuns: mount(["ingestion-job-runs", JOB, undefined], () => ({})),
    olderRuns: mount(["ingestion-job-runs", JOB, "2026-11-10T00:00:00Z"], () => ({})),
    failing: mount(["ingestion-job-failing", JOB], () => ({})),
    preview: mount(["delete-preview", JOB], () => ({})),
    tasks: mount(["job-tasks", JOB], () => ({})),
    anotherJob: mount(["job-tree", "49"], () => ({})),
  };
  const refresh = new JobPageRefresh(client, JOB, () => {});
  return { client, server, reads, sections, refresh, unmount: () => { refresh.stop(); mounted.forEach((stop) => stop()); } };
}

/** Mounts the page, lets every section make its first read, and starts the clock. */
async function openPage() {
  mock.timers.enable({ apis: ["setInterval", "Date"], now: Date.parse("2026-11-11T16:53:00Z") });
  const page = mountPage();
  await settle();
  page.firstReads = { ...page.reads };
  page.since = () => Object.fromEntries(Object.entries(page.reads).map(([name, count]) => [name, count - (page.firstReads[name] ?? 0)]));
  return page;
}

test("State now follows the run by itself: 205 of 1,002 becomes 1,002 with no button", async (context) => {
  const page = await openPage();
  context.after(() => { page.unmount(); mock.timers.reset(); });
  page.refresh.update(RUNNING);
  assert.equal(page.client.getQueryData(["job-tree", JOB]).loaded, 205);

  page.server.loaded = 1002;
  await tick(PAGE_REFRESH_MS - 1);
  assert.equal(page.client.getQueryData(["job-tree", JOB]).loaded, 205, "not before the timer");
  await tick(1);
  assert.equal(page.client.getQueryData(["job-tree", JOB]).loaded, 1002);
  assert.equal(page.client.getQueryData(["job-targets", JOB, "", 0]).loaded, 1002);
});

test("a tick reads only the sections with no timer of their own, and never the delete preview or the job's task list", async (context) => {
  const page = await openPage();
  context.after(() => { page.unmount(); mock.timers.reset(); });
  page.refresh.update(RUNNING);
  await tick(PAGE_REFRESH_MS);
  const { sections } = page;
  const read = page.since();
  for (const name of ["servers", "databases", "tables", "targets", "targetSources", "liveRun"]) {
    assert.equal(read[sections[name]], 1, `${name} read once on the tick`);
  }
  for (const name of ["endedRun", "card", "live", "newestRuns", "olderRuns", "failing", "preview", "tasks", "anotherJob"]) {
    assert.equal(read[sections[name]] ?? 0, 0, `${name} not read on the tick`);
  }
});

test("a run row whose run has ended is final: opened, it is read once and never again", async (context) => {
  const page = await openPage();
  context.after(() => { page.unmount(); mock.timers.reset(); });
  page.refresh.update(RUNNING);
  await tick(PAGE_REFRESH_MS * 3);
  assert.equal(page.since()[page.sections.endedRun] ?? 0, 0);
  assert.equal(page.since()[page.sections.liveRun], 3);
  assert.equal(isRunRowFinal(undefined), false, "a row not read yet is not final");
  assert.equal(isRunRowFinal({ outcome: { label: "Running" } }), false);
  assert.equal(isRunRowFinal({ outcome: { label: "Cancelling" } }), false);
  assert.equal(isRunRowFinal({ outcome: { label: "Failed" } }), true);
});

test("an opened database's tables are read while a group run covering them is active, and left alone once it is over", async (context) => {
  const page = await openPage();
  context.after(() => { page.unmount(); mock.timers.reset(); });
  page.refresh.update(RUNNING);
  await tick(PAGE_REFRESH_MS);
  assert.equal(page.since()[page.sections.tables], 1);

  page.refresh.update({ active: true, group_runs: [{ task_collection: "c1", active: false }, { task_collection: "c2", active: true }] });
  await tick(PAGE_REFRESH_MS);
  assert.equal(page.since()[page.sections.tables], 2, "the tick that follows the group run's last live read still covers it");
  await tick(PAGE_REFRESH_MS);
  assert.equal(page.since()[page.sections.tables], 2, "then its tables are final until a group run covers them again");
  assert.equal(page.since()[page.sections.servers], 3, "the server level is read on every tick while the run goes");
});

test("a table level whose database is not in the cached list is read on every tick", async (context) => {
  const page = await openPage();
  context.after(() => { page.unmount(); mock.timers.reset(); });
  page.refresh.update(RUNNING);
  await tick(PAGE_REFRESH_MS * 2);
  assert.equal(page.since()[page.sections.otherGroupTables], 2);
});

test("when the run ends every section is read once, and the page-wide timer stops", async (context) => {
  const page = await openPage();
  context.after(() => { page.unmount(); mock.timers.reset(); });
  page.refresh.update(RUNNING);
  await tick(PAGE_REFRESH_MS);
  const beforeEnd = page.since();

  page.server.loaded = 1002;
  page.server.runOutcome = "Succeeded";
  page.refresh.update(ENDED);
  await settle();
  const finalRead = Object.fromEntries(Object.entries(page.since()).map(([name, count]) => [name, count - (beforeEnd[name] ?? 0)]));
  const { sections } = page;
  for (const name of ["servers", "databases", "tables", "otherGroupTables", "targets", "targetSources", "liveRun", "card", "newestRuns", "failing"]) {
    assert.equal(finalRead[sections[name]], 1, `${name} read once in the final read`);
  }
  for (const name of ["live", "olderRuns", "preview", "tasks", "anotherJob"]) {
    assert.equal(finalRead[sections[name]] ?? 0, 0, `${name} not in the final read`);
  }
  assert.equal(page.client.getQueryData(["job-tree", JOB]).loaded, 1002);

  const settled = { ...page.reads };
  await tick(PAGE_REFRESH_MS * 4);
  assert.deepEqual(page.reads, settled, "no page-wide read after the final one");
});

test("the next run starts the timer again, and a page opened with nothing going reads nothing extra", async (context) => {
  const page = await openPage();
  context.after(() => { page.unmount(); mock.timers.reset(); });
  page.refresh.update(ENDED);
  await tick(PAGE_REFRESH_MS * 2);
  assert.deepEqual(page.reads, page.firstReads, "nothing going on a fresh page: no tick, no final read");

  page.refresh.update(RUNNING);
  await tick(PAGE_REFRESH_MS);
  assert.equal(page.since()[page.sections.servers], 1);
});

test("a live read that has not arrived yet starts nothing, and closing the page stops the timer", async (context) => {
  const page = await openPage();
  context.after(() => { page.unmount(); mock.timers.reset(); });
  page.refresh.update(undefined);
  await tick(PAGE_REFRESH_MS * 2);
  assert.deepEqual(page.reads, page.firstReads);

  page.refresh.update(RUNNING);
  page.refresh.stop();
  await tick(PAGE_REFRESH_MS * 2);
  assert.deepEqual(page.reads, page.firstReads);
});

test("the page's Updated stamp is told when a read lands", async (context) => {
  mock.timers.enable({ apis: ["setInterval", "Date"], now: Date.parse("2026-11-11T16:53:00Z") });
  const client = new QueryClient();
  const stamps = [];
  const refresh = new JobPageRefresh(client, JOB, (at) => stamps.push(at));
  context.after(() => { refresh.stop(); mock.timers.reset(); });
  refresh.update(RUNNING);
  await tick(PAGE_REFRESH_MS);
  assert.deepEqual(stamps, [Date.parse("2026-11-11T16:53:15Z")]);
});
