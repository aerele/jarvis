// Pure builders for the LLM Monitor echarts (plain option objects; node:test-able).
import { LIGHT_VARS, DARK_VARS } from "../theme.js";
const GREEN = "#48bb74",
	AMBER = "#f6ad55",
	RED = "#fc8181";

export function budgetGaugeOption(used, limit, dark = false) {
	const lim = Number(limit) || 0;
	if (lim <= 0) return null;
	const val = Math.max(0, Number(used) || 0);
	const pct = Math.min(100, Math.round((val / lim) * 100));
	const text = dark ? DARK_VARS["--text-2"] : LIGHT_VARS["--text-2"];
	const track = dark ? DARK_VARS["--surface-2"] : LIGHT_VARS["--border"];
	const color = pct >= 90 ? RED : pct >= 70 ? AMBER : GREEN;
	return {
		series: [
			{
				type: "gauge",
				startAngle: 210,
				endAngle: -30,
				min: 0,
				max: 100,
				progress: { show: true, width: 14, itemStyle: { color } },
				axisLine: { lineStyle: { width: 14, color: [[1, track]] } },
				axisTick: { show: false },
				splitLine: { show: false },
				axisLabel: { show: false },
				pointer: { show: false },
				anchor: { show: false },
				detail: {
					valueFormatter: (v) => `${v}%`,
					color: text,
					fontSize: 22,
					offsetCenter: [0, "10%"],
				},
				data: [{ value: pct }],
			},
		],
	};
}

// Real per-model rows (jarvis.account.get_llm_usage -> fleet-agent payload)
// are {model, provider, tokens_in, tokens_out, cost_usd} - there is no flat
// `tokens` or `cost` field. metric="tokens" (default) splits each model into
// its in/out bars (stacked, so the bar length is still the model's total)
// rather than collapsing to one number, which also gives the axis-trigger
// tooltip (chartTheme.js) real "Tokens in" / "Tokens out" values to show
// instead of a single hardcoded "Tokens" series. metric="cost" reads
// cost_usd for a per-model spend view.
export function perModelBarSpec(perModel, metric = "tokens") {
	const rows = Array.isArray(perModel) ? perModel : [];
	const x = rows.map((r) => String((r && r.model) || ""));
	if (metric === "cost") {
		const data = rows.map((r) => Number(r && r.cost_usd) || 0);
		// All-zero cost is real (BYO-key pools may not compute per-model spend) but
		// would otherwise render the same "axes with no bars" pathology this file
		// exists to fix - chartTheme only nulls out a chart when a series has no
		// data POINTS, and [0, 0, ...] still has points. Skip the chart instead.
		if (!data.some((v) => v > 0)) return null;
		return {
			type: "bar",
			x,
			series: [{ name: "Cost ($)", data }],
			options: { horizontal: true },
		};
	}
	return {
		type: "bar",
		x,
		series: [
			{ name: "Tokens in", data: rows.map((r) => Number(r && r.tokens_in) || 0) },
			{ name: "Tokens out", data: rows.map((r) => Number(r && r.tokens_out) || 0) },
		],
		options: { horizontal: true, stacked: true },
	};
}

// Shared USD formatter for the Cost tile (and any per-model cost readout).
// null/undefined/NaN and 0 all render as "$0.00" rather than "$NaN" or a bare
// "$". Sub-cent spend would otherwise round to the same "$0.00" as no spend
// at all, so a genuinely nonzero-but-tiny amount gets extra precision instead
// of silently reading as free.
export function formatUsd(v) {
	const n = Number(v);
	if (!Number.isFinite(n)) return "$0.00";
	const decimals = n !== 0 && Math.abs(n) < 0.01 ? 4 : 2;
	return `$${n.toLocaleString("en-US", {
		minimumFractionDigits: decimals,
		maximumFractionDigits: decimals,
	})}`;
}
