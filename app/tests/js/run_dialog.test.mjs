// Tests for the line Run now and Retry now show in their confirmation (src/frontend/src/runDialog.ts): what is loaded
// and where from, with a few table names and "and N more", and no word on cost. frontend_run_dialog_test.py runs this
// file from pytest.
import { test } from "node:test";
import assert from "node:assert/strict";
import { retryLine, runNowFallbackLine, runNowLine, sourceOf, tableList } from "../../src/frontend/src/runDialog.ts";

const oneDatabase = { tables: 1002, servers: 1, databases: 1, server: "lfc_sqlserver_discover", database: "lfc_eng_001",
                      names: ["dbo.a", "dbo.b", "dbo.c"] };

test("a long list names the first three and how many more", () => {
  assert.equal(runNowLine(oneDatabase, false),
               "It loads 1,002 tables from lfc_sqlserver_discover / lfc_eng_001: dbo.a, dbo.b, dbo.c and 999 more.");
});

test("three tables are all named, with no 'more'", () => {
  const three = { ...oneDatabase, tables: 3 };
  assert.equal(runNowLine(three, false), "It loads 3 tables from lfc_sqlserver_discover / lfc_eng_001: dbo.a, dbo.b and dbo.c.");
});

test("one table and two tables read as a sentence", () => {
  assert.equal(runNowLine({ ...oneDatabase, tables: 1, names: ["dbo.a"] }, false),
               "It loads 1 table from lfc_sqlserver_discover / lfc_eng_001: dbo.a.");
  assert.equal(tableList(["dbo.a", "dbo.b"], 2), "dbo.a and dbo.b");
});

test("a job over several databases says how many, and on how many servers", () => {
  const one = { ...oneDatabase, databases: 40, database: "a" };
  assert.equal(sourceOf(one), "40 databases on lfc_sqlserver_discover");
  assert.equal(sourceOf({ ...one, servers: 2 }), "40 databases on 2 servers");
});

test("a check job says it checks first and loads the tables that changed", () => {
  assert.equal(runNowLine(oneDatabase, true),
               "It checks 1,002 tables from lfc_sqlserver_discover / lfc_eng_001 for changes and loads those that changed: "
               + "dbo.a, dbo.b, dbo.c and 999 more.");
});

test("a job with no enabled table says it loads nothing", () => {
  assert.equal(runNowLine({ ...oneDatabase, tables: 0, names: [] }, false), "No table is enabled, so it loads nothing.");
});

test("a server with no name leaves out 'from'", () => {
  assert.equal(runNowLine({ ...oneDatabase, server: "", database: "", names: ["x.y"], tables: 1 }, false), "It loads 1 table: x.y.");
});

test("when the preview cannot be read, the line comes from the page's count and groups", () => {
  assert.equal(runNowFallbackLine(1002, ["g1"], false), "It loads 1,002 tables in 1 ingestion group: g1.");
  assert.equal(runNowFallbackLine(9, ["g1", "g2", "g3", "g4"], true),
               "It checks 9 tables for changes and loads those that changed, in 4 ingestion groups: g1, g2, g3 and 1 more.");
});

test("Retry now names the database and its server", () => {
  assert.equal(retryLine("lfc_eng_001", "lfc_sqlserver_discover"),
               "It loads the enabled tables of lfc_eng_001 on lfc_sqlserver_discover again, from their checkpoints.");
});

test("no line says anything about cost", () => {
  const lines = [runNowLine(oneDatabase, false), runNowLine(oneDatabase, true), runNowFallbackLine(5, ["g"], false),
                 retryLine("d", "s")];
  for (const line of lines) assert.doesNotMatch(line, /cost|money|\$/i);
});
