// Tests for what a state bar and a progress bar write under them (src/frontend/src/bars.ts): every count in words, so a
// state never rests on colour alone, and a full bar reads as all done. frontend_pending_test.py runs this from pytest.
import { test } from "node:test";
import assert from "node:assert/strict";
import { progressSummary, stateParts, stateSummary } from "../../src/frontend/src/bars.ts";

const texts = (summary) => summary.items.map((entry) => `${entry.tone}:${entry.text}`);

test("a card whose every table is loaded reads all done", () => {
  const job = { state_loaded: 100200, state_first_load: 0, state_failing: 0, late_databases: 0 };
  assert.deepEqual(stateSummary(job, true), { allDone: "All 100,200 loaded", items: [] });
  assert.deepEqual(stateSummary(job), { allDone: "All 100,200 loaded", items: [] });
});

test("a card counts every state, its zeros muted, and a rollup's late databases", () => {
  const job = { state_loaded: 64010, state_first_load: 9400, state_failing: 0, late_databases: 3 };
  assert.deepEqual(texts(stateSummary(job, true)),
    ["loaded:64,010 loaded", "late:3 late databases", "waiting:9,400 waiting", "zero:0 failing"]);
});

test("a jobs list counts an all-loaded job as N of N, and says All N loaded only where it may", () => {
  const job = { state_loaded: 1002, state_first_load: 0, state_failing: 0, late_databases: 0 };
  assert.deepEqual(stateSummary(job), { allDone: "All 1,002 loaded", items: [] });
  assert.deepEqual(stateSummary(job, false, false), { allDone: null, items: [{ text: "1,002 of 1,002 loaded", tone: "loaded" }] });
});

test("a rollup with late databases is not all done, even with every table loaded", () => {
  const group = { state_loaded: 9000, state_first_load: 0, state_failing: 0, late_databases: 1 };
  const summary = stateSummary(group);
  assert.equal(summary.allDone, null);
  assert.deepEqual(texts(summary), ["loaded:9,000 of 9,000 loaded", "late:1 late database"]);
});

test("a table row says how many of its tables are loaded and only the states it has", () => {
  const target = { state_current: 980, state_late: 12, state_first_load: 0, state_failing: 10 };
  assert.deepEqual(texts(stateSummary(target)), ["loaded:980 of 1,002 loaded", "late:12 late", "failing:10 failing"]);
});

test("a table row's late tables are a part of the bar; a rollup's late databases are not", () => {
  assert.deepEqual(stateParts({ state_current: 1, state_late: 2 }).parts.map((p) => p.tone), ["loaded", "late", "waiting", "failing"]);
  assert.deepEqual(stateParts({ state_loaded: 3, late_databases: 1 }).parts.map((p) => p.tone), ["loaded", "waiting", "failing"]);
});

test("nothing yet reads 0 of N, muted", () => {
  assert.deepEqual(texts(stateSummary({ state_current: 0, state_first_load: 200 })), ["zero:0 of 200 loaded", "waiting:200 waiting"]);
});

test("a run's progress counts done, in flight and failed, and reads all done at the end", () => {
  assert.deepEqual(texts(progressSummary({ unchanged: 500, loaded: 312, inFlight: 40, failed: 2, total: 1002 })),
    ["loaded:812 of 1,002 done", "running:40 in flight", "failing:2 failed"]);
  assert.deepEqual(progressSummary({ unchanged: 900, loaded: 102, inFlight: 0, failed: 0, total: 1002 }),
    { allDone: "All 1,002 done", items: [] });
});
