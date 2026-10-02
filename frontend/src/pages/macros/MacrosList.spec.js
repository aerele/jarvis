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
	Dropdown: { name: "Dropdown", props: ["options"], template: "<div />" },
}));
vi.mock("@/components/list/TabBar.vue", () => ({ default: { template: "<div />" } }));
vi.mock("./RunsTab.vue", () => ({ default: { name: "RunsTab", template: "<div />" } }));
vi.mock("@/components/list/ListPage.vue", () => ({
	default: {
		name: "ListPage",
		props: ["rows", "loading", "error", "quickFilters", "emptyState"],
		emits: ["refresh"],
		// Every row counts as selected: the bulk actions get all their names.
		computed: {
			selections() {
				return new Set((this.rows || []).map((r) => r.name));
			},
		},
		methods: { unselectAll() {} },
		template: `<div><div v-for="row in rows" :key="row.name" class="row" :data-name="row.name"><span class="schedule"><slot name="cell-schedule" :row="row" /></span><span class="run"><slot name="cell-_run" :row="row" /></span></div><slot name="select-actions" :selections="selections" :unselectAll="unselectAll" /></div>`,
	},
}));

import { toast, confirmDialog } from "frappe-ui";
import { deleteMacrosBulk } from "@/api/macros";
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

	it("the list's Try again (its refresh) loads the list", async () => {
		fetchPage.mockRejectedValue(new Error("The server is not reachable."));
		const { w } = await mountList(null);
		fetchPage.mockResolvedValue({ rows: [macro("close")], total: 1, has_more: false });
		listPage(w).vm.$emit("refresh");
		await flushPromises();
		expect(listPage(w).props("error")).toBe("");
		expect(
			listPage(w)
				.props("rows")
				.map((r) => r.name)
		).toEqual(["close"]);
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

describe("MacrosList: deleting macros that are running", () => {
	// The server stops a live run before it deletes the macro. The dialog says so
	// when the list knows of one, and the toast when the server stopped one.
	const openBulkDelete = async (rows) => {
		const { w } = await mountList(rows);
		const del = w
			.findComponent({ name: "Dropdown" })
			.props("options")
			.find((o) => o.label === "Delete");
		del.onClick();
		return confirmDialog.mock.calls.at(-1)[0];
	};
	const running = { last_run: { status: "running", error: "" } };
	const finished = { last_run: { status: "completed", error: "" } };

	it("one running macro: the confirmation says its run will be stopped", async () => {
		const dialog = await openBulkDelete([macro("live", running)]);
		expect(dialog.message).toContain("This macro is running. Deleting it stops the run.");
		expect(dialog.message).toContain("run history");
	});

	it("several selected: it says how many are running", async () => {
		const dialog = await openBulkDelete([
			macro("live", running),
			macro("parked", { last_run: { status: "waiting_capacity", error: "" } }),
			macro("idle", finished),
		]);
		expect(dialog.message).toContain("2 of these macros are running.");
	});

	it("nothing running: the confirmation is the plain one", async () => {
		const dialog = await openBulkDelete([macro("idle", finished), macro("never")]);
		expect(dialog.message).toBe(
			"Deletes the selected macros AND their run history. This can't be undone."
		);
	});

	it("the toast says how many runs were stopped", async () => {
		const dialog = await openBulkDelete([macro("live", running), macro("idle", finished)]);
		deleteMacrosBulk.mockResolvedValue({ deleted: 2, skipped: [], stopped_runs: 1 });
		await dialog.onConfirm({ hideDialog: vi.fn() });
		expect(deleteMacrosBulk).toHaveBeenCalledWith(["live", "idle"]);
		expect(toast.success).toHaveBeenCalledWith("Deleted 2 macros. 1 run was stopped.");
	});

	it("the toast is unchanged when nothing was stopped, or an older server answers", async () => {
		for (const answer of [{ deleted: 1, skipped: [], stopped_runs: 0 }, { deleted: 1 }]) {
			const dialog = await openBulkDelete([macro("idle", finished)]);
			deleteMacrosBulk.mockResolvedValue(answer);
			await dialog.onConfirm({ hideDialog: vi.fn() });
			expect(toast.success).toHaveBeenLastCalledWith("Deleted 1 macro");
		}
	});

	it("a macro that could not be stopped is reported as skipped, with the stops that did happen", async () => {
		const dialog = await openBulkDelete([macro("live", running), macro("busy", running)]);
		deleteMacrosBulk.mockResolvedValue({
			deleted: 1,
			skipped: [{ name: "busy", reason: "running" }],
			stopped_runs: 1,
		});
		await dialog.onConfirm({ hideDialog: vi.fn() });
		expect(toast.create).toHaveBeenCalledWith({
			message: "Deleted 1 (skipped 1: running). 1 run was stopped.",
			type: "info",
		});
	});
});
