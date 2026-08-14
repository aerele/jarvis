// #492: the tenant Knowledge Graph shipped with its whole action layer switched
// off. The page passed :show-actions / :can-act / :show-priority /
// :show-actions-tab as false (three of them overriding a package default of
// true), so the "+ link" button, the Actions tab and the priority strip never
// rendered, and `addWikiLink` was exported by src/api/wiki.js and imported by
// nothing. This covers the loop the page's own header comment describes: accept
// a suggested connection -> add_wiki_link -> refetch -> the edge appears.
//
// wiki-graph-core is mocked wholesale. It is a `file:` sibling whose own runtime
// deps (graphology, 3d-force-graph) are NOT installed by `npm ci` in frontend/,
// and its 3D renderer needs WebGL, so the real package cannot load under vitest.
// The stubs keep the package's real prop defaults, which is what makes "the page
// stopped forcing these off" an assertion about observable behaviour.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { h, ref } from "vue";
import { mount, flushPromises } from "@vue/test-utils";

const A_ID = "page:customer--acme";
const B_ID = "page:process--billing";
const A_SLUG = "customer--acme";
const B_SLUG = "process--billing";
const SUGGESTION = { a: A_ID, b: B_ID, aLabel: "Acme Corp", bLabel: "Billing", score: 0.8 };

const nodes = [
	{ id: A_ID, kind: "page", slug: A_SLUG, label: "Acme Corp", page_type: "Customer" },
	{ id: B_ID, kind: "page", slug: B_SLUG, label: "Billing", page_type: "Process" },
];
const UNLINKED = { nodes, edges: [] };
const LINKED = { nodes, edges: [{ source: A_ID, target: B_ID, kind: "links-to" }] };
// #644: A curated a link to B. `manual_links` only ever rides on the node that
// did the linking (the server-side asymmetry `get_wiki_graph` returns).
const LINKED_WITH_MANUAL = {
	nodes: [{ ...nodes[0], manual_links: [B_SLUG] }, nodes[1]],
	edges: [{ source: A_ID, target: B_ID, kind: "links-to" }],
};

const api = vi.hoisted(() => ({
	addWikiLink: vi.fn(async () => ({ ok: true, manual_links: ["process--billing"] })),
	removeWikiLink: vi.fn(async () => ({ ok: true, manual_links: [] })),
	getWikiGraph: vi.fn(),
	getWikiGraphHistory: vi.fn(async () => []),
}));
vi.mock("@/api/wiki", () => api);

vi.mock("@/theme", () => ({ useJarvisTheme: () => ({ effectiveDark: ref(false) }) }));

vi.mock("@/components/LayoutHeader.vue", () => ({
	default: {
		name: "LayoutHeader",
		setup:
			(_p, { slots }) =>
			() =>
				h("div", null, slots),
	},
}));

vi.mock("frappe-ui", () => {
	const passthrough = (name, tag) => ({
		name,
		inheritAttrs: false,
		setup:
			(_p, { slots, attrs }) =>
			() =>
				h(
					tag,
					{ onClick: attrs.onClick },
					slots.default ? slots.default() : attrs.label || ""
				),
	});
	return {
		Badge: passthrough("Badge", "span"),
		Breadcrumbs: passthrough("Breadcrumbs", "nav"),
		Button: passthrough("Button", "button"),
	};
});

// Captured live prop objects (reactive), plus handles on the child emits this
// suite drives: AnalysisTabs' add-link, Graph3D's node-click (selection),
// DetailPanel's remove-link.
const seen = vi.hoisted(() => ({
	detail: null,
	tabs: null,
	graph: null,
	fireAddLink: null,
	fireNodeClick: null,
	fireRemoveLink: null,
}));

vi.mock("wiki-graph-core", () => {
	const stub = (name, props, slot, emits = []) => ({
		name,
		props,
		emits,
		setup(componentProps, { emit }) {
			seen[slot] = componentProps;
			if (name === "AnalysisTabs") seen.fireAddLink = (s) => emit("add-link", s);
			if (name === "Graph3D") seen.fireNodeClick = (n) => emit("node-click", n);
			if (name === "DetailPanel") seen.fireRemoveLink = (t) => emit("remove-link", t);
			return () => h("div", { class: name });
		},
	});
	return {
		// Prop defaults copied from the real package: DetailPanel.vue showActions /
		// canRemoveLink, AnalysisTabs.vue canAct / showPriority / showActionsTab.
		DetailPanel: stub(
			"DetailPanel",
			{
				node: Object,
				showActions: { type: Boolean, default: true },
				canRemoveLink: { type: Boolean, default: false },
			},
			"detail",
			["focus", "remove-link"]
		),
		AnalysisTabs: stub(
			"AnalysisTabs",
			{
				analysis: Object,
				nodes: Array,
				actions: Object,
				history: Array,
				canAct: { type: Boolean, default: false },
				showPriority: { type: Boolean, default: true },
				showActionsTab: { type: Boolean, default: true },
			},
			"tabs",
			["pick", "add-link"]
		),
		Graph3D: stub(
			"Graph3D",
			{ data: Object, metrics: Object, mode: String, dark: Boolean },
			"graph",
			["node-click"]
		),
		FilterBar: { name: "FilterBar", setup: () => () => h("div") },
		ExclusionRules: { name: "ExclusionRules", setup: () => () => h("div") },
		runAnalysis: async () => ({
			metrics: {},
			lists: { suggestedLinks: [SUGGESTION], coRead: [], brokers: [] },
			communities: {},
		}),
		computeActions: () => ({
			stale: [],
			orphans: [],
			busFactor: [],
			duplicates: [],
			suggest: [SUGGESTION],
		}),
		overlayFilter: (g) => g,
		egoGraph: (g) => g,
		searchGraph: (g) => g,
	};
});

import KnowledgeGraph from "@/pages/wiki/KnowledgeGraph.vue";

async function mountGraph() {
	const wrapper = mount(KnowledgeGraph);
	await flushPromises();
	await flushPromises();
	return wrapper;
}

beforeEach(() => {
	localStorage.clear();
	api.addWikiLink.mockClear();
	api.addWikiLink.mockResolvedValue({ ok: true, manual_links: [B_SLUG] });
	api.removeWikiLink.mockClear();
	api.removeWikiLink.mockResolvedValue({ ok: true, manual_links: [] });
	api.getWikiGraphHistory.mockClear();
	api.getWikiGraph.mockReset();
	api.getWikiGraph.mockResolvedValue(UNLINKED);
});

describe("KnowledgeGraph action layer (#492)", () => {
	it("no longer forces the action layer off", async () => {
		await mountGraph();
		expect(seen.detail.showActions).toBe(true);
		expect(seen.tabs.canAct).toBe(true);
		expect(seen.tabs.showPriority).toBe(true);
		expect(seen.tabs.showActionsTab).toBe(true);
	});

	it("adds the curated link and the edge appears after the refetch", async () => {
		const wrapper = await mountGraph();
		expect(seen.graph.data.edges).toHaveLength(0);
		expect(wrapper.text()).toContain("0 links");

		// the server now knows about the link the click is about to create
		api.getWikiGraph.mockResolvedValue(LINKED);
		seen.fireAddLink(seen.tabs.actions.suggest[0]);
		await flushPromises();
		await flushPromises();

		// node ids are "page:<slug>"; the endpoint takes bare slugs
		expect(api.addWikiLink).toHaveBeenCalledWith(A_SLUG, B_SLUG);
		expect(api.getWikiGraph).toHaveBeenCalledTimes(2);
		expect(seen.graph.data.edges).toEqual([{ source: A_ID, target: B_ID, kind: "links-to" }]);
		expect(wrapper.text()).toContain("1 links");
		expect(wrapper.text()).toContain("Linked Acme Corp to Billing");
	});

	it("keeps the graph mounted while refetching, rather than blanking to the skeleton", async () => {
		const wrapper = await mountGraph();
		let release;
		api.getWikiGraph.mockReturnValueOnce(new Promise((r) => (release = () => r(LINKED))));

		seen.fireAddLink(SUGGESTION);
		await flushPromises();
		expect(wrapper.find(".kg-skel").exists()).toBe(false);
		expect(wrapper.find(".Graph3D").exists()).toBe(true);

		release();
		await flushPromises();
	});

	it("still reports success when only the refetch fails, because the link is stored", async () => {
		const wrapper = await mountGraph();
		api.getWikiGraph.mockRejectedValueOnce(new Error("Network error"));

		seen.fireAddLink(SUGGESTION);
		await flushPromises();
		await flushPromises();

		expect(api.addWikiLink).toHaveBeenCalledTimes(1);
		expect(wrapper.text()).toContain("Linked Acme Corp to Billing");
		expect(wrapper.text()).not.toContain("Network error");
		expect(wrapper.find(".kg-err").exists()).toBe(false);
	});

	it("surfaces a server denial and leaves the graph as it was", async () => {
		const wrapper = await mountGraph();
		api.addWikiLink.mockRejectedValueOnce(new Error("Not permitted."));

		seen.fireAddLink(SUGGESTION);
		await flushPromises();
		await flushPromises();

		expect(wrapper.text()).toContain("Not permitted.");
		expect(api.getWikiGraph).toHaveBeenCalledTimes(1);
		expect(seen.graph.data.edges).toHaveLength(0);
	});

	it("ignores a second click while one curation is in flight", async () => {
		const wrapper = await mountGraph();
		let release;
		api.addWikiLink.mockReturnValueOnce(new Promise((r) => (release = () => r({ ok: true }))));

		seen.fireAddLink(SUGGESTION);
		await flushPromises();
		expect(seen.tabs.canAct).toBe(false);

		seen.fireAddLink(SUGGESTION);
		await flushPromises();
		expect(api.addWikiLink).toHaveBeenCalledTimes(1);

		release();
		await flushPromises();
		expect(seen.tabs.canAct).toBe(true);
		expect(wrapper.exists()).toBe(true);
	});
});

// #644: the undo counterpart. add_wiki_link had no way to remove a curated link,
// so a mis-clicked "+ link" was permanent short of a Desk/DB edit.
describe("KnowledgeGraph remove-link affordance (#644)", () => {
	it("passes can-remove-link and the selected node's curated links to the detail panel", async () => {
		api.getWikiGraph.mockResolvedValue(LINKED_WITH_MANUAL);
		await mountGraph();
		expect(seen.detail.canRemoveLink).toBe(true);

		seen.fireNodeClick(seen.graph.data.nodes.find((n) => n.id === A_ID));
		await flushPromises();
		expect(seen.detail.node.manual_links).toEqual([B_SLUG]);
	});

	it("removes the curated link and the edge disappears after the refetch", async () => {
		api.getWikiGraph.mockResolvedValue(LINKED_WITH_MANUAL);
		const wrapper = await mountGraph();
		seen.fireNodeClick(seen.graph.data.nodes.find((n) => n.id === A_ID));
		await flushPromises();
		expect(seen.graph.data.edges).toHaveLength(1);

		// the server now reports the link gone
		api.getWikiGraph.mockResolvedValue(UNLINKED);
		seen.fireRemoveLink(B_SLUG);
		await flushPromises();
		await flushPromises();

		// node ids are "page:<slug>"; the endpoint takes the bare source slug plus
		// the bare target slug that DetailPanel emitted.
		expect(api.removeWikiLink).toHaveBeenCalledWith(A_SLUG, B_SLUG);
		expect(api.getWikiGraph).toHaveBeenCalledTimes(2);
		expect(seen.graph.data.edges).toHaveLength(0);
		expect(wrapper.text()).toContain(`Removed link to ${B_SLUG}`);
		// the panel re-points at the refetched node, whose manual_links no longer
		// includes B (the fixture's post-removal shape carries none at all).
		expect(seen.detail.node.manual_links || []).toEqual([]);
	});

	it("still reports success when only the refetch fails, because the link is gone", async () => {
		api.getWikiGraph.mockResolvedValueOnce(LINKED_WITH_MANUAL);
		const wrapper = await mountGraph();
		seen.fireNodeClick(seen.graph.data.nodes.find((n) => n.id === A_ID));
		await flushPromises();

		api.getWikiGraph.mockRejectedValueOnce(new Error("Network error"));
		seen.fireRemoveLink(B_SLUG);
		await flushPromises();
		await flushPromises();

		expect(api.removeWikiLink).toHaveBeenCalledTimes(1);
		expect(wrapper.text()).toContain(`Removed link to ${B_SLUG}`);
		expect(wrapper.text()).not.toContain("Network error");
	});

	it("surfaces a server denial and leaves the link in place", async () => {
		api.getWikiGraph.mockResolvedValue(LINKED_WITH_MANUAL);
		const wrapper = await mountGraph();
		seen.fireNodeClick(seen.graph.data.nodes.find((n) => n.id === A_ID));
		await flushPromises();

		api.removeWikiLink.mockRejectedValueOnce(new Error("Not permitted."));
		seen.fireRemoveLink(B_SLUG);
		await flushPromises();
		await flushPromises();

		expect(wrapper.text()).toContain("Not permitted.");
		expect(api.getWikiGraph).toHaveBeenCalledTimes(1);
		expect(seen.graph.data.edges).toHaveLength(1);
	});

	it("ignores a second click while one removal is in flight", async () => {
		api.getWikiGraph.mockResolvedValue(LINKED_WITH_MANUAL);
		const wrapper = await mountGraph();
		seen.fireNodeClick(seen.graph.data.nodes.find((n) => n.id === A_ID));
		await flushPromises();

		let release;
		api.removeWikiLink.mockReturnValueOnce(
			new Promise((r) => (release = () => r({ ok: true })))
		);
		seen.fireRemoveLink(B_SLUG);
		await flushPromises();
		expect(seen.tabs.canAct).toBe(false);

		seen.fireRemoveLink(B_SLUG);
		await flushPromises();
		expect(api.removeWikiLink).toHaveBeenCalledTimes(1);

		release();
		await flushPromises();
		expect(seen.tabs.canAct).toBe(true);
		expect(wrapper.exists()).toBe(true);
	});
});
