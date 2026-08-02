import { describe, it, expect, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { mount } from "@vue/test-utils";
import { frappeUiStubs } from "./stubs.js";
import { SKILLS_SCHEMA } from "./fixtures.js";

vi.mock("frappe-ui", () => ({
	...frappeUiStubs(),
	// ListPage's frappe-ui list composition — irrelevant to which filter surface
	// it mounts, so they are inert here.
	ListView: { template: "<div><slot /></div>" },
	ListHeader: { template: "<div><slot /></div>" },
	ListHeaderItem: { template: "<div />" },
	ListRows: { template: "<div />" },
	ListRowItem: { template: "<div />" },
	ListSelectBanner: { template: "<div />" },
	ListFooter: { template: "<div />" },
	Breadcrumbs: { template: "<div />" },
	Tooltip: { template: "<div><slot /></div>" },
}));
vi.mock("@/api", () => ({ searchLink: vi.fn(async () => []) }));
vi.mock("@/components/LayoutHeader.vue", () => ({ default: { template: "<div><slot /></div>" } }));
vi.mock("@/components/list/SortButton.vue", () => ({ default: { template: "<div />" } }));
vi.mock("@/components/list/ColumnsButton.vue", () => ({ default: { template: "<div />" } }));

import ListPage from "@/components/list/ListPage.vue";
import FilterButton from "@/components/list/FilterButton.vue";
import FilterGroup from "@/components/list/FilterGroup.vue";

const root = resolve(__dirname, "../..");
const read = (p) => readFileSync(resolve(root, p), "utf8");

// The two surfaces this wave migrates, and the six that must NOT move with it.
const MIGRATED = {
	skills: "src/pages/skills/SkillsList.vue",
	macros: "src/pages/macros/MacrosList.vue",
};
const UNMIGRATED = [
	"src/pages/dashboards/SavedDashboardsTab.vue",
	"src/pages/files/FilesList.vue",
	"src/pages/skills/WikiTab.vue",
	"src/pages/support/SupportListPage.vue",
	"src/pages/triggers/ActivityTab.vue",
	"src/pages/triggers/TriggersListPane.vue",
];

describe("only the two pilot surfaces move", () => {
	it.each(UNMIGRATED)("%s still drives the legacy FilterButton", (path) => {
		const source = read(path);
		expect(source).toContain(":filter-defs=");
		expect(source).not.toContain(":filter-state=");
		expect(source).not.toContain("view-key");
	});

	it.each(Object.entries(MIGRATED))("%s drives FilterGroup and drops filterDefs", (key, path) => {
		const source = read(path);
		expect(source).toContain(':filter-state="filterState"');
		expect(source).not.toContain(":filter-defs=");
		expect(source).toContain(`viewKey: "${key}"`);
	});

	// If FilterButton is ever deleted before waves 2-4 land, six lists lose their
	// filter action silently. Pin it.
	it("keeps FilterButton in the tree and wired into ListPage", () => {
		expect(read("src/components/list/FilterButton.vue")).toContain("filterDefs");
		expect(read("src/components/list/ListPage.vue")).toContain(
			'import FilterButton from "@/components/list/FilterButton.vue"'
		);
	});

	// The SPA's pilot set has to be the server's pilot set: a view whose endpoint
	// does not accept filters_v2 answers get_list_filter_schema with
	// list_filter_view_not_filterable, so a mismatch here is a dead Filter panel.
	it("matches the server registry's MIGRATED views exactly", () => {
		const registry = read("../jarvis/chat/list_registry.py");
		const migrated = [];
		const re = /view_key="([a-z_]+)",[\s\S]*?(?=\n\tListView\(|\n\)\n)/g;
		let match;
		while ((match = re.exec(registry))) {
			if (/status=MIGRATED/.test(match[0])) migrated.push(match[1]);
		}
		expect(migrated.sort()).toEqual(Object.keys(MIGRATED).sort());
	});
});

describe("ListPage mounts exactly one filter surface", () => {
	const base = {
		columns: [],
		rows: [],
		sortOptions: [],
	};

	it("legacy list ⇒ FilterButton only", () => {
		const w = mount(ListPage, {
			props: {
				...base,
				filterDefs: [{ key: "enabled", label: "Status", type: "select", options: [] }],
				filters: {},
			},
		});
		expect(w.findComponent(FilterButton).exists()).toBe(true);
		expect(w.findComponent(FilterGroup).exists()).toBe(false);
	});

	it("migrated list ⇒ FilterGroup only, even if filterDefs is somehow still passed", () => {
		const w = mount(ListPage, {
			props: {
				...base,
				filterDefs: [{ key: "enabled", label: "Status", type: "select", options: [] }],
				filterState: {
					viewKey: "skills",
					schema: SKILLS_SCHEMA,
					schemaState: "ready",
					schemaError: null,
					clauses: [],
					error: null,
					notice: "",
				},
			},
		});
		expect(w.findComponent(FilterGroup).exists()).toBe(true);
		expect(w.findComponent(FilterButton).exists()).toBe(false);
	});

	it("a list with neither shows no filter action at all", () => {
		const w = mount(ListPage, { props: base });
		expect(w.findComponent(FilterButton).exists()).toBe(false);
		expect(w.findComponent(FilterGroup).exists()).toBe(false);
	});
});

describe("the filter notice strip is readable without opening the panel", () => {
	const state = (extra) => ({
		viewKey: "skills",
		schema: SKILLS_SCHEMA,
		schemaState: "ready",
		schemaError: null,
		clauses: [],
		error: null,
		notice: "",
		...extra,
	});

	it("shows the dropped-clause note, dismissibly", async () => {
		const w = mount(ListPage, {
			props: {
				columns: [],
				rows: [],
				filterState: state({ notice: "1 filter from this link is no longer available." }),
			},
		});
		expect(w.text()).toContain("1 filter from this link is no longer available.");
		await w.find('button[aria-label="Dismiss"]').trigger("click");
		expect(w.emitted("dismiss-filter-notice")).toHaveLength(1);
	});

	// P3-3: the note is often the REASON the results look wrong, so a rejection
	// arriving later must not hide it.
	it("stacks the note and the rejection, note first", () => {
		const w = mount(ListPage, {
			props: {
				columns: [],
				rows: [],
				filterState: state({
					notice: "1 filter from this link is no longer available.",
					error: { code: "list_filter_invalid_value", kind: "row", message: "Bad value." },
				}),
			},
		});
		const strips = w.findAll('[role="status"]');
		expect(strips).toHaveLength(2);
		expect(strips[0].text()).toContain("no longer available");
		expect(strips[1].text()).toContain("Bad value.");
		// only the note is dismissible
		expect(strips[0].find('button[aria-label="Dismiss"]').exists()).toBe(true);
		expect(strips[1].find('button[aria-label="Dismiss"]').exists()).toBe(false);
	});

	it("offers Retry for a transient failure and no dismiss for a live rejection", () => {
		const transient = mount(ListPage, {
			props: {
				columns: [],
				rows: [],
				filterState: state({
					error: { code: "list_filter_schema_unavailable", kind: "transient", message: "Down." },
				}),
			},
		});
		expect(transient.findAll("button").some((b) => b.text() === "Retry")).toBe(true);
		expect(transient.find('button[aria-label="Dismiss"]').exists()).toBe(false);

		const rejection = mount(ListPage, {
			props: {
				columns: [],
				rows: [],
				filterState: state({
					error: { code: "list_filter_invalid_value", kind: "row", message: "Bad value." },
				}),
			},
		});
		expect(rejection.text()).toContain("Bad value.");
		expect(rejection.findAll("button").some((b) => b.text() === "Retry")).toBe(false);
	});
});
