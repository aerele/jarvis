import { describe, it, expect, vi } from "vitest";

// The real dayjs (no stand-in): the label's date wording is what is under test.
vi.mock("frappe-ui", async () => ({
	dayjs: (await import("./testDayjs.js")).default,
	dayjsLocal: () => ({}),
	getConfig: () => "",
}));

import { ranOnceLabel, scheduleLabel } from "./scheduleLabel";

describe("scheduleLabel", () => {
	it("names a one-time schedule's date and time", () => {
		expect(
			scheduleLabel({
				schedule_frequency: "once",
				schedule_date: "2026-11-02",
				schedule_time: "09:00:00",
			})
		).toBe("Once · Mon, 2 Nov 2026 · 9:00 am");
	});

	it("leaves the recurring wording as it was", () => {
		expect(
			scheduleLabel({
				schedule_frequency: "weekly",
				schedule_weekday: "Monday",
				schedule_date: "2026-11-02",
				schedule_time: "09:00:00",
			})
		).toBe("Weekly on Monday · 9:00 am");
	});
});

describe("ranOnceLabel", () => {
	it("says a one-time schedule ran, and when", () => {
		expect(
			ranOnceLabel({
				schedule_enabled: 0,
				schedule_frequency: "once",
				last_run_at: "2026-11-02 09:00:04",
			})
		).toBe("Ran once · Mon, 2 Nov");
	});

	it("is empty for anything else", () => {
		expect(
			ranOnceLabel({
				schedule_enabled: 1,
				schedule_frequency: "once",
				last_run_at: "2026-11-02 09:00:04",
			})
		).toBe("");
		expect(
			ranOnceLabel({
				schedule_enabled: 0,
				schedule_frequency: "daily",
				last_run_at: "2026-11-02 09:00:04",
			})
		).toBe("");
		expect(
			ranOnceLabel({ schedule_enabled: 0, schedule_frequency: "once", last_run_at: null })
		).toBe("");
	});
});
