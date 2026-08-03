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
  // (router.js), so hiding on that route keeps the FAB off the setup screen
  // WITHOUT suppressing it on normal desk pages — where a user sets Jarvis up
  // from the bubble even before a Company exists.
  assert.equal(shouldHideWidget(["setup-wizard"]), true);
});

test("HIDE_ON_ROUTES are the only hidden routes", () => {
  assert.equal(
    HIDE_ON_ROUTES.every((r) => shouldHideWidget([r]) === true),
    true
  );
});
