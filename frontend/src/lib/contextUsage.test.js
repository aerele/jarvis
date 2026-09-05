import { test } from "node:test";
import assert from "node:assert/strict";
import { compactTokenCount, contextUsageView } from "./contextUsage.js";

test("unknown context capacity renders an honest unavailable pill", () => {
	assert.deepEqual(contextUsageView({ used: 1200, capacity: 0, pct: 0 }), {
		label: "Context —",
		title: "Context usage is available after the first completed response.",
		tone: "neutral",
	});
});

test("context percentage uses warning and critical production thresholds", () => {
	assert.equal(contextUsageView({ used: 100, capacity: 1000, pct: 10 }).tone, "neutral");
	assert.equal(contextUsageView({ used: 760, capacity: 1000, pct: 76 }).tone, "warning");
	assert.equal(contextUsageView({ used: 950, capacity: 1000, pct: 95 }).tone, "critical");
});

test("context labels are compact and can derive a missing percentage", () => {
	assert.deepEqual(contextUsageView({ used: 125000, capacity: 200000 }), {
		label: "Context 63%",
		title: "125k of 200k tokens used",
		tone: "neutral",
	});
	assert.equal(compactTokenCount(1250), "1.3k");
	assert.equal(compactTokenCount(2_000_000), "2m");
});
