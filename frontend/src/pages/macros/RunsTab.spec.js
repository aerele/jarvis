import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The Runs tab's own list machinery (it does not use useListPage):
 *  - a failed load must say so, not "No macro runs yet";
 *  - the spinner must not stick when a realtime refresh overtakes a load;
 *  - a realtime refresh must not shrink a list of more than 100 rows back to the
 *    100 the server will return in one page.
 */

const api = vi.hoisted(() => ({
	listMacroRuns: vi.fn(),
	macroRunStats: vi.fn(),
	listMacros: vi.fn(),
	stopMacroRun: vi.fn(),
}));
vi.mock("@/api", () => api);

vi.mock("vue-router", () => ({ useRouter: () => ({ push: vi.fn() }) }));
// jsdom has no usable localStorage for useStorage; a plain ref is all the tab needs.
vi.mock("@vueuse/core", async () => {
	const { ref } = await import("vue");
	return { useStorage: (_key, initial) => ref(initial) };
});
vi.mock("@/utils/datetime", () => ({ timeAgo: () => "just now", exactDate: () => "" }));
vi.mock("@/components/LayoutHeader.vue", () => ({
	default: { name: "LayoutHeader", template: `<div><slot name="left-header" /></div>` },
}));
vi.mock("frappe-ui", () => ({
	toast: { success: vi.fn(), error: vi.fn(), create: vi.fn() },
	// Just the rows, in order: what the list holds is what these tests are about.
	ListView: {
		name: "ListView",
		props: ["rows", "columns", "rowKey", "options"],
		template: `<div class="list"><i v-for="r in rows" :key="r.name" class="row" :data-status="r.status">{{ r.name }}</i></div>`,
	},
	ListHeader: { template: "<div />" },
	ListHeaderItem: { template: "<div />" },
	ListRows: { template: "<div />" },
	ListRowItem: { template: "<div />" },
	ListFooter: {
		name: "ListFooter",
		props: ["modelValue", "options"],
		emits: ["update:modelValue", "loadMore"],
		template: `<div class="footer" />`,
	},
	Breadcrumbs: { template: "<div />" },
	Button: {
		name: "Button",
		props: ["tooltip", "icon", "loading", "variant", "theme"],
		emits: ["click"],
		template: `<button :data-tooltip="tooltip" @click="$emit('click')" />`,
	},
	Badge: { props: ["label"], template: "<span>{{ label }}</span>" },
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "options"],
		emits: ["update:modelValue"],
		template: "<div />",
	},
	Dialog: { template: "<div />" },
	FeatherIcon: { template: "<i />" },
	Tooltip: { template: "<span><slot /></span>" },
}));

import RunsTab from "./RunsTab.vue";

const run = (i, extra = {}) => ({
	name: `run-${String(i).padStart(3, "0")}`,
	macro_name: "Month-end close",
	status: "completed",
	...extra,
});
const runs = (from, to) => Array.from({ length: to - from }, (_, k) => run(from + k));
const page = (rows, hasMore, total) => ({ runs: rows, has_more: hasMore, total });

function deferred() {
	let resolve, reject;
	const promise = new Promise((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

function mountTab() {
	const handlers = [];
	const socket = { on: (_e, fn) => handlers.push(fn), off: () => {} };
	const w = mount(RunsTab, { global: { provide: { $socket: socket } } });
	return { w, emit: (p) => handlers.forEach((fn) => fn(p)) };
}

const names = (w) => w.findAll(".row").map((r) => r.text());
const refreshBtn = (w) =>
	w.findAllComponents({ name: "Button" }).find((b) => b.props("tooltip") === "Refresh");
const footer = (w) => w.findComponent({ name: "ListFooter" });

beforeEach(() => {
	vi.clearAllMocks();
	api.macroRunStats.mockResolvedValue({});
	api.listMacros.mockResolvedValue([]);
});

describe("RunsTab: a load that failed", () => {
	it("says the load failed, with the reason, not 'No macro runs yet'", async () => {
		api.listMacroRuns.mockRejectedValue(new Error("The server is not reachable."));
		const { w } = mountTab();
		await flushPromises();
		expect(w.text()).toContain("Couldn't load macro runs");
		expect(w.text()).toContain("The server is not reachable.");
		expect(w.text()).not.toContain("No macro runs yet");
	});

	it("is replaced by the list once a refresh works", async () => {
		api.listMacroRuns.mockRejectedValueOnce(new Error("The server is not reachable."));
		const { w } = mountTab();
		await flushPromises();
		api.listMacroRuns.mockResolvedValue(page(runs(0, 2), false, 2));
		await refreshBtn(w).trigger("click");
		await flushPromises();
		expect(w.text()).not.toContain("Couldn't load macro runs");
		expect(names(w)).toEqual(["run-000", "run-001"]);
	});

	it("keeps the rows already shown when a later refresh fails", async () => {
		api.listMacroRuns.mockResolvedValueOnce(page(runs(0, 2), false, 2));
		const { w, emit } = mountTab();
		await flushPromises();
		api.listMacroRuns.mockRejectedValue(new Error("The server is not reachable."));
		emit({ kind: "macro:done" });
		await flushPromises();
		expect(names(w)).toEqual(["run-000", "run-001"]);
		expect(w.text()).not.toContain("Couldn't load macro runs");
	});

	it("an empty list that loaded fine still reads 'No macro runs yet'", async () => {
		api.listMacroRuns.mockResolvedValue(page([], false, 0));
		const { w } = mountTab();
		await flushPromises();
		expect(w.text()).toContain("No macro runs yet");
	});
});

describe("RunsTab: a realtime refresh that overtakes a load", () => {
	it("does not leave the spinner on (and the pane blank) for good", async () => {
		// The first load is in flight when a run ends; its response is dropped as
		// stale. The refresh that replaced it is a silent one, and used to never
		// clear the spinner the first load had set.
		const first = deferred();
		const second = deferred();
		api.listMacroRuns.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
		const { w, emit } = mountTab();
		await flushPromises();
		expect(refreshBtn(w).props("loading")).toBe(true);

		emit({ kind: "macro:done" });
		first.resolve(page([], false, 0));
		await flushPromises();
		second.resolve(page([], false, 0));
		await flushPromises();

		expect(refreshBtn(w).props("loading")).toBe(false);
		expect(w.text()).toContain("No macro runs yet");
	});
});

describe("RunsTab: a realtime refresh with more than 100 rows loaded", () => {
	// 120 rows on screen: the first page of 100, then Load More for 20.
	async function with120() {
		api.listMacroRuns
			.mockResolvedValueOnce(page(runs(0, 100), true, 300))
			.mockResolvedValueOnce(page(runs(100, 120), true, 300));
		const mounted = mountTab();
		await flushPromises();
		footer(mounted.w).vm.$emit("loadMore");
		await flushPromises();
		expect(names(mounted.w)).toHaveLength(120);
		return mounted;
	}

	it("asks for the first page only, and keeps the rows loaded beyond it", async () => {
		const { w, emit } = await with120();
		// Two new runs since; the server caps a page at 100.
		const fresh = [run("new-a"), run("new-b"), ...runs(0, 98)];
		fresh[7] = { ...fresh[7], status: "failed" }; // run-005 changed
		api.listMacroRuns.mockResolvedValueOnce(page(fresh, true, 302));
		emit({ kind: "macro:progress" });
		await flushPromises();

		expect(api.listMacroRuns).toHaveBeenLastCalledWith(
			expect.objectContaining({ limit: 100, start: 0 })
		);
		const shown = names(w);
		expect(shown).toHaveLength(122);
		// Newest first, nothing twice, the old tail still in its place.
		expect(shown.slice(0, 3)).toEqual(["run-new-a", "run-new-b", "run-000"]);
		expect(shown.slice(-2)).toEqual(["run-118", "run-119"]);
		expect(new Set(shown).size).toBe(122);
		expect(shown).toEqual([...fresh.map((r) => r.name), ...runs(98, 120).map((r) => r.name)]);
		// The refreshed page's copy of a row wins.
		expect(w.findAll(".row")[7].attributes("data-status")).toBe("failed");
		expect(footer(w).props("options")).toEqual({ rowCount: 122, totalCount: 302 });
	});

	it("Load More still continues from the end of the list afterwards", async () => {
		const { w, emit } = await with120();
		api.listMacroRuns.mockResolvedValueOnce(page(runs(0, 100), true, 300));
		emit({ kind: "macro:progress" });
		await flushPromises();
		expect(names(w)).toHaveLength(120);

		api.listMacroRuns.mockResolvedValueOnce(page(runs(120, 140), true, 300));
		footer(w).vm.$emit("loadMore");
		await flushPromises();
		expect(api.listMacroRuns).toHaveBeenLastCalledWith(
			expect.objectContaining({ start: 120 })
		);
		expect(names(w)).toHaveLength(140);
	});

	it("a row that left the first page is gone, not kept as a stale copy", async () => {
		const { w, emit } = await with120();
		// run-003 no longer matches (deleted, or filtered out): the page closes up
		// and reaches one row further down.
		const fresh = runs(0, 101).filter((r) => r.name !== "run-003");
		api.listMacroRuns.mockResolvedValueOnce(page(fresh, true, 299));
		emit({ kind: "macro:done" });
		await flushPromises();
		const shown = names(w);
		expect(shown).not.toContain("run-003");
		expect(shown).toHaveLength(119);
		expect(shown.slice(-1)).toEqual(["run-119"]);
	});

	it("when the server says that page is everything, the list is that page", async () => {
		const { w, emit } = await with120();
		api.listMacroRuns.mockResolvedValueOnce(page(runs(0, 40), false, 40));
		emit({ kind: "macro:done" });
		await flushPromises();
		expect(names(w)).toHaveLength(40);
	});
});
