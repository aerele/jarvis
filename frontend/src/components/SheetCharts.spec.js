import { describe, it, expect } from "vitest";
import { mount } from "@vue/test-utils";
import SheetCharts from "@/components/SheetCharts.vue";
import { chartsForSheet, tablePreviewFields } from "@/components/sheetCharts";

const JvChartStub = {
	name: "JvChart",
	props: ["spec", "dark"],
	template: "<div class='chart'>{{ spec.title }}</div>",
};
const chart = (sheet, title) => ({ sheet, title, type: "bar", x: ["a"], series: [], options: {} });

describe("sheetCharts helpers", () => {
	it("keeps charts from the preview response (the artifact panel used to drop them)", () => {
		const r = {
			kind: "table",
			sheets: [{ name: "One", rows: [] }],
			charts: [chart("One", "A")],
		};
		expect(tablePreviewFields(r).charts).toHaveLength(1);
		expect(tablePreviewFields({ ...r, charts: undefined }).charts).toEqual([]);
	});

	it("follows the selected sheet", () => {
		const charts = [chart("One", "A"), chart("Two", "B")];
		expect(chartsForSheet(charts, "Two").map((c) => c.title)).toEqual(["B"]);
		expect(chartsForSheet(undefined, "Two")).toEqual([]);
	});
});

describe("charts on sheets the preview did not load", () => {
	// preview_file loads only the first sheet's cells; the charts may live on another
	const charts = [chart("Data", "On Data"), chart("Summary", "On Summary")];

	it("shows them on the first (only) tab", () => {
		expect(chartsForSheet(charts, "Summary", ["Summary"]).map((c) => c.title)).toEqual([
			"On Data",
			"On Summary",
		]);
	});

	it("does not repeat them on other loaded tabs", () => {
		expect(chartsForSheet(charts, "Data", ["Summary", "Data"]).map((c) => c.title)).toEqual([
			"On Data",
		]);
	});

	it("captions a chart that is not from the sheet on screen", () => {
		const opts = { global: { stubs: { JvChart: JvChartStub } } };
		const w = mount(SheetCharts, { ...opts, props: { charts, sheetName: "Summary" } });
		expect(w.text()).toContain("Data");
		expect(w.findAll(".text-xs").map((e) => e.text())).toEqual(["Data"]);
	});
});

describe("SheetCharts", () => {
	it("renders one JvChart per spec, nothing when empty", () => {
		const opts = { global: { stubs: { JvChart: JvChartStub } } };
		const w = mount(SheetCharts, {
			...opts,
			props: { charts: [chart("One", "A")], dark: true },
		});
		expect(w.findAllComponents(JvChartStub)).toHaveLength(1);
		expect(w.findComponent(JvChartStub).props("dark")).toBe(true);
		expect(
			mount(SheetCharts, { ...opts, props: { charts: [] } })
				.findComponent(JvChartStub)
				.exists()
		).toBe(false);
	});
});
