import { test } from "node:test";
import assert from "node:assert/strict";
import { shouldHideWidget } from "./widget_visibility.mjs";

test("shown on a normal desk page", () => {
  assert.equal(shouldHideWidget(["List", "Sales Invoice"]), false);
  assert.equal(shouldHideWidget(["Form", "User"]), false);
  assert.equal(shouldHideWidget([]), false);
  assert.equal(shouldHideWidget(undefined), false);
});

test("hidden on Jarvis's own full-page routes", () => {
  assert.equal(shouldHideWidget(["jarvis-chat"]), true);
  assert.equal(shouldHideWidget(["jarvis-onboarding"]), true);
  assert.equal(shouldHideWidget(["jarvis-chat"], true), true);
  assert.equal(shouldHideWidget(["jarvis-onboarding"], true), true);
});

test("hidden on Frappe's setup wizard (the site-setup screen)", () => {
  // While setup isn't complete Frappe forces route[0] to "setup-wizard"
  // (router.js), so hiding on that route keeps the FAB off the setup screen.
  assert.equal(shouldHideWidget(["setup-wizard"]), true);
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

test("opt-in hides only users without the existing access verdict", () => {
  for (const hasAccess of [false, undefined, null]) {
    assert.equal(
      shouldHideWidget(["List", "User"], true, hasAccess, true),
      true
    );
  }
  assert.equal(shouldHideWidget(["List", "User"], true, true, true), false);
});

test("disabled or missing option preserves unauthorized users' shortcut", () => {
  for (const option of [false, undefined]) {
    assert.equal(
      shouldHideWidget(["List", "User"], true, false, option),
      false
    );
  }
});

test("access never overrides existing route and setup hiding", () => {
  for (const option of [false, true]) {
    assert.equal(shouldHideWidget(["setup-wizard"], true, true, option), true);
    assert.equal(shouldHideWidget(["List", "User"], false, true, option), true);
  }
});
