// Build gate for the holiday art (the worn item and the welcome scenes).
// Run after `vite build`:  node scripts/check-holiday-chunks.mjs
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
// keyframe) and the Diwali scene (its diya clay path, the toran's thread colour).
export const ART_MARKERS = [
  "M60.5 26 C66 33 67 38.5",
  "@keyframes jvh-hop",
  "M-16 0 Q-15 12 0 13 Q11 13 15 3",
  'stroke="#a16207"',
];
// A string only the always-loaded words contain.
const WORDS_MARKER = "Wishing you a happy Diwali in advance!";

/** Returns a list of problems; empty means the build is as intended. */
export function checkHolidayChunks(assetsDir, indexHtml) {
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
  for (const marker of ART_MARKERS) {
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
  if (!text.get(entryName).includes(WORDS_MARKER)) {
    problems.push("the holiday words are missing from the main entry chunk");
  }
  return problems;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const root = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    "../../jarvis"
  );
  const assets = path.join(root, "public/frontend/assets");
  const html = path.join(root, "www/jarvis.html");
  if (!existsSync(assets) || !existsSync(html)) {
    console.error(
      "check-holiday-chunks: run `vite build` first (no build output found)"
    );
    process.exit(2);
  }
  const problems = checkHolidayChunks(assets, readFileSync(html, "utf8"));
  for (const problem of problems)
    console.error(`check-holiday-chunks: ${problem}`);
  if (problems.length) process.exit(1);
  console.log(
    "check-holiday-chunks: ok (worn and scene art only in holiday-* chunks, words in main)"
  );
}
