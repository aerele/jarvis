import { test } from "node:test";
import assert from "node:assert/strict";
import { buildOption } from "./chartTheme.js";

test("rejects malformed / unknown specs -> null", () => {
	assert.equal(buildOption(null), null);
	assert.equal(buildOption({}), null);
	assert.equal(buildOption({ type: "pie3d" }), null);
	assert.equal(buildOption("nope"), null);
	assert.equal(buildOption({ type: "bar", series: [] }), null);
});

test("records-form spec is pivoted into the columnar shape", () => {
	// The exact shape the agent emitted live (2026-07-05): rows + field refs
	// instead of the documented x-labels + per-series data arrays.
	const o = buildOption({
		type: "bar",
		title: "Top-sold items",
		data: [
			{ item: "SKU005 Sneakers", sold: 150, actual: 32, gap: 118 },
			{ item: "SKU003 Book", sold: 100, actual: -100, gap: 200 },
		],
		x: "item",
		series: [
			{ field: "sold", name: "Units sold" },
			{ field: "actual", name: "Actual stock" },
			{ field: "gap", name: "Mfg gap" },
		],
	});
	assert.ok(o, "records-form spec must render, not null");
	assert.deepEqual(o.xAxis.data, ["SKU005 Sneakers", "SKU003 Book"]);
	assert.deepEqual(
		o.series.map((s) => s.name),
		["Units sold", "Actual stock", "Mfg gap"]
	);
	assert.deepEqual(o.series[0].data, [150, 100]);
	assert.deepEqual(o.series[1].data, [32, -100]); // negatives pass through
	assert.deepEqual(o.series[2].data, [118, 200]);
});

test("columnar spec with a stray data key is not re-pivoted", () => {
	const o = buildOption({
		type: "bar",
		data: [{ item: "A", v: 9 }],
		x: ["A", "B"],
		series: [{ name: "R", data: [1, 2] }],
	});
	assert.deepEqual(o.xAxis.data, ["A", "B"]);
	assert.deepEqual(o.series[0].data, [1, 2]);
});

test("spec whose series carry no data points -> null (chat shows fallback)", () => {
	assert.equal(buildOption({ type: "bar", x: ["A"], series: [{ name: "R" }] }), null);
	assert.equal(buildOption({ type: "bar", x: ["A"], series: [{ name: "R", data: [] }] }), null);
	// records form with an empty rows array stays undrawable -> null
	assert.equal(
		buildOption({ type: "bar", data: [], x: "item", series: [{ field: "v", name: "V" }] }),
		null
	);
});

test("bar: category x, rounded bars, palette, dashed value split", () => {
	const o = buildOption({ type: "bar", x: ["A", "B"], series: [{ name: "R", data: [1, 2] }] });
	assert.equal(o.xAxis.type, "category");
	assert.deepEqual(o.xAxis.data, ["A", "B"]);
	assert.equal(o.xAxis.axisTick.show, false);
	assert.equal(o.yAxis.splitLine.lineStyle.type, "dashed");
	assert.equal(o.series[0].type, "bar");
	assert.ok(Array.isArray(o.series[0].itemStyle.borderRadius));
	assert.ok(Array.isArray(o.color) && o.color.length > 0);
});

test("horizontal bar swaps axes", () => {
	const o = buildOption({
		type: "bar",
		x: ["A"],
		series: [{ data: [1] }],
		options: { horizontal: true },
	});
	assert.equal(o.yAxis.type, "category");
	assert.equal(o.xAxis.type, "value");
});

test("line + smooth + area gradient", () => {
	const o = buildOption({
		type: "area",
		x: ["A", "B"],
		series: [{ name: "R", data: [1, 2] }],
		options: { smooth: true },
	});
	assert.equal(o.series[0].type, "line");
	assert.equal(o.series[0].smooth, 0.4);
	assert.equal(o.series[0].areaStyle.color.type, "linear");
});

test("stacked sets a shared stack key on every series", () => {
	const o = buildOption({
		type: "bar",
		x: ["A"],
		series: [{ data: [1] }, { data: [2] }],
		options: { stacked: true },
	});
	assert.equal(o.series[0].stack, o.series[1].stack);
	assert.ok(o.series[0].stack);
});

test("pie maps x+series[0] to {name,value}; donut has inner radius", () => {
	const pie = buildOption({ type: "pie", x: ["A", "B"], series: [{ data: [3, 7] }] });
	assert.equal(pie.series[0].type, "pie");
	assert.deepEqual(pie.series[0].data, [
		{ name: "A", value: 3 },
		{ name: "B", value: 7 },
	]);
	const donut = buildOption({ type: "donut", x: ["A"], series: [{ data: [1] }] });
	assert.ok(Array.isArray(donut.series[0].radius));
});

test("legend only when >1 series; dark text differs from light", () => {
	const one = buildOption({ type: "bar", x: ["A"], series: [{ data: [1] }] });
	assert.equal(one.legend, undefined);
	const dark = buildOption({ type: "bar", x: ["A"], series: [{ data: [1] }], title: "t" }, true);
	const light = buildOption(
		{ type: "bar", x: ["A"], series: [{ data: [1] }], title: "t" },
		false
	);
	assert.notEqual(dark.xAxis.axisLabel.color, light.xAxis.axisLabel.color);
});

test("every chart type sets a transparent background (dark-mode safe)", () => {
	const specs = [
		{ type: "bar", x: ["A"], series: [{ data: [1] }] },
		{ type: "pie", x: ["A"], series: [{ data: [1] }] },
		{ type: "scatter", series: [{ data: [[1, 2]] }] },
		{ type: "heatmap", x: ["A"], y: ["B"], series: [{ data: [[0, 0, 5]] }] },
		{ type: "boxplot", x: ["A"], series: [{ data: [[1, 2, 3, 4, 5]] }] },
		{ type: "radar", x: ["A", "B"], series: [{ data: [1, 2] }] },
		{ type: "funnel", x: ["A"], series: [{ data: [1] }] },
		{ type: "gauge", series: [{ data: [42] }] },
	];
	for (const s of specs) assert.equal(buildOption(s).backgroundColor, "transparent", s.type);
});

test("scatter/bubble: value axes; bubble sizes the symbol from the 3rd value", () => {
	const sc = buildOption({
		type: "scatter",
		series: [
			{
				name: "S",
				data: [
					[1, 2],
					[3, 4],
				],
			},
		],
	});
	assert.equal(sc.xAxis.type, "value");
	assert.equal(sc.yAxis.type, "value");
	assert.equal(sc.series[0].type, "scatter");
	assert.equal(typeof sc.series[0].symbolSize, "number");
	const bub = buildOption({
		type: "bubble",
		series: [
			{
				data: [
					[1, 2, 10],
					[3, 4, 40],
				],
			},
		],
	});
	assert.equal(typeof bub.series[0].symbolSize, "function");
	assert.ok(bub.series[0].symbolSize([3, 4, 40]) > bub.series[0].symbolSize([1, 2, 10]));
});

test("heatmap: category x+y and a visualMap scaled to the data range", () => {
	const o = buildOption({
		type: "heatmap",
		x: ["Mon", "Tue"],
		y: ["AM", "PM"],
		series: [
			{
				data: [
					[0, 0, 3],
					[1, 1, 9],
				],
			},
		],
	});
	assert.equal(o.xAxis.type, "category");
	assert.equal(o.yAxis.type, "category");
	assert.equal(o.series[0].type, "heatmap");
	assert.equal(o.visualMap.min, 3);
	assert.equal(o.visualMap.max, 9);
});

test("boxplot: category x, five-number data passes through", () => {
	const o = buildOption({ type: "boxplot", x: ["A"], series: [{ data: [[1, 2, 3, 4, 5]] }] });
	assert.equal(o.series[0].type, "boxplot");
	assert.deepEqual(o.series[0].data, [[1, 2, 3, 4, 5]]);
});

test("radar: indicators from x with per-spoke max headroom", () => {
	const o = buildOption({
		type: "radar",
		x: ["Speed", "Cost"],
		series: [{ name: "P", data: [10, 20] }],
	});
	assert.equal(o.series[0].type, "radar");
	assert.equal(o.radar.indicator[0].name, "Speed");
	assert.ok(o.radar.indicator[1].max >= 20);
	assert.deepEqual(o.series[0].data[0].value, [10, 20]);
});

test("funnel maps x+series[0] to stages; gauge reads a single value + options.max", () => {
	const fn = buildOption({ type: "funnel", x: ["Lead", "Won"], series: [{ data: [100, 20] }] });
	assert.equal(fn.series[0].type, "funnel");
	assert.deepEqual(fn.series[0].data, [
		{ name: "Lead", value: 100 },
		{ name: "Won", value: 20 },
	]);
	const g = buildOption({
		type: "gauge",
		series: [{ name: "SLA", data: [87] }],
		options: { max: 100 },
	});
	assert.equal(g.series[0].type, "gauge");
	assert.equal(g.series[0].max, 100);
	assert.equal(g.series[0].data[0].value, 87);
});
