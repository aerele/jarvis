import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The Macros list page. useListPage is the real one (the page's wiring to it is
 * what the first test is about); ListPage is a stub that records its props and
 * renders the cells this file looks at.
 */

const api = vi.hoisted(() => ({
	runMacro: vi.fn(),
	getListFilterSchema: vi.fn(async () => ({})),
	getListFilterCapabilities: vi.fn(async () => ({})),
}));
vi.mock("@/api", () => api);
vi.mock("@/api/macros", () => ({ deleteMacrosBulk: vi.fn() }));
const fetchPage = vi.hoisted(() => vi.fn());
vi.mock("@/pages/list/listFetchers", () => ({ macrosListFetch: fetchPage }));

const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("vue-router", async () => {
	const { reactive } = await import("vue");
	const route = reactive({ name: "MacrosList", params: {}, query: {} });
	return { useRoute: () => route, useRouter: () => router };
});
// jsdom has no usable localStorage for useStorage; a plain ref is all the page needs.
vi.mock("@vueuse/core", async (importOriginal) => {
	const actual = await importOriginal();
	const { ref } = await import("vue");
	return { ...actual, useStorage: (_key, initial) => ref(initial) };
});
vi.mock("frappe-ui", () => ({
	toast: { success: vi.fn(), error: vi.fn(), create: vi.fn() },
	confirmDialog: vi.fn(),
	getConfig: () => "",
	dayjs: () => ({ format: () => "" }),
	dayjsLocal: (d) => ({
		format: () => String(d || ""),
		fromNow: () => "",
		isValid: () => !!d,
		valueOf: () => 0,
	}),
	Button: {
		name: "Button",
		props: ["label", "icon", "disabled", "loading", "tooltip", "variant", "iconLeft"],
		emits: ["click"],
		template: `<button :disabled="disabled" @click="$emit('click')">{{ label }}</button>`,
	},
	Badge: { props: ["label"], template: `<span class="badge">{{ label }}</span>` },
	Tooltip: { template: "<span><slot /></span>" },
	Dropdown: { template: "<div />" },
}));
vi.mock("@/components/list/TabBar.vue", () => ({ default: { template: "<div />" } }));
vi.mock("./RunsTab.vue", () => ({ default: { name: "RunsTab", template: "<div />" } }));
vi.mock("@/components/list/ListPage.vue", () => ({
	default: {
		name: "ListPage",
		props: ["rows", "loading", "error", "quickFilters", "emptyState"],
		template: `<div><div v-for="row in rows" :key="row.name" class="row" :data-name="row.name"><span class="schedule"><slot name="cell-schedule" :row="row" /></span><span class="run"><slot name="cell-_run" :row="row" /></span></div></div>`,
	},
}));

import { toast } from "frappe-ui";
import MacrosList from "./MacrosList.vue";

const macro = (name, extra = {}) => ({
	name,
	macro_name: name,
	enabled: 1,
	step_count: 2,
	merge_status: "",
	schedule_enabled: 0,
	...extra,
});

async function mountList(rows = []) {
	if (rows) fetchPage.mockResolvedValue({ rows, total: rows.length, has_more: false });
	const handlers = [];
	const socket = { on: (_e, fn) => handlers.push(fn), off: () => {} };
	const w = mount(MacrosList, { global: { provide: { $socket: socket } } });
	await flushPromises();
	return { w, emit: (p) => handlers.forEach((fn) => fn(p)) };
}

const listPage = (w) => w.findComponent({ name: "ListPage" });
const rowEl = (w, name) => w.find(`.row[data-name="${name}"]`);
const runBtn = (w, name) => rowEl(w, name).findComponent({ name: "Button" });

beforeEach(() => {
	vi.clearAllMocks();
});

describe("MacrosList: a load that failed", () => {
	it("hands the list its error, so it is not shown as 'No Macros Found'", async () => {
		// ListPage shows its error state ahead of the empty state, but only when it
		// is given the error: the page used not to pass it.
		fetchPage.mockRejectedValue(new Error("The server is not reachable."));
		const { w } = await mountList(null);
		expect(listPage(w).props("rows")).toEqual([]);
		expect(listPage(w).props("error")).toBe("The server is not reachable.");
	});

	it("a list that loaded carries no error, empty or not", async () => {
		const { w } = await mountList([]);
		expect(listPage(w).props("error")).toBe("");
		expect(listPage(w).props("emptyState").title).toBe("No Macros Found");
	});
});

describe("MacrosList: the two quick filters", () => {
	it("each says what its 'All' is, since a select shows no label", async () => {
		const { w } = await mountList([]);
		const byKey = Object.fromEntries(
			listPage(w)
				.props("quickFilters")
				.map((f) => [f.key, f])
		);
		expect(byKey.enabled.options[0]).toEqual({ label: "All statuses", value: "" });
		expect(byKey.schedule_enabled.options[0]).toEqual({ label: "All schedules", value: "" });
	});
});

describe("MacrosList: the Schedule cell", () => {
	it("prints the time the way the form does, on a 12-hour clock", async () => {
		const { w } = await mountList([
			macro("daily", {
				schedule_enabled: 1,
				schedule_frequency: "daily",
				schedule_time: "09:00:00",
			}),
			macro("weekly", {
				schedule_enabled: 1,
				schedule_frequency: "weekly",
				schedule_weekday: "Monday",
				schedule_time: "17:30:00",
			}),
			macro("manual"),
		]);
		expect(rowEl(w, "daily").find(".schedule").text()).toBe("Daily · 9:00 am");
		expect(rowEl(w, "weekly").find(".schedule").text()).toBe("Weekly on Monday · 5:30 pm");
		expect(rowEl(w, "manual").find(".schedule").text()).toBe("-");
	});
});

describe("MacrosList: the row's Run button", () => {
	it("is off, with the reason, while the macro's summary is being written", async () => {
		const { w } = await mountList([
			macro("ready"),
			macro("busy", { merge_status: "pending" }),
		]);
		expect(runBtn(w, "busy").props("disabled")).toBe(true);
		expect(runBtn(w, "busy").props("tooltip")).toContain("Summarizing");
		await runBtn(w, "busy").trigger("click");
		expect(api.runMacro).not.toHaveBeenCalled();
		expect(runBtn(w, "ready").props("disabled")).toBe(false);
		expect(runBtn(w, "ready").props("tooltip")).toBe("Run");
	});

	it("starts the run and hands over to its chat", async () => {
		api.runMacro.mockResolvedValue({ data: { conversation: "conv-1" } });
		const { w } = await mountList([macro("ready")]);
		await runBtn(w, "ready").trigger("click");
		await flushPromises();
		expect(api.runMacro).toHaveBeenCalledWith("ready");
		expect(router.push).toHaveBeenCalledWith("/c/conv-1");
	});

	it("holds every other row's Run while one is starting", async () => {
		api.runMacro.mockReturnValue(new Promise(() => {}));
		const { w } = await mountList([macro("first"), macro("second")]);
		await runBtn(w, "first").trigger("click");
		expect(runBtn(w, "first").props("loading")).toBe(true);
		expect(runBtn(w, "second").props("disabled")).toBe(true);
	});

	it("unlocks when the summary lands", async () => {
		const { w, emit } = await mountList([macro("busy", { merge_status: "pending" })]);
		fetchPage.mockResolvedValue({
			rows: [macro("busy", { merge_status: "ready" })],
			total: 1,
			has_more: false,
		});
		emit({ kind: "macro:merged", macro: "busy", macro_name: "busy", status: "ready" });
		await flushPromises();
		expect(runBtn(w, "busy").props("disabled")).toBe(false);
	});
});

describe("MacrosList: a macro's name in a toast is text, not markup", () => {
	// frappe-ui's Toast binds its message with v-html.
	const NAME = 'Close <img src=x onerror="go()"> & go';
	const SAFE = "Close &lt;img src=x onerror=&quot;go()&quot;&gt; &amp; go";

	it("the 'Running' toast", async () => {
		api.runMacro.mockResolvedValue({ data: {} });
		const { w } = await mountList([macro("m1", { macro_name: NAME })]);
		await runBtn(w, "m1").trigger("click");
		await flushPromises();
		expect(toast.success).toHaveBeenCalledWith(`Running “${SAFE}”`);
	});

	it("the 'Summary ready' toast", async () => {
		const { emit } = await mountList([]);
		emit({ kind: "macro:merged", macro: "m1", macro_name: NAME, status: "ready" });
		const [message] = toast.success.mock.calls[0];
		expect(message).toContain(SAFE);
		expect(message).not.toContain("<img");
	});

	it("the 'could not summarize' toast", async () => {
		const { emit } = await mountList([]);
		emit({ kind: "macro:merged", macro: "m1", macro_name: NAME, status: "failed" });
		const { message } = toast.create.mock.calls[0][0];
		expect(message).toContain(SAFE);
		expect(message).not.toContain("<img");
	});
});
