import { test } from "node:test";
import assert from "node:assert/strict";
import { contextFromRoute, contextLabel } from "./desk_context.mjs";

test("contextFromRoute: form view yields doctype + name", () => {
  assert.deepEqual(contextFromRoute(["Form", "Sales Invoice", "INV-0001"]), {
    doctype: "Sales Invoice",
    name: "INV-0001",
  });
});

test("contextFromRoute: list view yields doctype only", () => {
  assert.deepEqual(contextFromRoute(["List", "Sales Invoice", "List"]), {
    doctype: "Sales Invoice",
  });
});

test("contextFromRoute: report with filters", () => {
  assert.deepEqual(
    contextFromRoute(["query-report", "Accounts Receivable"], {
      filters: { company: "Acme", status: "Overdue" },
    }),
    {
      report_name: "Accounts Receivable",
      filters: { company: "Acme", status: "Overdue" },
    }
  );
});

test("contextFromRoute: report with no filters omits the filters key", () => {
  assert.deepEqual(contextFromRoute(["query-report", "Accounts Receivable"]), {
    report_name: "Accounts Receivable",
  });
});

test("contextFromRoute: report with an empty filter object omits the key", () => {
  assert.deepEqual(
    contextFromRoute(["query-report", "Accounts Receivable"], { filters: {} }),
    {
      report_name: "Accounts Receivable",
    }
  );
});

test("contextFromRoute: unknown routes yield null", () => {
  assert.equal(contextFromRoute(["Workspaces", "Home"]), null);
  assert.equal(contextFromRoute([]), null);
  assert.equal(contextFromRoute(null), null);
  assert.equal(contextFromRoute(undefined), null);
  // A brand-new unsaved doc has no name segment: there is no record to point at.
  assert.equal(contextFromRoute(["Form", "Sales Invoice"]), null);
});

test("contextLabel: renders each shape", () => {
  assert.equal(
    contextLabel({ doctype: "Sales Invoice", name: "INV-0001" }),
    "Sales Invoice INV-0001"
  );
  assert.equal(
    contextLabel({ doctype: "Sales Invoice" }),
    "the Sales Invoice list"
  );
  assert.equal(
    contextLabel({ report_name: "Accounts Receivable" }),
    "Accounts Receivable report"
  );
});

test("contextLabel: empty for absent or unrecognized context", () => {
  assert.equal(contextLabel(null), "");
  assert.equal(contextLabel(undefined), "");
  assert.equal(contextLabel({}), "");
  assert.equal(contextLabel("nonsense"), "");
});
