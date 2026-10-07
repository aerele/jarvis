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
