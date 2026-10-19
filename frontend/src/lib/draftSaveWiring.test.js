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
	assert.match(
		fn,
		/stampDraftModel\(model, \{ messageKey, card, editable: true, actionKey \}\)/
	);
});

test("the summary card is stamped, never editable", () => {
	const fn = body("ensureActionSummary");
	assert.match(fn, /const messageKey = actionFor\.value/);
	assert.match(
		fn,
		/stampDraftModel\(model, \{ messageKey, card, editable: false, actionKey: key \}\)/
	);
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

test("an open panel on the same draft is kept, not rebuilt from the agent's proposal", () => {
	const fn = body("openDraftPanel");
	const keep = fn.indexOf("openPanelFor(draftPanel.value, actionKey)");
	assert.ok(keep > 0, "openDraftPanel looks for the open panel first");
	assert.ok(keep < fn.indexOf("buildDraftModel(a)"), "before any rebuild");
	assert.match(fn, /if \(open\) \{[\s\S]*?return false;/);
});

test("the card's Confirm saves the open panel's values when it edits this draft", () => {
	const fn = body("confirmSummary");
	const panel = fn.indexOf("applyDraft(0, panel)");
	assert.match(fn, /openPanelFor\(draftPanel\.value, model\.actionKey\)/);
	assert.ok(panel > 0 && panel < fn.indexOf("applyDraft(0, model)"));
});

test("a re-sent proposal toasts 'updated' only when the panel was rebuilt", () => {
	assert.match(
		src,
		/\.then\(\(rebuilt\) => \{\s*if \(rebuilt && draftPanel\.value\) draftPanel\.value\.updatedToast = true;/
	);
});

test("the card's Confirm and Preview read the same model", () => {
	assert.match(
		src,
		/:disabled="\s*!cardModel \|\|\s*cardModel\.applying \|\|\s*convStreaming\s*"/
	);
	assert.match(src, /:model="cardModel"/);
});

test("a click outside the panel closes it only when nothing would be lost", () => {
	assert.match(src, /@click\.self="onOverlayBackdropClick\(dismissDraftPanel\)"/);
	const fn = body("dismissDraftPanel");
	assert.match(
		fn,
		/if \(draftIsDirty\(draftPanel\.value\)\) \{[\s\S]*?return;\s*\}\s*closeDraftPanel\(\);/
	);
	assert.match(body("openDraftPanel"), /markDraftBaseline\(model\);/);
});
