// The payment telemetry sink: ordinary transitions are capped separately from
// the shared error budget; illegal transitions always report. Pure, node --test.

import test from "node:test";
import assert from "node:assert/strict";

import { makeTelemetryReporter, TRANSITION_REPORT_CAP } from "./paymentTelemetry.js";

test("X6: ordinary transitions are capped, so they cannot exhaust the error budget", () => {
	const calls = [];
	const sink = makeTelemetryReporter((ctx) => calls.push(ctx), { cap: 5 });
	for (let i = 0; i < 50; i++) sink({ event: "payment_transition", from: "a", to: "b" });
	assert.equal(calls.length, 5, "transitions beyond the cap are dropped");
});

test("X6: illegal transitions bypass the cap and are always reported", () => {
	const calls = [];
	const sink = makeTelemetryReporter((ctx) => calls.push(ctx), { cap: 2 });
	// Burn the transition cap first.
	for (let i = 0; i < 10; i++) sink({ event: "payment_transition" });
	const before = calls.length;
	assert.equal(before, 2);
	// An illegal transition still reports, over the cap.
	sink({ event: "payment_illegal_transition", attempted: "x" });
	assert.equal(calls.length, before + 1);
	assert.equal(calls[calls.length - 1].error_code, "payment_illegal_transition");
});

test("X6: a throwing report never escapes the sink", () => {
	const sink = makeTelemetryReporter(() => {
		throw new Error("boom");
	});
	assert.doesNotThrow(() => sink({ event: "payment_transition" }));
});

test("X6: the default cap is a small positive number", () => {
	assert.ok(TRANSITION_REPORT_CAP > 0 && TRANSITION_REPORT_CAP < 100);
});
