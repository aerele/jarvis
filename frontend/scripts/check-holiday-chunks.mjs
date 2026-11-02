// Build gate for the holiday art (the worn item and the welcome scenes).
// Run after `vite build`:  node scripts/check-holiday-chunks.mjs        (the desktop SPA)
//                          node scripts/check-holiday-chunks.mjs --pwa  (the phone PWA)
//
// The art (jarvis/public/js/shared/holiday/worn.mjs, stage.mjs and scenes/*.mjs) is lazy:
// it must live in `holiday-*` chunks and never in the main entry chunk or any other
// chunk. The words and the answer reader are tiny and must stay in main. If a refactor
// turns the dynamic import into a static one, the art silently lands in main for every
// user; this is the check that fails when that happens.
import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Strings only the art contains: the worn item (the Diwali flame path, a reaction
// keyframe), then one literal drawing string from each of the nine scenes (Diwali's diya clay
// path and toran thread, and the Christmas sleigh, New Year cannon bands, Birthday streamer,
// Eid lantern, Pongal kolam, Onam pookalam, Holi blur filter and Independence kite). Literal
// text, no interpolation, so the minifier leaves it whole.
export const ART_MARKERS = [
  "M60.5 26 C66 33 67 38.5",
  "@keyframes jvh-hop",
  "M-16 0 Q-15 12 0 13 Q11 13 15 3",
  'stroke="#a16207"',
  "M9 -17 L19 -11 L24 -13",
  "M-6 -18 H6 M-6 -9 H6",
  "M0 22 q-4 12 0 24 q4 12 0 24 q-3 8 0 16",
  "M-7 5 H7 L9 15 L5 24 H-5 L-9 15 Z",
  "M33.2 52 Q36.6 50 40 52 T47 52 T54.6 52",
  "M-24 -74 Q0 -64 24 -74 Q17 -82 0 -82 Q-17 -82 -24 -74 Z",
  '<feGaussianBlur stdDeviation="2"/>',
  "M0 -16 L12 0 L0 18 L-12 0 Z",
];
// The worn item's markers alone (the first two above): the phone has no scenes yet.
export const WORN_MARKERS = ART_MARKERS.slice(0, 2);
const SCENE_MARKERS = ART_MARKERS.slice(2);
// A string only the always-loaded words contain.
const WORDS_MARKER = "Wishing you a happy Diwali in advance!";

/**
 * Returns a list of problems; empty means the build is as intended.
 * `markers`: the art strings that must be in holiday-* chunks and nowhere else.
 * `wordsIn`: "entry" (the words are in the main entry chunk: the desktop) or "any" (in some
 * chunk that is not a holiday-* one: the PWA, where the words ride in the welcome view).
 */
export function checkHolidayChunks(
  assetsDir,
  indexHtml,
  { markers = ART_MARKERS, wordsIn = "entry" } = {}
) {
  const problems = [];
  const files = readdirSync(assetsDir).filter((f) => f.endsWith(".js"));
  const holiday = files.filter((f) => f.startsWith("holiday-"));
  const entryName = /assets\/(index-[A-Za-z0-9_-]+\.js)/.exec(indexHtml)?.[1];
  if (!entryName || !files.includes(entryName)) {
    return [
      `main entry chunk not found (looked for it in the built index.html)`,
    ];
  }
  if (holiday.length === 0) problems.push("no holiday-* chunk was built");
  // Each chunk is read once.
  const text = new Map(
    files.map((f) => [f, readFileSync(path.join(assetsDir, f), "utf8")])
  );
  for (const marker of markers) {
    for (const [file, body] of text) {
      if (body.includes(marker) && !file.startsWith("holiday-")) {
        problems.push(
          `worn art (${JSON.stringify(
            marker
          )}) is in ${file}, not only holiday-*`
        );
      }
    }
    const inHoliday = holiday.some((f) => text.get(f).includes(marker));
    if (holiday.length && !inHoliday)
      problems.push(`holiday-* chunks lack ${JSON.stringify(marker)}`);
  }
  const wordsHome =
    wordsIn === "entry"
      ? [entryName]
      : files.filter((f) => !f.startsWith("holiday-"));
  if (!wordsHome.some((f) => text.get(f).includes(WORDS_MARKER))) {
    problems.push(
      wordsIn === "entry"
        ? "the holiday words are missing from the main entry chunk"
        : "the holiday words are missing from the chunks outside holiday-*"
    );
  }
  return problems;
}

// The built service worker lists what it precaches as {"url":"...","revision":...}.
const PRECACHE_URL = /"url":"([^"]+)"/g;
// A static import in minified output: `from"./x.js"` or `import"./x.js"`. A dynamic
// `import("./x.js")` has a bracket and is not matched, which is the point: a lazy chunk
// may be loaded on demand, but a precached chunk must never need one to start.
const STATIC_IMPORT = /(?:\bfrom|\bimport)\s*["']\.\/([^"']+\.js)["']/g;

/**
 * The PWA build, in `distDir` (assets/ and the service worker sw.js): the art rules above
 * (worn art only in holiday-* chunks, no scene art, words present and outside holiday-*)
 * plus the precache rules: no holiday-* file is precached, and no precached chunk
 * statically imports a file that is not, or it would fail offline.
 */
export function checkPwaBuild(distDir, indexHtml) {
  const swFile = path.join(distDir, "sw.js");
  if (!existsSync(swFile)) return [`${swFile}: sw.js not found`];
  const assetsDir = path.join(distDir, "assets");
  const problems = checkHolidayChunks(assetsDir, indexHtml, {
    markers: WORN_MARKERS,
    wordsIn: "any",
  });
  const sw = readFileSync(swFile, "utf8");
  const precached = new Set(
    [...sw.matchAll(PRECACHE_URL)].map((m) => path.posix.basename(m[1]))
  );
  if (precached.size === 0) {
    // A gate that stops seeing the precache would pass for the wrong reason.
    problems.push("the service worker has no precache entries to check");
    return problems;
  }
  for (const name of precached) {
    if (name.startsWith("holiday-"))
      problems.push(`the precache lists ${name}; holiday art must stay lazy`);
  }
  for (const file of readdirSync(assetsDir).filter((f) => f.endsWith(".js"))) {
    const body = readFileSync(path.join(assetsDir, file), "utf8");
    // The phone has no scenes yet, so scene art outside a lazy chunk is a leak. (A scene
    // marker may sit in the worn chunk: the worn art draws some of the same shapes.)
    for (const marker of SCENE_MARKERS) {
      if (!file.startsWith("holiday-") && body.includes(marker))
        problems.push(`scene art (${JSON.stringify(marker)}) is in ${file}`);
    }
    if (!precached.has(file)) continue;
    for (const [, target] of body.matchAll(STATIC_IMPORT)) {
      if (!precached.has(path.posix.basename(target)))
        problems.push(
          `${file} is precached but statically imports ${target}, which is not`
        );
    }
  }
  return problems;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const pwa = process.argv.includes("--pwa");
  const root = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "../../jarvis"
  );
  const dist = path.join(root, pwa ? "public/pwa" : "public/frontend");
  const html = path.join(
    root,
    pwa ? "www/jarvis_mobile.html" : "www/jarvis.html"
  );
  if (!existsSync(path.join(dist, "assets")) || !existsSync(html)) {
    console.error(
      `check-holiday-chunks: run \`vite build\` first (no ${
        pwa ? "PWA" : "SPA"
      } build output found)`
    );
    process.exit(2);
  }
  const indexHtml = readFileSync(html, "utf8");
  const problems = pwa
    ? checkPwaBuild(dist, indexHtml)
    : checkHolidayChunks(path.join(dist, "assets"), indexHtml);
  for (const problem of problems)
    console.error(`check-holiday-chunks: ${problem}`);
  if (problems.length) process.exit(1);
  console.log(
    pwa
      ? "check-holiday-chunks: ok (PWA: worn art only in holiday-* chunks, none precached, precache closed under static imports)"
      : "check-holiday-chunks: ok (worn and scene art only in holiday-* chunks, words in main)"
  );
}
