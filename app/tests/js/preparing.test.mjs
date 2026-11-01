// Tests for what a job's page says while it isn't Ready (src/frontend/src/preparing.ts): its one line at each stage, its
// bar, why a database was set aside, and its schedule once Ready. frontend_preparing_test.py runs this from pytest.
import { test } from "node:test";
import assert from "node:assert/strict";
import { minutesLeftText, preparingBar, preparingLine, scheduleWhenReady, setAsideWhy } from "../../src/frontend/src/preparing.ts";

const reading = { state: "preparing", stage: "reading",
  reading: { databases: 3000, read: 1240, tables: 58280, minutes_left: 12 } };

test("reading says how many databases of how many, the tables so far and the minutes left", () => {
  assert.equal(preparingLine(reading), "Preparing: reading tables, 1,240 of 3,000 databases (58,280 tables so far), about 12 min left");
  assert.deepEqual(preparingBar(reading), { done: 1240, total: 3000 });
  const early = { ...reading, reading: { ...reading.reading, minutes_left: null } };
  assert.equal(preparingLine(early), "Preparing: reading tables, 1,240 of 3,000 databases (58,280 tables so far)");
  assert.equal(preparingLine({ state: "preparing", stage: "reading", reading: null }), "Preparing: reading tables");
});

test("the minutes left read in hours past an hour", () => {
  assert.equal(minutesLeftText(null), null);
  assert.equal(minutesLeftText(1), "about 1 min left");
  assert.equal(minutesLeftText(60), "about 1 h left");
  assert.equal(minutesLeftText(80), "about 1 h 20 min left");
});

test("writing counts the tables written, and making the Databricks jobs counts them", () => {
  const writing = { state: "preparing", stage: "writing", progress: { tables_written: 60000, tables_total: 141000 } };
  assert.equal(preparingLine(writing), "Preparing: writing the job's tables, 60,000 of 141,000");
  assert.deepEqual(preparingBar(writing), { done: 60000, total: 141000 });
  const syncing = { state: "preparing", stage: "syncing", progress: { jobs: 14 } };
  assert.equal(preparingLine(syncing), "Preparing: making the job's 14 Databricks jobs");
  assert.equal(preparingBar(syncing), null);
});

test("needs you says how many databases wait, and why", () => {
  const unreadable = { state: "needs_you", stage: "writing", reading: { databases: 3000, read: 3000, tables: 1 },
    counts: { waiting: 3, waiting_unreadable: 3, waiting_clash: 0 } };
  assert.equal(preparingLine(unreadable), "Needs you: 3 of 3,000 databases couldn't be read");
  const one = { state: "needs_you", counts: { waiting: 1, waiting_unreadable: 1, waiting_clash: 0 } };
  assert.equal(preparingLine(one), "Needs you: 1 database couldn't be read");
  const clash = { state: "needs_you", counts: { waiting: 2, waiting_unreadable: 0, waiting_clash: 2 } };
  assert.equal(preparingLine(clash), "Needs you: 2 databases not added");
  const both = { state: "needs_you", counts: { waiting: 5, waiting_unreadable: 3, waiting_clash: 2 } };
  assert.equal(preparingLine(both), "Needs you: 5 databases set aside");
  assert.equal(preparingLine({ state: "ready" }), "Ready");
});

test("each set-aside database says why, and what happens to it next", () => {
  assert.equal(setAsideWhy({ server: "s", database: "d", kind: "unreadable", why: "Login failed" }), "couldn't be read: Login failed");
  assert.equal(setAsideWhy({ server: "s", database: "d", kind: "clash", why: "another table already writes main.b.t",
    job: "Sales" }), "not added: another table already writes main.b.t (the job Sales)");
  assert.equal(setAsideWhy({ server: "s", database: "d", kind: "closed", why: "no access" }), "left out: no access");
  assert.equal(setAsideWhy({ server: "s", database: "d", kind: "unreadable", why: "x", again: { by: "a" } }),
    "couldn't be read: x; to be read again");
  assert.equal(setAsideWhy({ server: "s", database: "d", kind: "clash", why: "x", replan: { by: "a" } }),
    "not added: x; to be planned again with the new target names");
});

test("the schedule says whether it turns on once the job is ready", () => {
  assert.equal(scheduleWhenReady({ turn_on: true }), "Schedule: on when ready");
  assert.equal(scheduleWhenReady({ turn_on: false }), "Schedule: stays paused when ready");
});
