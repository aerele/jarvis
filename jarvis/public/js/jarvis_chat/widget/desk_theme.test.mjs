import { test } from "node:test";
import assert from "node:assert/strict";
import { resolveDark } from "./desk_theme.mjs";

test("an explicit data-theme wins over everything", () => {
  assert.equal(resolveDark("dark", "light", false), true);
  assert.equal(resolveDark("light", "dark", true), false);
});

test("falls back to the mode when data-theme is absent", () => {
  assert.equal(resolveDark(null, "dark", false), true);
  assert.equal(resolveDark("", "light", true), false);
});

test("automatic mode follows the OS preference", () => {
  assert.equal(resolveDark(null, "automatic", true), true);
  assert.equal(resolveDark(null, "automatic", false), false);
  assert.equal(resolveDark(null, "auto", true), true);
});

test("nothing set at all follows the OS preference", () => {
  assert.equal(resolveDark(null, null, true), true);
  assert.equal(resolveDark(null, null, false), false);
});

test("case is not significant", () => {
  assert.equal(resolveDark("Dark", null, false), true);
  assert.equal(resolveDark(null, "DARK", false), true);
});

test("an unknown mode is treated as light, not as dark", () => {
  assert.equal(resolveDark(null, "sepia", false), false);
  assert.equal(resolveDark(null, "sepia", true), false);
});
