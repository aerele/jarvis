// ChatView is too large to mount for one save path, so the rules live in draftSave.js
// (draftSave.test.js) and this pins that ChatView actually uses them: the panel is
// stamped editable, the summary card is not, and every save goes through the payload
// and the outcome helpers.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("../views/ChatView.vue", import.meta.url), "utf8");

function body(name) {
	const start = src.indexOf(`function ${name}(`);
	assert.ok(start > 0, `${name} not found`);
	const next = src.indexOf("\nfunction ", start + 1);
	const nextAsync = src.indexOf("\nasync function ", start + 1);
	const end = Math.min(...[next, nextAsync].filter((i) => i > 0));
	return src.slice(start, end);
}

test("the open panel is stamped editable with the draft it opened on", () => {
	const fn = body("openDraftPanel");
	assert.match(fn, /const messageKey = actionFor\.value/);
	assert.match(fn, /stampDraftModel\(model, \{ messageKey, card, editable: true \}\)/);
});

test("the summary card is stamped, never editable", () => {
	const fn = body("ensureActionSummary");
	assert.match(fn, /const messageKey = actionFor\.value/);
	assert.match(fn, /stampDraftModel\(model, \{ messageKey, card, editable: false \}\)/);
});

test("a save sends the helper's payload and follows its outcome, closing on closed", () => {
	const fn = body("applyDraft");
	assert.match(fn, /api\.applyAction\(\s*applyActionPayload\(p,/);
	assert.match(fn, /draftSaveOutcome\(r, brand\.agentName\)/);
	assert.match(
		fn,
		/outcome\.kind === "closed"\) \{\s*(\/\/[^\n]*\n\s*)*closeDraftPanel\(\);\s*notify\(outcome\.note/
	);
	assert.match(fn, /outcome\.kind === "keep"\) \{[\s\S]*?p\.error = outcome\.error;/);
});
