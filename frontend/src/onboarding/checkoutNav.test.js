// The external-checkout return decision. Pure, so node --test runs it with no DOM.

import test from "node:test";
import assert from "node:assert/strict";

import { shouldHonorCheckoutReturn } from "./checkoutNav.js";

test("no marker, no return", () => {
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "", inCheckout: true, attemptId: "a1" }),
		false
	);
});

test("a marker outside a checkout state is never honoured", () => {
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "a1", inCheckout: false, attemptId: "a1" }),
		false
	);
});

test("a matching marker on a live sheet is honoured", () => {
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "a1", inCheckout: true, attemptId: "a1" }),
		true
	);
});

// The reviewer's Probe-B: a marker left by attempt N must NOT drive a return
// during attempt N+1's live in-page sheet - that would drop a real confirm.
test("X3 Probe-B: a stale marker from a previous attempt is refused against a newer sheet", () => {
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "attN", inCheckout: true, attemptId: "attN1" }),
		false
	);
});

test("a marker is allowed when the current attempt id is unknown (all we have to go on)", () => {
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "a1", inCheckout: true, attemptId: null }),
		true
	);
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "1", inCheckout: true, attemptId: "" }),
		true
	);
});
