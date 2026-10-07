import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { ref } from "vue";

vi.mock("frappe-ui", () => ({
	Dialog: { props: ["modelValue", "options"], template: "<div><slot name='body'/></div>" },
	Button: {
		props: ["label"],
		emits: ["click"],
		template: "<button @click=\"$emit('click')\">{{ label }}</button>",
	},
	FeatherIcon: { template: "<i/>" },
}));
vi.mock("@/theme", () => ({ useJarvisTheme: () => ({ effectiveDark: ref(false) }) }));
const previewFile = vi.fn();
vi.mock("@/api", () => ({ previewFile: (...a) => previewFile(...a) }));

import FilePreview from "@/components/FilePreview.vue";

const JvChartStub = {
	name: "JvChart",
	props: ["spec", "dark"],
	template: "<div class='chart'>{{ spec.title }}</div>",
};
const chart = (sheet, title) => ({
	sheet,
	title,
	type: "bar",
	x: ["a"],
	series: [{ name: "v", data: [1] }],
	options: {},
});
const sheets = [
	{ name: "One", rows: [["h"], ["r"]] },
	{ name: "Two", rows: [["h2"], ["r2"]] },
];

async function open(preview) {
	previewFile.mockResolvedValue(preview);
	const w = mount(FilePreview, {
		props: { modelValue: true, fileUrl: "/private/files/a.xlsx", fileName: "a.xlsx" },
		global: { stubs: { JvChart: JvChartStub } },
	});
	await w.setProps({ modelValue: false });
	await w.setProps({ modelValue: true });
	await flushPromises();
	return w;
}

describe("FilePreview xlsx charts", () => {
	beforeEach(() => previewFile.mockReset());

	it("renders the sheet's charts with JvChart above the grid", async () => {
		const w = await open({
			kind: "table",
			sheets,
			charts: [chart("One", "Outstanding"), chart("Two", "Other")],
		});
		const charts = w.findAllComponents(JvChartStub);
		expect(charts).toHaveLength(1);
		expect(charts[0].props("spec").title).toBe("Outstanding");
		expect(w.html().indexOf("chart")).toBeLessThan(w.html().indexOf("<table"));
	});

	it("follows the sheet tab", async () => {
		const w = await open({ kind: "table", sheets, charts: [chart("Two", "Other")] });
		expect(w.findAllComponents(JvChartStub)).toHaveLength(0);
		await w
			.findAll("button")
			.find((b) => b.text() === "Two")
			.trigger("click");
		expect(w.findAllComponents(JvChartStub)).toHaveLength(1);
	});

	it("shows only the grid when the preview has no charts", async () => {
		const w = await open({ kind: "table", sheets });
		expect(w.findAllComponents(JvChartStub)).toHaveLength(0);
		expect(w.find("table").exists()).toBe(true);
	});
});
