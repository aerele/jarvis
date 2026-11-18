import { describe, it, expect } from "vitest";
import { fmtTokens, contextReading, limitWindow, limitReading } from "./tokens.js";

describe("fmtTokens", () => {
});

describe("contextReading", () => {
	it("reads the used/capacity pair when fresh", () => {
		const r = contextReading({ context: { used: 42000, capacity: 200000, fresh: true } });
		expect(r).toEqual({ fresh: true, text: "42k of 200k context in use" });
	});
	it("falls back to not measured yet when not fresh", () => {
		const r = contextReading({ context: { used: 0, capacity: 0, fresh: false } });
		expect(r).toEqual({ fresh: false, text: "Not measured yet" });
	});
});

describe("limitWindow", () => {
	it("an all-time cap counts the cumulative total", () => {
		const w = limitWindow({
			total_tokens: 500000,
			period_tokens: 40000,
			limit_period: "All time",
		});
		expect(w).toEqual({ used: 500000, label: "all time" });
	});
	it("a missing period reads as all time", () => {
		expect(limitWindow({ total_tokens: 7 })).toEqual({ used: 7, label: "all time" });
	});
	it("a daily / weekly / monthly cap counts the current window", () => {
		const m = { total_tokens: 500000, period_tokens: 40000 };
		expect(limitWindow({ ...m, limit_period: "Daily" })).toEqual({
			used: 40000,
			label: "today",
		});
		expect(limitWindow({ ...m, limit_period: "Weekly" })).toEqual({
			used: 40000,
			label: "this week",
		});
		expect(limitWindow({ ...m, limit_period: "Monthly" })).toEqual({
			used: 40000,
			label: "this month",
		});
	});
});

describe("limitReading", () => {
	const weekly = {
		monthly_token_limit: 1000000,
		limit_period: "Weekly",
		period_tokens: 400000,
		total_tokens: 4200000,
	};
	it("no cap reads as none", () => {
		expect(limitReading({ monthly_token_limit: 0 }).state).toBe("none");
		expect(limitReading(null).state).toBe("none");
	});
	it("under 80% is quiet, with the window label and reset day", () => {
		expect(limitReading(weekly)).toEqual({
			state: "quiet",
			pct: 40,
			used: 400000,
			limit: 1000000,
			label: "this week",
			reset: "Sunday",
		});
	});
	it("80% and above warns; the cap itself is full", () => {
		expect(limitReading({ ...weekly, period_tokens: 820000 }).state).toBe("warn");
		expect(limitReading({ ...weekly, period_tokens: 1000000 }).state).toBe("full");
		expect(limitReading({ ...weekly, period_tokens: 1200000 }).pct).toBe(100);
	});
	it("daily and monthly name their reset; all time has none", () => {
		expect(limitReading({ ...weekly, limit_period: "Daily" }).reset).toBe("midnight");
		expect(limitReading({ ...weekly, limit_period: "Monthly" }).reset).toBe("the 1st");
		expect(limitReading({ ...weekly, limit_period: "All time" }).reset).toBe("");
	});
});
