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
vi.mock("@/api/macros", () => ({ deleteMacrosBulk: vi.fn(), dismissMacroNotices: vi.fn() }));
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
	toast: { success: vi.fn(), error: vi.fn(), create: vi.fn(), remove: vi.fn() },
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
	Tooltip: {
		props: ["text"],
		template: `<span class="tooltip" :data-text="text"><slot /></span>`,
	},
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
		template: `<div><slot name="banner" /><div v-for="row in rows" :key="row.name" class="row" :data-name="row.name"><span class="name"><slot name="cell-macro_name" :row="row" /></span><span class="schedule"><slot name="cell-schedule" :row="row" /></span><span class="run"><slot name="cell-_run" :row="row" /></span></div><slot name="select-actions" :selections="selections" :unselectAll="unselectAll" /></div>`,
	},
}));

import { toast, confirmDialog } from "frappe-ui";
import { deleteMacrosBulk, dismissMacroNotices } from "@/api/macros";
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

	it("a macro that was not deleted is named, with why and the stops that did happen", async () => {
		// It used to read "Deleted 1 (skipped 1: running)": no name, nothing to do.
		const dialog = await openBulkDelete([macro("live", running), macro("busy", running)]);
		deleteMacrosBulk.mockResolvedValue({
			deleted: 1,
			skipped: [
				{
					name: "busy",
					title: "Busy <b>macro</b>",
					reason: "running",
					message:
						"Its run was stopped, but another run started before it could be deleted. Delete it again.",
				},
			],
			stopped_runs: 4,
		});
		await dialog.onConfirm({ hideDialog: vi.fn() });
		expect(toast.create).toHaveBeenCalledWith({
			message:
				"Deleted 1 of 2. Not deleted: “Busy &lt;b&gt;macro&lt;/b&gt;”: Its run was stopped, " +
				"but another run started before it could be deleted. Delete it again. 4 runs were stopped.",
			type: "info",
		});
	});

	it("a second press of Confirm does not start a second delete", async () => {
		const dialog = await openBulkDelete([macro("live", running)]);
		let land;
		deleteMacrosBulk.mockReturnValue(new Promise((resolve) => (land = resolve)));
		const hideDialog = vi.fn();
		const first = dialog.onConfirm({ hideDialog });
		expect(hideDialog).toHaveBeenCalledTimes(1); // closed at once
		await dialog.onConfirm({ hideDialog });
		expect(deleteMacrosBulk).toHaveBeenCalledTimes(1);
		land({ deleted: 1, skipped: [], stopped_runs: 1 });
		await first;
		expect(toast.success).toHaveBeenCalledTimes(1);
	});

	it("a slow bulk delete says what it is doing", async () => {
		vi.useFakeTimers();
		try {
			const dialog = await openBulkDelete([macro("live", running)]);
			let land;
			deleteMacrosBulk.mockReturnValue(new Promise((resolve) => (land = resolve)));
			toast.create.mockReturnValue("note-1");
			const going = dialog.onConfirm({ hideDialog: vi.fn() });
			vi.advanceTimersByTime(1000);
			expect(toast.create).toHaveBeenCalledWith(
				expect.objectContaining({ message: "Stopping runs and deleting macros..." })
			);
			land({ deleted: 1, skipped: [], stopped_runs: 1 });
			await going;
			expect(toast.remove).toHaveBeenCalledWith("note-1");
		} finally {
			vi.useRealTimers();
		}
	});

	it("a run that starts while the list is open is learned of, once", async () => {
		const { emit } = await mountList([macro("idle", finished), macro("live", running)]);
		fetchPage.mockClear();
		emit({ kind: "macro:progress", macro: "not-on-this-page", step: 1 });
		emit({ kind: "macro:progress", macro: "live", step: 2 });
		await flushPromises();
		expect(fetchPage).not.toHaveBeenCalled();
		emit({ kind: "macro:progress", macro: "idle", step: 1 });
		await flushPromises();
		expect(fetchPage).toHaveBeenCalledTimes(1);
	});
});

describe("MacrosList: an admin's hold and delete", () => {
	const HELD_SAYS =
		"An admin has put this macro on hold: Paused for review. It will not run until an admin releases it.";
	const held = (name) =>
		macro(name, { enabled: 0, admin_hold: 1, admin_hold_reason: "Paused for review" });
	const notice = (name, message) => ({ name, message, creation: "2026-10-03 10:00:00" });

	it("badges a held macro On hold, with the reason, and keeps its Run off", async () => {
		const { w } = await mountList([held("close"), macro("open")]);
		const badge = rowEl(w, "close").find(".name .jv-macro-held");
		expect(badge.exists()).toBe(true);
		expect(badge.text()).toBe("On hold");
		expect(rowEl(w, "close").find(".name .tooltip:last-of-type").attributes("data-text")).toBe(
			HELD_SAYS
		);
		expect(runBtn(w, "close").props("disabled")).toBe(true);
		expect(runBtn(w, "close").props("tooltip")).toBe(HELD_SAYS);
		await runBtn(w, "close").trigger("click");
		expect(api.runMacro).not.toHaveBeenCalled();
		expect(rowEl(w, "open").find(".jv-macro-held").exists()).toBe(false);
		expect(runBtn(w, "open").props("disabled")).toBe(false);
	});

	it("shows the admin-delete notices that came with the list, as text", async () => {
		fetchPage.mockResolvedValue({
			rows: [],
			total: 0,
			has_more: false,
			notices: [
				notice("N1", "An admin deleted your macro <b>Close</b>."),
				notice("N2", "An admin deleted your macro Open."),
			],
		});
		const { w } = await mountList(null);
		const lines = w.findAll(".jv-macro-notice");
		expect(lines.map((l) => l.attributes("data-notice"))).toEqual(["N1", "N2"]);
		expect(lines[0].text()).toContain("An admin deleted your macro <b>Close</b>.");
		expect(lines[0].find("b").exists()).toBe(false);
	});

	it("an answer without the notices key leaves the notices shown alone", async () => {
		fetchPage.mockResolvedValue({
			rows: [],
			total: 0,
			has_more: false,
			notices: [notice("N1", "An admin deleted your macro Close.")],
		});
		const { w } = await mountList(null);
		expect(w.findAll(".jv-macro-notice")).toHaveLength(1);
		fetchPage.mockResolvedValue({ rows: [], total: 0, has_more: false });
		listPage(w).vm.$emit("refresh");
		await flushPromises();
		expect(fetchPage).toHaveBeenCalledTimes(2);
		expect(w.findAll(".jv-macro-notice")).toHaveLength(1);
	});

	it("the badge's reason is in the text, for a keyboard or a screen reader", async () => {
		const { w } = await mountList([held("close")]);
		const said = rowEl(w, "close").find(".name .jv-macro-held-reason");
		expect(said.classes()).toContain("sr-only");
		expect(said.text()).toBe(HELD_SAYS);
	});

	it("shows no notice line when there is none, or from an older server", async () => {
		const { w } = await mountList([macro("a")]);
		expect(w.find('[data-testid="admin-notices"]').exists()).toBe(false);
	});

	it("dismisses a notice: marks it read on the server, then drops the line", async () => {
		fetchPage.mockResolvedValue({
			rows: [],
			total: 0,
			has_more: false,
			notices: [
				notice("N1", "An admin deleted your macro Close."),
				notice("N2", "Another."),
			],
		});
		let answer;
		dismissMacroNotices.mockReturnValue(new Promise((r) => (answer = r)));
		const { w } = await mountList(null);
		const dismissOf = (i) =>
			w.findAll(".jv-macro-notice")[i].findComponent({ name: "Button" });
		await dismissOf(0).trigger("click");
		expect(dismissMacroNotices).toHaveBeenCalledWith(["N1"]);
		// In flight: the line stays, and the other Dismiss waits.
		expect(w.findAll(".jv-macro-notice")).toHaveLength(2);
		expect(dismissOf(0).props("loading")).toBe(true);
		expect(dismissOf(1).props("disabled")).toBe(true);
		answer({ ok: true, dismissed: 1 });
		await flushPromises();
		expect(w.findAll(".jv-macro-notice").map((l) => l.attributes("data-notice"))).toEqual([
			"N2",
		]);
	});

	it("keeps the notice and says why when the dismiss fails", async () => {
		fetchPage.mockResolvedValue({
			rows: [],
			total: 0,
			has_more: false,
			notices: [notice("N1", "An admin deleted your macro Close.")],
		});
		dismissMacroNotices.mockRejectedValue(new Error("Server is busy"));
		const { w } = await mountList(null);
		await w.find(".jv-macro-notice").findComponent({ name: "Button" }).trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalled();
		expect(w.findAll(".jv-macro-notice")).toHaveLength(1);
	});
});

describe("MacrosList: the Armed badge", () => {
	it("says what armed means, and not that an admin set it: its owner does", async () => {
		const { w } = await mountList([macro("armed", { skip_confirmation: 1 }), macro("plain")]);
		const tip = rowEl(w, "armed").find(".tooltip");
		expect(tip.attributes("data-text")).toBe(
			"Armed: this macro's runs write without asking for confirmation."
		);
		expect(tip.text()).toBe("Armed");
		expect(rowEl(w, "plain").find(".tooltip").exists()).toBe(false);
	});
});
