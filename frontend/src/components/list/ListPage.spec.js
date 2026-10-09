import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

/**
 * ListPage's error state: a list that failed to load must offer a way forward.
 * It used to name the failure and stop there, and the only way to retry was to
 * find the Refresh icon in the toolbar or reload the page.
 */

vi.mock("frappe-ui", () => {
	const blank = { template: "<div><slot /></div>" };
	return {
		ListView: blank,
		ListHeader: blank,
		ListHeaderItem: blank,
		ListRows: blank,
		ListRowItem: blank,
		ListSelectBanner: { name: "ListSelectBanner", template: "<div><slot /></div>" },
		ListFooter: blank,
		Breadcrumbs: blank,
		FormControl: blank,
		Dropdown: {
			name: "Dropdown",
			props: ["options", "placement"],
			template: `<div><slot :open="false" /><button v-for="o in options" :key="o.label" class="opt"
				:data-selected="o.selected ? 'yes' : 'no'" @click="o.onClick()">{{ o.label }}</button></div>`,
		},
		FeatherIcon: { template: "<i />" },
		Button: {
			name: "Button",
			props: ["label", "icon", "loading", "tooltip", "variant"],
			emits: ["click"],
			template: `<button @click="$emit('click')">{{ label }}</button>`,
		},
	};
});
const stub = vi.hoisted(() => ({ default: { template: "<div><slot /></div>" } }));
vi.mock("@/components/LayoutHeader.vue", () => stub);
vi.mock("@/components/list/FilterButton.vue", () => stub);
vi.mock("@/components/list/FilterGroup.vue", () => stub);
vi.mock("@/components/list/SortButton.vue", () => stub);
vi.mock("@/components/list/ColumnsButton.vue", () => stub);

import ListPage from "./ListPage.vue";

const tryAgain = (w) =>
	w.findAllComponents({ name: "Button" }).find((b) => b.props("label") === "Try again");

describe("ListPage: a list that failed to load", () => {
	it("names the failure and offers Try again, which asks the page to refresh", async () => {
		const w = mount(ListPage, { props: { rows: [], error: "The server is not reachable." } });
		expect(w.text()).toContain("Couldn't load this list");
		expect(w.text()).toContain("The server is not reachable.");
		await tryAgain(w).trigger("click");
		expect(w.emitted("refresh")).toHaveLength(1);
	});

	it("shows the retry as busy while the page is loading again", () => {
		const w = mount(ListPage, { props: { rows: [], error: "No.", loading: true } });
		expect(tryAgain(w).props("loading")).toBe(true);
	});

	it("offers nothing to retry on an empty list that loaded fine", () => {
		const w = mount(ListPage, {
			props: { rows: [], error: "", emptyState: { title: "No Macros Found" } },
		});
		expect(w.text()).toContain("No Macros Found");
		expect(tryAgain(w)).toBeUndefined();
	});
});

describe("ListPage: the selection bar", () => {
	it("uses the raised pop-up surface, so it stands out in dark mode too", () => {
		// frappe-ui's own bar is bg-surface-white, the page colour in dark mode (admin-v2#625)
		const w = mount(ListPage, {
			props: {
				rows: [{ name: "a" }],
				columns: [{ label: "A", key: "a" }],
				selectable: true,
			},
		});
		const bar = w.findComponent({ name: "ListSelectBanner" });
		expect(bar.exists()).toBe(true);
		expect(bar.classes()).toEqual(expect.arrayContaining(["border", "!bg-surface-modal"]));
	});
});

describe("ListPage: a select quick filter", () => {
	// frappe-ui's Select opens item-aligned, over its field, so a chosen option moved the
	// list up the page (admin-v2#624). A Dropdown always opens below its field.
	const qf = {
		key: "status",
		label: "Status",
		type: "select",
		options: [
			{ label: "All", value: "" },
			{ label: "Processing", value: "processing" },
			{ label: "Draft created", value: "draft_created" },
		],
	};
	const options = (w) => w.findComponent({ name: "Dropdown" }).findAll(".opt");

	it("is a dropdown whose button names the filter and shows the chosen option, marked in its list", () => {
		const w = mount(ListPage, {
			props: { rows: [], quickFilters: [qf], filters: { status: "draft_created" } },
		});
		expect(w.findComponent({ name: "Dropdown" }).exists()).toBe(true);
		const trigger = w.find('button[aria-label="Status: Draft created"]');
		expect(trigger.text()).toBe("Draft created");
		const marked = options(w).filter((o) => o.attributes("data-selected") === "yes");
		expect(marked.map((o) => o.text())).toEqual(["Draft created"]);
	});

	it("moves focus into the opened menu, so the arrow keys reach the options", () => {
		vi.stubGlobal("requestAnimationFrame", (cb) => cb());
		const menu = document.createElement("div");
		menu.setAttribute("data-reka-menu-content", "");
		menu.tabIndex = -1;
		document.body.appendChild(menu);
		try {
			const w = mount(ListPage, { props: { rows: [], quickFilters: [qf], filters: {} } });
			w.findComponent({ name: "Dropdown" }).vm.$emit("update:open", true);
			expect(document.activeElement).toBe(menu);
			w.unmount();
		} finally {
			menu.remove();
			vi.unstubAllGlobals();
		}
	});

	it("applies the option picked, and All clears the filter", async () => {
		const w = mount(ListPage, {
			props: { rows: [], quickFilters: [qf], filters: { status: "draft_created" } },
		});
		await options(w)
			.find((o) => o.text() === "Processing")
			.trigger("click");
		expect(w.emitted("update:filters").at(-1)[0]).toEqual({ status: "processing" });
		await options(w)
			.find((o) => o.text() === "All")
			.trigger("click");
		expect(w.emitted("update:filters").at(-1)[0]).toEqual({});
	});
});
