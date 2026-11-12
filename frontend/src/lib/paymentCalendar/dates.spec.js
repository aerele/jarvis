import {
	addDays,
	addMonths,
	monthGrid,
	weekStrip,
	weekLabel,
	isSelectable,
	siteToday,
	msUntilSiteMidnight,
	dateLabel,
	dayMonthLabel,
	fullDateLabel,
	weekdayShort,
	snapshotStamp,
} from "./dates";

describe("Payment Calendar dates", () => {
	it("keeps February leap day and complete weeks", () => {
		const days = monthGrid("2028-02");
		expect(days).toHaveLength(35);
		expect(days).toContain("2028-02-29");
		expect(days[0]).toBe("2028-01-30");
		expect(days[34]).toBe("2028-03-04");
	});
	it.each([
		["2026-02", 4, "2026-02-01", "2026-02-28"],
		["2026-11", 5, "2026-11-01", "2026-12-05"],
		["2026-08", 6, "2026-07-26", "2026-09-05"],
	])("sizes %s to %i Sunday-start weeks", (month, weeks, first, last) => {
		const days = monthGrid(month);
		expect(days).toHaveLength(weeks * 7);
		expect(days[0]).toBe(first);
		expect(days.at(-1)).toBe(last);
		expect(new Date(days[0] + "T12:00:00Z").getUTCDay()).toBe(0);
	});
	it("returns no grid for an invalid month", () => {
		expect(monthGrid("2026-13")).toEqual([]);
		expect(monthGrid("")).toEqual([]);
	});
	it("builds a Monday-start week strip containing the date", () => {
		expect(weekStrip("2026-11-11")).toEqual([
			"2026-11-09",
			"2026-11-10",
			"2026-11-11",
			"2026-11-12",
			"2026-11-13",
			"2026-11-14",
			"2026-11-15",
		]);
	});
	it("keeps Sunday at the end of its own week", () => {
		expect(weekStrip("2026-11-01")[0]).toBe("2026-10-26");
		expect(weekStrip("2026-11-01")[6]).toBe("2026-11-01");
		expect(weekStrip("2026-11-02")[0]).toBe("2026-11-02");
	});
	it("carries the week strip across year and leap-day boundaries", () => {
		const year = weekStrip("2026-12-31");
		expect(year[0]).toBe("2026-12-28");
		expect(year[6]).toBe("2027-01-03");
		const leap = weekStrip("2028-02-29");
		expect(leap[0]).toBe("2028-02-28");
		expect(leap[6]).toBe("2028-03-05");
	});
	it("returns no week strip for an invalid date", () => {
		expect(weekStrip("2026-02-30")).toEqual([]);
		expect(weekStrip(null)).toEqual([]);
	});
	it("labels a week as Monday – Sunday with an en dash", () => {
		expect(weekLabel("2026-11-01")).toBe("26 Oct \u2013 1 Nov");
		expect(weekLabel("2026-11-11")).toBe("9 Nov \u2013 15 Nov");
		expect(weekLabel("2026-12-31")).toBe("28 Dec \u2013 3 Jan");
		expect(weekLabel("2026-02-30")).toBe("");
	});
	it("derives grid and week dates from the string, not the browser zone", () => {
		const original = process.env.TZ;
		for (const zone of ["Pacific/Kiritimati", "Pacific/Pago_Pago"]) {
			process.env.TZ = zone;
			expect(monthGrid("2026-11")[0]).toBe("2026-11-01");
			expect(weekStrip("2026-11-11")[0]).toBe("2026-11-09");
			expect(weekLabel("2026-11-01")).toBe("26 Oct \u2013 1 Nov");
		}
		if (original === undefined) delete process.env.TZ;
		else process.env.TZ = original;
	});
	it("carries across year and month boundaries", () => {
		expect(addMonths("2026-12", 1)).toBe("2027-01");
		expect(addDays("2028-02-28", 1)).toBe("2028-02-29");
		expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
	});
	it("does not make carry-window dates selectable", () => {
		const scope = { selectable_start: "2025-10-01", selectable_end: "2027-09-30" };
		expect(isSelectable("2027-09-30", scope)).toBe(true);
		expect(isSelectable("2027-10-01", scope)).toBe(false);
		expect(isSelectable("2025-09-30", scope)).toBe(false);
		expect(isSelectable("2026-02-30", scope)).toBe(false);
	});
	it("uses the site date when the browser's calendar day differs", () => {
		const epoch = Date.parse("2026-10-24T20:00:00Z");
		expect(siteToday("Asia/Kolkata", epoch)).toBe("2026-10-25");
		expect(siteToday("America/Los_Angeles", epoch)).toBe("2026-10-24");
		expect(siteToday("", epoch)).toBe("");
	});
	it("finds midnight through 23-hour and 25-hour DST days", () => {
		expect(msUntilSiteMidnight("Europe/Berlin", Date.parse("2026-03-28T23:00:00Z"))).toBe(
			23 * 3600000
		);
		expect(msUntilSiteMidnight("Europe/Berlin", Date.parse("2026-10-24T22:00:00Z"))).toBe(
			25 * 3600000
		);
	});
	it("formats contractual dates without shifting their day", () => {
		expect(dateLabel("2026-10-24")).toContain("24");
		expect(dateLabel("2026-02-30")).toBe("Unavailable date");
	});
	it("formats the template's short date labels in the page's language", () => {
		expect(dayMonthLabel("2026-11-11")).toBe("11 Nov");
		expect(fullDateLabel("2026-11-11")).toBe("Wednesday, 11 Nov");
		expect(weekdayShort("2026-11-09")).toBe("Mon");
		for (const fn of [dayMonthLabel, fullDateLabel, weekdayShort])
			expect(fn("2026-02-30")).toBe("");
	});
});

describe("September", () => {
	it("is always the three-letter Sep, whatever the runtime locale data says", () => {
		expect(dayMonthLabel("2026-09-15")).toBe("15 Sep");
		expect(fullDateLabel("2026-09-16")).toBe("Wednesday, 16 Sep");
		expect(snapshotStamp("2026-09-15T05:12:00Z", "Asia/Kolkata")).toBe("15 Sep, 10:42 IST");
	});
});

describe("snapshotStamp", () => {
	it("formats the template stamp in the site zone", () => {
		expect(snapshotStamp("2026-11-11T05:12:00+00:00", "Asia/Kolkata")).toBe(
			"11 Nov, 10:42 IST"
		);
	});
	it("uses the site zone's day, not the browser's", () => {
		expect(snapshotStamp("2026-11-11T20:00:00Z", "Asia/Kolkata")).toBe("12 Nov, 01:30 IST");
	});
	it("falls back to UTC when the site has no zone", () => {
		expect(snapshotStamp("2026-11-11T05:12:00Z", "")).toBe("11 Nov, 05:12 UTC");
	});
	it("never throws on bad input", () => {
		expect(snapshotStamp("not a date", "Asia/Kolkata")).toBe("Unavailable time");
		expect(snapshotStamp("2026-11-11T05:12:00Z", "Not/AZone")).toBe("Unavailable time");
	});
});
