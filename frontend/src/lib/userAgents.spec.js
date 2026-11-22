import { describe, it, expect, vi } from "vitest";

vi.mock("frappe-ui", () => ({ dayjs: () => ({ format: () => "" }) }));

import {
	MAX_INSTRUCTIONS,
	canRetrySync,
	definitionChanged,
	definitionPayload,
	emptyForm,
	formFromAgent,
	isSyncing,
	lastRun,
	scheduleChanged,
	schedulePayload,
	scheduleSummary,
	stateBadge,
	validateForm,
} from "@/lib/userAgents";

const agent = (extra = {}) => ({
	name: "UA-1",
	agent_name: "Overdue invoices",
	description: "",
	instructions: "List overdue invoices.",
	enabled: true,
	allow_shell: false,
	tools: ["get_list"],
	sync: { status: "Synced", error: "", message: "", synced_at: "2026-11-22 09:00:00" },
	schedule: {
		enabled: false,
		frequency: "daily",
		weekday: "",
		day_of_month: null,
		time: null,
		prompt: "",
		next_run_at: null,
	},
	last_run: { at: null, status: "", error: "", message: "", conversation: null },
	ready: true,
	...extra,
});
const withSync = (sync, extra = {}) =>
	agent({
		sync: { status: "Synced", error: "", message: "", synced_at: null, ...sync },
		...extra,
	});

describe("stateBadge", () => {
	it("says Ready for an enabled, synced agent", () => {
		expect(stateBadge(agent())).toEqual({ label: "Ready", theme: "green" });
	});
	it("says Syncing while the sync is pending", () => {
		expect(stateBadge(withSync({ status: "Pending" })).label).toBe("Syncing");
	});
	it("says Sync failed for a failed sync", () => {
		expect(stateBadge(withSync({ status: "Failed", message: "x" }))).toEqual({
			label: "Sync failed",
			theme: "red",
		});
	});
	it("says Off for a switched-off agent whatever its sync says", () => {
		expect(stateBadge(withSync({ status: "Failed" }, { enabled: false })).label).toBe("Off");
	});
});

describe("sync helpers", () => {
	it("offers a retry for any enabled agent that is not synced", () => {
		expect(canRetrySync(withSync({ status: "Failed" }))).toBe(true);
		expect(canRetrySync(withSync({ status: "Pending", message: "Busy." }))).toBe(true);
		// A job that never ran leaves Pending with no reason.
		expect(canRetrySync(withSync({ status: "Pending" }))).toBe(true);
		expect(canRetrySync(agent())).toBe(false);
		expect(canRetrySync(withSync({ status: "Failed" }, { enabled: false }))).toBe(false);
	});
	it("polls only a pending sync with no reason yet", () => {
		expect(isSyncing(withSync({ status: "Pending" }))).toBe(true);
		expect(isSyncing(withSync({ status: "Pending", message: "Busy." }))).toBe(false);
		expect(isSyncing(withSync({ status: "Pending" }, { enabled: false }))).toBe(false);
		expect(isSyncing(agent())).toBe(false);
	});
});

describe("scheduleSummary and lastRun", () => {
	it("reads Off, daily, weekly and monthly schedules", () => {
		expect(scheduleSummary(agent())).toBe("Off");
		const on = (s) => agent({ schedule: { ...agent().schedule, enabled: true, ...s } });
		expect(scheduleSummary(on({ frequency: "daily", time: "09:00:00" }))).toBe(
			"Daily · 9:00 am"
		);
		expect(
			scheduleSummary(on({ frequency: "weekly", weekday: "Monday", time: "17:30:00" }))
		).toBe("Weekly on Monday · 5:30 pm");
		expect(
			scheduleSummary(on({ frequency: "monthly", day_of_month: 15, time: "09:00:00" }))
		).toBe("Monthly on the 15th · 9:00 am");
	});
	it("reports a started or failed run and nothing for no run", () => {
		expect(lastRun(agent())).toBeNull();
		const run = (status, message = "") =>
			agent({ last_run: { at: "2026-11-22 09:00:00", status, error: "", message } });
		expect(lastRun(run("Started"))).toMatchObject({ status: "Started" });
		expect(lastRun(run("Failed", "Usage limit reached."))).toMatchObject({
			status: "Failed",
			message: "Usage limit reached.",
		});
	});
});

describe("validateForm", () => {
	const valid = () => ({ ...emptyForm(), agent_name: "Stock", instructions: "Watch stock." });

	it("accepts a name and instructions", () => {
		expect(validateForm(valid())).toEqual({});
	});
	it("asks for a name and instructions", () => {
		const errors = validateForm(emptyForm());
		expect(errors.agent_name).toBe("Give the agent a name.");
		expect(errors.instructions).toBe("Tell the agent what to do.");
	});
	it("refuses instructions over the limit and says how long they may be", () => {
		const form = { ...valid(), instructions: "x".repeat(MAX_INSTRUCTIONS + 1) };
		expect(validateForm(form).instructions).toContain("12,000");
		expect(validateForm({ ...valid(), instructions: "x".repeat(MAX_INSTRUCTIONS) })).toEqual(
			{}
		);
	});
	it("refuses more tools than the server allows", () => {
		const tools = Array.from({ length: 3 }, (_, i) => `t${i}`);
		expect(validateForm({ ...valid(), tools }, { maxTools: 2 }).tools).toBe(
			"Pick at most 2 tools."
		);
	});
	it("needs a prompt, a time and a day only when the schedule is on", () => {
		expect(validateForm({ ...valid(), prompt: "" })).toEqual({});
		const on = { ...valid(), schedule_enabled: true, frequency: "weekly", day: "", time: "" };
		const errors = validateForm(on);
		expect(errors.prompt).toBe("Write what the agent should do on each run.");
		expect(errors.time).toBe("Pick a time.");
		expect(errors.day).toBe("Pick a weekday.");
		expect(validateForm({ ...on, frequency: "monthly" }).day).toBe("Pick a day of the month.");
	});
});

describe("payloads and change detection", () => {
	it("sends the definition trimmed, with tools sorted", () => {
		const form = {
			...emptyForm(),
			agent_name: " A ",
			instructions: " do it ",
			tools: ["b", "a"],
			allow_shell: true,
		};
		expect(definitionPayload(form)).toEqual({
			agent_name: "A",
			description: "",
			instructions: "do it",
			tools: ["a", "b"],
			allow_shell: true,
		});
	});
	it("sends the day only for the frequency that uses it", () => {
		const base = { ...emptyForm(), schedule_enabled: true, prompt: "go" };
		expect(schedulePayload({ ...base, frequency: "weekly", day: "Friday" })).toMatchObject({
			weekday: "Friday",
			day_of_month: 0,
		});
		expect(schedulePayload({ ...base, frequency: "monthly", day: "15" })).toMatchObject({
			weekday: "",
			day_of_month: 15,
		});
		expect(schedulePayload({ ...base, frequency: "daily", day: "15" })).toMatchObject({
			weekday: "",
			day_of_month: 0,
		});
	});
	it("tells a definition change from a schedule change", () => {
		const a = agent();
		const form = formFromAgent(a);
		expect(definitionChanged(form, a)).toBe(false);
		expect(scheduleChanged(form, a)).toBe(false);
		expect(definitionChanged({ ...form, tools: ["get_list", "get_doc"] }, a)).toBe(true);
		expect(definitionChanged({ ...form, tools: [...a.tools] }, a)).toBe(false);
		expect(scheduleChanged({ ...form, schedule_enabled: true }, a)).toBe(true);
		expect(scheduleChanged({ ...form, prompt: "go" }, a)).toBe(true);
	});
	it("ignores the frequency of a schedule that stays off", () => {
		const a = agent();
		expect(
			scheduleChanged({ ...formFromAgent(a), frequency: "weekly", day: "Monday" }, a)
		).toBe(false);
	});
});
