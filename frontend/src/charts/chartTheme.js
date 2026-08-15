// Safe high-level chart spec -> themed ECharts option. Mirrors Frappe Insights'
// look (palette, dashed gridlines, no ticks, rounded bars, smooth/gradient).
// PURE: no echarts import (gradients use plain colorStops objects, bubble sizing
// uses a plain function), so it is unit-testable under `node --test`. The chat
// owns the theme; the agent never sends raw ECharts options.
import { LIGHT_VARS, DARK_VARS } from "../theme.js";

const PALETTE = [
	"#2490ef",
	"#48bb74",
	"#f6ad55",
	"#fc8181",
	"#9f7aea",
	"#38b2ac",
	"#ed64a6",
	"#ecc94b",
	"#667eea",
	"#a0aec0",
];
const TYPES = new Set([
	"bar",
	"line",
	"area",
	"pie",
	"donut",
	"scatter",
	"bubble",
	"heatmap",
	"boxplot",
	"radar",
	"funnel",
	"gauge",
]);

const num = (v) => Number(v) || 0;

// The agent sometimes emits a "records" spec variant instead of the documented
// columnar one: {data: [{...row}, ...], x: "<field>", series: [{field, name}]}.
// It has exactly one correct interpretation, so pivot it into the columnar
// shape ({x: [labels], series: [{name, data: [...]}]}) rather than render an
// empty plot. Specs already in the columnar shape pass through untouched.
function normalizeSpec(spec) {
	if (!spec || typeof spec !== "object") return spec;
	const rows = Array.isArray(spec.data) ? spec.data : null;
	const series = Array.isArray(spec.series) ? spec.series : [];
	if (!rows || !rows.length || !series.some((s) => s && s.field && !Array.isArray(s.data)))
		return spec;
	const out = { ...spec };
	delete out.data;
	if (typeof spec.x === "string") {
		out.x = rows.map((r) => String(r && r[spec.x] != null ? r[spec.x] : ""));
	}
	out.series = series.map((s) => {
		if (!s || !s.field || Array.isArray(s.data)) return s;
		return { ...s, data: rows.map((r) => num(r && r[s.field])) };
	});
	return out;
}

export function buildOption(spec, dark = false) {
	spec = normalizeSpec(spec);
	if (!spec || typeof spec !== "object" || !TYPES.has(spec.type)) return null;
	const series = Array.isArray(spec.series) ? spec.series : [];
	if (!series.length) return null;
	// No series carries any data points -> nothing to draw. Return null so the
	// chat shows its "Couldn't render this chart." fallback instead of a
	// silently empty plot (title + legend over a blank box).
	if (!series.some((s) => Array.isArray(s && s.data) && s.data.length)) return null;
	const text = dark ? DARK_VARS["--text-2"] : LIGHT_VARS["--text-2"];
	const grid = dark ? DARK_VARS["--border"] : LIGHT_VARS["--border"];
	const title = spec.title
		? { text: String(spec.title), left: 8, top: 4, textStyle: { fontSize: 13, color: text } }
		: undefined;
	switch (spec.type) {
		case "pie":
		case "donut":
			return pieOption(spec, series, text, title);
		case "scatter":
			return scatterOption(spec, series, { text, grid, title }, false);
		case "bubble":
			return scatterOption(spec, series, { text, grid, title }, true);
		case "heatmap":
			return heatmapOption(spec, series, { text, grid, title, dark });
		case "boxplot":
			return boxplotOption(spec, series, { text, grid, title });
		case "radar":
			return radarOption(spec, series, { text, grid, title });
		case "funnel":
			return funnelOption(spec, series, { text, title });
		case "gauge":
			return gaugeOption(spec, series, { text, title });
		default:
			return axisOption(spec, series, { text, grid, title });
	}
}

function axisOption(spec, series, { text, grid, title }) {
	const x = Array.isArray(spec.x) ? spec.x.map(String) : [];
	const opts = spec.options || {};
	const cat = {
		type: "category",
		data: x,
		axisTick: { show: false },
		axisLine: { lineStyle: { color: grid } },
		axisLabel: { color: text, hideOverlap: true },
	};
	const val = {
		type: "value",
		axisLabel: { color: text },
		splitLine: { lineStyle: { type: "dashed", color: grid } },
	};
	return {
		backgroundColor: "transparent",
		color: PALETTE,
		title,
		grid: { left: 8, right: 16, top: title ? 36 : 20, bottom: 8, containLabel: true },
		tooltip: { trigger: "axis", confine: true },
		legend:
			series.length > 1
				? { type: "scroll", top: title ? 4 : 0, right: 8, textStyle: { color: text } }
				: undefined,
		xAxis: opts.horizontal ? val : cat,
		yAxis: opts.horizontal ? cat : val,
		series: series.map((s, i) => makeSeries(spec, s, i)),
	};
}

function makeSeries(spec, s, i) {
	const data = Array.isArray(s.data) ? s.data : [];
	const color = PALETTE[i % PALETTE.length];
	const opts = spec.options || {};
	const stack = opts.stacked ? "total" : undefined;
	if (spec.type === "bar") {
		const r = opts.horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0];
		return { name: s.name, type: "bar", data, stack, itemStyle: { borderRadius: r } };
	}
	const out = {
		name: s.name,
		type: "line",
		data,
		stack,
		smooth: opts.smooth ? 0.4 : false,
		smoothMonotone: "x",
		showSymbol: false,
		lineStyle: { width: 2 },
		itemStyle: { color },
	};
	if (spec.type === "area") {
		out.areaStyle = {
			opacity: 0.85,
			color: {
				type: "linear",
				x: 0,
				y: 0,
				x2: 0,
				y2: 1,
				colorStops: [
					{ offset: 0, color },
					{ offset: 1, color: "rgba(255,255,255,0)" },
				],
			},
		};
	}
	return out;
}

function pieOption(spec, series, text, title) {
	const labels = Array.isArray(spec.x) ? spec.x.map(String) : [];
	const d0 = Array.isArray(series[0] && series[0].data) ? series[0].data : [];
	const data = d0.map((v, i) => ({
		name: labels[i] != null ? labels[i] : `#${i + 1}`,
		value: num(v),
	}));
	return {
		backgroundColor: "transparent",
		color: PALETTE,
		title,
		tooltip: { trigger: "item", confine: true },
		legend: { type: "scroll", bottom: 0, textStyle: { color: text } },
		series: [
			{
				type: "pie",
				radius: spec.type === "donut" ? ["45%", "70%"] : "65%",
				center: ["50%", title ? "54%" : "48%"],
				data,
				label: { color: text },
				itemStyle: {
					borderRadius: 4,
					borderColor: text === "#333333" ? "#fff" : "#1a202c",
					borderWidth: 1,
				},
			},
		],
	};
}

// scatter: series data is [[x, y], ...]; bubble adds a 3rd magnitude value
// [[x, y, size], ...] mapped to the symbol radius. Both use value axes.
function scatterOption(spec, series, { text, grid, title }, bubble) {
	const opts = spec.options || {};
	const valAxis = (name) => ({
		type: "value",
		name: name || undefined,
		nameTextStyle: { color: text },
		axisLabel: { color: text },
		axisLine: { lineStyle: { color: grid } },
		splitLine: { lineStyle: { type: "dashed", color: grid } },
	});
	let maxSize = 1;
	if (bubble) {
		for (const s of series)
			for (const d of Array.isArray(s.data) ? s.data : [])
				maxSize = Math.max(maxSize, num(d && d[2]));
	}
	return {
		backgroundColor: "transparent",
		color: PALETTE,
		title,
		grid: { left: 8, right: 16, top: title ? 36 : 20, bottom: 8, containLabel: true },
		tooltip: { trigger: "item", confine: true },
		legend:
			series.length > 1
				? { type: "scroll", top: title ? 4 : 0, right: 8, textStyle: { color: text } }
				: undefined,
		xAxis: valAxis(opts.xName),
		yAxis: valAxis(opts.yName),
		series: series.map((s, i) => ({
			name: s.name,
			type: "scatter",
			data: Array.isArray(s.data) ? s.data : [],
			symbolSize: bubble ? (d) => 8 + Math.sqrt(num(d && d[2]) / maxSize) * 38 : 10,
			itemStyle: { color: PALETTE[i % PALETTE.length], opacity: 0.75 },
		})),
	};
}

// heatmap: category x + category y grids; series[0].data is [[xIndex, yIndex,
// value], ...]. A visualMap colours cells by value.
function heatmapOption(spec, series, { text, grid, title, dark }) {
	const xs = Array.isArray(spec.x) ? spec.x.map(String) : [];
	const ys = Array.isArray(spec.y) ? spec.y.map(String) : [];
	const data = Array.isArray(series[0] && series[0].data) ? series[0].data : [];
	let min = Infinity,
		max = -Infinity;
	for (const d of data) {
		const v = num(d && d[2]);
		if (v < min) min = v;
		if (v > max) max = v;
	}
	if (!isFinite(min)) {
		min = 0;
		max = 1;
	}
	return {
		backgroundColor: "transparent",
		title,
		tooltip: { position: "top", confine: true },
		grid: { left: 8, right: 16, top: title ? 36 : 20, bottom: 56, containLabel: true },
		xAxis: {
			type: "category",
			data: xs,
			splitArea: { show: true },
			axisLabel: { color: text, hideOverlap: true },
			axisLine: { lineStyle: { color: grid } },
		},
		yAxis: {
			type: "category",
			data: ys,
			splitArea: { show: true },
			axisLabel: { color: text },
			axisLine: { lineStyle: { color: grid } },
		},
		visualMap: {
			min,
			max,
			calculable: true,
			orient: "horizontal",
			left: "center",
			bottom: 0,
			textStyle: { color: text },
			inRange: {
				color: dark
					? ["#1e3a5f", "#2490ef", "#63b3ed"]
					: ["#e6f0fb", "#2490ef", "#1a56a0"],
			},
		},
		series: [
			{
				type: "heatmap",
				data,
				label: { show: false },
				emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" } },
			},
		],
	};
}

// boxplot: category x; each series data item is [min, q1, median, q3, max].
function boxplotOption(spec, series, { text, grid, title }) {
	const x = Array.isArray(spec.x) ? spec.x.map(String) : [];
	return {
		backgroundColor: "transparent",
		color: PALETTE,
		title,
		grid: { left: 8, right: 16, top: title ? 36 : 20, bottom: 8, containLabel: true },
		tooltip: { trigger: "item", confine: true },
		legend:
			series.length > 1
				? { type: "scroll", top: title ? 4 : 0, right: 8, textStyle: { color: text } }
				: undefined,
		xAxis: {
			type: "category",
			data: x,
			axisTick: { show: false },
			axisLine: { lineStyle: { color: grid } },
			axisLabel: { color: text, hideOverlap: true },
		},
		yAxis: {
			type: "value",
			axisLabel: { color: text },
			splitLine: { lineStyle: { type: "dashed", color: grid } },
		},
		series: series.map((s, i) => ({
			name: s.name,
			type: "boxplot",
			data: Array.isArray(s.data) ? s.data : [],
			itemStyle: {
				color: PALETTE[i % PALETTE.length] + "33",
				borderColor: PALETTE[i % PALETTE.length],
			},
		})),
	};
}

// radar: spec.x names the spokes; each series data is one value per spoke. The
// per-spoke max is 1.1x the largest value seen there so shapes fill the web.
function radarOption(spec, series, { text, grid, title }) {
	const names = Array.isArray(spec.x) ? spec.x.map(String) : [];
	const maxes = names.map((_, j) => {
		let m = 0;
		for (const s of series) m = Math.max(m, num((s.data || [])[j]));
		return m > 0 ? m * 1.1 : 1;
	});
	return {
		backgroundColor: "transparent",
		color: PALETTE,
		title,
		tooltip: { trigger: "item", confine: true },
		legend:
			series.length > 1
				? { type: "scroll", bottom: 0, textStyle: { color: text } }
				: undefined,
		radar: {
			indicator: names.map((name, j) => ({ name, max: maxes[j] })),
			axisName: { color: text },
			splitLine: { lineStyle: { color: grid } },
			splitArea: { show: false },
			axisLine: { lineStyle: { color: grid } },
		},
		series: [
			{
				type: "radar",
				data: series.map((s, i) => ({
					name: s.name,
					value: (Array.isArray(s.data) ? s.data : []).map(num),
					areaStyle: { opacity: 0.1 },
					lineStyle: { color: PALETTE[i % PALETTE.length] },
					itemStyle: { color: PALETTE[i % PALETTE.length] },
				})),
			},
		],
	};
}

// funnel: spec.x names the stages; series[0].data is the value per stage.
function funnelOption(spec, series, { text, title }) {
	const labels = Array.isArray(spec.x) ? spec.x.map(String) : [];
	const d0 = Array.isArray(series[0] && series[0].data) ? series[0].data : [];
	const data = d0.map((v, i) => ({
		name: labels[i] != null ? labels[i] : `#${i + 1}`,
		value: num(v),
	}));
	return {
		backgroundColor: "transparent",
		color: PALETTE,
		title,
		tooltip: { trigger: "item", confine: true },
		legend: { type: "scroll", bottom: 0, textStyle: { color: text } },
		series: [
			{
				type: "funnel",
				left: "10%",
				right: "10%",
				top: title ? 40 : 24,
				bottom: 28,
				minSize: "0%",
				maxSize: "100%",
				sort: "descending",
				gap: 2,
				label: { show: true, position: "inside", color: "#fff" },
				data,
			},
		],
	};
}

// gauge: a single value (series[0].data[0]) against a max (options.max, else
// 1.25x the value).
function gaugeOption(spec, series, { text, title }) {
	const opts = spec.options || {};
	const d0 = Array.isArray(series[0] && series[0].data) ? series[0].data : [];
	const value = num(d0[0]);
	const max = num(opts.max) || (value > 0 ? Math.ceil(value * 1.25) : 100);
	return {
		backgroundColor: "transparent",
		color: PALETTE,
		title,
		series: [
			{
				type: "gauge",
				min: 0,
				max,
				progress: { show: true, width: 12 },
				axisLine: { lineStyle: { width: 12 } },
				axisTick: { show: false },
				splitLine: { length: 10, lineStyle: { color: "auto" } },
				axisLabel: { color: text, distance: 14 },
				pointer: { width: 5 },
				detail: {
					valueAnimation: true,
					color: text,
					fontSize: 22,
					offsetCenter: [0, "70%"],
				},
				title: { color: text, offsetCenter: [0, "92%"] },
				data: [{ value, name: (series[0] && series[0].name) || "" }],
			},
		],
	};
}
