import { describe, it, expect, vi } from "vitest";

// The stored date read as a calendar day; the real dayjs is not needed to check the
// label's wording, only that the date is formatted and placed.
vi.mock("frappe-ui", () => ({
	dayjs: (d) => ({ format: () => `day(${d})` }),
	dayjsLocal: () => ({}),
	getConfig: () => "",
}));

import { scheduleLabel } from "./scheduleLabel";

describe("scheduleLabel", () => {
	it("names a one-time schedule's date and time", () => {
		expect(
			scheduleLabel({
				schedule_frequency: "once",
				schedule_date: "2026-11-02",
				schedule_time: "09:00:00",
			})
		).toBe("Once · day(2026-11-02) · 9:00 am");
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
