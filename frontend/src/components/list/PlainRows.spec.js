import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

// The package index pulls in the whole resource layer, so import the four components
// directly. The Button's router hook is the one thing that needs a stand-in.
vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }), useRoute: () => ({}) }));
vi.mock("frappe-ui", async () => ({
	ListRowItem: { template: "<span />" },
	Button: (await import("../../../node_modules/frappe-ui/src/components/Button/Button.vue"))
		.default,
}));
import ListView from "../../../node_modules/frappe-ui/src/components/ListView/ListView.vue";
import ListRows from "../../../node_modules/frappe-ui/src/components/ListView/ListRows.vue";
import Button from "../../../node_modules/frappe-ui/src/components/Button/Button.vue";
import PlainRows from "./PlainRows.vue";
import LayoutHeader from "@/components/LayoutHeader.vue";

// Real frappe-ui, no mocks: the point is the DOM a browser's accessibility check sees.
const columns = [
	{ label: "File", key: "file", width: 3 },
	{ label: "", key: "_retry", width: "5rem", align: "right" },
];
const rows = [
	{ name: "A", file: "a.pdf" },
	{ name: "B", file: "b.pdf" },
];

function mountList(plain) {
	return mount(
		{
			components: { ListView, ListRows, PlainRows, Button },
			props: ["plain"],
			template: `<ListView :columns="columns" :rows="rows" row-key="name" :options="{ rowHeight: 40 }">
			<template #default><ListRows><PlainRows v-if="plain" /></ListRows></template>
			<template #cell="{ column, row }">
				<span v-if="column.key === 'file'"><a href="/x">{{ row.file }}</a></span>
				<Button v-else :label="'Retry ' + row.file">Retry</Button>
			</template>
		</ListView>`,
			setup: () => ({ columns, rows }),
		},
		{ props: { plain } }
	);
}

describe("PlainRows", () => {
	it("frappe-ui's own row wraps the cells in a button (the failure this replaces)", () => {
		const w = mountList(false);
		expect(w.findAll("button a").length).toBeGreaterThan(0);
		expect(w.findAll("button button").length).toBeGreaterThan(0);
	});

	it("renders no link or button inside another button", () => {
		const w = mountList(true);
		expect(w.findAll('[data-testid="plain-row"]')).toHaveLength(2);
		expect(w.findAll("button a")).toHaveLength(0);
		expect(w.findAll("button button")).toHaveLength(0);
		expect(w.findAll("a button")).toHaveLength(0);
	});

	it("keeps the real Button's accessible name distinct per row", () => {
		const w = mountList(true);
		expect(w.findAll("button").map((b) => b.attributes("aria-label"))).toEqual([
			"Retry a.pdf",
			"Retry b.pdf",
		]);
	});
});

describe("icon-only toolbar buttons", () => {
	it("get an accessible name from `label`, which the real Button turns into aria-label", () => {
		const w = mount(Button, { props: { icon: "refresh-cw", label: "Refresh" } });
		expect(w.find("button").attributes("aria-label")).toBe("Refresh");
		const bare = mount(Button, { props: { icon: "refresh-cw" } });
		expect(bare.find("button").attributes("aria-label")).toBeUndefined();
	});
});

describe("LayoutHeader", () => {
	it("grows and wraps downward instead of clipping its controls upward", async () => {
		document.body.insertAdjacentHTML("beforeend", '<div id="app-header"></div>');
		const w = mount(LayoutHeader, { attachTo: document.body });
		await new Promise((r) => setTimeout(r, 20));
		const header = document.querySelector("#app-header header");
		expect(header.className).toContain("min-h-10.5");
		expect(header.className).toContain("flex-wrap");
		expect(header.className).not.toMatch(/(^|\s)h-10\.5/);
		w.unmount();
	});
});
