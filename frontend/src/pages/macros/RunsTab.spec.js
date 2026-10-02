import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The Runs tab's own list machinery (it does not use useListPage):
 *  - a failed load must say so, not "No macro runs yet", and offer "Try again";
 *  - the spinner must not stick when a realtime refresh arrives during a load;
 *  - a realtime refresh never supersedes a load the user asked for, so rows are
 *    only ever merged with rows fetched under the same filters;
 *  - a realtime refresh re-fetches every loaded page (the server returns at most
 *    100 rows a page), so nothing shrinks and nothing stale survives.
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
		props: ["tooltip", "icon", "loading", "variant", "theme", "label"],
		emits: ["click"],
		template: `<button :data-tooltip="tooltip" @click="$emit('click')">{{ label }}</button>`,
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

import { toast } from "frappe-ui";
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
const tryAgainBtn = (w) =>
	w.findAllComponents({ name: "Button" }).find((b) => b.props("label") === "Try again");
const footer = (w) => w.findComponent({ name: "ListFooter" });
const setStatus = (w, v) =>
	w.findAllComponents({ name: "FormControl" })[0].vm.$emit("update:modelValue", v);

// What list_macro_runs would answer for `args` over `all` (newest first).
function serve(all, { status, limit, start }) {
	const match = all.filter((r) => !status || r.status === status);
	return page(match.slice(start, start + limit), start + limit < match.length, match.length);
}

// Every request waits until the test answers it, so the test decides the order
// in which responses land. `server()` is read when the answer is given.
function gated(server) {
	const sent = [];
	api.listMacroRuns.mockImplementation((args) => {
		const d = deferred();
		sent.push({
			args,
			answer: () => d.resolve(serve(server(), args)),
			fail: (message) => d.reject(new Error(message)),
		});
		return d.promise;
	});
	return sent;
}

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

	it("offers Try again, which loads the list", async () => {
		api.listMacroRuns.mockRejectedValueOnce(new Error("The server is not reachable."));
		const { w } = mountTab();
		await flushPromises();
		api.listMacroRuns.mockResolvedValue(page(runs(0, 2), false, 2));
		await tryAgainBtn(w).trigger("click");
		await flushPromises();
		expect(names(w)).toEqual(["run-000", "run-001"]);
		expect(tryAgainBtn(w)).toBeUndefined();
	});

	it("takes the error down while the retry is in flight", async () => {
		api.listMacroRuns.mockRejectedValueOnce(new Error("The server is not reachable."));
		const { w } = mountTab();
		await flushPromises();
		api.listMacroRuns.mockReturnValueOnce(deferred().promise);
		await tryAgainBtn(w).trigger("click");
		await flushPromises();
		expect(w.text()).not.toContain("Couldn't load macro runs");
		expect(w.text()).not.toContain("No macro runs yet");
		expect(refreshBtn(w).props("loading")).toBe(true);
	});

	it("a realtime refresh that fails does not flash the empty state over the error", async () => {
		api.listMacroRuns.mockRejectedValue(new Error("The server is not reachable."));
		const { w, emit } = mountTab();
		await flushPromises();
		api.listMacroRuns.mockReturnValueOnce(deferred().promise);
		emit({ kind: "macro:progress" });
		await flushPromises();
		expect(w.text()).toContain("Couldn't load macro runs");
		expect(w.text()).not.toContain("No macro runs yet");
	});

	it("says a failing realtime refresh once, not once per event", async () => {
		api.listMacroRuns.mockResolvedValueOnce(page(runs(0, 2), false, 2));
		const { w, emit } = mountTab();
		await flushPromises();
		api.listMacroRuns.mockRejectedValue(new Error("The server is not reachable."));
		for (let i = 0; i < 4; i++) {
			emit({ kind: "macro:progress" });
			await flushPromises();
		}
		expect(toast.error).toHaveBeenCalledTimes(1);
		expect(names(w)).toEqual(["run-000", "run-001"]);

		// Once a refresh works again, the next outage is news.
		api.listMacroRuns.mockResolvedValueOnce(page(runs(0, 2), false, 2));
		emit({ kind: "macro:progress" });
		await flushPromises();
		api.listMacroRuns.mockRejectedValue(new Error("The server is not reachable."));
		emit({ kind: "macro:progress" });
		await flushPromises();
		expect(toast.error).toHaveBeenCalledTimes(2);
	});

	it("still says a failure the user asked for, every time", async () => {
		api.listMacroRuns.mockRejectedValue(new Error("The server is not reachable."));
		const { w } = mountTab();
		await flushPromises();
		await tryAgainBtn(w).trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledTimes(2);
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

describe("RunsTab: a realtime refresh never overtakes a load the user asked for", () => {
	// 60 runs, every third one failed: 20 match the "Failed" filter.
	const sixty = () =>
		Array.from({ length: 60 }, (_, i) => run(i, i % 3 === 2 ? { status: "failed" } : {}));
	const failedNames = sixty()
		.filter((r) => r.status === "failed")
		.map((r) => r.name);

	async function onFailedFilter() {
		const sent = gated(sixty);
		const mounted = mountTab();
		sent[0].answer();
		await flushPromises();
		setStatus(mounted.w, "failed");
		sent[1].answer();
		await flushPromises();
		expect(names(mounted.w)).toEqual(failedNames);
		return { ...mounted, sent };
	}

	it("a filter change: the refresh waits, and the old filter's rows are not merged in", async () => {
		const { w, emit, sent } = await onFailedFilter();
		setStatus(w, "");
		emit({ kind: "macro:progress" });
		emit({ kind: "macro:done" });
		await flushPromises();
		// Nothing was sent for the events: the filter change is still the request in flight.
		expect(sent).toHaveLength(3);
		expect(sent[2].args).toEqual(expect.objectContaining({ status: "", limit: 20, start: 0 }));

		sent[2].answer();
		await flushPromises();
		// Its answer is applied (not dropped as stale), then ONE refresh runs for
		// the two events, under the new filter.
		expect(sent).toHaveLength(4);
		expect(sent[3].args).toEqual(expect.objectContaining({ status: "", limit: 20, start: 0 }));
		sent[3].answer();
		await flushPromises();

		expect(names(w)).toEqual(runs(0, 20).map((r) => r.name));
		expect(footer(w).props("options")).toEqual({ rowCount: 20, totalCount: 60 });
		// Load More is alive and continues from the end of the list.
		footer(w).vm.$emit("loadMore");
		await flushPromises();
		expect(sent[4].args).toEqual(expect.objectContaining({ status: "", start: 20 }));
	});

	it("a Load More: the page asked for is added, then the refresh runs", async () => {
		const sent = gated(sixty);
		const { w, emit } = mountTab();
		sent[0].answer();
		await flushPromises();
		footer(w).vm.$emit("loadMore");
		emit({ kind: "macro:progress" });
		await flushPromises();
		expect(sent).toHaveLength(2);

		sent[1].answer();
		await flushPromises();
		expect(names(w)).toHaveLength(40);
		// The deferred refresh covers everything now on screen.
		expect(sent[2].args).toEqual(expect.objectContaining({ limit: 40, start: 0 }));
		sent[2].answer();
		await flushPromises();
		expect(names(w)).toEqual(runs(0, 40).map((r) => r.name));
	});

	it("a filter change while a refresh is in flight wins, and the refresh's late answer is ignored", async () => {
		const { w, emit, sent } = await onFailedFilter();
		emit({ kind: "macro:progress" }); // sent[2]: a refresh under "Failed"
		await flushPromises();
		setStatus(w, ""); // sent[3]
		sent[3].answer();
		await flushPromises();
		sent[2].answer(); // late, and for the old filter
		await flushPromises();
		expect(names(w)).toEqual(runs(0, 20).map((r) => r.name));
		expect(refreshBtn(w).props("loading")).toBe(false);
	});

	it("a Load More clicked while a refresh is in flight is not lost, and the refresh runs again after it", async () => {
		const sent = gated(sixty);
		const { w, emit } = mountTab();
		sent[0].answer();
		await flushPromises();
		emit({ kind: "macro:progress" }); // sent[1]: a refresh
		await flushPromises();
		footer(w).vm.$emit("loadMore"); // sent[2]
		await flushPromises();
		sent[1].answer(); // the superseded refresh: ignored
		sent[2].answer();
		await flushPromises();
		expect(names(w)).toHaveLength(40);
		expect(sent[3].args).toEqual(expect.objectContaining({ limit: 40, start: 0 }));
	});

	it("events during a refresh are folded into one more refresh, not one each", async () => {
		const sent = gated(sixty);
		const { emit } = mountTab();
		sent[0].answer();
		await flushPromises();
		emit({ kind: "macro:progress" });
		emit({ kind: "macro:progress" });
		emit({ kind: "macro:done" });
		await flushPromises();
		expect(sent).toHaveLength(2);
		sent[1].answer();
		await flushPromises();
		expect(sent).toHaveLength(3);
		sent[2].answer();
		await flushPromises();
		expect(sent).toHaveLength(3);
	});

	it("a filter change that fails does not leave the old filter's rows under the new one", async () => {
		const { w, sent } = await onFailedFilter();
		setStatus(w, "completed");
		sent[2].fail("The server is not reachable.");
		await flushPromises();
		expect(names(w)).toEqual([]);
		expect(w.text()).toContain("Couldn't load macro runs");
	});
});

describe("RunsTab: a realtime refresh with more than 100 rows loaded", () => {
	// 120 rows on screen: a first page of 20, then Load More five times.
	async function with120(all) {
		const state = { all };
		const loading = gated(() => state.all);
		const mounted = mountTab();
		loading[0].answer();
		await flushPromises();
		for (let n = 1; n <= 5; n++) {
			footer(mounted.w).vm.$emit("loadMore");
			await flushPromises();
			loading[n].answer();
			await flushPromises();
		}
		expect(names(mounted.w)).toHaveLength(120);
		// What is sent from here on, numbered from 0.
		const sent = gated(() => state.all);
		return { ...mounted, sent, state };
	}

	it("re-fetches every loaded page, 100 rows at a time, one after the other", async () => {
		const { w, emit, sent, state } = await with120(runs(0, 300));
		// Two new runs since, and run-005 failed.
		state.all = [run("new-a"), run("new-b"), ...runs(0, 300)];
		state.all[7] = { ...state.all[7], status: "failed" };
		emit({ kind: "macro:progress" });
		await flushPromises();
		expect(sent).toHaveLength(1);
		expect(sent[0].args).toEqual({ status: "", macro: "", limit: 100, start: 0 });
		sent[0].answer();
		await flushPromises();
		expect(sent).toHaveLength(2);
		expect(sent[1].args).toEqual({ status: "", macro: "", limit: 20, start: 100 });
		sent[1].answer();
		await flushPromises();

		const shown = names(w);
		// Still 120 rows: nothing shrank, nothing twice, the server's order.
		expect(shown).toEqual(state.all.slice(0, 120).map((r) => r.name));
		expect(w.findAll(".row")[7].attributes("data-status")).toBe("failed");
		expect(footer(w).props("options")).toEqual({ rowCount: 120, totalCount: 302 });
	});

	it("a row beyond the first 100 is refreshed too, and a deleted one goes", async () => {
		const all = runs(0, 120);
		all[110] = { ...all[110], status: "running" };
		const { w, emit, sent, state } = await with120(all);
		expect(w.findAll(".row")[110].attributes("data-status")).toBe("running");
		// The run finished; runs 112 to 119 were deleted with their macro.
		state.all = all.slice(0, 112).map((r) => ({ ...r, status: "completed" }));
		emit({ kind: "macro:done" });
		await flushPromises();
		sent[0].answer();
		await flushPromises();
		sent[1].answer();
		await flushPromises();

		expect(names(w)).toEqual(runs(0, 112).map((r) => r.name));
		expect(w.findAll(".row")[110].attributes("data-status")).toBe("completed");
		// The count is the server's: never "120 of 112".
		expect(footer(w).props("options")).toEqual({ rowCount: 112, totalCount: 112 });
	});

	it("stops paging when the server says there is no more", async () => {
		const { w, emit, sent, state } = await with120(runs(0, 300));
		state.all = runs(0, 40);
		emit({ kind: "macro:done" });
		await flushPromises();
		sent[0].answer();
		await flushPromises();
		expect(sent).toHaveLength(1);
		expect(names(w)).toHaveLength(40);
	});

	it("a row that moved between two pages while they were fetched is shown once", async () => {
		const { w, emit, sent, state } = await with120(runs(0, 300));
		emit({ kind: "macro:progress" });
		await flushPromises();
		sent[0].answer();
		await flushPromises();
		// A run starts between the two requests: run-099 slides onto the second page.
		state.all = [run("new-a"), ...runs(0, 300)];
		sent[1].answer();
		await flushPromises();
		const shown = names(w);
		expect(new Set(shown).size).toBe(shown.length);
		expect(shown.filter((n) => n === "run-099")).toHaveLength(1);
	});

	it("Load More still continues from the end of the list afterwards", async () => {
		const { w, emit, sent } = await with120(runs(0, 300));
		emit({ kind: "macro:progress" });
		await flushPromises();
		sent[0].answer();
		await flushPromises();
		sent[1].answer();
		await flushPromises();
		expect(names(w)).toHaveLength(120);

		footer(w).vm.$emit("loadMore");
		await flushPromises();
		expect(sent[2].args).toEqual(expect.objectContaining({ start: 120 }));
		sent[2].answer();
		await flushPromises();
		expect(names(w)).toHaveLength(140);
	});

	it("a filter change part-way through the pages stops the refresh", async () => {
		const { w, emit, sent } = await with120(runs(0, 300));
		emit({ kind: "macro:progress" });
		await flushPromises();
		setStatus(w, "failed"); // sent[1], while the refresh's first page is in flight
		sent[0].answer();
		await flushPromises();
		// No second page is asked for under the old filter.
		expect(sent).toHaveLength(2);
		expect(sent[1].args).toEqual(expect.objectContaining({ status: "failed", start: 0 }));
	});

	it("refreshes at most five pages: past that the list falls back to the first 500 rows", async () => {
		const sent = gated(() => runs(0, 900));
		const { w, emit } = mountTab();
		const answerLast = async () => {
			sent[sent.length - 1].answer();
			await flushPromises();
		};
		await answerLast();
		footer(w).vm.$emit("update:modelValue", 100); // 100 rows a page
		await flushPromises();
		await answerLast();
		while (names(w).length < 700) {
			footer(w).vm.$emit("loadMore");
			await flushPromises();
			await answerLast();
		}

		const before = sent.length;
		emit({ kind: "macro:done" });
		await flushPromises();
		for (let i = 0; i < 5; i++) {
			expect(sent).toHaveLength(before + i + 1);
			expect(sent[before + i].args).toEqual({
				status: "",
				macro: "",
				limit: 100,
				start: i * 100,
			});
			await answerLast();
		}
		expect(sent).toHaveLength(before + 5);
		expect(names(w)).toEqual(runs(0, 500).map((r) => r.name));
		expect(footer(w).props("options")).toEqual({ rowCount: 500, totalCount: 900 });
		// Load More carries on from there.
		footer(w).vm.$emit("loadMore");
		await flushPromises();
		expect(sent[sent.length - 1].args).toEqual(expect.objectContaining({ start: 500 }));
	});
});

describe("RunsTab: a realtime refresh of a short list", () => {
	it("asks for a full page, so a new run can appear", async () => {
		const state = { all: runs(0, 3) };
		const sent = gated(() => state.all);
		const { w, emit } = mountTab();
		sent[0].answer();
		await flushPromises();
		state.all = [run("new-a"), ...runs(0, 3)];
		emit({ kind: "macro:done" });
		await flushPromises();
		expect(sent[1].args).toEqual(expect.objectContaining({ limit: 20, start: 0 }));
		sent[1].answer();
		await flushPromises();
		expect(names(w)).toHaveLength(4);
	});
});
