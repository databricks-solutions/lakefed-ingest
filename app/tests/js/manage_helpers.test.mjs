// Tests for Manage's helpers (app/src/frontend/src/pages/manageHelpers.ts). Run with `node --test`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { exportHref, schemaNote } from "../../src/frontend/src/pages/manageHelpers.ts";

const SCHEMA = { database: 13, app: 13, check: { state: "current" }, run: null, error: null };

test("no job ticked exports every ingestion job; ticked ones are named by id", () => {
  assert.equal(exportHref([]), "/api/manage/export.csv");
  assert.equal(exportHref([7]), "/api/manage/export.csv?jobs=7");
  assert.equal(exportHref([7, 12]), "/api/manage/export.csv?jobs=7,12");
});

test("a current schema says nothing; a setup run going says the database is upgrading", () => {
  assert.equal(schemaNote(SCHEMA), null);
  assert.equal(schemaNote(undefined), null);
  const running = { ...SCHEMA, database: 12, run: { run_id: 41, url: "https://h/run/41" } };
  assert.deepEqual(schemaNote(running), { tone: "info", text: "Upgrading the state database." });
});

test("a database behind says so, with why the app could not start the setup job", () => {
  const behind = { ...SCHEMA, database: 12, check: { state: "failed", error: "PermissionDenied: no" } };
  assert.equal(schemaNote(behind).tone, "warn");
  assert.match(schemaNote(behind).text, /^The state database is behind \(version 12, the app's is 13\)\./);
  assert.match(schemaNote(behind).text, /could not start the setup job: PermissionDenied: no\.$/);
  assert.doesNotMatch(schemaNote({ ...behind, check: { state: "busy" } }).text, /could not/);
});

test("no schema at all is the deploy's setup step, and an unreadable version is shown as such", () => {
  assert.match(schemaNote({ ...SCHEMA, database: 0 }).text, /no schema yet: run the setup job once/);
  assert.deepEqual(schemaNote({ ...SCHEMA, database: null, error: "can't read the state database's schema version: x" }),
                   { tone: "bad", text: "can't read the state database's schema version: x." });
});
