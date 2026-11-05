// Tests for the one-line table cell helpers (src/frontend/src/cells.ts): a connection type's display name, a host with
// its port, and whether a cell's text is cut. Run with `node --test`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { SOURCE_TYPE_NAMES, connectionTypeName, hostLabel, isCut, tableMinWidth, tipKeepsClick } from "../../src/frontend/src/cells.ts";

test("each type the app reads is named as Catalog Explorer names it", () => {
  assert.equal(connectionTypeName("SQLSERVER"), "SQL Server");
  assert.equal(connectionTypeName("POSTGRESQL"), "PostgreSQL");
  assert.equal(connectionTypeName("MYSQL"), "MySQL");
  assert.equal(connectionTypeName("ORACLE"), "Oracle");
});

test("the name is found whatever the case, as the connection API and the control table spell it differently", () => {
  assert.equal(connectionTypeName("sqlserver"), "SQL Server");
  assert.equal(connectionTypeName("SqlServer"), "SQL Server");
});

test("a type with no name here shows as it came, and no type shows as nothing", () => {
  assert.equal(connectionTypeName("SQLDW"), "SQLDW");
  assert.equal(connectionTypeName("ADOBE_COMMERCE"), "ADOBE_COMMERCE");
  assert.equal(connectionTypeName(null), "");
  assert.equal(connectionTypeName(undefined), "");
});

test("the table of names is the one the Source type column uses", () => {
  assert.deepEqual(Object.keys(SOURCE_TYPE_NAMES).sort(), ["mysql", "oracle", "postgresql", "sqlserver"]);
});

test("a host is shown with its port when it has one", () => {
  assert.equal(hostLabel("db.example.com", "1433"), "db.example.com:1433");
  assert.equal(hostLabel("db.example.com", 1433), "db.example.com:1433");
  assert.equal(hostLabel("db.example.com", 14330), "db.example.com:14330");
  assert.equal(hostLabel("db.example.com"), "db.example.com");
  assert.equal(hostLabel("db.example.com", null), "db.example.com");
  assert.equal(hostLabel("db.example.com", ""), "db.example.com");
});

test("text is cut when its content is wider than its box, with a pixel allowed for rounding", () => {
  assert.equal(isCut(300, 200), true);
  assert.equal(isCut(202, 200), true);
  assert.equal(isCut(201, 200), false);
  assert.equal(isCut(200, 200), false);
  assert.equal(isCut(0, 0), false);
});

test("a click on text with no tooltip goes on to the row around it, whatever else is asked", () => {
  assert.equal(tipKeepsClick("", false), false);
  assert.equal(tipKeepsClick("", true), false);
});

test("a click on text with a tooltip stays with the tooltip unless the row is meant to take it too", () => {
  assert.equal(tipKeepsClick("a long connection name", false), true);
  assert.equal(tipKeepsClick("a long connection name", true), false);
});

test("a one-line table's narrowest width is its fixed columns plus a set room for each flexible one", () => {
  assert.equal(tableMinWidth([40, null, 100, null, 180]), 40 + 160 + 100 + 160 + 180);
  assert.equal(tableMinWidth([40, null, 100, null, 180], 100), 40 + 100 + 100 + 100 + 180);
  assert.equal(tableMinWidth([80, 120]), 200);
  assert.equal(tableMinWidth([]), 0);
});
