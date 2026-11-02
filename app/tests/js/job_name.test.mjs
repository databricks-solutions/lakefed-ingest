// Tests for a new ingestion job's default name (src/frontend/src/jobName.ts): the Databricks Jobs pattern of a name
// and the time, in a form whose groups are clean identifiers, and the preview's word that a name is taken.
import { test } from "node:test";
import assert from "node:assert/strict";
import { defaultJobName, nameTaken } from "../../src/frontend/src/jobName.ts";

const identifier = (text) => text.toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");    // queries.identifier

test("the default name is a name and the time, to the second", () => {
  assert.equal(defaultJobName(new Date(2026, 9, 30, 9, 5, 7)), "Ingestion job 2026-10-30 09:05:07");
});

test("its groups are clean identifiers: one underscore between parts", () => {
  assert.equal(identifier(defaultJobName(new Date(2026, 9, 30, 11, 16, 42))), "ingestion_job_2026_10_30_11_16_42");
});

test("two jobs made a second apart get different names", () => {
  assert.notEqual(defaultJobName(new Date(2026, 9, 30, 11, 16, 42)), defaultJobName(new Date(2026, 9, 30, 11, 16, 43)));
});

test("a taken name is read from the preview's line problems", () => {
  const taken = [{ errors: ["group ingestion_job_x_sql_a_c01 is already in use: give the job another name"] }];
  assert.equal(nameTaken(taken), true);
  assert.equal(nameTaken([{ errors: ["select_list is empty"] }]), false);
  assert.equal(nameTaken(undefined), false);
});
