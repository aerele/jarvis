import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { PROPOSED_LABEL_AFTER_S, proposedLabel } from "./cardAge.js";
import { pendingActionsFromRows } from "./pendingResync.js";

const NOW = 1_800_000_000_000;
const ago = (s) => NOW / 1000 - s;

test("a card says how old it is only after an hour", () => {
	assert.equal(PROPOSED_LABEL_AFTER_S, 3600);
	assert.equal(proposedLabel(ago(3599), NOW), "");
	assert.equal(proposedLabel(ago(3600), NOW), "Proposed 1h ago");
	assert.equal(proposedLabel(ago(49 * 3600), NOW), "Proposed 2d ago");
});

test("no usable time means no label", () => {
	for (const bad of [null, undefined, 0, "", "soon"]) assert.equal(proposedLabel(bad, NOW), "");
	assert.equal(proposedLabel(ago(-7200), NOW), "");
});

test("ChatView never parses a row's site-timezone creation as a local time", () => {
	const src = fs.readFileSync(new URL("../views/ChatView.vue", import.meta.url), "utf8");
	assert.ok(src.includes("pendingActionsFromRows("));
	const [card] = pendingActionsFromRows(
		[
			{
				role: "tool",
				tool_status: "pending",
				tool_call_id: "time-token",
				pending_card: {},
				pending_card_safe: true,
				created_at: 1_700_000_001,
			},
		],
		"CONV-TIME",
	);
	assert.equal(card.created_at, 1_700_000_001);
});
