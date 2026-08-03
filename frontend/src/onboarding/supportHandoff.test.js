// "I have checked five times and it still says pending" is the moment a human
// has to be offered. Nothing on the server counts a customer's status checks -
// deliberately: a check is a read, and a read that mutates a counter is a write
// nobody asked for - so the count is CLIENT-LOCAL, keyed to the attempt it is
// about, and a new intent starts it over.
//
// ON_HOLD mandates are the reason the ceiling exists at all: a bank can leave an
// e-NACH pending indefinitely by design, so "poll until it resolves" has no end
// and the page must eventually say so instead of spinning forever.

import test from "node:test";
import assert from "node:assert/strict";

import {
	SUPPORT_AFTER_CHECKS,
	emptyCounter,
	recordCheck,
	shouldOfferSupport,
} from "./supportHandoff.js";

test("a fresh counter offers nothing", () => {
	assert.equal(shouldOfferSupport(emptyCounter()), false);
});

test("checks accumulate against one attempt", () => {
	let c = emptyCounter();
	for (let i = 0; i < SUPPORT_AFTER_CHECKS; i++) {
		c = recordCheck(c, { attemptId: "att_1", generation: 1 });
	}
	assert.equal(c.checks, SUPPORT_AFTER_CHECKS);
	assert.equal(shouldOfferSupport(c), true);
});

test("one check short of the ceiling offers nothing", () => {
	let c = emptyCounter();
	for (let i = 0; i < SUPPORT_AFTER_CHECKS - 1; i++) {
		c = recordCheck(c, { attemptId: "att_1", generation: 1 });
	}
	assert.equal(shouldOfferSupport(c), false);
});

test("a DIFFERENT attempt starts the count over", () => {
	let c = emptyCounter();
	for (let i = 0; i < SUPPORT_AFTER_CHECKS; i++) {
		c = recordCheck(c, { attemptId: "att_1", generation: 1 });
	}
	c = recordCheck(c, { attemptId: "att_2", generation: 1 });
	assert.equal(c.checks, 1);
	assert.equal(shouldOfferSupport(c), false);
});

test("a NEW INTENT on the same attempt starts the count over", () => {
	// The generation advance IS the new intent. A customer who retried has a
	// live checkout in front of them again; carrying the old attempt's
	// frustration counter into it would offer support before they have even
	// tried to pay.
	let c = emptyCounter();
	for (let i = 0; i < SUPPORT_AFTER_CHECKS; i++) {
		c = recordCheck(c, { attemptId: "att_1", generation: 1 });
	}
	c = recordCheck(c, { attemptId: "att_1", generation: 2 });
	assert.equal(c.checks, 1);
	assert.equal(shouldOfferSupport(c), false);
});

test("an attempt with no id still counts, and still resets when the id appears", () => {
	// A legacy control plane sends no attempt_id. The counter must not collapse
	// every such answer into one bucket that never resets, nor refuse to count.
	let c = emptyCounter();
	c = recordCheck(c, {});
	c = recordCheck(c, {});
	assert.equal(c.checks, 2);
	c = recordCheck(c, { attemptId: "att_1" });
	assert.equal(c.checks, 1);
});

test("recordCheck never mutates the counter it was given", () => {
	const c = emptyCounter();
	const next = recordCheck(c, { attemptId: "att_1" });
	assert.equal(c.checks, 0);
	assert.equal(next.checks, 1);
	assert.notEqual(c, next);
});

test("the ceiling is small enough to reach in one sitting", () => {
	// A customer staring at "we have not confirmed this payment" will click
	// Check a handful of times, not fifty. A ceiling they never reach is the
	// same as no ceiling at all.
	assert.ok(SUPPORT_AFTER_CHECKS >= 2 && SUPPORT_AFTER_CHECKS <= 6);
});
