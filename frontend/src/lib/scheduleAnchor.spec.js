import { describe, it, expect } from "vitest";
import {
	ordinal,
	deriveScheduleDay,
	scheduleAnchorPhrase,
	WEEKDAY_OPTIONS,
	DAY_OF_MONTH_OPTIONS,
} from "./scheduleAnchor";

describe("ordinal", () => {
	it("handles the 11/12/13 exception", () => {
		expect(ordinal(11)).toBe("11th");
		expect(ordinal(12)).toBe("12th");
		expect(ordinal(13)).toBe("13th");
	});
	it("handles the mod-10 cases outside the teens", () => {
		expect(ordinal(1)).toBe("1st");
		expect(ordinal(2)).toBe("2nd");
		expect(ordinal(3)).toBe("3rd");
		expect(ordinal(21)).toBe("21st");
		expect(ordinal(22)).toBe("22nd");
		expect(ordinal(23)).toBe("23rd");
		expect(ordinal(31)).toBe("31st");
	});
	it("defaults to th for everything else", () => {
		expect(ordinal(4)).toBe("4th");
		expect(ordinal(15)).toBe("15th");
		expect(ordinal(20)).toBe("20th");
	});
});

describe("option lists", () => {
	it("WEEKDAY_OPTIONS has all 7 days, Monday first", () => {
		expect(WEEKDAY_OPTIONS).toHaveLength(7);
		expect(WEEKDAY_OPTIONS[0].value).toBe("Monday");
		expect(WEEKDAY_OPTIONS[6].value).toBe("Sunday");
	});
	it("DAY_OF_MONTH_OPTIONS covers 1..31 with ordinal labels", () => {
		expect(DAY_OF_MONTH_OPTIONS).toHaveLength(31);
		expect(DAY_OF_MONTH_OPTIONS[0]).toEqual({ label: "1st", value: "1" });
		expect(DAY_OF_MONTH_OPTIONS[30]).toEqual({ label: "31st", value: "31" });
	});
});

describe("deriveScheduleDay", () => {
	it("reads the weekday for a weekly row", () => {
		expect(deriveScheduleDay("weekly", "Wednesday", null)).toBe("Wednesday");
	});
	it("reads the day of month (stringified) for a monthly row", () => {
		expect(deriveScheduleDay("monthly", null, 15)).toBe("15");
	});
	it("is empty for daily, or a weekly/monthly row with no anchor saved", () => {
		expect(deriveScheduleDay("daily", "Wednesday", 15)).toBe("");
		expect(deriveScheduleDay("weekly", null, null)).toBe("");
		expect(deriveScheduleDay("monthly", null, null)).toBe("");
		expect(deriveScheduleDay("monthly", null, 0)).toBe("");
	});
});

describe("scheduleAnchorPhrase", () => {
	it("names the weekday for weekly", () => {
		expect(scheduleAnchorPhrase("weekly", "Monday")).toBe("on Monday");
	});
	it("names the ordinal day for monthly", () => {
		expect(scheduleAnchorPhrase("monthly", "15")).toBe("on the 15th");
	});
	it("is empty with no day, or for daily", () => {
		expect(scheduleAnchorPhrase("weekly", "")).toBe("");
		expect(scheduleAnchorPhrase("monthly", "")).toBe("");
		expect(scheduleAnchorPhrase("daily", "")).toBe("");
	});
});
