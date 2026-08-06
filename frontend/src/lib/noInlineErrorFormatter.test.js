// Guard for #699: 42 inline copies of the pre-#696 error formatter, spread
// across 38 files, never called the shared errMessage() fix from #696 and
// could still paint frappe-ui's raw internal-crash TypeError text straight at
// a customer. This test greps the whole src tree for the SAME shape the #699
// reproduction command used and fails the moment a new copy appears anywhere
// outside lib/errors.js itself.
//
// One exception is intentional and pinned to an exact file+line below:
// SaveDashboardDialog.vue's isThemeError() reads the first entry of the
// server's message array to pattern-match its text for CONTROL FLOW (deciding
// whether to offer a "fix in chat" hand-off), not to display it - flattening
// that into errMessage() would be the exact mistake #699 warned against, so
// it is left alone on purpose.
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SRC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// The reproduction command from jarvis#699, unchanged: a property read of
// `messages` immediately followed by an optional `?.` or `&&`, then a
// zero-index array access - regardless of what wraps it.
const BANNED_PATTERN = /\.messages\s*(\?\.)?\s*(&&)?\s*\[0\]/;

// file (relative to src/) -> allowed line numbers (1-based) for the pattern.
const ALLOWED = new Map([["pages/dashboards/SaveDashboardDialog.vue", new Set([122])]]);

function walk(dir, out) {
	for (const entry of readdirSync(dir)) {
		if (entry === "node_modules") continue;
		const full = path.join(dir, entry);
		const st = statSync(full);
		if (st.isDirectory()) {
			walk(full, out);
		} else if (/\.(js|vue)$/.test(entry)) {
			out.push(full);
		}
	}
	return out;
}

test("no inline duplicate of the pre-#696 error formatter exists outside lib/errors.js", () => {
	const files = walk(SRC_DIR, []);
	const offenders = [];

	for (const file of files) {
		const rel = path.relative(SRC_DIR, file).split(path.sep).join("/");
		const lines = readFileSync(file, "utf8").split("\n");
		lines.forEach((line, i) => {
			if (!BANNED_PATTERN.test(line)) return;
			if (rel === "lib/errors.js") return; // the canonical implementation
			const allowedLines = ALLOWED.get(rel);
			if (allowedLines && allowedLines.has(i + 1)) return; // pinned exception
			offenders.push(`${rel}:${i + 1}: ${line.trim()}`);
		});
	}

	assert.deepEqual(
		offenders,
		[],
		"Found a copy of the pre-#696 error formatter outside lib/errors.js. Import " +
			'errMessage from "@/lib/errors" instead of re-deriving a display message ' +
			"from the server's error shape inline (jarvis#699)."
	);
});
