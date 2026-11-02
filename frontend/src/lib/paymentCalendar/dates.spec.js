import {
	addDays,
	addMonths,
	monthGrid,
	isSelectable,
	siteToday,
	msUntilSiteMidnight,
	dateLabel,
} from "./dates";

describe("Payment Calendar dates", () => {
	it("keeps February leap day and complete weeks", () => {
		const days = monthGrid("2028-02");
		expect(days).toHaveLength(42);
		expect(days).toContain("2028-02-29");
		expect(days[0]).toBe("2028-01-30");
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
});
