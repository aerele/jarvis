import { test } from "node:test";
import assert from "node:assert/strict";
import { comparePendingCards, sortPendingCards } from "./sortPendingCards.js";

// Real behavioural tests for the order a typed "confirm N" indexes into. A
// source grep of the comparator text passes even when the client drops
// expires_at (how the Desk-widget wrong-write bug shipped), so pin behaviour.

test("orders by expires_at ascending, earliest-minted is number 1", () => {
	const out = sortPendingCards([
		{ token: "z", expires_at: 200 },
		{ token: "a", expires_at: 100 },
	]);
	assert.deepEqual(
		out.map((c) => c.token),
		["a", "z"]
	);
});

test("tie-breaks equal expires_at by token in code-unit order, matching the server", () => {
	// 'A' (0x41) < 'z' (0x7A) by code unit; a locale compare would disagree.
	const out = sortPendingCards([
		{ token: "z9", expires_at: 100 },
		{ token: "A0", expires_at: 100 },
	]);
	assert.deepEqual(
		out.map((c) => c.token),
		["A0", "z9"]
	);
});

test("treats a missing expires_at as 0 without throwing", () => {
	const out = sortPendingCards([{ token: "b", expires_at: 5 }, { token: "a" }]);
	assert.deepEqual(
		out.map((c) => c.token),
		["a", "b"]
	);
});

test("does not mutate its input", () => {
	const input = [
		{ token: "z", expires_at: 2 },
		{ token: "a", expires_at: 1 },
	];
	sortPendingCards(input);
	assert.deepEqual(
		input.map((c) => c.token),
		["z", "a"]
	);
});

test("distinct tokens never tie (total order)", () => {
	assert.ok(comparePendingCards({ token: "a", expires_at: 1 }, { token: "b", expires_at: 1 }) < 0);
	assert.ok(comparePendingCards({ token: "b", expires_at: 1 }, { token: "a", expires_at: 1 }) > 0);
	assert.equal(comparePendingCards({ token: "a", expires_at: 1 }, { token: "a", expires_at: 1 }), 0);
});
