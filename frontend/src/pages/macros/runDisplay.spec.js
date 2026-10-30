import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { RUN_THEMES, STATUS_LABELS, RUN_STATUS_OPTIONS, statusLabel } from "./runDisplay";
import { LIVE_RUN } from "@/lib/macroRunOutcome";

/**
 * How a run's status is worded and coloured on every screen. The doctype is the
 * source of the statuses a run can have; a status this bundle has no word for
 * would show as a raw code (or as a gray badge no filter can select).
 */

// vitest runs from frontend/: the doctype is the app's, one level up.
const doctype = JSON.parse(
	readFileSync(
		resolve(process.cwd(), "../jarvis/jarvis/doctype/jarvis_macro_run/jarvis_macro_run.json"),
		"utf8"
	)
);
const STATUSES = doctype.fields.find((f) => f.fieldname === "status").options.split("\n");

describe("a run waiting for a report", () => {
	it("is worded and coloured as a parked run, not a failure", () => {
		expect(statusLabel("waiting_report")).toBe("Waiting for report");
		expect(RUN_THEMES.waiting_report).toBe("orange");
	});
});

describe("every status the doctype allows is covered", () => {
	it("has a badge colour", () => {
		for (const s of STATUSES) expect(RUN_THEMES, s).toHaveProperty(s);
	});

	it("has a readable label: a worded one, or the capitalised status", () => {
		for (const s of STATUSES) {
			const label = statusLabel(s);
			expect(label, s).not.toBe("");
			expect(label, s).not.toContain("_");
		}
		for (const s of STATUSES.filter((s) => s.includes("_")))
			expect(STATUS_LABELS, s).toHaveProperty(s);
	});

	it("is a choice in the Runs tab's status filter, with the label the badge shows", () => {
		const options = RUN_STATUS_OPTIONS.filter((o) => o.value);
		expect(options.map((o) => o.value).sort()).toEqual([...STATUSES].sort());
		for (const o of options) expect(o.label, o.value).toBe(statusLabel(o.value));
		expect(RUN_STATUS_OPTIONS[0]).toEqual({ label: "All statuses", value: "" });
	});

	it("keeps the live statuses a subset of the doctype's", () => {
		for (const s of LIVE_RUN) expect(STATUSES).toContain(s);
	});
});
