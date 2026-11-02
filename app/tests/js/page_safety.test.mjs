// Tests for what keeps a page from going white: the preview's server rows read whatever shape the server sends
// (src/frontend/src/pages/previewRows.ts), and a page that throws shows a message instead of nothing
// (src/frontend/src/errorBoundary.ts). frontend_pending_test.py runs this file from pytest.
import { test } from "node:test";
import assert from "node:assert/strict";
import { serverRowCells, targetBlock } from "../../src/frontend/src/pages/previewRows.ts";
import { PageErrorBoundary } from "../../src/frontend/src/errorBoundary.ts";

const LABELS = [["ct", "Change tracking"], ["cursor", "Cursor column"], ["full", "Full reload"]];

test("a by-database preview's server row, with no per-table rows and no load types, is drawn", () => {
  // The shape d4ceb6b sent for Create by database: no load_types at all.
  const cells = serverRowCells({ server: "lfc_sqlserver_discover", databases: 100, tables: 100000,
                                 groups: ["job_lfc_c01", "job_lfc_c02"], read_through: "UC connection" }, LABELS);
  assert.deepEqual(cells, { server: "lfc_sqlserver_discover", readThrough: "UC connection",
                            loadTypes: "load types set when the tables are written", counts: "100 · 100,000", groups: "2" });
});

test("a server row with one load type names it, and with several counts each", () => {
  assert.equal(serverRowCells({ server: "s", databases: 1, tables: 3, groups: [], load_types: { ct: 3 } }, LABELS).loadTypes,
               "Change tracking");
  assert.equal(serverRowCells({ server: "s", databases: 1, tables: 3, groups: [], load_types: { ct: 2, full: 1 } }, LABELS).loadTypes,
               "Change tracking 2, Full reload 1");
});

test("a field in another shape never throws: groups as a number, counts as text, nothing at all", () => {
  assert.equal(serverRowCells({ server: "s", databases: "7", tables: "12", groups: 4 }, LABELS).groups, "4");
  assert.equal(serverRowCells({ server: "s", databases: "7", tables: "12", groups: 4 }, LABELS).counts, "7 · 12");
  assert.deepEqual(serverRowCells({}, LABELS), { server: "", readThrough: "", loadTypes: "load types set when the tables are written",
                                                 counts: "0 · 0", groups: "0" });
});

function texts(node) {
  if (node === null || node === undefined || typeof node === "boolean") return [];
  if (typeof node === "string" || typeof node === "number") return [String(node)];
  if (Array.isArray(node)) return node.flatMap(texts);
  return texts(node.props?.children);
}

test("a page that throws while drawing shows what happened and what to do, never a white page", () => {
  const boundary = new PageErrorBoundary({ children: "the page" });
  assert.equal(boundary.render(), "the page");
  boundary.state = PageErrorBoundary.getDerivedStateFromError(new TypeError("Cannot convert undefined or null to object"));
  const shown = texts(boundary.render()).join(" ");
  assert.match(shown, /This page hit an error/);
  assert.match(shown, /TypeError: Cannot convert undefined or null to object/);
  assert.match(shown, /Reload the page/);
  assert.match(shown, /Go back/);
});

test("Create names the target fields still blank, and a preview is never what waits on them", () => {
  assert.equal(targetBlock({ sink_catalog: "", sink_schema: "", sink_table: "{table}" }),
               "Set the target catalog and schema to create the job");
  assert.equal(targetBlock({ sink_catalog: "main", sink_schema: " ", sink_table: "{table}" }),
               "Set the target schema to create the job");
  assert.equal(targetBlock({ sink_catalog: "", sink_schema: "s", sink_table: "" }),
               "Set the target catalog and table to create the job");
  assert.equal(targetBlock({ sink_catalog: "main", sink_schema: "s", sink_table: "{table}" }), null);
});
