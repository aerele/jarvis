// Guard for #699: 42 inline copies of the pre-#696 error formatter, spread
// across 38 files, never called the shared errMessage() fix from #696 and
// could still paint frappe-ui's raw internal-crash TypeError text straight at
// a customer. This test greps the whole src tree for the SAME shape the #699
// reproduction command used and fails the moment a new copy appears anywhere
// outside lib/errors.js itself.
//
// One exception is intentional and pinned by its exact source line below:
// SaveDashboardDialog.vue's isThemeError() reads the first entry of the
// server's message array to pattern-match its text for CONTROL FLOW (deciding
// whether to offer a "fix in chat" hand-off), not to display it - flattening
// that into errMessage() would be the exact mistake #699 warned against, so
// it is left alone on purpose.
//
// The second test here guards the OTHER half of the same lesson. errMessage()
// returns plain text: it decodes the entities Frappe escaped, which is right
// for a `{{ }}` sink and wrong for an HTML one. frappe-ui's Toast binds its
// `message` prop with v-html, so a message routed there has to go through
// errHtml() instead, or the decode hands the sink live markup that the server
// had already made safe. Both rules exist because the safe call is invisible
// at the call site - only a test makes the wrong one loud.
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

// A toast whose text comes from the plain-text formatter rather than the
// escaping one. Matched on a single line, which covers every shape in the tree
// today (the direct `toast.error(errMsg(e))` and the one template-literal
// case); a call split across lines would slip past, so this is a ratchet
// against regrowth, not a proof of absence.
const TOAST_CALL = /toast\.[a-zA-Z]+\(/;
const PLAIN_FORMATTER_CALL = /\b(errMessage|errMsg|_err)\s*\(/;

// file (relative to src/) -> the exact trimmed source lines allowed to carry
// the pattern. Pinned by TEXT, not line number, so an edit elsewhere in the
// file cannot turn this guard red for no reason.
const ALLOWED = new Map([
	[
		"pages/dashboards/SaveDashboardDialog.vue",
		new Set(['const m = (e.messages && e.messages[0]) || e.message || "";']),
	],
]);

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
			// This file quotes the banned shape twice on purpose: once in the
			// ALLOWED pin above and once in the regex. Skipping it keeps the
			// guard from matching its own definition.
			if (rel === "lib/noInlineErrorFormatter.test.js") return;
			const allowed = ALLOWED.get(rel);
			if (allowed && allowed.has(line.trim())) return; // pinned exception
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

test("no toast is fed by the plain-text formatter (Toast renders `message` via v-html)", () => {
	const offenders = [];

	for (const file of walk(SRC_DIR, [])) {
		const rel = path.relative(SRC_DIR, file).split(path.sep).join("/");
		if (rel === "lib/errors.js" || rel.endsWith(".test.js")) continue;
		readFileSync(file, "utf8")
			.split("\n")
			.forEach((line, i) => {
				if (!TOAST_CALL.test(line)) return;
				if (!PLAIN_FORMATTER_CALL.test(line)) return;
				offenders.push(`${rel}:${i + 1}: ${line.trim()}`);
			});
	}

	assert.deepEqual(
		offenders,
		[],
		"A toast is being given text from errMessage(), which DECODES the entities " +
			"Frappe escaped. frappe-ui's Toast binds `message` with v-html, so that " +
			"decoded text is re-parsed as markup and the server's escaping is undone. " +
			'Use errHtml() from "@/lib/errors" for toasts; errMessage() is for text ' +
			"sinks only (jarvis#699)."
	);
});
