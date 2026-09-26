import { test } from "node:test";
import assert from "node:assert/strict";
import { comparePendingCards, sortPendingCards } from "./sortPendingCards.js";

// Real behavioural tests for the order a typed "confirm N" indexes into. A
// source grep of the comparator text passes even when the client drops
// created_at/expires_at (how the Desk-widget wrong-write bug shipped), so pin
// behaviour.

test("orders by created_at ascending, earliest-minted is number 1", () => {
	const out = sortPendingCards([
		{ token: "z", created_at: 200 },
		{ token: "a", created_at: 100 },
	]);
	assert.deepEqual(
		out.map((c) => c.token),
		["a", "z"]
	);
});

test("tie-breaks equal created_at by token in code-unit order, matching the server", () => {
	// 'A' (0x41) < 'z' (0x7A) by code unit; a locale compare would disagree.
	const out = sortPendingCards([
		{ token: "z9", created_at: 100 },
		{ token: "A0", created_at: 100 },
	]);
	assert.deepEqual(
		out.map((c) => c.token),
		["A0", "z9"]
	);
});

test("created_at wins over a misleading expires_at (a later mint, earlier expiry)", () => {
	const out = sortPendingCards([
		{ token: "late", created_at: 200, expires_at: 150 },
		{ token: "early", created_at: 100, expires_at: 999 },
	]);
	assert.deepEqual(
		out.map((c) => c.token),
		["early", "late"]
	);
});

test("falls back to expires_at when created_at is missing (a mixed deploy)", () => {
	const out = sortPendingCards([
		{ token: "z", expires_at: 200 },
		{ token: "a", expires_at: 100 },
	]);
	assert.deepEqual(
		out.map((c) => c.token),
		["a", "z"]
	);
});

test("mixes a pre-P0c (expires_at only) card with a post-P0c (created_at) one correctly", () => {
	const out = sortPendingCards([
		{ token: "new", created_at: 200 },
		{ token: "old", expires_at: 100 },
	]);
	assert.deepEqual(
		out.map((c) => c.token),
		["old", "new"]
	);
});

test("treats both fields missing as 0 without throwing", () => {
	const out = sortPendingCards([{ token: "b", created_at: 5 }, { token: "a" }]);
	assert.deepEqual(
		out.map((c) => c.token),
		["a", "b"]
	);
});

test("does not mutate its input", () => {
	const input = [
		{ token: "z", created_at: 2 },
		{ token: "a", created_at: 1 },
	];
	sortPendingCards(input);
	assert.deepEqual(
		input.map((c) => c.token),
		["z", "a"]
	);
});

test("distinct tokens never tie (total order)", () => {
	assert.ok(
		comparePendingCards({ token: "a", created_at: 1 }, { token: "b", created_at: 1 }) < 0
	);
	assert.ok(
		comparePendingCards({ token: "b", created_at: 1 }, { token: "a", created_at: 1 }) > 0
	);
	assert.equal(
		comparePendingCards({ token: "a", created_at: 1 }, { token: "a", created_at: 1 }),
		0
	);
});
