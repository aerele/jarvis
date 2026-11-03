// The post-build bundle gate, proved against a temp assets dir: a clean build passes, a
// planted violation fails, and a build with nothing to check fails too.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { ART_MARKERS } from "../../../../../frontend/scripts/check-holiday-chunks.mjs";
import { checkBuiltDeskBundles } from "./holiday_built_bundles.mjs";

const ART = "jarvis_holiday_touch.bundle.ABC123.js";
const WIDGET = "jarvis_widget.bundle.DEF456.js";

// A fake sites/assets with the two bundles; `files` overrides or adds built files.
function fixture(t, { files = {}, manifest } = {}) {
  const dir = mkdtempSync(path.join(os.tmpdir(), "jv-bundles-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const dist = path.join(dir, "jarvis/dist/js");
  mkdirSync(dist, { recursive: true });
  const built = {
    [ART]: `x=${JSON.stringify(ART_MARKERS[0])}`,
    [WIDGET]: "y=1",
    ...files,
  };
  for (const [name, body] of Object.entries(built))
    writeFileSync(path.join(dist, name), body);
  const entries = manifest || {
    "jarvis_holiday_touch.bundle.js": `/assets/jarvis/dist/js/${ART}`,
    "jarvis_widget.bundle.js": `/assets/jarvis/dist/js/${WIDGET}`,
  };
  writeFileSync(path.join(dir, "assets.json"), JSON.stringify(entries));
  return dir;
}

test("a clean build passes", (t) => {
  assert.deepEqual(checkBuiltDeskBundles(fixture(t)), []);
});

test("an art marker in the widget bundle fails, naming the file and the marker", (t) => {
  const dir = fixture(t, {
    files: { [WIDGET]: `y=${JSON.stringify(ART_MARKERS[1])}` },
  });
  const [problem, ...rest] = checkBuiltDeskBundles(dir);
  assert.equal(rest.length, 0);
  assert.match(problem, new RegExp(WIDGET.replace(/\./g, "\\.")));
  assert.match(problem, /@keyframes jvh-hop/);
});

test("an art marker in any other built file fails", (t) => {
  const dir = fixture(t, { files: { "chunk.AAA.js": ART_MARKERS[3] } });
  assert.equal(checkBuiltDeskBundles(dir).length, 1);
});

test("every marker is looked for, not just the first", (t) => {
  for (const marker of ART_MARKERS) {
    const dir = fixture(t, { files: { [WIDGET]: marker } });
    assert.equal(checkBuiltDeskBundles(dir).length, 1, marker);
  }
});

test("a build with no art bundle, or an art bundle without the art, fails", (t) => {
  const none = fixture(t, {
    manifest: { "jarvis_widget.bundle.js": `/assets/jarvis/dist/js/${WIDGET}` },
  });
  assert.match(checkBuiltDeskBundles(none)[0], /is not built/);
  const empty = fixture(t, { files: { [ART]: "x=1" } });
  assert.match(checkBuiltDeskBundles(empty)[0], /lacks the worn art/);
});

test("a missing widget bundle or manifest fails rather than passing on nothing", (t) => {
  const noWidget = fixture(t, {
    manifest: {
      "jarvis_holiday_touch.bundle.js": `/assets/jarvis/dist/js/${ART}`,
    },
  });
  assert.match(
    checkBuiltDeskBundles(noWidget).join("\n"),
    /jarvis_widget\.bundle\.js is not built/
  );
  const missing = mkdtempSync(path.join(os.tmpdir(), "jv-bundles-"));
  t.after(() => rmSync(missing, { recursive: true, force: true }));
  assert.match(checkBuiltDeskBundles(missing)[0], /assets\.json/);
});
