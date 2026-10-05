import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The Jarvis Admin's Macros pane (Settings, Administration): every user's macros,
 * the filters, the loading / empty / error states, and the row actions: Stop run,
 * Hold (the reason is asked by MacroHoldDialog, stubbed here), Release and Delete.
 * The decisions behind the cells (lib/macroRunOutcome, lib/macroSchedule) are the
 * real ones; frappe-ui and the read-only dialog are stubs.
 */

vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label", "variant", "iconLeft", "loading", "disabled", "size", "theme"],
		emits: ["click"],
		template: `<button class="stub-button" :disabled="disabled" :data-loading="loading ? '1' : ''" :data-variant="variant" @click="$emit('click')">{{ label }}</button>`,
	},
	Badge: {
		name: "Badge",
		props: ["label", "theme"],
		template: `<span class="badge">{{ label }}</span>`,
	},
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: `<i class="stub-icon" />` },
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: `<span>{{ message }}</span>`,
	},
	FormControl: {
		name: "FormControl",
		props: ["type", "options", "modelValue", "label", "placeholder"],
		emits: ["update:modelValue"],
		template: `
			<label class="stub-control">
				<span class="stub-label">{{ label }}</span>
				<select v-if="type === 'select'" :value="modelValue"
					@change="$emit('update:modelValue', $event.target.value)">
					<option v-for="o in options" :key="o.value" :value="o.value" :disabled="o.disabled">{{ o.label }}</option>
				</select>
				<input v-else :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />
			</label>`,
	},
	toast: { success: vi.fn(), error: vi.fn(), create: vi.fn() },
}));

vi.mock("@/utils/datetime", () => ({
	timeAgo: (d) => (d ? "a while ago" : ""),
	exactDate: (d) => String(d || ""),
	toLocalMs: (d) => (d ? Date.parse(String(d).replace(" ", "T")) : null),
	formatTime12h: (t) => (t ? "9:00 am" : ""),
}));

const api = vi.hoisted(() => ({
	adminListMacros: vi.fn(),
	adminMacroOwners: vi.fn(),
	adminStopRun: vi.fn(),
	adminRelease: vi.fn(),
	adminDelete: vi.fn(),
}));
vi.mock("@/api/macrosAdmin", () => api);

const confirm = vi.hoisted(() => vi.fn());
vi.mock("@/composables/useConfirm", () => ({ useConfirm: () => ({ confirm }) }));

vi.mock("@/components/settings/MacroAdminDialog.vue", () => ({
	default: {
		name: "MacroAdminDialog",
		props: ["modelValue", "name"],
		emits: ["update:modelValue", "gone"],
		template: `<div class="stub-detail" :data-name="name" :data-open="modelValue ? '1' : ''" />`,
	},
}));

vi.mock("@/components/settings/MacroHoldDialog.vue", () => ({
	default: {
		name: "MacroHoldDialog",
		props: ["modelValue", "name", "macroName", "ownerLabel"],
		emits: ["update:modelValue", "held", "failed"],
		template: `<div class="stub-hold" :data-name="name" :data-open="modelValue ? '1' : ''" :data-owner="ownerLabel" />`,
	},
}));

vi.mock("@/components/settings/MacroHandoverDialog.vue", () => ({
	default: {
		name: "MacroHandoverDialog",
		props: ["modelValue", "name", "macroName", "owner", "ownerLabel"],
		emits: ["update:modelValue", "handed", "failed"],
		template: `<div class="stub-handover" :data-name="name" :data-open="modelValue ? '1' : ''" :data-macro="macroName" :data-owner="owner" :data-owner-label="ownerLabel" />`,
	},
}));

// Who is looking: an admin whose own macros have no Hold.
vi.mock("@/data/session", () => ({ session: { user: "admin@example.test" } }));

import { toast } from "frappe-ui";
import MacrosAdminPane from "./MacrosAdminPane.vue";

const row = (name, extra = {}) => ({
	name,
	macro_name: `Macro ${name}`,
	owner: "asha@example.test",
	owner_full_name: "Asha Rao",
	enabled: 1,
	skip_confirmation: 0,
	schedule_enabled: 0,
	schedule_frequency: "daily",
	schedule_time: "09:00:00",
	next_run_at: null,
	next_run_is_retry: 0,
	last_run: null,
	live_run: "",
	...extra,
});
const page = (rows, extra = {}) => ({
	rows,
	total: rows.length,
	has_more: false,
	start: 0,
	page_length: 20,
	...extra,
});
const OWNERS = [
	{ user: "asha@example.test", full_name: "Asha Rao", macros: 2 },
	{ user: "ben@example.test", full_name: "ben@example.test", macros: 1 },
];
// `n` rows named r0, r1, ...: a list long enough to page.
const many = (n, from = 0) => Array.from({ length: n }, (_, i) => row(`r${from + i}`));
// The server's paging over `all`: what a real list call answers for start/pageLength.
const serve = (all) => (q) =>
	Promise.resolve(
		page(all.slice(q.start, q.start + q.pageLength), {
			total: all.length,
			has_more: q.start + q.pageLength < all.length,
			start: q.start,
			page_length: q.pageLength,
		})
	);

async function mountWith(rows, extra, options) {
	api.adminListMacros.mockResolvedValue(page(rows, extra));
	const w = mount(MacrosAdminPane, options);
	await flushPromises();
	return w;
}

const button = (w, label) => w.findAll("button.stub-button").find((b) => b.text() === label);
const rowsOf = (w) => w.findAll(".jv-macro-admin-row");
const control = (w, label) =>
	w.findAll(".stub-control").find((c) => c.find(".stub-label").text() === label);
const lastCall = () => api.adminListMacros.mock.calls.at(-1)[0];

beforeEach(() => {
	vi.clearAllMocks();
	confirm.mockResolvedValue(true);
	api.adminListMacros.mockReset();
	api.adminMacroOwners.mockReset();
	api.adminMacroOwners.mockResolvedValue({ owners: OWNERS, more: false });
	api.adminRelease.mockResolvedValue({ ok: true, released: true });
	api.adminDelete.mockResolvedValue({ ok: true, deleted: true, stopped_runs: 0 });
	api.adminStopRun.mockResolvedValue({
		ok: true,
		stopped: true,
		status: "stopped",
		message: "",
	});
});
afterEach(() => {
	vi.useRealTimers();
});

describe("MacrosAdminPane, states", () => {
	it("says Loading until the first answer, then lists the macros", async () => {
		let answer;
		api.adminListMacros.mockReturnValue(new Promise((r) => (answer = r)));
		const w = mount(MacrosAdminPane);
		await flushPromises();
		expect(w.text()).toContain("Loading…");
		expect(rowsOf(w)).toHaveLength(0);
		answer(page([row("a")]));
		await flushPromises();
		expect(w.text()).not.toContain("Loading…");
		expect(rowsOf(w)).toHaveLength(1);
	});

	it("says so when nobody has a macro, with nothing to clear", async () => {
		const w = await mountWith([]);
		expect(w.text()).toContain("Nobody has made a macro yet.");
		expect(button(w, "Clear filters")).toBeUndefined();
	});

	it("says no macros MATCH when a filter is on, and Clear filters shows them all again", async () => {
		const w = await mountWith([row("a")]);
		api.adminListMacros.mockResolvedValue(page([]));
		await control(w, "Armed").find("select").setValue("1");
		await flushPromises();
		expect(w.text()).toContain("No macros match these filters.");

		api.adminListMacros.mockResolvedValue(page([row("a")]));
		await button(w, "Clear filters").trigger("click");
		await flushPromises();
		expect(lastCall().filters).toEqual({});
		expect(rowsOf(w)).toHaveLength(1);
	});

	it("shows the error with Try again when the first load fails, and recovers", async () => {
		api.adminListMacros.mockRejectedValue(new Error("You need the Jarvis Admin role"));
		const w = mount(MacrosAdminPane);
		await flushPromises();
		expect(w.find("[role='alert']").text()).toContain("You need the Jarvis Admin role");
		expect(w.text()).not.toContain("Nobody has made a macro yet.");

		api.adminListMacros.mockResolvedValue(page([row("a")]));
		await button(w, "Try again").trigger("click");
		await flushPromises();
		expect(w.find("[role='alert']").exists()).toBe(false);
		expect(rowsOf(w)).toHaveLength(1);
	});

	it("keeps the rows it had when a refresh fails, and says they are the earlier ones", async () => {
		const w = await mountWith([row("a"), row("b")]);
		api.adminListMacros.mockRejectedValue(new Error("Server is busy"));
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(2);
		const alert = w.find("[role='alert']");
		expect(alert.text()).toContain("Server is busy");
		expect(alert.text()).toContain("Showing what was loaded before.");

		api.adminListMacros.mockResolvedValue(page([row("a")]));
		await button(w, "Try again").trigger("click");
		await flushPromises();
		expect(w.find("[role='alert']").exists()).toBe(false);
		expect(rowsOf(w)).toHaveLength(1);
	});

	it("drops the answer to an earlier request that arrives late", async () => {
		const w = await mountWith([row("a")]);
		let slow;
		api.adminListMacros.mockReturnValueOnce(new Promise((r) => (slow = r)));
		await control(w, "Armed").find("select").setValue("1");
		api.adminListMacros.mockResolvedValueOnce(page([row("new")]));
		await control(w, "Armed").find("select").setValue("0");
		await flushPromises();
		slow(page([row("stale")]));
		await flushPromises();
		expect(rowsOf(w).map((r) => r.text())).toEqual([expect.stringContaining("Macro new")]);
	});
});

describe("MacrosAdminPane, rows", () => {
	it("shows the owner, the name, on or off, armed, the schedule and the last run", async () => {
		const w = await mountWith([
			row("a", {
				skip_confirmation: 1,
				schedule_enabled: 1,
				next_run_at: "2999-01-01 09:00:00",
				last_run: { status: "failed", started_at: "2026-01-01 09:00:00" },
			}),
			row("b", {
				enabled: 0,
				owner: "ben@example.test",
				owner_full_name: "ben@example.test",
			}),
		]);
		const [a, b] = rowsOf(w);
		expect(a.text()).toContain("Macro a");
		expect(a.text()).toContain("Asha Rao");
		expect(a.text()).toContain("asha@example.test");
		// The Macros list's own words for the switch (its Status filter).
		expect(a.findAll(".badge").map((x) => x.text())).toEqual(["Enabled", "Armed"]);
		expect(a.text()).toContain("Daily · 9:00 am");
		expect(a.text()).toContain("Failed");
		expect(b.findAll(".badge").map((x) => x.text())).toEqual(["Draft"]);
		expect(b.text()).toContain("Not scheduled");
		expect(b.text()).toContain("Never ran");
		// An owner with no full name is shown once, not twice.
		expect(b.text().split("ben@example.test")).toHaveLength(2);
	});

	it("gives every cell that can be cut short its full value on hover", async () => {
		const w = await mountWith([
			row("a", {
				macro_name: "A very long macro name that the column cuts short",
				owner: "a.very.long.address@a-long-domain.example.test",
				owner_full_name: "Ashalatha Venkataraman Rao",
				schedule_enabled: 1,
				schedule_frequency: "weekly",
				schedule_weekday: "Wednesday",
			}),
		]);
		const a = rowsOf(w)[0];
		expect(a.find(".jv-macro-admin-open").attributes("title")).toBe(
			"A very long macro name that the column cuts short"
		);
		const titles = a.findAll("[title]").map((el) => el.attributes("title"));
		expect(titles).toContain("Ashalatha Venkataraman Rao");
		expect(titles).toContain("a.very.long.address@a-long-domain.example.test");
		const schedule = a.find(".jv-macro-admin-schedule");
		expect(schedule.text()).toContain("Weekly");
		expect(schedule.attributes("title")).toBe(schedule.text());
	});

	it("is a table to a screen reader, in a box the keyboard can scroll", async () => {
		const w = await mountWith([row("a"), row("b")]);
		const table = w.find("[role='table']");
		expect(table.findAll("[role='columnheader']").map((h) => h.text())).toEqual([
			"Macro",
			"Owner",
			"Schedule",
			"Last run",
			"Actions",
		]);
		const lines = table.findAll("[role='row']");
		expect(lines).toHaveLength(3);
		expect(lines[1].findAll("[role='cell']")).toHaveLength(5);
		const box = w.find(".jv-macro-admin-scroll");
		expect(box.attributes("tabindex")).toBe("0");
		expect(box.attributes("aria-label")).toBe("Macros list");
		expect(box.classes()).toContain("overflow-x-auto");
		expect(box.find("[role='table']").exists()).toBe(true);
	});

	it("says in words that an administrator stopped the last run", async () => {
		const w = await mountWith([
			row("a", { last_run: { status: "stopped", stopped_by_admin: 1 } }),
			row("b", { last_run: { status: "stopped", stopped_by_admin: 0 } }),
		]);
		const [a, b] = rowsOf(w).map((r) => r.find(".jv-macro-admin-last"));
		expect(a.text()).toBe("Stopped by an admin");
		// A warning, as an engine stop is on the owner's list; the owner's own Stop is not.
		expect(a.find(".text-ink-amber-3").exists()).toBe(true);
		expect(b.text()).toBe("Stopped");
		expect(b.find(".text-ink-amber-3").exists()).toBe(false);
	});

	it("says Running beside Stop when the live run is an older one", async () => {
		// The last run failed; an earlier one is still going, and that is what Stop
		// acts on. The row read "Failed" next to a Stop button with no explanation.
		const w = await mountWith([
			row("a", { live_run: "RUN-0", last_run: { status: "failed" } }),
			row("b", { live_run: "RUN-1", last_run: { status: "running" } }),
			row("c", { last_run: { status: "failed" } }),
		]);
		const [a, b, c] = rowsOf(w);
		expect(a.find(".jv-macro-admin-last").text()).toBe("Failed");
		expect(a.find(".jv-macro-admin-running").text()).toBe("Running");
		// Its last-run cell already says so.
		expect(b.find(".jv-macro-admin-running").exists()).toBe(false);
		expect(b.find(".jv-macro-admin-last").text()).toBe("Running");
		expect(c.find(".jv-macro-admin-running").exists()).toBe(false);
	});

	it("offers Stop run only on a row with a live run", async () => {
		const w = await mountWith([
			row("a", { live_run: "RUN-1", last_run: { status: "running" } }),
			row("b", { last_run: { status: "completed" } }),
		]);
		const [a, b] = rowsOf(w);
		expect(a.find(".jv-macro-admin-stop").exists()).toBe(true);
		expect(a.text()).toContain("Running");
		expect(b.find(".jv-macro-admin-stop").exists()).toBe(false);
	});

	it("opens a row in the read-only dialog, from a real button that names it", async () => {
		const w = await mountWith([row("a")]);
		expect(w.find(".stub-detail").exists()).toBe(false);
		const open = rowsOf(w)[0].find("button.jv-macro-admin-open");
		expect(open.attributes("type")).toBe("button");
		expect(open.attributes("aria-label")).toBe(
			"Open Macro a, owned by Asha Rao (asha@example.test). Read-only."
		);
		await open.trigger("click");
		const detail = w.find(".stub-detail");
		expect(detail.attributes("data-name")).toBe("a");
		expect(detail.attributes("data-open")).toBe("1");
	});

	it("loads the next page after the rows it has, and never lists a macro twice", async () => {
		const w = await mountWith([row("a"), row("b")], { total: 3, has_more: true });
		expect(w.text()).toContain("Showing 2 of 3");
		api.adminListMacros.mockResolvedValue(page([row("b"), row("c")], { total: 3, start: 2 }));
		await button(w, "Load more").trigger("click");
		await flushPromises();
		expect(lastCall().start).toBe(2);
		expect(rowsOf(w)).toHaveLength(3);
		expect(w.text()).toContain("Showing 3 of 3");
		expect(button(w, "Load more")).toBeUndefined();
	});

	it("re-reads every row on screen on Refresh, however many were loaded", async () => {
		// It asked for one page of at most 100, so past 100 rows a Refresh (or a
		// stop) cut the list back to its first 100.
		const all = many(130);
		api.adminListMacros.mockImplementation(serve(all));
		const w = mount(MacrosAdminPane);
		await flushPromises();
		for (let i = 0; i < 6; i++) {
			await button(w, "Load more").trigger("click");
			await flushPromises();
		}
		expect(rowsOf(w)).toHaveLength(130);

		api.adminListMacros.mockClear();
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(130);
		expect(w.text()).toContain("Showing 130 of 130");
		// In pages the server accepts: never one request for more than 100.
		expect(api.adminListMacros.mock.calls.map((c) => [c[0].start, c[0].pageLength])).toEqual([
			[0, 100],
			[100, 30],
		]);
	});

	it("keeps Load more going after a Refresh of a long list", async () => {
		const all = many(150);
		api.adminListMacros.mockImplementation(serve(all));
		const w = mount(MacrosAdminPane);
		await flushPromises();
		for (let i = 0; i < 5; i++) {
			await button(w, "Load more").trigger("click");
			await flushPromises();
		}
		expect(rowsOf(w)).toHaveLength(120);
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(120);
		await button(w, "Load more").trigger("click");
		await flushPromises();
		expect(lastCall()).toMatchObject({ start: 120, pageLength: 20 });
		expect(rowsOf(w)).toHaveLength(140);
	});

	it("reloads the list when the opened macro turns out to be deleted", async () => {
		const w = await mountWith([row("a"), row("b")]);
		await rowsOf(w)[0].find("button.jv-macro-admin-open").trigger("click");
		api.adminListMacros.mockResolvedValue(page([row("b")]));
		w.findComponent({ name: "MacroAdminDialog" }).vm.$emit("gone", "a");
		await flushPromises();
		expect(rowsOf(w).map((r) => r.attributes("data-macro"))).toEqual(["b"]);
	});
});

describe("MacrosAdminPane, filters", () => {
	it("labels every control", async () => {
		const w = await mountWith([row("a")]);
		expect(w.findAll(".stub-label").map((l) => l.text())).toEqual([
			"Search",
			"Owner",
			"Armed",
			"Schedule",
			"Runs",
			"Hold",
		]);
	});

	it("explains Armed where the admin sees it: under the filters and on the badge", async () => {
		const HELP = "Armed: this macro's runs write without asking for confirmation.";
		const w = await mountWith([row("a", { skip_confirmation: 1 }), row("b")]);
		expect(w.find(".jv-macro-admin-help").text()).toBe(HELP);
		const [a, b] = rowsOf(w);
		expect(a.find(".jv-macro-admin-armed").attributes("title")).toBe(HELP);
		expect(a.find(".jv-macro-admin-armed .badge").text()).toBe("Armed");
		expect(b.find(".jv-macro-admin-armed").exists()).toBe(false);
	});

	it("finds the armed and scheduled macros in one step, and shows what it set", async () => {
		const w = await mountWith([row("a")]);
		const preset = () => w.find(".jv-macro-admin-preset");
		expect(preset().text()).toBe("Armed and scheduled");
		expect(preset().attributes("aria-pressed")).toBe("false");
		const calls = api.adminListMacros.mock.calls.length;
		await preset().trigger("click");
		await flushPromises();
		// One request, with both.
		expect(api.adminListMacros.mock.calls.length).toBe(calls + 1);
		expect(lastCall()).toMatchObject({ filters: { armed: "1", scheduled: "1" }, start: 0 });
		expect(control(w, "Armed").find("select").element.value).toBe("1");
		expect(control(w, "Schedule").find("select").element.value).toBe("1");
		expect(preset().attributes("aria-pressed")).toBe("true");
		// Changing either select by hand un-presses it; pressing it again clears both.
		await control(w, "Schedule").find("select").setValue("0");
		await flushPromises();
		expect(preset().attributes("aria-pressed")).toBe("false");
		await preset().trigger("click");
		await preset().trigger("click");
		await flushPromises();
		expect(lastCall().filters).toEqual({});
	});

	it("asks for the owner choices once, not with every page or filter", async () => {
		const w = await mountWith([row("a"), row("b")], { total: 3, has_more: true });
		expect(api.adminMacroOwners).toHaveBeenCalledTimes(1);
		await control(w, "Armed").find("select").setValue("1");
		await control(w, "Armed").find("select").setValue("");
		await flushPromises();
		await button(w, "Load more").trigger("click");
		await flushPromises();
		expect(api.adminMacroOwners).toHaveBeenCalledTimes(1);
		// Refresh is the one thing that asks again.
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		expect(api.adminMacroOwners).toHaveBeenCalledTimes(2);
	});

	it("lists the macros when the owner choices cannot be loaded, and says when there are more", async () => {
		api.adminMacroOwners.mockRejectedValue(new Error("nope"));
		let w = await mountWith([row("a")]);
		expect(rowsOf(w)).toHaveLength(1);
		expect(w.find("[role='alert']").exists()).toBe(false);
		expect(control(w, "Owner").findAll("option")).toHaveLength(1);

		api.adminMacroOwners.mockResolvedValue({ owners: OWNERS, more: true });
		w = await mountWith([row("a")]);
		const last = control(w, "Owner").findAll("option").at(-1);
		expect(last.text()).toBe("More owners: use Search");
		expect(last.attributes("disabled")).toBeDefined();
	});

	it("drops the old filter's rows when a filter change fails, so Load more cannot mix them", async () => {
		// The rows stayed, with Load more: it then fetched the NEW filter's second
		// page and appended it to the OLD filter's rows.
		const w = await mountWith(many(20), { total: 60, has_more: true });
		api.adminListMacros.mockRejectedValue(new Error("Server is busy"));
		await control(w, "Armed").find("select").setValue("1");
		await flushPromises();
		expect(rowsOf(w)).toHaveLength(0);
		expect(button(w, "Load more")).toBeUndefined();
		expect(w.find("[role='alert']").text()).toContain("Server is busy");
		expect(w.text()).not.toContain("Showing what was loaded before.");

		// Try again reads the new filter from its first row.
		api.adminListMacros.mockResolvedValue(page([row("armed")]));
		await button(w, "Try again").trigger("click");
		await flushPromises();
		expect(lastCall()).toMatchObject({ filters: { armed: "1" }, start: 0 });
		expect(rowsOf(w).map((r) => r.attributes("data-macro"))).toEqual(["armed"]);
	});

	it("starts over, not at the next page, when Load more is pressed with a search still pending", async () => {
		const w = await mountWith(many(20), { total: 60, has_more: true });
		vi.useFakeTimers();
		await control(w, "Search").find("input").setValue("inv");
		api.adminListMacros.mockResolvedValue(page([row("inv")]));
		await button(w, "Load more").trigger("click");
		vi.useRealTimers();
		await flushPromises();
		expect(lastCall()).toMatchObject({ search: "inv", start: 0 });
		expect(rowsOf(w).map((r) => r.attributes("data-macro"))).toEqual(["inv"]);
	});

	it("offers the owners the server sent, with how many macros each has", async () => {
		const w = await mountWith([row("a")]);
		const options = control(w, "Owner")
			.findAll("option")
			.map((o) => [o.attributes("value"), o.text()]);
		expect(options).toEqual([
			["", "All owners"],
			["asha@example.test", "Asha Rao (2)"],
			["ben@example.test", "ben@example.test (1)"],
		]);
	});

	it("sends each filter, and only the ones that are set, from the first row", async () => {
		const w = await mountWith([row("a")]);
		expect(lastCall()).toEqual({ search: "", filters: {}, start: 0, pageLength: 20 });
		for (const [label, value, sent] of [
			["Owner", "ben@example.test", { owner: "ben@example.test" }],
			["Armed", "1", { owner: "ben@example.test", armed: "1" }],
			["Schedule", "0", { owner: "ben@example.test", armed: "1", scheduled: "0" }],
			[
				"Runs",
				"1",
				{ owner: "ben@example.test", armed: "1", scheduled: "0", live_run: "1" },
			],
			["Armed", "", { owner: "ben@example.test", scheduled: "0", live_run: "1" }],
		]) {
			await control(w, label).find("select").setValue(value);
			await flushPromises();
			expect(lastCall().filters, label).toEqual(sent);
			expect(lastCall().start).toBe(0);
		}
	});

	it("waits for the typing to stop before it searches", async () => {
		const w = await mountWith([row("a")]);
		vi.useFakeTimers();
		const calls = api.adminListMacros.mock.calls.length;
		const input = control(w, "Search").find("input");
		await input.setValue("in");
		await input.setValue("  invoices ");
		expect(api.adminListMacros.mock.calls.length).toBe(calls);
		vi.advanceTimersByTime(300);
		expect(api.adminListMacros.mock.calls.length).toBe(calls + 1);
		expect(lastCall().search).toBe("invoices");
	});

	it("does not ask for the owner choices again when a search runs", async () => {
		const w = await mountWith([row("a")]);
		expect(api.adminMacroOwners).toHaveBeenCalledTimes(1);
		vi.useFakeTimers();
		const calls = api.adminListMacros.mock.calls.length;
		const input = control(w, "Search").find("input");
		await input.setValue("in");
		await input.setValue("invoices");
		vi.advanceTimersByTime(300);
		vi.useRealTimers();
		await flushPromises();
		expect(api.adminListMacros.mock.calls.length).toBe(calls + 1);
		expect(api.adminMacroOwners).toHaveBeenCalledTimes(1);
	});
});

describe("MacrosAdminPane, Stop run", () => {
	const HOSTILE = `<img src=x onerror=alert(1)> & "co"`;
	const live = (extra = {}) =>
		row("a", { live_run: "RUN-1", last_run: { status: "running" }, ...extra });
	const stopButton = (w) => rowsOf(w)[0].find(".jv-macro-admin-stop");

	it("asks first, naming the macro and its owner in the first sentence", async () => {
		const w = await mountWith([live()]);
		await stopButton(w).trigger("click");
		await flushPromises();
		const asked = confirm.mock.calls[0][0];
		expect(asked.title).toBe("Stop this run?");
		expect(asked.message.split("?")[0]).toBe(
			"Stop the run of “Macro a”, owned by Asha Rao (asha@example.test)"
		);
		expect(asked.message).toContain("The owner will see that an administrator stopped it.");
		expect(asked.confirmLabel).toBe("Stop run");
		expect(asked.danger).toBe(true);
		expect(stopButton(w).attributes("aria-label")).toBe(
			"Stop the run of Macro a, owned by Asha Rao (asha@example.test)"
		);
	});

	it("does nothing when the admin cancels", async () => {
		confirm.mockResolvedValue(false);
		const w = await mountWith([live()]);
		const calls = api.adminListMacros.mock.calls.length;
		await stopButton(w).trigger("click");
		await flushPromises();
		expect(api.adminStopRun).not.toHaveBeenCalled();
		expect(api.adminListMacros.mock.calls.length).toBe(calls);
		expect(toast.success).not.toHaveBeenCalled();
	});

	it("stops the live run, says so, and re-reads the rows on screen", async () => {
		const w = await mountWith([live(), row("b")]);
		api.adminListMacros.mockResolvedValue(
			page([row("a", { last_run: { status: "stopped", stopped_by_admin: 1 } }), row("b")])
		);
		await stopButton(w).trigger("click");
		await flushPromises();
		expect(api.adminStopRun).toHaveBeenCalledWith("RUN-1");
		expect(toast.success).toHaveBeenCalledWith("Stopped the run of “Macro a”");
		expect(lastCall()).toMatchObject({ start: 0, pageLength: 20 });
		expect(stopButton(w).exists()).toBe(false);
		// The row's new state, in words.
		expect(rowsOf(w)[0].find(".jv-macro-admin-last").text()).toBe("Stopped by an admin");
	});

	it("puts focus on the row's own button after a stop, not nowhere", async () => {
		// The Stop button that had focus is removed by the reload.
		const w = await mountWith([row("z"), live()], undefined, { attachTo: document.body });
		api.adminListMacros.mockResolvedValue(
			page([row("z"), row("a", { last_run: { status: "stopped", stopped_by_admin: 1 } })])
		);
		const stopOf = () => rowsOf(w)[1].find(".jv-macro-admin-stop");
		stopOf().element.focus();
		await stopOf().trigger("click");
		await flushPromises();
		expect(stopOf().exists()).toBe(false);
		expect(document.activeElement).toBe(rowsOf(w)[1].find(".jv-macro-admin-open").element);

		// The row itself went (its macro was deleted meanwhile): the list takes it.
		api.adminListMacros.mockResolvedValue(page([row("z"), live()]));
		await button(w, "Refresh").trigger("click");
		await flushPromises();
		api.adminListMacros.mockResolvedValue(page([row("z")]));
		await stopOf().trigger("click");
		await flushPromises();
		expect(document.activeElement).toBe(w.find(".jv-macro-admin-scroll").element);
		w.unmount();
	});

	it("puts a name in the confirmation as text and in the toast escaped", async () => {
		// The confirmation renders text ({{ }}): escaping there would show the
		// entities. The toast renders HTML: unescaped, the name would be markup.
		const w = await mountWith([live({ macro_name: HOSTILE, owner_full_name: HOSTILE })]);
		await stopButton(w).trigger("click");
		await flushPromises();
		expect(confirm.mock.calls[0][0].message).toContain(`“${HOSTILE}”, owned by ${HOSTILE} (`);
		const said = toast.success.mock.calls[0][0];
		expect(said).toBe(
			"Stopped the run of “&lt;img src=x onerror=alert(1)&gt; &amp; &quot;co&quot;”"
		);
		expect(said).not.toContain("<img");
		// And on the page it is text: no element was made from it.
		expect(w.find("img").exists()).toBe(false);
		expect(rowsOf(w)[0].text()).toContain(HOSTILE);
	});

	it("says the run had already ended when the server says so, escaped", async () => {
		api.adminStopRun.mockResolvedValue({
			ok: true,
			stopped: false,
			status: "completed",
			message: "This run had <b>already</b> ended.",
		});
		const w = await mountWith([live()]);
		await stopButton(w).trigger("click");
		await flushPromises();
		expect(toast.success).not.toHaveBeenCalled();
		expect(toast.create).toHaveBeenCalledWith({
			message: "This run had &lt;b&gt;already&lt;/b&gt; ended.",
			type: "info",
		});
		expect(api.adminListMacros.mock.calls.length).toBe(2);
	});

	it("shows why a stop failed, escaped, and re-reads the row", async () => {
		// The server escapes what it throws; the error formatter decodes it back to
		// text. Into a toast (HTML) it has to be escaped again.
		api.adminStopRun.mockRejectedValue(
			new Error("This run could not be stopped: &lt;img src=x onerror=alert(1)&gt;")
		);
		const w = await mountWith([live()]);
		await stopButton(w).trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith(
			"This run could not be stopped: &lt;img src=x onerror=alert(1)&gt;"
		);
		expect(toast.success).not.toHaveBeenCalled();
		// Still live as far as the server says: the button is back, and usable.
		expect(api.adminListMacros.mock.calls.length).toBe(2);
		expect(stopButton(w).exists()).toBe(true);
		expect(stopButton(w).attributes("disabled")).toBeUndefined();
	});

	it("does not leave a Stop button on a run that turned out to be gone", async () => {
		// "This run no longer exists.": the row was stale. It kept its Stop button,
		// and every later click failed the same way until a manual Refresh.
		api.adminStopRun.mockRejectedValue(new Error("This run no longer exists."));
		const w = await mountWith([live()]);
		api.adminListMacros.mockResolvedValue(
			page([row("a", { last_run: { status: "failed" } })])
		);
		await stopButton(w).trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith("This run no longer exists.");
		expect(stopButton(w).exists()).toBe(false);
		expect(rowsOf(w)[0].find(".jv-macro-admin-last").text()).toBe("Failed");
	});

	it("disables every Stop while one is in flight", async () => {
		let answer;
		api.adminStopRun.mockReturnValue(new Promise((r) => (answer = r)));
		const w = await mountWith([live(), row("b", { live_run: "RUN-2" })]);
		await stopButton(w).trigger("click");
		await flushPromises();
		const stops = w.findAll(".jv-macro-admin-stop");
		expect(stops.map((s) => s.attributes("disabled"))).toEqual(["", ""]);
		expect(stops[0].attributes("data-loading")).toBe("1");
		await stops[1].trigger("click");
		expect(confirm).toHaveBeenCalledTimes(1);
		answer({ ok: true, stopped: true });
		await flushPromises();
	});
});

describe("MacrosAdminPane, hold, release and delete", () => {
	const HOSTILE = `<img src=x onerror=alert(1)> & "co"`;
	const holdButton = (w, i = 0) => rowsOf(w)[i].find(".jv-macro-admin-hold");
	const releaseButton = (w, i = 0) => rowsOf(w)[i].find(".jv-macro-admin-release");
	const deleteButton = (w, i = 0) => rowsOf(w)[i].find(".jv-macro-admin-delete");
	const held = (name = "a", extra = {}) =>
		row(name, {
			enabled: 0,
			admin_hold: 1,
			admin_hold_reason: "Sends too many emails",
			...extra,
		});

	it("shows On hold, with its reason, and offers Release instead of Hold", async () => {
		const w = await mountWith([held(), row("b")]);
		const [a, b] = rowsOf(w);
		expect(a.find(".jv-macro-admin-held .badge").text()).toBe("On hold");
		expect(a.find(".jv-macro-admin-held").attributes("title")).toBe(
			"On hold: Sends too many emails"
		);
		// The title shows on hover only: the reason is in the text too.
		const said = a.find(".jv-macro-admin-held .sr-only");
		expect(said.text()).toBe("On hold: Sends too many emails");
		expect(b.find(".jv-macro-admin-own").exists()).toBe(false);
		expect(holdButton(w).exists()).toBe(false);
		expect(releaseButton(w).exists()).toBe(true);
		expect(b.find(".jv-macro-admin-held").exists()).toBe(false);
		expect(holdButton(w, 1).exists()).toBe(true);
		expect(releaseButton(w, 1).exists()).toBe(false);
	});

	it("offers no Hold, Release or Hand over on the admin's own macro, only Delete", async () => {
		const w = await mountWith([
			row("mine", { owner: "admin@example.test" }),
			held("mine-held", { owner: "admin@example.test" }),
		]);
		for (const i of [0, 1]) {
			expect(holdButton(w, i).exists()).toBe(false);
			expect(releaseButton(w, i).exists()).toBe(false);
			expect(rowsOf(w)[i].find(".jv-macro-admin-handover").exists()).toBe(false);
			expect(deleteButton(w, i).exists()).toBe(true);
			// Said, not just left out.
			expect(rowsOf(w)[i].find(".jv-macro-admin-own").text()).toBe(
				"You cannot hold, release or hand over your own macro."
			);
		}
	});

	it("filters on hold", async () => {
		const w = await mountWith([row("a")]);
		await control(w, "Hold").find("select").setValue("1");
		await flushPromises();
		expect(lastCall().filters).toEqual({ on_hold: "1" });
	});

	it("asks for a reason in the hold dialog, and re-reads the rows once it is held", async () => {
		const w = await mountWith([row("a")]);
		expect(w.find(".stub-hold").exists()).toBe(false);
		await holdButton(w).trigger("click");
		const dialog = w.find(".stub-hold");
		expect(dialog.attributes("data-name")).toBe("a");
		expect(dialog.attributes("data-open")).toBe("1");
		expect(dialog.attributes("data-owner")).toBe("Asha Rao (asha@example.test)");
		// Nothing is sent from the pane itself: the dialog holds, with the reason.
		expect(confirm).not.toHaveBeenCalled();

		api.adminListMacros.mockResolvedValue(page([held()]));
		const hold = w.findComponent({ name: "MacroHoldDialog" });
		hold.vm.$emit("update:modelValue", false);
		hold.vm.$emit("held", { name: "a", stopped_runs: 1 });
		await flushPromises();
		expect(lastCall()).toMatchObject({ start: 0, pageLength: 20 });
		expect(releaseButton(w).exists()).toBe(true);
	});

	it("re-reads the rows when the hold answers an error: the hold may still stand", async () => {
		const w = await mountWith([row("a")]);
		await holdButton(w).trigger("click");
		const calls = api.adminListMacros.mock.calls.length;
		api.adminListMacros.mockResolvedValue(page([held()]));
		w.findComponent({ name: "MacroHoldDialog" }).vm.$emit("failed", { name: "a" });
		await flushPromises();
		expect(api.adminListMacros.mock.calls.length).toBeGreaterThan(calls);
		expect(releaseButton(w).exists()).toBe(true);
	});

	it("releases after asking, says nothing turns back on, and re-reads the rows", async () => {
		const w = await mountWith([held()]);
		api.adminListMacros.mockResolvedValue(page([row("a", { enabled: 0 })]));
		await releaseButton(w).trigger("click");
		await flushPromises();
		const asked = confirm.mock.calls[0][0];
		expect(asked.title).toBe("Release the hold?");
		expect(asked.message).toContain("“Macro a”, owned by Asha Rao (asha@example.test)");
		expect(asked.message).toContain("Nothing turns itself back on");
		expect(api.adminRelease).toHaveBeenCalledWith("a");
		expect(toast.success).toHaveBeenCalledWith("Released the hold on “Macro a”");
		expect(holdButton(w).exists()).toBe(true);
	});

	it("does not release or delete when the admin cancels", async () => {
		confirm.mockResolvedValue(false);
		const w = await mountWith([held()]);
		const calls = api.adminListMacros.mock.calls.length;
		await releaseButton(w).trigger("click");
		await deleteButton(w).trigger("click");
		await flushPromises();
		expect(api.adminRelease).not.toHaveBeenCalled();
		expect(api.adminDelete).not.toHaveBeenCalled();
		expect(api.adminListMacros.mock.calls.length).toBe(calls);
	});

	it("says a macro that was not on hold was not, as information", async () => {
		api.adminRelease.mockResolvedValue({ ok: true, released: false });
		const w = await mountWith([held()]);
		await releaseButton(w).trigger("click");
		await flushPromises();
		expect(toast.success).not.toHaveBeenCalled();
		expect(toast.create).toHaveBeenCalledWith({
			message: "This macro was not on hold.",
			type: "info",
		});
	});

	it("deletes after a danger confirmation that says the run is stopped first", async () => {
		const w = await mountWith([row("a", { live_run: "RUN-1" }), row("b")]);
		api.adminDelete.mockResolvedValue({ ok: true, deleted: true, stopped_runs: 1 });
		api.adminListMacros.mockResolvedValue(page([row("b")]));
		await deleteButton(w).trigger("click");
		await flushPromises();
		const asked = confirm.mock.calls[0][0];
		expect(asked.danger).toBe(true);
		expect(asked.confirmLabel).toBe("Delete");
		expect(asked.message).toContain("Its run is stopped first.");
		expect(asked.message).toContain("The owner is told an admin deleted it.");
		expect(api.adminDelete).toHaveBeenCalledWith("a");
		expect(toast.success).toHaveBeenCalledWith("Deleted “Macro a”; 1 run stopped first");
		expect(rowsOf(w).map((r) => r.attributes("data-macro"))).toEqual(["b"]);
	});

	it("shows why an action failed, escaped, and re-reads the rows", async () => {
		api.adminDelete.mockRejectedValue(new Error("This macro was deleted. &lt;b&gt;"));
		const w = await mountWith([row("a")]);
		api.adminListMacros.mockResolvedValue(page([]));
		await deleteButton(w).trigger("click");
		await flushPromises();
		expect(toast.error).toHaveBeenCalledWith("This macro was deleted. &lt;b&gt;");
		expect(toast.success).not.toHaveBeenCalled();
		expect(rowsOf(w)).toHaveLength(0);
	});

	it("shows the action in flight and disables every other one meanwhile", async () => {
		let answer;
		api.adminRelease.mockReturnValue(new Promise((r) => (answer = r)));
		const w = await mountWith([held(), row("b", { live_run: "RUN-2" })]);
		await releaseButton(w).trigger("click");
		await flushPromises();
		expect(releaseButton(w).attributes("data-loading")).toBe("1");
		for (const b of [deleteButton(w), holdButton(w, 1), deleteButton(w, 1)]) {
			expect(b.attributes("disabled")).toBe("");
		}
		expect(rowsOf(w)[1].find(".jv-macro-admin-stop").attributes("disabled")).toBe("");
		await deleteButton(w, 1).trigger("click");
		expect(confirm).toHaveBeenCalledTimes(1);
		answer({ ok: true, released: true });
		await flushPromises();
		expect(deleteButton(w).attributes("disabled")).toBeUndefined();
	});

	it("puts a name in the confirmation as text and in the toast escaped", async () => {
		const w = await mountWith([held("a", { macro_name: HOSTILE })]);
		await releaseButton(w).trigger("click");
		await flushPromises();
		expect(confirm.mock.calls[0][0].message).toContain(`“${HOSTILE}”`);
		const said = toast.success.mock.calls[0][0];
		expect(said).not.toContain("<img");
		expect(w.find("img").exists()).toBe(false);
	});
});

describe("MacrosAdminPane, hand over", () => {
	const handoverButton = (w, i = 0) => rowsOf(w)[i].find(".jv-macro-admin-handover");

	it("offers Hand over on another user's macro, held or not", async () => {
		const w = await mountWith([row("a"), row("b", { admin_hold: 1, enabled: 0 })]);
		for (const i of [0, 1]) {
			const b = handoverButton(w, i);
			expect(b.exists()).toBe(true);
			expect(b.text()).toBe("Hand over");
		}
		expect(handoverButton(w).attributes("aria-label")).toBe(
			"Hand Macro a, owned by Asha Rao (asha@example.test), to another user"
		);
	});

	it("opens the hand-over dialog for the row, and sends nothing itself", async () => {
		const w = await mountWith([row("a")]);
		expect(w.find(".stub-handover").exists()).toBe(false);
		await handoverButton(w).trigger("click");
		const dialog = w.find(".stub-handover");
		expect(dialog.attributes("data-name")).toBe("a");
		expect(dialog.attributes("data-open")).toBe("1");
		expect(dialog.attributes("data-macro")).toBe("Macro a");
		expect(dialog.attributes("data-owner")).toBe("asha@example.test");
		expect(dialog.attributes("data-owner-label")).toBe("Asha Rao (asha@example.test)");
		expect(confirm).not.toHaveBeenCalled();
	});

	it("disables the other actions while the dialog is open", async () => {
		const w = await mountWith([row("a"), row("b", { live_run: "RUN-2" })]);
		await handoverButton(w).trigger("click");
		for (const b of [
			handoverButton(w, 1),
			rowsOf(w)[1].find(".jv-macro-admin-delete"),
			rowsOf(w)[1].find(".jv-macro-admin-hold"),
			rowsOf(w)[1].find(".jv-macro-admin-stop"),
		]) {
			expect(b.attributes("disabled")).toBe("");
		}
	});

	it("re-reads the rows once it is handed over, and when it answers an error", async () => {
		for (const event of ["handed", "failed"]) {
			const w = await mountWith([row("a")]);
			await handoverButton(w).trigger("click");
			const calls = api.adminListMacros.mock.calls.length;
			api.adminListMacros.mockResolvedValue(
				page([row("a", { owner: "ben@example.test", owner_full_name: "Ben", enabled: 0 })])
			);
			const dialog = w.findComponent({ name: "MacroHandoverDialog" });
			dialog.vm.$emit("update:modelValue", false);
			dialog.vm.$emit(event, { name: "a" });
			await flushPromises();
			expect(api.adminListMacros.mock.calls.length).toBeGreaterThan(calls);
			expect(rowsOf(w)[0].text()).toContain("Ben");
			w.unmount();
		}
	});

	it("after a refusal, shows the open dialog the owner the re-read found", async () => {
		const w = await mountWith([row("a")]);
		await handoverButton(w).trigger("click");
		api.adminListMacros.mockResolvedValue(
			page([row("a", { owner: "ben@example.test", owner_full_name: "Ben" })])
		);
		const dialog = w.findComponent({ name: "MacroHandoverDialog" });
		dialog.vm.$emit("failed", { name: "a", message: "This macro changed hands." });
		await flushPromises();
		const stub = w.find(".stub-handover");
		expect(stub.attributes("data-open")).toBe("1");
		expect(stub.attributes("data-owner")).toBe("ben@example.test");
		expect(stub.attributes("data-owner-label")).toBe("Ben (ben@example.test)");
		expect(toast.error).not.toHaveBeenCalled();
	});

	it("after a refusal, closes the dialog with its message when the macro is no longer listed", async () => {
		const w = await mountWith([row("a"), row("b")]);
		await handoverButton(w).trigger("click");
		api.adminListMacros.mockResolvedValue(page([row("b")]));
		const dialog = w.findComponent({ name: "MacroHandoverDialog" });
		dialog.vm.$emit("failed", { name: "a", message: "<b>Changed</b> hands." });
		await flushPromises();
		expect(w.find(".stub-handover").attributes("data-open")).toBe("");
		expect(toast.error).toHaveBeenCalledWith("&lt;b&gt;Changed&lt;/b&gt; hands.");
	});
});
