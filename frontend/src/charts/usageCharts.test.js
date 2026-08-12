import { test } from "node:test";
import assert from "node:assert/strict";
import { budgetGaugeOption, perModelBarSpec, formatUsd } from "./usageCharts.js";
import { buildOption } from "./chartTheme.js";

test("gauge: null when no positive limit", () => {
	assert.equal(budgetGaugeOption(5, 0), null);
	assert.equal(budgetGaugeOption(5, -1), null);
});
test("gauge: percent, caps at 100, red at >=90%", () => {
	const o = budgetGaugeOption(45, 100);
	assert.equal(o.series[0].type, "gauge");
	assert.equal(o.series[0].data[0].value, 45);
	const over = budgetGaugeOption(150, 100);
	assert.equal(over.series[0].data[0].value, 100);
	assert.equal(over.series[0].progress.itemStyle.color, "#fc8181");
});

// The real per-model row shape has no `tokens` / `cost` field - only
// tokens_in / tokens_out / cost_usd. A fixture in the old {model, tokens,
// cost} shape must still resolve every value to 0 (proves the old bug is
// gone, not silently reintroduced by a looser field read).
test("perModelBarSpec: tokens metric ignores a stale {tokens} fixture (no such field)", () => {
	const s = perModelBarSpec([{ model: "gpt-5.5", tokens: 10, cost: 0.2 }], "tokens");
	assert.equal(s.type, "bar");
	assert.deepEqual(s.x, ["gpt-5.5"]);
	assert.deepEqual(s.series[0].data, [0]);
	assert.deepEqual(s.series[1].data, [0]);
});

test("perModelBarSpec: tokens metric reads tokens_in/tokens_out and produces non-zero bars", () => {
	const rows = [
		{ model: "gpt-5.5", provider: "openai", tokens_in: 1200, tokens_out: 800, cost_usd: 0.42 },
		{
			model: "claude",
			provider: "anthropic",
			tokens_in: 300,
			tokens_out: 100,
			cost_usd: 0.05,
		},
	];
	const s = perModelBarSpec(rows, "tokens");
	assert.equal(s.type, "bar");
	assert.deepEqual(s.x, ["gpt-5.5", "claude"]);
	assert.equal(s.series.length, 2);
	assert.equal(s.series[0].name, "Tokens in");
	assert.equal(s.series[1].name, "Tokens out");
	assert.deepEqual(s.series[0].data, [1200, 300]);
	assert.deepEqual(s.series[1].data, [800, 100]);
	// stacked so a horizontal bar's length still reads as the model's total.
	assert.equal(s.options.stacked, true);
	assert.equal(s.options.horizontal, true);
	// every bar has a real (nonzero) value - the bug (all-0 from reading the
	// nonexistent `tokens` field) is fixed.
	assert.ok(s.series[0].data.every((v) => v > 0));
	assert.ok(s.series[1].data.every((v) => v > 0));
	assert.notEqual(buildOption(s), null);
});

test("perModelBarSpec: cost metric reads cost_usd", () => {
	const cost = perModelBarSpec(
		[{ model: "m", tokens_in: 10, tokens_out: 5, cost_usd: 0.2 }],
		"cost"
	);
	assert.equal(cost.series[0].name, "Cost ($)");
	assert.deepEqual(cost.series[0].data, [0.2]);
	assert.notEqual(buildOption(cost), null);
});

test("perModelBarSpec: cost metric returns null when every row's cost is 0 (BYO-key pools) instead of an empty axes-only chart", () => {
	const rows = [
		{ model: "m1", tokens_in: 10, tokens_out: 5, cost_usd: 0 },
		{ model: "m2", tokens_in: 4, tokens_out: 2 },
	];
	assert.equal(perModelBarSpec(rows, "cost"), null);
});

test("perModelBarSpec: empty/missing rows stay safe", () => {
	assert.deepEqual(perModelBarSpec(undefined).x, []);
	assert.deepEqual(perModelBarSpec([{ model: "m" }]).series[0].data, [0]);
});

test("formatUsd: zero and nullish coerce to $0.00", () => {
	assert.equal(formatUsd(0), "$0.00");
	assert.equal(formatUsd(null), "$0.00");
	assert.equal(formatUsd(undefined), "$0.00");
	assert.equal(formatUsd(NaN), "$0.00");
	assert.equal(formatUsd("abc"), "$0.00");
});
test("formatUsd: normal amounts render at 2dp with thousands separators", () => {
	assert.equal(formatUsd(0.42), "$0.42");
	assert.equal(formatUsd(1234.5), "$1,234.50");
	assert.equal(formatUsd(5), "$5.00");
});
test("formatUsd: sub-cent nonzero amounts get extra precision instead of reading as free", () => {
	assert.equal(formatUsd(0.003), "$0.0030");
	assert.equal(formatUsd(0.0099), "$0.0099");
});
