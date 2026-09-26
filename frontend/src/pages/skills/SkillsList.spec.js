import { mount, flushPromises } from "@vue/test-utils";
import { describe, it, expect, vi } from "vitest";

// A skill File Box follows to a fixed document type carries a "File Box → <type>" chip.
vi.mock("vue-router", () => ({
	useRoute: () => ({ query: {} }),
	useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("frappe-ui", () => ({
	Badge: { props: ["label"], template: '<span class="badge">{{ label }}</span>' },
	Button: { props: ["label"], template: "<button>{{ label }}</button>" },
	Avatar: { template: "<i/>" },
	Tooltip: { template: "<span><slot/></span>" },
	Dropdown: { template: "<div/>" },
	toast: { success: vi.fn(), error: vi.fn(), create: vi.fn() },
	confirmDialog: vi.fn(),
}));
const rows = vi.hoisted(() => [
	{ name: "S1", skill_name: "bills", use_in_file_box: 1, file_box_creates: "Purchase Invoice" },
	{ name: "S2", skill_name: "opted-out", use_in_file_box: 0, file_box_creates: "Sales Invoice" },
	{ name: "S3", skill_name: "any-type", use_in_file_box: 1, file_box_creates: null },
]);
vi.mock("@/composables/useListPage", async () => {
	const { ref, reactive } = await import("vue");
	return {
		useListPage: () => ({
			rows: ref(rows),
			total: ref(rows.length),
			hasMore: ref(false),
			loading: ref(false),
			filters: reactive({}),
			sort: reactive({}),
			pageLength: ref(20),
			filterState: ref({}),
			setFilters: vi.fn(),
			setSort: vi.fn(),
			resetLoad: vi.fn(),
			loadMore: vi.fn(),
			setClauses: vi.fn(),
			requestSchema: vi.fn(),
			dismissFilterNotice: vi.fn(),
		}),
	};
});
vi.mock("@/components/list/ListPage.vue", () => ({
	default: {
		props: ["rows"],
		template: `<div><div v-for="row in rows" :key="row.name" class="row" :data-name="row.name"><slot name="cell-skill_name" :row="row" /></div></div>`,
	},
}));
vi.mock("@/pages/list/listFetchers", () => ({ skillsListFetch: vi.fn() }));
vi.mock("./SyncPill.vue", () => ({ default: { template: "<i/>" } }));
vi.mock("@/data/session", () => ({ session: { user: "me@example.com" } }));
vi.mock("@/utils/datetime", () => ({ timeAgo: () => "now", exactDate: () => "" }));
vi.mock("@/api/skills", () => ({ deleteCustomSkillsBulk: vi.fn() }));

import SkillsList from "./SkillsList.vue";

describe("SkillsList: the File Box chip", () => {
	it("shows File Box → <type> only for a skill File Box uses with a fixed type", async () => {
		const w = mount(SkillsList);
		await flushPromises();
		const chips = (name) => w.findAll(`.row[data-name="${name}"] .badge`).map((b) => b.text());
		expect(chips("S1")).toEqual(["File Box → Purchase Invoice"]);
		expect(chips("S2")).toEqual([]);
		expect(chips("S3")).toEqual([]);
		w.unmount();
	});
});
