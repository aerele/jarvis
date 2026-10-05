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
		ListSelectBanner: blank,
		ListFooter: blank,
		Breadcrumbs: blank,
		FormControl: blank,
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
