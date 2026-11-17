// The PWA marks every same-origin /api/ request `X-Jarvis-Client: pwa`. The shim itself is the
// desktop SPA's shared copy (frontend/src/lib/clientHeader.js, covered by its vitest spec); what
// is the PWA's own is that it is installed as the app's FIRST import, as "pwa", from @shared.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const read = (rel) => fs.readFileSync(path.join(here, rel), "utf8");

test("main.js imports the client-header shim before anything else", () => {
	const first = read("../main.js")
		.split("\n")
		.find((line) => /^import /.test(line));
	assert.match(first, /\.\/lib\/clientHeaderPwa/);
});

test("the shim entry installs the shared copy as pwa", () => {
	const entry = read("./clientHeaderPwa.js");
	assert.match(entry, /from "@shared\/lib\/clientHeader"/);
	assert.match(entry, /install\("pwa"\)/);
});

test("the shared shim file the entry points at exists", () => {
	assert.ok(fs.existsSync(path.join(here, "../../../frontend/src/lib/clientHeader.js")));
});
