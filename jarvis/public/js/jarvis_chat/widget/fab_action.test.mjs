import { test } from "node:test";
import assert from "node:assert/strict";
import { fabAction, panelTogglePlan } from "./fab_action.mjs";

const MIN = 600; // stand-in for PANEL_MIN_VIEWPORT_PX

// ---- fabAction: routing the tap -------------------------------------------

test("fabAction: no access -> no-access page, never opens the panel", () => {
  // The gate: a role-less user never opens (so never mounts) the Panel, so its
  // on-mount get_chat_ui_settings() PermissionError dialog cannot fire. Viewport
  // is irrelevant when there is no access.
  assert.equal(fabAction(false, 1200, MIN), "no-access");
  assert.equal(fabAction(false, 400, MIN), "no-access");
});

test("fabAction: no access wins over a narrow viewport", () => {
  // Access is checked first, so a role-less user on a phone still lands on the
  // recoverable no-access page, not the full chat SPA.
  assert.equal(fabAction(false, 320, MIN), "no-access");
});

test("fabAction: access on a wide viewport -> toggle the in-place panel", () => {
  assert.equal(fabAction(true, 1200, MIN), "toggle");
  assert.equal(fabAction(true, MIN, MIN), "toggle"); // exactly at the threshold
});

test("fabAction: access on a narrow viewport -> hand off to the full chat SPA", () => {
  assert.equal(fabAction(true, MIN - 1, MIN), "full");
  assert.equal(fabAction(true, 320, MIN), "full");
});

// ---- panelTogglePlan: driving the lazily mounted Panel --------------------

test("panelTogglePlan: FIRST open mounts closed and defers the reveal a tick", () => {
  // Regression guard: on the first open the reveal MUST be deferred so Panel.vue's
  // non-immediate watch(open) sees false->true and restores the conversation. A
  // same-tick reveal opens the panel blank and mints a new conversation.
  assert.deepEqual(panelTogglePlan({ mounted: false, open: false }), {
    mount: true,
    open: true,
    deferReveal: true,
    readContext: true,
  });
});

test("panelTogglePlan: reopen toggles an already-mounted panel in the same tick", () => {
  // Already mounted and watching, so no deferral: open synchronously and re-read
  // the record context (matches the old read-context-before-open behaviour).
  assert.deepEqual(panelTogglePlan({ mounted: true, open: false }), {
    mount: false,
    open: true,
    deferReveal: false,
    readContext: true,
  });
});

test("panelTogglePlan: closing hides without re-reading context or deferring", () => {
  // Negative case: a close must not re-read the Desk context and must not defer.
  assert.deepEqual(panelTogglePlan({ mounted: true, open: true }), {
    mount: false,
    open: false,
    deferReveal: false,
    readContext: false,
  });
});
