import { describe, it, expect } from "vitest";
import {
	activityOptions,
	activityLabel,
	defaultRange,
	flagTheme,
	formatCount,
	formatWhen,
	legendLines,
	outcomeLabel,
	outcomeTheme,
	reportUrl,
	earliestEvent,
	formatDay,
	queryFromState,
	stateFromQuery,
} from "./activityFormat";

describe("activityFormat", () => {
	it("reads a site datetime as stored, without a timezone shift", () => {
		expect(formatWhen("2026-11-17 14:05:09.123456")).toBe("Nov 17, 14:05");
		expect(formatWhen("2026-01-02T00:00:00")).toBe("Jan 2, 00:00");
		expect(formatWhen("not a date")).toBe("not a date");
		expect(formatWhen(null)).toBe("");
	});

	it("formats counts and survives junk", () => {
		expect(formatCount(1234567)).toBe("1,234,567");
		expect(formatCount(undefined)).toBe("0");
	});

	it("opens on the last 30 days, open-ended (To empty: the server reads up to now)", () => {
		expect(defaultRange(new Date(2026, 10, 17))).toEqual({ from: "2026-10-18", to: "" });
	});

	it("finds the earliest recorded event across both sources", () => {
		expect(earliestEvent({ agent: "2026-10-01 09:00:00", app: "2026-10-20 08:00:00" })).toBe(
			"2026-10-01"
		);
		expect(earliestEvent({ agent: null, app: "2026-10-20 08:00:00" })).toBe("2026-10-20");
		expect(earliestEvent({ agent: null, app: null })).toBe("");
		expect(earliestEvent(undefined)).toBe("");
		expect(earliestEvent({ agent: "junk" })).toBe("");
	});

	it("formats a day without a timezone shift", () => {
		expect(formatDay("2026-10-01")).toBe("Oct 1, 2026");
		expect(formatDay("nope")).toBe("nope");
	});

	it("labels what the server filters on and keeps unknown values visible", () => {
		expect(activityLabel("denied_call")).toBe("Refused call");
		expect(activityLabel("brand_new")).toBe("brand_new");
		expect(outcomeLabel("ok")).toBe("OK");
		expect(new Set(activityOptions().map((o) => o.value)).size).toBe(activityOptions().length);
	});

	it("themes outcomes and flags", () => {
		expect(outcomeTheme("applied")).toBe("green");
		expect(outcomeTheme("refused")).toBe("red");
		expect(outcomeTheme("partial")).toBe("orange");
		expect(outcomeTheme("x")).toBe("gray");
		expect(flagTheme("delete_burst")).toBe("red");
		expect(flagTheme("off_hours")).toBe("orange");
	});

	it("builds the legend from the thresholds it is given", () => {
		const lines = legendLines({
			delete_burst_count: 10,
			delete_burst_minutes: 30,
			big_export_rows: 1000,
			daily_export_rows: 2000,
			refusals_per_day: 3,
			business_hours_start: 9,
			business_hours_end: 18,
		});
		expect(lines.map((l) => l.key)).toEqual([
			"delete_burst",
			"big_export",
			"daily_export",
			"refusals",
			"off_hours",
		]);
		expect(lines[0].text).toContain("10 or more ERP deletes");
		expect(lines[0].text).toContain("30 minutes");
		expect(lines[4].text).toContain("before 09:00 or from 18:00");
		expect(legendLines(null)).toEqual([]);
	});

	it("links the Desk report with the filters that apply to the view", () => {
		const f = {
			from: "2026-11-01",
			to: "2026-11-09",
			user: "a@b.co",
			activity: "read",
			outcome: "ok",
		};
		const summary = reportUrl({ view: "summary", ...f });
		expect(summary).toContain("view=Summary");
		expect(summary).not.toContain("activity=");
		const events = reportUrl({ view: "events", ...f });
		expect(events).toContain("view=Events");
		expect(events).toContain("activity=read");
		expect(events).toContain("outcome=ok");
		expect(events).toContain("user=a%40b.co");
	});
});

describe("activityFormat query sync", () => {
	const defaults = { from: "2026-10-18", to: "" };
	const filters = {
		from: "2026-11-02",
		to: "2026-11-09",
		user: "a@b.co",
		activity: "delete",
		outcome: "denied",
	};

	it("writes the applied filters and tab, leaving empty ones out", () => {
		expect(queryFromState({ tab: "events", filters })).toEqual({
			tab: "events",
			from: "2026-11-02",
			to: "2026-11-09",
			user: "a@b.co",
			activity: "delete",
			outcome: "denied",
		});
		// summary is the default tab; a cleared From is kept (as empty) so it reads back as cleared
		expect(
			queryFromState({
				tab: "summary",
				filters: { from: "", to: "", user: "", activity: "", outcome: "" },
			})
		).toEqual({ from: "" });
	});

	it("reads them back", () => {
		expect(stateFromQuery(queryFromState({ tab: "events", filters }), defaults)).toEqual({
			tab: "events",
			filters,
		});
	});

	it("falls back to the defaults for a bare URL", () => {
		expect(stateFromQuery({}, defaults)).toEqual({
			tab: "summary",
			filters: { from: "2026-10-18", to: "", user: "", activity: "", outcome: "" },
		});
	});

	it("keeps a deliberately cleared From", () => {
		expect(stateFromQuery({ from: "" }, defaults).filters.from).toBe("");
	});

	it("ignores invalid values and keeps the valid ones", () => {
		const s = stateFromQuery(
			{
				tab: "nonsense",
				from: "not-a-date",
				to: "2026-02-31",
				user: ["x", "y"],
				activity: "drop table",
				outcome: "ok",
			},
			defaults
		);
		expect(s).toEqual({
			tab: "summary",
			filters: { from: "2026-10-18", to: "", user: "", activity: "", outcome: "ok" },
		});
	});

	it("ignores a To that precedes From", () => {
		const s = stateFromQuery({ from: "2026-11-10", to: "2026-11-01" }, defaults);
		expect(s.filters).toMatchObject({ from: "2026-11-10", to: "" });
	});
});
