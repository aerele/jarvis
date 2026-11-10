#!/usr/bin/env node
// F-Q8 differential oracle: what does the REAL OpenClaw directive parser say?
//
//   node scripts/fq8_differential.mjs <openclaw-dist-dir> <cases.json>
//
// <cases.json> is a JSON array of strings. The output (stdout) is a JSON array with one
// entry per string: true when the host's own `parseInlineSessionDirectives` finds a
// `/model` directive in that text (after the host's input sanitizer), false when not,
// null when the host would reject the text (U+0000). The output is {chunk, unicode, verdicts}.
//
// The parser is not importable from the package, so this script cuts it out of the
// pinned dist. It finds the code by the `//#region` markers, never by file name or line
// number (the hashed chunk name and the line numbers change at every pin bump), and it
// fails loudly (exit 2) when a region, an import or a probe result is missing.
// scripts/fq8_differential.py drives it and builds the committed fixture.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const START = "//#region src/auto-reply/reply/directive-parsing.ts";
const LAST = "//#region src/auto-reply/reply/directive-handling.parse.ts";
const END = "//#endregion";
// Every helper that the region calls and the chunk imports. Missing one is a hard error.
const REQUIRED = [
  "resolveTimerTimeoutMs", "finiteSecondsToTimerSafeMilliseconds", "resolveIntegerOption",
  "MAX_TIMER_TIMEOUT_MS", "asDateTimestampMs", "parseStrictPositiveInteger",
  "normalizeFastMode", "normalizeOptionalLowercaseString", "normalizeOptionalString",
  "normalizeLowercaseStringOrEmpty", "normalizeStringEntries", "normalizeAtHashSlug",
  "uniqueStrings", "escapeRegExp", "parseDurationMs", "normalizeExecTarget",
  "splitTrailingAuthProfile", "normalizeElevatedLevel", "normalizeTraceLevel",
  "isSessionDefaultDirectiveValue", "normalizeReasoningLevel", "normalizeThinkLevel",
  "normalizeVerboseLevel", "normalizeQueueMode", "normalizeQueueDropPolicy",
];

function fail(msg) {
  console.error(`fq8_differential: ${msg}`);
  process.exit(2);
}

function findChunk(dist) {
  if (!fs.existsSync(dist)) fail(`dist directory not found: ${dist}`);
  const hits = fs
    .readdirSync(dist)
    .filter((f) => f.endsWith(".mjs"))
    .map((f) => path.join(dist, f))
    .filter((f) => {
      const text = fs.readFileSync(f, "utf8");
      return text.includes(START) && text.includes(LAST);
    });
  if (hits.length !== 1) fail(`expected one chunk holding both regions, found ${hits.length}`);
  return hits[0];
}

function cutRegion(source) {
  const start = source.indexOf(START);
  const lastStart = source.indexOf(LAST);
  if (start < 0 || lastStart < 0 || lastStart < start) fail("region markers not found in order");
  const end = source.indexOf(END, lastStart);
  if (end < 0) fail("the end marker of the last region was not found");
  const region = source.slice(start, end + END.length);
  if (!/function parseInlineSessionDirectives\(/.test(region)) fail("parseInlineSessionDirectives not in the region");
  return region;
}

function importsFor(source, region, distDir) {
  const found = new Map(); // local name -> {imported, url}
  const importRe = /^import\s*\{([^}]*)\}\s*from\s*"(\.[^"]+)";?\s*$/gm;
  for (const m of source.matchAll(importRe)) {
    const url = pathToFileURL(path.resolve(distDir, m[2])).href;
    for (const spec of m[1].split(",")) {
      const parts = spec.trim().split(/\s+as\s+/);
      if (!parts[0]) continue;
      found.set(parts[parts.length - 1], { imported: parts[0], url });
    }
  }
  for (const name of REQUIRED) if (!found.has(name)) fail(`import not found in the chunk: ${name}`);
  const byUrl = new Map();
  for (const [local, { imported, url }] of found) {
    if (!new RegExp(`\\b${local}\\b`).test(region)) continue;
    if (!byUrl.has(url)) byUrl.set(url, []);
    byUrl.get(url).push(imported === local ? local : `${imported} as ${local}`);
  }
  return [...byUrl].map(([url, specs]) => `import { ${specs.join(", ")} } from ${JSON.stringify(url)};`).join("\n");
}

async function loadParser(distDir) {
  const chunk = findChunk(distDir);
  const source = fs.readFileSync(chunk, "utf8");
  const region = cutRegion(source);
  const imports = importsFor(source, region, distDir);
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "fq8-host-"));
  const file = path.join(tmp, "host.mjs");
  fs.writeFileSync(file, `${imports}\n${region}\nexport { parseInlineSessionDirectives };\n`);
  const mod = await import(pathToFileURL(file).href);
  fs.rmSync(tmp, { recursive: true, force: true });
  return { parse: mod.parseInlineSessionDirectives, chunk: path.basename(chunk) };
}

// The host's input sanitizer (`sanitizeChatSendMessageInput`): NFC, CR to LF, controls removed.
function hostSanitize(raw) {
  const text = raw.normalize("NFC").replace(/\r\n?/g, "\n");
  if (text.includes("\x00")) return null;
  return text.replace(/[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "");
}

// Known answers. If a pin bump changes the parser, these fail before any row is trusted.
function probe(parse) {
  const run = (t) => parse(t, { modelAliases: [], allowStatusDirective: true });
  const expect = (label, ok) => ok || fail(`probe failed: ${label}`);
  const model = run("/model openai/x --runtime codex");
  expect("model + runtime", model.hasModelDirective && model.rawModelRuntime === "codex");
  expect("glued /think:/model", run("/think:/model openai/x").hasModelDirective);
  expect("glued /status:/model", run("/status:/model openai/x").hasModelDirective);
  expect("feff before /model", run("\ufeff/model x").hasModelDirective);
  expect("/models is no directive", !run("/models").hasModelDirective);
  expect("path is no directive", !run("see /private/files/x.csv").hasModelDirective);
  expect("think", run("/think high hello").hasThinkDirective);
  expect("exec host", run("/exec host=gateway").hasExecDirective);
  expect("queue", run("/queue debounce:2s cap:5").hasQueueDirective);
  expect("model profile", run("/model a/b@prof").rawModelProfile === "prof");
  expect("aliases", parse("/fastx", { modelAliases: ["fastx"], allowStatusDirective: true }).hasModelDirective === true);
}

const [dist, casesFile] = process.argv.slice(2);
if (!dist || !casesFile) fail("usage: fq8_differential.mjs <dist-dir> <cases.json>");
const { parse, chunk } = await loadParser(path.resolve(dist));
probe(parse);
const cases = JSON.parse(fs.readFileSync(casesFile, "utf8"));
if (!Array.isArray(cases)) fail("cases.json must hold an array of strings");
const verdicts = cases.map((raw) => {
  const text = hostSanitize(raw);
  if (text === null) return null;
  return parse(text, { modelAliases: [], allowStatusDirective: true }).hasModelDirective === true;
});
process.stdout.write(JSON.stringify({ chunk, unicode: process.versions.unicode, verdicts }));
