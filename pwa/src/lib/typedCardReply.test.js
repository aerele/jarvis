import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
	discardedTokens,
	isRecentCard,
	markCardsEarlier,
	typedApprovalHint,
} from "./typedCardReply.js";

test("the bare go-ahead hint is only for a card parked since the user last spoke", () => {
	assert.equal(typedApprovalHint([{ token: "a" }]), 'or type "go ahead"');
	assert.equal(typedApprovalHint([{ token: "a", recent: false }]), 'or type "confirm 1"');
	assert.equal(
		typedApprovalHint([{ token: "a" }, { token: "b" }]),
		'or type "confirm all", or "confirm 1 and 2"'
	);
	assert.equal(
		typedApprovalHint([{ token: "a", recent: false }, { token: "b" }]),
		'or type "confirm 1 and 2"'
	);
	assert.equal(typedApprovalHint([]), "");
});

test("discarded tokens come from typed_rejection, and older servers send none", () => {
	const res = { typed_rejection: { discarded: [{ token: "a" }], skipped: [{ token: "b" }] } };
	assert.deepEqual([...discardedTokens(res)], ["a"]);
	assert.equal(discardedTokens({ ok: true }).size, 0);
});

test("a send ages only its own conversation's cards", () => {
	const cards = [
		{ token: "a", conversation: "c1" },
		{ token: "b", conversation: "c2" },
	];
	markCardsEarlier(cards, "c1");
	assert.deepEqual(cards.map(isRecentCard), [false, true]);
});

test("switching conversation clears the older-cards note", () => {
	// No component harness here (see pumpFence.test.js), so the wiring is pinned
	// against the source: the note must not follow the user into another chat.
	const src = fs.readFileSync(
		path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "views", "ChatView.vue"),
		"utf8"
	);
	const at = src.indexOf("() => props.id,");
	assert.notEqual(at, -1);
	const watcher = src.slice(at, src.indexOf("\n);", at));
	assert.match(watcher, /olderCardsNote\.value = false;/);
});
