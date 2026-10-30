// Self-test for the chunk gate: it plants the violations the gate exists to catch.
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  ART_MARKERS,
  WORN_MARKERS,
  checkHolidayChunks,
  checkPwaBuild,
} from "./check-holiday-chunks.mjs";

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

// ---- the PWA build: the same art rule, plus what the service worker precaches.
const PWA_HTML =
  '<script type="module" src="/assets/jarvis/pwa/assets/index-AAAA.js"></script>';
const swFor = (...files) =>
  "precacheAndRoute([" +
  files
    .map((f) => `{"url":"/assets/jarvis/pwa/assets/${f}","revision":null}`)
    .join(",") +
  "])";
const WORN = WORN_MARKERS.join(" ");

function pwaBuild(files, sw) {
  const dir = mkdtempSync(path.join(tmpdir(), "holiday-pwa-"));
  mkdirSync(path.join(dir, "assets"));
  for (const [name, body] of Object.entries(files))
    writeFileSync(path.join(dir, "assets", name), body);
  writeFileSync(path.join(dir, "sw.js"), sw);
  return dir;
}
const check = (dir) => checkPwaBuild(dir, PWA_HTML).join("\n");

const GOOD = {
  "index-AAAA.js": 'import"./vendor-DDDD.js";const a=1;',
  "vendor-DDDD.js": "export default 1",
  "NewChatView-EEEE.js": `import{x}from"./vendor-DDDD.js";const w="${WORDS}";import("./holiday-worn-BBBB.js")`,
  "holiday-worn-BBBB.js": WORN,
};
const GOOD_SW = swFor("index-AAAA.js", "vendor-DDDD.js", "NewChatView-EEEE.js");

test("pwa: a good build passes", () => {
  assert.equal(check(pwaBuild(GOOD, GOOD_SW)), "");
});

test("pwa: a STATIC import of a non-precached ordinary chunk fails", () => {
  // The lazy chunk is a real file in the build but not in the precache.
  const files = {
    ...GOOD,
    "lazy-GGGG.js": "export default 3",
    "NewChatView-EEEE.js":
      GOOD["NewChatView-EEEE.js"] + 'import{z}from"./lazy-GGGG.js";',
  };
  assert.match(
    check(pwaBuild(files, GOOD_SW)),
    /NewChatView-EEEE\.js is precached but statically imports lazy-GGGG\.js/
  );
});

test("pwa: words that exist only in a holiday chunk fail", () => {
  const files = {
    ...GOOD,
    "NewChatView-EEEE.js": "x",
    "holiday-worn-BBBB.js": WORN + WORDS,
  };
  assert.match(check(pwaBuild(files, GOOD_SW)), /words are missing/);
});

test("pwa: a holiday chunk in the precache fails", () => {
  const sw = swFor(
    "index-AAAA.js",
    "vendor-DDDD.js",
    "NewChatView-EEEE.js",
    "holiday-worn-BBBB.js"
  );
  assert.match(
    check(pwaBuild(GOOD, sw)),
    /precache lists holiday-worn-BBBB\.js/
  );
});

test("pwa: a precached chunk that statically imports a non-precached file fails", () => {
  const files = {
    ...GOOD,
    "NewChatView-EEEE.js":
      GOOD["NewChatView-EEEE.js"] + 'import{y}from"./holiday-worn-BBBB.js";',
  };
  assert.match(
    check(pwaBuild(files, GOOD_SW)),
    /NewChatView-EEEE\.js is precached but statically imports holiday-worn-BBBB\.js/
  );
  const unlisted = {
    ...GOOD,
    "orphan-FFFF.js": "export default 2",
    "index-AAAA.js": 'import"./orphan-FFFF.js";',
  };
  assert.match(
    check(pwaBuild(unlisted, GOOD_SW)),
    /index-AAAA\.js is precached but statically imports orphan-FFFF\.js/
  );
});

test("pwa: a DYNAMIC import of a non-precached ordinary chunk is fine", () => {
  const files = {
    ...GOOD,
    "lazy-GGGG.js": "export default 3",
    "NewChatView-EEEE.js":
      GOOD["NewChatView-EEEE.js"] + 'import("./lazy-GGGG.js");',
  };
  assert.equal(check(pwaBuild(files, GOOD_SW)), "");
});

test("pwa: worn art in a precached chunk fails", () => {
  const files = {
    ...GOOD,
    "NewChatView-EEEE.js": GOOD["NewChatView-EEEE.js"] + WORN,
  };
  assert.match(
    check(pwaBuild(files, GOOD_SW)),
    /NewChatView-EEEE\.js, not only holiday/
  );
});

test("pwa: scene art in a non-holiday chunk fails, there are no scenes on the phone yet", () => {
  const files = {
    ...GOOD,
    "NewChatView-EEEE.js": GOOD["NewChatView-EEEE.js"] + ' stroke="#a16207"',
  };
  assert.match(
    check(pwaBuild(files, GOOD_SW)),
    /scene art.*NewChatView-EEEE\.js/
  );
});

test("pwa: a scene marker in the worn chunk is fine, the worn art draws some of the same shapes", () => {
  const files = { ...GOOD, "holiday-worn-BBBB.js": WORN + ' stroke="#a16207"' };
  assert.equal(check(pwaBuild(files, GOOD_SW)), "");
});

test("pwa: a service worker with no precache entries fails, it must not pass silently", () => {
  assert.match(
    check(pwaBuild(GOOD, "self.skipWaiting()")),
    /no precache entries/
  );
});

test("pwa: a missing service worker is reported, not thrown", () => {
  const dir = pwaBuild(GOOD, GOOD_SW);
  unlinkSync(path.join(dir, "sw.js"));
  assert.match(check(dir), /sw\.js not found/);
});

test("pwa: the worn markers are the desktop list's worn part", () => {
  assert.deepEqual(WORN_MARKERS, ART_MARKERS.slice(0, 2));
});
