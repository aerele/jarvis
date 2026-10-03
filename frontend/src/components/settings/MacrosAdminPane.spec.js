import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The Jarvis Admin's Macros pane (Settings, Administration): every user's macros,
 * the filters, the loading / empty / error states, and the one action, Stop run.
 * The decisions behind the cells (lib/macroRunOutcome, lib/macroSchedule) are the
 * real ones; frappe-ui and the read-only dialog are stubs.
 */

vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label", "variant", "iconLeft", "loading", "disabled", "size", "theme"],
		emits: ["click"],
		template: `<button class="stub-button" :disabled="disabled" :data-loading="loading ? '1' : ''" @click="$emit('click')">{{ label }}</button>`,
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
					<option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option>
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

const api = vi.hoisted(() => ({ adminListMacros: vi.fn(), adminStopRun: vi.fn() }));
vi.mock("@/api/macrosAdmin", () => api);

const confirm = vi.hoisted(() => vi.fn());
vi.mock("@/composables/useConfirm", () => ({ useConfirm: () => ({ confirm }) }));

vi.mock("@/components/settings/MacroAdminDialog.vue", () => ({
	default: {
		name: "MacroAdminDialog",
		props: ["modelValue", "name"],
		template: `<div class="stub-detail" :data-name="name" :data-open="modelValue ? '1' : ''" />`,
	},
}));

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
	owners: [
		{ user: "asha@example.test", full_name: "Asha Rao", macros: 2 },
		{ user: "ben@example.test", full_name: "ben@example.test", macros: 1 },
	],
	...extra,
});

async function mountWith(rows, extra) {
	api.adminListMacros.mockResolvedValue(page(rows, extra));
	const w = mount(MacrosAdminPane);
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
				last_run: { name: "", status: "failed", started_at: "2026-01-01 09:00:00" },
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
		expect(a.findAll(".badge").map((x) => x.text())).toEqual(["On", "Armed"]);
		expect(a.text()).toContain("Daily · 9:00 am");
		expect(a.text()).toContain("Failed");
		expect(b.findAll(".badge").map((x) => x.text())).toEqual(["Off"]);
		expect(b.text()).toContain("Not scheduled");
		expect(b.text()).toContain("Never ran");
		// An owner with no full name is shown once, not twice.
		expect(b.text().split("ben@example.test")).toHaveLength(2);
	});

	it("offers Stop run only on a row with a live run", async () => {
		const w = await mountWith([
			row("a", { live_run: "RUN-1", last_run: { name: "RUN-1", status: "running" } }),
			row("b", { last_run: { name: "", status: "completed" } }),
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
		]);
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
});

describe("MacrosAdminPane, Stop run", () => {
	const HOSTILE = `<img src=x onerror=alert(1)> & "co"`;
	const live = (extra = {}) =>
		row("a", { live_run: "RUN-1", last_run: { name: "RUN-1", status: "running" }, ...extra });
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
			page([row("a", { last_run: { name: "", status: "stopped" } }), row("b")])
		);
		await stopButton(w).trigger("click");
		await flushPromises();
		expect(api.adminStopRun).toHaveBeenCalledWith("RUN-1");
		expect(toast.success).toHaveBeenCalledWith("Stopped the run of “Macro a”");
		expect(lastCall()).toMatchObject({ start: 0, pageLength: 20 });
		expect(stopButton(w).exists()).toBe(false);
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

	it("shows why a stop failed, escaped, and leaves the row as it was", async () => {
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
		expect(stopButton(w).exists()).toBe(true);
		expect(stopButton(w).attributes("disabled")).toBeUndefined();
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
