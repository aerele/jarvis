// P0-01/S6/D1 surface-to-wire integration: the full browser transport chain for
// each migrated view — useListPage (what every page mounts) driving the page's
// REAL fetchFn (`wrapper(adapter(p))`, imported from listFetchers, the EXACT
// function object each .vue passes to useListPage) through to frappe-ui's `call`.
//
// D1 (the blocker this round): the spec USED to rebuild the chain inline
// (`fetchFn: (p) => wrapper(adapt(p))`), choosing the adapter itself. That left a
// page free to drop `filters_v2` in its own mapping — `adapter({ ...p, filters_v2:
// undefined })`, the exact "claims-filtering-but-doesn't" defect — with the whole
// suite green, because the spec never ran the page's line. Now the page and the
// spec are bound to the SAME fetcher (listFetchers), so neutering `filters_v2`
// there turns this red; and a source guard below pins each .vue's `fetchFn:` line
// to its fetcher so a page cannot quietly wrap or bypass it. (Mounting each full
// .vue page would add only a fragile child-component dependency tree over this
// identical chain — see tabNavigation.spec's ~15 mocks.)
import { describe, it, expect, vi, beforeEach } from "vitest";
import { defineComponent } from "vue";
import { mount, flushPromises } from "@vue/test-utils";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const callDouble = vi.hoisted(() =>
	vi.fn(async () => ({ rows: [], total: 0, data: { rows: [], total: 0 } }))
);
vi.mock("frappe-ui", () => ({ call: callDouble, toast: { error: vi.fn(), create: vi.fn() } }));
vi.mock("@vueuse/core", async () => {
	const { ref } = await import("vue");
	return { useStorage: (_k, v) => ref(v) };
});

import { useListPage } from "@/composables/useListPage";
// The REAL page fetchers (S6/D1): the exact `fetchFn` each .vue mounts, so the
// chain under test is the page's own — wrapper AND adapter — not a rebuild.
import {
	skillsListFetch,
	macrosListFetch,
	dashboardsListFetch,
	triggersListFetch,
	wikiListFetch,
} from "@/pages/list/listFetchers";

function host(options) {
	let api = null;
	const Host = defineComponent({
		setup() {
			api = useListPage(options);
			return () => null;
		},
	});
	mount(Host);
	return api;
}

function schemaFor(root, fieldname) {
	return {
		contract_version: 1,
		view_key: "v",
		root_doctype: root,
		limits: { max_clauses: 20, max_in_values: 100, max_value_chars: 1000 },
		fields: [
			{
				doctype: root,
				fieldname,
				label: "F",
				fieldtype: "Small Text",
				options: "",
				is_standard: false,
				is_child: false,
				json_array: false,
				default_operator: "like",
				operators: ["=", "!=", "like", "not like", "in", "not in", "is"],
			},
		],
	};
}

// [view key, page fetcher, wire method, root doctype, filter fieldname, page .vue,
//  fetcher name as the page imports it]
const VIEWS = [
	[
		"skills",
		skillsListFetch,
		"jarvis.chat.custom_skills_api.list_custom_skills_page",
		"Jarvis Custom Skill",
		"description",
		"src/pages/skills/SkillsList.vue",
		"skillsListFetch",
	],
	[
		"macros",
		macrosListFetch,
		"jarvis.chat.macros_api.list_macros_page",
		"Jarvis Macro",
		"description",
		"src/pages/macros/MacrosList.vue",
		"macrosListFetch",
	],
	[
		"saved_dashboards",
		dashboardsListFetch,
		"jarvis.chat.dashboards_api.list_dashboards_page",
		"Jarvis Dashboard",
		"description",
		"src/pages/dashboards/SavedDashboardsTab.vue",
		"dashboardsListFetch",
	],
	[
		"triggers",
		triggersListFetch,
		"jarvis.chat.triggers_api.list_triggers_page",
		"Jarvis Trigger",
		"description",
		"src/pages/triggers/TriggersListPane.vue",
		"triggersListFetch",
	],
	[
		"wiki_pages",
		wikiListFetch,
		"jarvis.chat.wiki.list_wiki_pages_page",
		"Jarvis Wiki Page",
		"summary",
		"src/pages/skills/WikiTab.vue",
		"wikiListFetch",
	],
];

beforeEach(() => callDouble.mockClear());

describe("migrated surface → PAGE FETCHER → wrapper → wire carries filters_v2 (P0-01/S6/D1)", () => {
	it.each(VIEWS)("%s", async (key, fetcher, method, root, fieldname) => {
		let n = 0;
		// Drive the page's REAL fetcher — the exact function it passes to useListPage
		// — so a dropped `filters_v2` anywhere in the page's transport (adapter OR
		// wrapper) fails here.
		const api = host({
			fetchFn: fetcher,
			storageKey: `st-${key}`,
			viewKey: key,
			fetchSchema: async () => schemaFor(root, fieldname),
		});
		await flushPromises();
		await api.requestSchema();
		await api.setClauses([
			{
				id: `c${++n}`,
				doctype: root,
				fieldname,
				operator: "like",
				value: "x",
				display: null,
			},
		]);
		await flushPromises();

		const forThisView = callDouble.mock.calls.filter((c) => c[0] === method);
		expect(forThisView.length).toBeGreaterThan(0);
		const args = forThisView.at(-1)[1];
		expect(args.filters_v2).toBe(
			JSON.stringify([{ doctype: root, fieldname, operator: "like", value: "x" }])
		);
	});
});

describe("each migrated page mounts EXACTLY its listFetchers fetcher (D1 source guard)", () => {
	// Behavioural binding above proves the fetcher reaches the wire with filters_v2.
	// This proves each .vue actually MOUNTS that fetcher, bare — so a page cannot
	// drop the filter by wrapping it (`fetchFn: (p) => fetcher({ ...p, filters_v2:
	// undefined })`) or swapping in an inline chain the spec never sees.
	it.each(VIEWS)("%s", (key, _fetcher, _method, _root, _fieldname, pageFile, fetcherName) => {
		// vitest runs with cwd = frontend/, and pageFile is repo-relative from there.
		const src = readFileSync(resolve(process.cwd(), pageFile), "utf8");
		expect(src).toContain(`import { ${fetcherName} } from "@/pages/list/listFetchers"`);
		// Exact bare reference — no arrow, no re-mapping of the request object.
		expect(src).toContain(`fetchFn: ${fetcherName},`);
	});
});
