// Which one card shows under the last assistant message when a kept draft, a question
// card and a legacy confirm strip could all want the spot.
import { test } from "node:test";
import assert from "node:assert/strict";
import { tailCard } from "./tailCard.js";

const pick = (hasDraft, draftIsTail, tailHasAsk, tailHasConfirm) =>
	tailCard({ hasDraft, draftIsTail, tailHasAsk, tailHasConfirm });

test("a draft in the last message wins over its own ask and confirm", () => {
	assert.equal(pick(true, true, false, false), "draft");
	assert.equal(pick(true, true, true, false), "draft");
	assert.equal(pick(true, true, true, true), "draft");
});

test("a kept draft shows when the last message has no card of its own", () => {
	assert.equal(pick(true, false, false, false), "draft");
});

test("a kept draft is hidden while the last message has an ask or a confirm", () => {
	assert.equal(pick(true, false, true, false), "ask");
	assert.equal(pick(true, false, false, true), "confirm");
	assert.equal(pick(true, false, true, true), "confirm", "confirm is first in the thread");
});

test("without a draft the ask or confirm shows as before", () => {
	assert.equal(pick(false, false, true, false), "ask");
	assert.equal(pick(false, false, false, true), "confirm");
	assert.equal(pick(false, false, false, false), "none");
});
