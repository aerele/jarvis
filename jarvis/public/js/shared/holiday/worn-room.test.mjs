// worn-room.mjs holds the two numbers the widget needs before any art loads. It must stay
// text only (no imports), and scene-geometry.mjs must hand out the very same values.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { WORN_GAP_PX, WORN_OVERHANG } from "./worn-room.mjs";
import * as geometry from "./scene-geometry.mjs";

test("worn-room.mjs has no imports, so it can ride in the always-loaded bundle", () => {
  const source = readFileSync(
    new URL("./worn-room.mjs", import.meta.url),
    "utf8"
  );
  assert.equal(/^\s*import\s/m.test(source), false);
});

test("scene-geometry.mjs re-exports the same objects", () => {
  assert.equal(geometry.WORN_OVERHANG, WORN_OVERHANG);
  assert.equal(geometry.WORN_GAP_PX, WORN_GAP_PX);
});
