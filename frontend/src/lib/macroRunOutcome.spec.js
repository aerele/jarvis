import { describe, it, expect } from "vitest";
import { lastRunCell, lastRunLine, runDetail, macroDoneSignal } from "./macroRunOutcome.js";

/**
 * How a macro run went, as the screens say it. Until now nothing outside the Runs
 * tab said anything: the list showed a bare time, the form nothing, and a failed
 * run reached nobody who was not looking at its conversation.
 */

const labels = { when: "Thu, Oct 1, 2026 10:15 AM", relative: "2 hours ago" };

describe("lastRunCell: the Macros list's Last run cell", () => {
	it("a macro that never ran shows nothing", () => {
		expect(lastRunCell({ lastRun: null, ...labels })).toBeNull();
		expect(lastRunCell({ lastRun: undefined, ...labels })).toBeNull();
	});

	it("a run that worked keeps showing when it ran", () => {
		const cell = lastRunCell({ lastRun: { status: "completed", error: "" }, ...labels });
		expect(cell).toEqual({ text: "2 hours ago", hint: labels.when, tone: "" });
	});

	it("a failed run says Failed, with the reason one hover away", () => {
		const cell = lastRunCell({
			lastRun: { status: "failed", error: "Step 2 failed: no such customer" },
			...labels,
		});
		expect(cell.text).toBe("Failed");
		expect(cell.tone).toBe("bad");
		expect(cell.hint).toContain("Step 2 failed: no such customer");
		expect(cell.hint).toContain(labels.when);
	});

	it("a run that stopped for a reason says Stopped and why", () => {
		const cell = lastRunCell({
			lastRun: {
				status: "stopped",
				error: "Step 1 is waiting for your confirmation (Send email).",
			},
			...labels,
		});
		expect(cell.text).toBe("Stopped");
		expect(cell.tone).toBe("warn");
		expect(cell.hint).toContain("waiting for your confirmation");
	});

	it("a run the user stopped by hand is not a warning", () => {
		const cell = lastRunCell({ lastRun: { status: "stopped", error: "" }, ...labels });
		expect(cell).toEqual({ text: "Stopped", hint: labels.when, tone: "muted" });
	});

	it("a completed run that left drafts is not shown as a failure", () => {
		const note = "Step 1 proposed a draft for you to apply; the macro did not wait for it.";
		const cell = lastRunCell({ lastRun: { status: "completed", error: note }, ...labels });
		expect(cell.text).toBe("2 hours ago");
		expect(cell.tone).toBe("");
		expect(cell.hint).toContain(note);
	});

	it("a run with no recorded time still says how it went, with no stray punctuation", () => {
		// A scheduler-recorded failure may carry neither a start nor a finish.
		const failed = lastRunCell({
			lastRun: { status: "failed", error: "It could not be started." },
			when: "",
			relative: "",
		});
		expect(failed.hint).toBe("It could not be started.");
		const done = lastRunCell({ lastRun: { status: "completed" }, when: "", relative: "" });
		expect(done.text).toBe("Completed");
	});

	it("an older server that sends no last run keeps the cell it had", () => {
		const legacy = { when: "Wed, Sep 30, 2026 9:00 AM", relative: "a day ago" };
		expect(lastRunCell({ lastRun: undefined, when: "", relative: "", legacy })).toEqual({
			text: "a day ago",
			hint: legacy.when,
			tone: "",
		});
		expect(
			lastRunCell({ lastRun: undefined, when: "", relative: "", legacy: null })
		).toBeNull();
		// `null` is the server saying "never ran": the legacy time must not resurface.
		expect(lastRunCell({ lastRun: null, when: "", relative: "", legacy })).toBeNull();
	});

	it("a run still going says so", () => {
		expect(lastRunCell({ lastRun: { status: "running" }, ...labels }).text).toBe("Running");
		expect(lastRunCell({ lastRun: { status: "waiting_capacity" }, ...labels }).text).toBe(
			"Waiting"
		);
	});

	it("an unknown status is shown as it is, never as a success", () => {
		const cell = lastRunCell({ lastRun: { status: "something_new" }, ...labels });
		expect(cell.text).toBe("something_new");
	});
});

describe("lastRunLine: the form's line about the last run", () => {
	it("says nothing when the last run simply worked, or there was none", () => {
		expect(lastRunLine(null)).toBeNull();
		expect(lastRunLine({ status: "completed", error: "" })).toBeNull();
		expect(lastRunLine({ status: "running" })).toBeNull();
		expect(lastRunLine({ status: "stopped", error: "" })).toBeNull();
	});

	it("a failed run is stated with its reason", () => {
		const line = lastRunLine({ status: "failed", error: "Step 2 failed: boom" });
		expect(line).toEqual({ text: "The last run failed. Step 2 failed: boom", tone: "bad" });
	});

	it("a failed run with no stored reason still says it failed", () => {
		expect(lastRunLine({ status: "failed", error: "" }).text).toBe("The last run failed.");
	});

	it("a run that stopped for a reason, and a completed run with a note, give the reason alone", () => {
		const waiting = "Step 1 is waiting for your confirmation (Send email).";
		expect(lastRunLine({ status: "stopped", error: waiting })).toEqual({
			text: waiting,
			tone: "warn",
		});
		const note = "Step 1 proposed a draft for you to apply; the macro did not wait for it.";
		expect(lastRunLine({ status: "completed", error: note })).toEqual({
			text: note,
			tone: "",
		});
	});
});

describe("runDetail: the Runs tab's detail dialog", () => {
	it("is titled for what the run did, not always 'Run failed'", () => {
		expect(runDetail({ status: "failed" })).toEqual({ title: "Run failed", label: "Error" });
		expect(runDetail({ status: "stopped" })).toEqual({
			title: "Run stopped",
			label: "Reason",
		});
		expect(runDetail({ status: "completed" })).toEqual({
			title: "Run completed",
			label: "Note",
		});
		expect(runDetail({ status: "running" })).toEqual({ title: "Run details", label: "Note" });
	});
});

describe("macroDoneSignal: what a macro:done event is worth telling the user", () => {
	const done = (over) => ({
		kind: "macro:done",
		macro_name: "Month-end close",
		conversation: "conv-m",
		status: "failed",
		error: "Step 2 failed: no such customer",
		...over,
	});

	it("a failed run is announced with the macro's name and the reason", () => {
		expect(macroDoneSignal(done())).toEqual({
			title: "Macro failed: Month-end close",
			body: "Step 2 failed: no such customer",
		});
	});

	it("a failed run with no reason still gets a sentence", () => {
		expect(macroDoneSignal(done({ error: "" })).body).toBe(
			"Open the run to see what happened."
		);
	});

	it("an event from an older server (no name) is still announced", () => {
		expect(macroDoneSignal(done({ macro_name: undefined })).title).toBe("Macro failed");
	});

	it("a run that worked, or that the user stopped, is not announced", () => {
		expect(macroDoneSignal(done({ status: "completed", error: "" }))).toBeNull();
		expect(macroDoneSignal(done({ status: "stopped", error: "" }))).toBeNull();
	});

	it("a run stopped at a confirmation card is not announced twice", () => {
		// The card itself already raised "needs your confirmation" when it parked.
		expect(
			macroDoneSignal(done({ status: "stopped", error: "Step 1 is waiting…" }))
		).toBeNull();
	});
});
