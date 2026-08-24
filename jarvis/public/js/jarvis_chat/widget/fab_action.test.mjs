import { test } from "node:test";
import assert from "node:assert/strict";
import { fabAction } from "./fab_action.mjs";

const MIN = 600; // stand-in for PANEL_MIN_VIEWPORT_PX

test("no access -> redirect to the no-access page, never opens the panel", () => {
  // The whole point of the gate: a role-less user never opens (and so never
  // mounts) the Panel, so its on-mount get_chat_ui_settings() PermissionError
  // dialog can't fire. Viewport is irrelevant when there's no access.
  assert.equal(fabAction(false, 1200, MIN), "no-access");
  assert.equal(fabAction(false, 400, MIN), "no-access");
});

test("access on a wide viewport -> toggle the in-place panel", () => {
  assert.equal(fabAction(true, 1200, MIN), "toggle");
  assert.equal(fabAction(true, MIN, MIN), "toggle"); // exactly at the threshold
});

test("access on a narrow viewport -> hand off to the full chat SPA", () => {
  assert.equal(fabAction(true, MIN - 1, MIN), "full");
  assert.equal(fabAction(true, 320, MIN), "full");
});

test("no access wins over a narrow viewport", () => {
  // The access check is first, so a no-access user on a phone still lands on
  // the recoverable no-access page, not the full chat SPA.
  assert.equal(fabAction(false, 320, MIN), "no-access");
});
