// The payment telemetry sink: ordinary transitions are capped separately from
// the shared error budget; illegal transitions get their own larger (but still
// bounded) cap; both caps are per SESSION, not per mount. Pure, node --test.

import test from "node:test";
import assert from "node:assert/strict";

import {
	makeTelemetryReporter,
	TRANSITION_REPORT_CAP,
	ILLEGAL_REPORT_CAP,
	_resetTelemetryCapsForTest,
} from "./paymentTelemetry.js";

test("X6: ordinary transitions are capped, so they cannot exhaust the error budget", () => {
	_resetTelemetryCapsForTest();
	const calls = [];
	const sink = makeTelemetryReporter((ctx) => calls.push(ctx), { cap: 5 });
	for (let i = 0; i < 50; i++) sink({ event: "payment_transition", from: "a", to: "b" });
	assert.equal(calls.length, 5, "transitions beyond the cap are dropped");
});

test("X6: illegal transitions bypass the ordinary cap and are always reported", () => {
	_resetTelemetryCapsForTest();
	const calls = [];
	const sink = makeTelemetryReporter((ctx) => calls.push(ctx), { cap: 2 });
	// Burn the transition cap first.
	for (let i = 0; i < 10; i++) sink({ event: "payment_transition" });
	const before = calls.length;
	assert.equal(before, 2);
	// An illegal transition still reports, over the ordinary cap.
	sink({ event: "payment_illegal_transition", attempted: "x" });
	assert.equal(calls.length, before + 1);
	assert.equal(calls[calls.length - 1].error_code, "payment_illegal_transition");
});

test("X6: the illegal path is BOUNDED, not an unbounded bypass", () => {
	// Red-first for the X6 correction: an unbounded illegal bypass let a wedged
	// machine spend the entire shared error budget. With a real per-session illegal
	// cap, illegal reports stop at the cap too.
	_resetTelemetryCapsForTest();
	const calls = [];
	const sink = makeTelemetryReporter((ctx) => calls.push(ctx), { illegalCap: 3 });
	for (let i = 0; i < 100; i++) sink({ event: "payment_illegal_transition", attempted: "x" });
	assert.equal(calls.length, 3, "illegal transitions beyond their cap are dropped");
});

test("X6: the caps are per SESSION - a remount does NOT reset the budget", () => {
	// Red-first for the X6 correction: the cap was per-MOUNT (a per-closure counter),
	// so every remount spent the cap afresh and the "per session" promise was false.
	// A second reporter (a fresh mount) must SHARE the already-spent session budget.
	_resetTelemetryCapsForTest();
	const calls = [];
	const report = (ctx) => calls.push(ctx);
	const mount1 = makeTelemetryReporter(report, { cap: 3 });
	for (let i = 0; i < 3; i++) mount1({ event: "payment_transition" });
	assert.equal(calls.length, 3, "first mount fills the session cap");
	// A remount (new reporter, same session) must not get a fresh 3.
	const mount2 = makeTelemetryReporter(report, { cap: 3 });
	for (let i = 0; i < 3; i++) mount2({ event: "payment_transition" });
	assert.equal(calls.length, 3, "a remount shares the per-session cap, it does not reset it");
});

test("X6: a throwing report never escapes the sink", () => {
	_resetTelemetryCapsForTest();
	const sink = makeTelemetryReporter(() => {
		throw new Error("boom");
	});
	assert.doesNotThrow(() => sink({ event: "payment_transition" }));
});

test("X6: the caps are small/bounded positive numbers", () => {
	assert.ok(TRANSITION_REPORT_CAP > 0 && TRANSITION_REPORT_CAP < 100);
	assert.ok(ILLEGAL_REPORT_CAP > TRANSITION_REPORT_CAP && ILLEGAL_REPORT_CAP < 1000);
});
