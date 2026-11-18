import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { GOTO_PAGES } from "./goto_pages.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const routerSrc = fs.readFileSync(
  path.join(
    HERE,
    "..",
    "..",
    "..",
    "..",
    "frontend",
    "src",
    "router",
    "index.js"
  ),
  "utf8"
);

test("goto pages: the keys the agent may name, and the routes they open", () => {
  assert.deepEqual(GOTO_PAGES, {
    dashboards: "DashboardsPage",
    "payment-calendar": "PaymentCalendar",
  });
});

test("goto pages: every route name is a route the SPA router defines", () => {
  for (const [page, route] of Object.entries(GOTO_PAGES)) {
    assert.ok(
      routerSrc.includes(`name: "${route}"`),
      `${page} -> ${route} is not a route name in frontend/src/router/index.js`
    );
  }
});

test("goto pages: the table cannot be changed at run time", () => {
  assert.ok(Object.isFrozen(GOTO_PAGES));
});
