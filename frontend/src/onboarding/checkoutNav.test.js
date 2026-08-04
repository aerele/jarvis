// The external-checkout return marker (X3): a marker is honoured ONLY when it can
// be positively matched to the live sheet's attempt. Pure, node --test.

import test from "node:test";
import assert from "node:assert/strict";

import { shouldHonorCheckoutReturn } from "./checkoutNav.js";

test("no marker is never honoured", () => {
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "", inCheckout: true, attemptId: "att_1" }),
		false
	);
});

test("a marker outside a checkout state is not a return", () => {
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "att_1", inCheckout: false, attemptId: "att_1" }),
		false
	);
});

test("a marker matching the live attempt is honoured", () => {
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "att_1", inCheckout: true, attemptId: "att_1" }),
		true
	);
});

test("a marker for a DIFFERENT attempt is a stale leftover, never honoured", () => {
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "att_1", inCheckout: true, attemptId: "att_2" }),
		false
	);
});

test("X3: a null live attemptId cannot verify the marker, so it is NOT honoured", () => {
	// The residual hole: the old code honoured a marker whenever the live attemptId
	// was null/"" ("all we have to go on"), which let a stale marker drive a
	// returnFromCheckout that dropped a live confirm. Fail closed on an unverifiable
	// attempt.
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "att_1", inCheckout: true, attemptId: null }),
		false
	);
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "att_1", inCheckout: true, attemptId: "" }),
		false
	);
	// The bare "1" fallback the writer stamps when no attempt is known is equally
	// unverifiable against an unknown live attempt.
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "1", inCheckout: true, attemptId: null }),
		false
	);
});

test("attempt ids compare as strings (a numeric live id still matches its stamped marker)", () => {
	assert.equal(
		shouldHonorCheckoutReturn({ marker: "42", inCheckout: true, attemptId: 42 }),
		true
	);
});
