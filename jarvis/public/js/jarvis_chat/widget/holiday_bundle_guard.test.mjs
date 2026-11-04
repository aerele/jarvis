// The holiday art is lazy: it must never ride in Desk's always-loaded bundles. Two guards.
// 1. hooks.py does not list the art bundle in app_include_js.
// 2. Nothing the widget bundle can reach (walking every static and dynamic import, through
//    .vue script blocks too) is art. worn-room.mjs (text only) is allowed; the art is not.
// The walker takes a reader, so the self-test can plant a violation and watch it fire.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const JS_ROOT = fileURLToPath(new URL("../../", import.meta.url));
const HOOKS = fileURLToPath(new URL("../../../../hooks.py", import.meta.url));
const ENTRY = path.join(JS_ROOT, "jarvis_widget.bundle.js");
const ART_BUNDLE = "jarvis_holiday_touch.bundle.js";
const FORBIDDEN = [
  /(^|\/)worn\.mjs$/,
  /(^|\/)stage\.mjs$/,
  /(^|\/)scene-geometry\.mjs$/,
  /(^|\/)art\.mjs$/,
  /(^|\/)mini-fx\.mjs$/,
  /\/scenes\//,
  /jarvis_holiday_touch\.bundle\.js$/,
];
const EXTENSIONS = ["", ".mjs", ".js", ".vue"];
const IMPORT_RE =
  /(?:\bimport|\bexport)\s[^'";]*?\sfrom\s*["']([^"']+)["']|\bimport\s*["']([^"']+)["']|\bimport\(\s*["']([^"']+)["']\s*\)/g;

// The code of a file: all of a script module, the <script> blocks of a .vue file.
function codeOf(file, text) {
  if (!file.endsWith(".vue")) return text;
  return [...text.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
    .map((m) => m[1])
    .join("\n");
}

function relativeImports(file, text) {
  return [...codeOf(file, text).matchAll(IMPORT_RE)]
    .map((m) => m[1] || m[2] || m[3])
    .filter((spec) => spec.startsWith("."));
}

/** Every file reachable from `entry`, as paths. `read(path)` gives its text or null. */
export function reachable(entry, read) {
  const seen = new Set();
  const queue = [entry];
  while (queue.length) {
    const file = queue.pop();
    if (seen.has(file)) continue;
    const text = read(file);
    if (text === null) continue;
    seen.add(file);
    for (const spec of relativeImports(file, text)) {
      const base = path.resolve(path.dirname(file), spec);
      const found = EXTENSIONS.map((ext) => base + ext).find(
        (candidate) => read(candidate) !== null
      );
      if (found) queue.push(found);
    }
  }
  return [...seen];
}

const forbiddenIn = (files) =>
  files.filter((f) => FORBIDDEN.some((rule) => rule.test(f)));

function readDisk(file) {
  try {
    return readFileSync(file, "utf8");
  } catch (_) {
    return null; // missing, or a directory
  }
}

test("the art bundle is not in hooks.py app_include_js", () => {
  const hooks = readFileSync(HOOKS, "utf8");
  const block = hooks.match(/app_include_js\s*=\s*\[([\s\S]*?)\n\]/);
  assert.ok(block, "app_include_js list not found in hooks.py");
  assert.ok(block[1].includes("jarvis_widget.bundle.js"));
  assert.ok(!block[1].includes(ART_BUNDLE));
});

test("no file the widget bundle reaches is holiday art", () => {
  const files = reachable(ENTRY, readDisk);
  // The walk really walked: it got through the bundle into the widget and its helpers.
  assert.ok(files.some((f) => f.endsWith("jarvis_chat/widget/Widget.vue")));
  assert.ok(files.some((f) => f.endsWith("jarvis_chat/widget/Panel.vue")));
  assert.deepEqual(forbiddenIn(files), []);
});

// -- the guard's own test: plant a violation and prove the walk finds it -----------------

const fake = (files) => (file) => file in files ? files[file] : null;
const ROOT = "/js";

test("self-test: a static import of the worn art is found", () => {
  const read = fake({
    [`${ROOT}/entry.js`]: `import W from "./w/Widget.vue";`,
    [`${ROOT}/w/Widget.vue`]: `<template/><script>import { x } from "./util.mjs";</script>`,
    [`${ROOT}/w/util.mjs`]: `import { wornMarkup } from "../s/worn.mjs";`,
    [`${ROOT}/s/worn.mjs`]: `export const wornMarkup = 1;`,
  });
  assert.deepEqual(forbiddenIn(reachable(`${ROOT}/entry.js`, read)), [
    `${ROOT}/s/worn.mjs`,
  ]);
});

test("self-test: a re-export, a dynamic import and a scenes/ file are found", () => {
  const read = fake({
    [`${ROOT}/entry.js`]: `export { a } from "./a.mjs"; const b = () => import("./b.mjs");`,
    [`${ROOT}/a.mjs`]: `export * from "./geo/scene-geometry.mjs";`,
    [`${ROOT}/geo/scene-geometry.mjs`]: ``,
    [`${ROOT}/b.mjs`]: `import "./scenes/christmas.mjs";`,
    [`${ROOT}/scenes/christmas.mjs`]: ``,
  });
  assert.deepEqual(forbiddenIn(reachable(`${ROOT}/entry.js`, read)).sort(), [
    `${ROOT}/geo/scene-geometry.mjs`,
    `${ROOT}/scenes/christmas.mjs`,
  ]);
});

test("self-test: worn-room.mjs and a bare package import are allowed", () => {
  const read = fake({
    [`${ROOT}/entry.js`]: `import { createApp } from "vue"; import { WORN_OVERHANG } from "./worn-room.mjs";`,
    [`${ROOT}/worn-room.mjs`]: `export const WORN_OVERHANG = {};`,
  });
  const files = reachable(`${ROOT}/entry.js`, read);
  assert.equal(files.length, 2);
  assert.deepEqual(forbiddenIn(files), []);
});

test("self-test: the art bundle itself is found when imported", () => {
  const read = fake({
    [`${ROOT}/entry.js`]: `import "./jarvis_holiday_touch.bundle.js";`,
    [`${ROOT}/jarvis_holiday_touch.bundle.js`]: ``,
  });
  assert.equal(forbiddenIn(reachable(`${ROOT}/entry.js`, read)).length, 1);
});
