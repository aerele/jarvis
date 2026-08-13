import { test } from "node:test";
import assert from "node:assert/strict";
import { shouldHideWidget, HIDE_ON_ROUTES } from "./widget_visibility.mjs";

test("shown on a normal desk page", () => {
  assert.equal(shouldHideWidget(["List", "Sales Invoice"]), false);
  assert.equal(shouldHideWidget(["Form", "User"]), false);
  assert.equal(shouldHideWidget([]), false);
  assert.equal(shouldHideWidget(undefined), false);
});

test("hidden on Jarvis's own full-page routes", () => {
  assert.equal(shouldHideWidget(["jarvis-chat"]), true);
  assert.equal(shouldHideWidget(["jarvis-onboarding"]), true);
});

test("hidden on Frappe's setup wizard (the site-setup screen)", () => {
  // While setup isn't complete Frappe forces route[0] to "setup-wizard"
  // (router.js), so hiding on that route keeps the FAB off the setup screen.
  assert.equal(shouldHideWidget(["setup-wizard"]), true);
});

test("HIDE_ON_ROUTES are the only hidden routes when setup is complete", () => {
  assert.equal(
    HIDE_ON_ROUTES.every((r) => shouldHideWidget([r], true) === true),
    true
  );
});

test("hidden on a normal desk page when the site has no Company yet", () => {
  // frappe.boot.jarvis_site_setup_complete === false (jarvis/boot.py).
  assert.equal(shouldHideWidget(["List", "Sales Invoice"], false), true);
  assert.equal(shouldHideWidget([], false), true);
});

test("shown when setup-complete is unknown (older boot payload)", () => {
  // Strict === false only, so a boot payload without the key behaves like
  // before: route-based hiding is the only gate.
  assert.equal(shouldHideWidget(["List", "Sales Invoice"], undefined), false);
});

test("shown on a normal desk page once setup is complete", () => {
  assert.equal(shouldHideWidget(["List", "Sales Invoice"], true), false);
});
