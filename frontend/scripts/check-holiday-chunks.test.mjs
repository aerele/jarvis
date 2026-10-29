// Self-test for the chunk gate: it plants the violations the gate exists to catch.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { ART_MARKERS, checkHolidayChunks } from "./check-holiday-chunks.mjs";

const ART = ART_MARKERS.join(" ");
const WORDS = "Wishing you a happy Diwali in advance!";
const HTML =
  '<script type="module" src="/assets/jarvis/frontend/assets/index-AAAA.js"></script>';

function build(files) {
  const dir = mkdtempSync(path.join(tmpdir(), "holiday-chunks-"));
  for (const [name, body] of Object.entries(files))
    writeFileSync(path.join(dir, name), body);
  return dir;
}

test("a good build passes", () => {
  const dir = build({ "index-AAAA.js": WORDS, "holiday-worn-BBBB.js": ART });
  assert.deepEqual(checkHolidayChunks(dir, HTML), []);
});

test("art in the main chunk fails", () => {
  const dir = build({
    "index-AAAA.js": WORDS + ART,
    "holiday-worn-BBBB.js": ART,
  });
  assert.match(
    checkHolidayChunks(dir, HTML).join("\n"),
    /index-AAAA\.js, not only holiday/
  );
});

test("art in some other chunk fails", () => {
  const dir = build({
    "index-AAAA.js": WORDS,
    "ChatView-CCCC.js": ART,
    "holiday-worn-BBBB.js": ART,
  });
  assert.match(checkHolidayChunks(dir, HTML).join("\n"), /ChatView-CCCC\.js/);
});

test("no holiday chunk at all fails", () => {
  const dir = build({ "index-AAAA.js": WORDS });
  assert.match(checkHolidayChunks(dir, HTML).join("\n"), /no holiday-\* chunk/);
});

test("words missing from main fails", () => {
  const dir = build({ "index-AAAA.js": "x", "holiday-worn-BBBB.js": ART });
  assert.match(checkHolidayChunks(dir, HTML).join("\n"), /words are missing/);
});

test("a missing entry chunk is reported, not thrown", () => {
  const dir = build({ "holiday-worn-BBBB.js": ART });
  assert.match(
    checkHolidayChunks(dir, HTML).join("\n"),
    /main entry chunk not found/
  );
});

test("the scene art is in the gate's markers", () => {
  assert.ok(ART_MARKERS.some((m) => m.includes("M-16 0 Q-15 12")));
  assert.ok(ART_MARKERS.some((m) => m.includes("#a16207")));
});

test("scene art in the main chunk fails, even with the worn art in its own chunk", () => {
  const dir = build({
    "index-AAAA.js":
      WORDS + ' stroke="#a16207" M-16 0 Q-15 12 0 13 Q11 13 15 3',
    "holiday-worn-BBBB.js": ART,
  });
  assert.match(
    checkHolidayChunks(dir, HTML).join("\n"),
    /index-AAAA\.js, not only holiday/
  );
});

test("a build whose holiday chunks lack the scene art fails", () => {
  const dir = build({
    "index-AAAA.js": WORDS,
    "holiday-worn-BBBB.js": "M60.5 26 C66 33 67 38.5 @keyframes jvh-hop",
  });
  assert.match(
    checkHolidayChunks(dir, HTML).join("\n"),
    /holiday-\* chunks lack/
  );
});
