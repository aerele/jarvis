// P0-01 surface-to-wrapper integration: the full browser transport chain for
// each migrated view — useListPage (what every page mounts) driving the REAL
// feature wrapper as its fetchFn, through to frappe-ui's `call`. This is the
// end-to-end evidence that a canonical clause set on a page reaches the endpoint
// as filters_v2, not just that each link works in isolation. (Mounting each full
// .vue page adds only a fragile dependency tree over this identical chain.)
import { describe, it, expect, vi, beforeEach } from "vitest";
import { defineComponent } from "vue";
import { mount, flushPromises } from "@vue/test-utils";

const callDouble = vi.hoisted(() =>
	vi.fn(async () => ({ rows: [], total: 0, data: { rows: [], total: 0 } }))
);
vi.mock("frappe-ui", () => ({ call: callDouble, toast: { error: vi.fn(), create: vi.fn() } }));
vi.mock("@vueuse/core", async () => {
	const { ref } = await import("vue");
	return { useStorage: (_k, v) => ref(v) };
});

import { useListPage } from "@/composables/useListPage";
import { listCustomSkillsPage, listMacrosPage } from "@/api";
import { listDashboardsPage } from "@/api/dashboards";
import { listTriggersPage } from "@/api/triggers";
import { listWikiPagesPage } from "@/api/wiki";

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

const VIEWS = [
	[
		"skills",
		listCustomSkillsPage,
		"jarvis.chat.custom_skills_api.list_custom_skills_page",
		"Jarvis Custom Skill",
		"description",
	],
	[
		"macros",
		listMacrosPage,
		"jarvis.chat.macros_api.list_macros_page",
		"Jarvis Macro",
		"description",
	],
	[
		"saved_dashboards",
		listDashboardsPage,
		"jarvis.chat.dashboards_api.list_dashboards_page",
		"Jarvis Dashboard",
		"description",
	],
	[
		"triggers",
		listTriggersPage,
		"jarvis.chat.triggers_api.list_triggers_page",
		"Jarvis Trigger",
		"description",
	],
	[
		"wiki_pages",
		listWikiPagesPage,
		"jarvis.chat.wiki.list_wiki_pages_page",
		"Jarvis Wiki Page",
		"summary",
	],
];

beforeEach(() => callDouble.mockClear());

describe("migrated surface → wrapper → wire carries filters_v2 (P0-01)", () => {
	it.each(VIEWS)("%s", async (key, wrapper, method, root, fieldname) => {
		let n = 0;
		const api = host({
			fetchFn: wrapper,
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
