// worn-room.mjs holds the two numbers the widget needs before any art loads. It must stay
// text only (no imports), and scene-geometry.mjs must hand out the very same values.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { WORN_GAP_PX, WORN_OVERHANG } from "./worn-room.mjs";
import * as geometry from "./scene-geometry.mjs";

<<<<<<< HEAD
// Static, dynamic and re-export forms; line comments are dropped first.
const IMPORTS = /\bimport\b\s*[({"'*\w]|^\s*export\b[^;]*\bfrom\b/m;
const withoutComments = (text) => text.replace(/\/\/.*$/gm, "");

=======
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
test("worn-room.mjs has no imports, so it can ride in the always-loaded bundle", () => {
  const source = readFileSync(
    new URL("./worn-room.mjs", import.meta.url),
    "utf8"
  );
<<<<<<< HEAD
  assert.equal(IMPORTS.test(withoutComments(source)), false);
});

test("the import check catches static, dynamic and re-export forms", () => {
  for (const code of [
    'import { a } from "./x.mjs";',
    'import "./x.mjs";',
    'const m = await import("./x.mjs");',
    'export { a } from "./x.mjs";',
    'export * from "./x.mjs";',
  ]) {
    assert.equal(IMPORTS.test(withoutComments(code)), true, code);
  }
  assert.equal(
    IMPORTS.test(withoutComments("// no imports here\nexport const a = 1;")),
    false
  );
=======
  assert.equal(/^\s*import\s/m.test(source), false);
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
});

test("scene-geometry.mjs re-exports the same objects", () => {
  assert.equal(geometry.WORN_OVERHANG, WORN_OVERHANG);
  assert.equal(geometry.WORN_GAP_PX, WORN_GAP_PX);
});
