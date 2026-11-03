// Post-build gate for the Desk bundles: the holiday art must exist only in
// jarvis_holiday_touch.bundle.*.js. holiday_bundle_guard.test.mjs proves it from the sources;
// this proves it from what `bench build` actually wrote, which no CI job builds.
//
// Run after `bench build --app jarvis`:
//   node jarvis/public/js/jarvis_chat/widget/holiday_built_bundles.mjs <bench>/sites/assets
// It reads assets.json, then every built file under assets/jarvis/dist/js, and fails (exit 1)
// when an art marker is outside the art bundle, or when the art bundle or the widget bundle is
// missing (a gate that finds nothing to check has not checked anything).
import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ART_MARKERS } from "../../../../../frontend/scripts/check-holiday-chunks.mjs";

const ART_ENTRY = "jarvis_holiday_touch.bundle.js";
const WIDGET_ENTRY = "jarvis_widget.bundle.js";
const DIST = "jarvis/dist/js";

// The built file an assets.json value points at, inside the assets dir.
function builtPath(assetsDir, url) {
  return path.join(assetsDir, String(url).replace(/^\/?assets\//, ""));
}

function readManifest(assetsDir) {
  try {
    return JSON.parse(
      readFileSync(path.join(assetsDir, "assets.json"), "utf8")
    );
  } catch (error) {
    return null;
  }
}

/** Problems with the built Desk bundles in `assetsDir` (sites/assets); empty means fine. */
export function checkBuiltDeskBundles(assetsDir) {
  const manifest = readManifest(assetsDir);
  if (!manifest)
    return [`no readable assets.json in ${assetsDir}: run bench build first`];
  const problems = [];
  const art = manifest[ART_ENTRY] && builtPath(assetsDir, manifest[ART_ENTRY]);
  if (!art || !existsSync(art)) problems.push(`${ART_ENTRY} is not built`);
  else if (!readFileSync(art, "utf8").includes(ART_MARKERS[0]))
    problems.push(
      `${ART_ENTRY} lacks the worn art, so the check proves nothing`
    );
  const widget =
    manifest[WIDGET_ENTRY] && builtPath(assetsDir, manifest[WIDGET_ENTRY]);
  if (!widget || !existsSync(widget))
    problems.push(`${WIDGET_ENTRY} is not built`);
  const dist = path.join(assetsDir, DIST);
  const others = existsSync(dist)
    ? readdirSync(dist).filter(
        (f) => f.endsWith(".js") && builtPath(dist, f) !== art
      )
    : [];
  for (const file of others) {
    const body = readFileSync(path.join(dist, file), "utf8");
    for (const marker of ART_MARKERS.filter((m) => body.includes(m)))
      problems.push(
        `holiday art (${JSON.stringify(
          marker
        )}) is in ${file}, outside ${ART_ENTRY}`
      );
  }
  return problems;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const assetsDir = process.argv[2];
  if (!assetsDir) {
    console.error("usage: holiday_built_bundles.mjs <bench>/sites/assets");
    process.exit(2);
  }
  const problems = checkBuiltDeskBundles(path.resolve(assetsDir));
  for (const problem of problems) console.error(`holiday bundles: ${problem}`);
  if (problems.length) process.exit(1);
  console.log("holiday bundles: the art is only in the art bundle");
}
