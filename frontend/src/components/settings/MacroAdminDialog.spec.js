import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * The read-only view of another user's macro. What matters: it shows the steps,
 * the summary and the recent runs; nothing in it can change or run the macro;
 * another user's free text is rendered as text.
 */

vi.mock("frappe-ui", () => ({
	Button: {
		name: "Button",
		props: ["label", "variant", "iconLeft", "loading"],
		emits: ["click"],
		template: `<button class="stub-button" @click="$emit('click')">{{ label }}</button>`,
	},
	Badge: {
		name: "Badge",
		props: ["label", "theme"],
		template: `<span class="badge">{{ label }}</span>`,
	},
	FeatherIcon: { name: "FeatherIcon", props: ["name"], template: `<i class="stub-icon" />` },
	Dialog: {
		name: "Dialog",
		props: ["modelValue", "options"],
		emits: ["update:modelValue"],
		template: `<div v-if="modelValue" class="stub-dialog"><h4>{{ options && options.title }}</h4><slot name="body-content" /><slot name="actions" /></div>`,
	},
}));

vi.mock("@/utils/datetime", () => ({
	timeAgo: (d) => (d ? "a while ago" : ""),
	exactDate: (d) => String(d || ""),
	toLocalMs: (d) => (d ? Date.parse(String(d).replace(" ", "T")) : null),
	formatTime12h: (t) => (t ? "9:00 am" : ""),
}));

const api = vi.hoisted(() => ({ adminGetMacro: vi.fn() }));
vi.mock("@/api/macrosAdmin", () => api);

import MacroAdminDialog from "./MacroAdminDialog.vue";

const HOSTILE = `<img src=x onerror=alert(1)>`;
const macro = (extra = {}) => ({
	name: "m1",
	macro_name: "Month end",
	owner: "asha@example.test",
	owner_full_name: "Asha Rao",
	description: "",
	enabled: 1,
	skip_confirmation: 1,
	schedule_enabled: 1,
	schedule_frequency: "daily",
	schedule_time: "09:00:00",
	next_run_at: "2999-01-01 09:00:00",
	next_run_is_retry: 0,
	merged_prompt: "Close the month in one go.",
	merge_status: "ready",
	steps: [
		{
			label: "Post",
			prompt: "Post the accruals",
			model_override: "",
			thinking_override: "",
			skill_count: 2,
		},
		{
			label: "",
			prompt: "Send the report",
			model_override: "gpt-5.5",
			thinking_override: "high",
			skill_count: 0,
		},
	],
	runs: [
		{
			name: "RUN-2",
			status: "stopped",
			trigger: "scheduled",
			creation: "2026-01-02 09:00:00",
			started_at: "2026-01-02 09:00:00",
			finished_at: "2026-01-02 09:01:00",
			current_step: 1,
			total_steps: 2,
			run_mode: "stepped",
			error: "Stopped by an administrator.",
		},
		{
			name: "RUN-1",
			status: "waiting_capacity",
			trigger: "manual",
			creation: "2026-01-01 09:00:00",
			started_at: null,
			finished_at: null,
			current_step: 0,
			total_steps: 2,
			run_mode: "stepped",
			error: "",
		},
	],
	live_run: "RUN-1",
	...extra,
});

async function open(data = macro(), props = {}) {
	api.adminGetMacro.mockResolvedValue(data);
	const w = mount(MacroAdminDialog, { props: { modelValue: true, name: "m1", ...props } });
	await flushPromises();
	return w;
}

beforeEach(() => vi.clearAllMocks());

describe("MacroAdminDialog", () => {
	it("does not ask the server while it is closed", async () => {
		const w = mount(MacroAdminDialog, { props: { modelValue: false, name: "m1" } });
		await flushPromises();
		expect(api.adminGetMacro).not.toHaveBeenCalled();
		api.adminGetMacro.mockResolvedValue(macro());
		await w.setProps({ modelValue: true });
		await flushPromises();
		expect(api.adminGetMacro).toHaveBeenCalledWith("m1");
	});

	it("shows the owner, the state, the summary and every step's prompt", async () => {
		const w = await open();
		expect(w.find("h4").text()).toBe("Month end");
		expect(w.text()).toContain(
			"Read-only. Only Asha Rao (asha@example.test) can edit or run this macro."
		);
		expect(w.findAll("dd .badge").map((b) => b.text())).toEqual(["On", "Armed"]);
		expect(w.text()).toContain("Daily · 9:00 am");
		const texts = w.findAll("pre.jv-macro-admin-text").map((p) => p.text());
		expect(texts).toEqual([
			"Close the month in one go.",
			"Post the accruals",
			"Send the report",
		]);
		expect(w.text()).toContain("Steps (2)");
		expect(w.text()).toContain("1. Post · 2 skills");
		expect(w.text()).toContain("2. Step 2 · model gpt-5.5, thinking high");
	});

	it("shows the recent runs: status, trigger, steps and the reason", async () => {
		const w = await open();
		const rows = w.findAll("tbody tr").map((tr) => tr.findAll("td").map((td) => td.text()));
		expect(rows).toEqual([
			["a while ago", "Stopped", "scheduled", "1/2", "Stopped by an administrator."],
			["a while ago", "Waiting for capacity", "manual", "0/2", "-"],
		]);
		expect(w.findAll("thead th").map((th) => th.attributes("scope"))).toEqual(
			Array(5).fill("col")
		);
	});

	it("has nothing to edit, save or run it with, and no way into a run's chat", async () => {
		const w = await open();
		expect(w.findAll("input, textarea, select, [contenteditable]")).toHaveLength(0);
		expect(w.findAll("a")).toHaveLength(0);
		expect(w.findAll("button").map((b) => b.text())).toEqual(["Close"]);
	});

	it("says so when there is no summary, or one is on its way, and when it never ran", async () => {
		let w = await open(macro({ merged_prompt: "", merge_status: "", runs: [] }));
		expect(w.text()).toContain("None. It runs its steps one by one.");
		expect(w.text()).toContain("It has not run yet.");
		expect(w.find("table").exists()).toBe(false);
		w = await open(macro({ merged_prompt: "", merge_status: "pending" }));
		expect(w.text()).toContain("Being summarized.");
	});

	it("renders another user's text as text, never as markup", async () => {
		const w = await open(
			macro({
				macro_name: HOSTILE,
				owner_full_name: HOSTILE,
				description: HOSTILE,
				merged_prompt: HOSTILE,
				steps: [{ label: HOSTILE, prompt: HOSTILE, skill_count: 0 }],
				runs: [{ name: "R", status: "failed", trigger: "manual", error: HOSTILE }],
			})
		);
		expect(w.find("img").exists()).toBe(false);
		expect(w.find("pre.jv-macro-admin-text").text()).toBe(HOSTILE);
		expect(w.find("tbody td:last-child").text()).toBe(HOSTILE);
	});

	it("shows the error with Try again, and recovers", async () => {
		api.adminGetMacro.mockRejectedValue(new Error("Jarvis Macro m1 not found"));
		const w = mount(MacroAdminDialog, { props: { modelValue: true, name: "m1" } });
		await flushPromises();
		expect(w.find("[role='alert']").text()).toContain("Jarvis Macro m1 not found");
		api.adminGetMacro.mockResolvedValue(macro());
		await w
			.findAll("button")
			.find((b) => b.text() === "Try again")
			.trigger("click");
		await flushPromises();
		expect(w.find("[role='alert']").exists()).toBe(false);
		expect(w.text()).toContain("Post the accruals");
	});

	it("reads the other macro when it is pointed at one, and Close closes it", async () => {
		const w = await open();
		api.adminGetMacro.mockResolvedValue(macro({ name: "m2", macro_name: "Other" }));
		await w.setProps({ name: "m2" });
		await flushPromises();
		expect(api.adminGetMacro).toHaveBeenLastCalledWith("m2");
		expect(w.find("h4").text()).toBe("Other");
		await w
			.findAll("button")
			.find((b) => b.text() === "Close")
			.trigger("click");
		expect(w.emitted("update:modelValue")).toEqual([[false]]);
	});
});
