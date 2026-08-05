import { describe, it, expect } from "vitest";
import { pickGreeting, timeBucket, gapBucket } from "./greeting.js";

// Build a local-time instant, so these assertions do not drift with the runner's
// timezone (the greeting follows the user's own clock by design).
const at = (y, m, d, h) => new Date(y, m - 1, d, h, 0, 0).getTime();
const DAY = 86400000;

describe("timeBucket", () => {
	it("maps the day into its buckets", () => {
		expect(timeBucket(9)).toBe("morning");
		expect(timeBucket(14)).toBe("afternoon");
		expect(timeBucket(19)).toBe("evening");
		expect(timeBucket(6)).toBe("early");
	});

	it("wraps night across midnight", () => {
		// The charming edge: 23:00 through 04:59 is one bucket, not two.
		expect(timeBucket(23)).toBe("night");
		expect(timeBucket(0)).toBe("night");
		expect(timeBucket(2)).toBe("night");
		expect(timeBucket(4)).toBe("night");
	});

	it("pins the bucket boundaries", () => {
		expect(timeBucket(22)).toBe("evening"); // still evening
		expect(timeBucket(5)).toBe("early"); // night has ended
		expect(timeBucket(8)).toBe("morning");
		expect(timeBucket(12)).toBe("afternoon");
		expect(timeBucket(17)).toBe("evening");
	});

	it("falls back rather than throwing on junk", () => {
		expect(timeBucket(undefined)).toBe("morning");
		expect(timeBucket("nope")).toBe("morning");
	});
});

describe("gapBucket", () => {
	const now = at(2026, 8, 20, 10);

	it("is null for a recent chat", () => {
		expect(gapBucket(now, new Date(now - 2 * DAY))).toBe(null);
	});

	it("pins the thresholds", () => {
		expect(gapBucket(now, new Date(now - 3 * DAY))).toBe("short");
		expect(gapBucket(now, new Date(now - 13 * DAY))).toBe("short");
		expect(gapBucket(now, new Date(now - 14 * DAY))).toBe("long");
	});

	it("is null when there is no history at all", () => {
		expect(gapBucket(now, null)).toBe(null);
		expect(gapBucket(now, "")).toBe(null);
		expect(gapBucket(now, "not a date")).toBe(null);
	});

	it("treats a future timestamp as no gap, not a huge one", () => {
		// Clock skew between the bench and the browser must not produce
		// "Long time no see" for someone who just chatted.
		expect(gapBucket(now, new Date(now + 2 * DAY))).toBe(null);
	});

	it("parses Frappe's naive 'YYYY-MM-DD HH:MM:SS' form", () => {
		expect(gapBucket(at(2026, 8, 20, 10), "2026-08-01 09:30:00")).toBe("long");
	});
});

describe("pickGreeting", () => {
	it("greets by time of day when there is no gap", () => {
		const line = pickGreeting({ now: at(2026, 8, 20, 14), lastChatAt: at(2026, 8, 20, 9) });
		expect(["Afternoon", "Good afternoon"]).toContain(line);
	});

	it("uses a late-night line after 11pm", () => {
		const line = pickGreeting({ now: at(2026, 8, 20, 23), lastChatAt: at(2026, 8, 20, 9) });
		expect(["Up late", "Burning the midnight oil", "Night owl hours"]).toContain(line);
	});

	it("welcomes back after a gap, and the gap wins over the clock", () => {
		const now = at(2026, 8, 20, 14);
		const line = pickGreeting({ now, lastChatAt: now - 20 * DAY });
		expect(["Long time no see", "Welcome back"]).toContain(line);
		expect(["Afternoon", "Good afternoon"]).not.toContain(line);
	});

	it("is stable for the same inputs (no reshuffle on re-render)", () => {
		const args = { now: at(2026, 8, 20, 23), lastChatAt: at(2026, 8, 20, 9) };
		const first = pickGreeting(args);
		for (let i = 0; i < 5; i++) expect(pickGreeting(args)).toBe(first);
	});

	it("varies across days rather than showing one line forever", () => {
		const seen = new Set();
		for (let day = 1; day <= 14; day++) {
			seen.add(pickGreeting({ now: at(2026, 9, day, 23), lastChatAt: at(2026, 9, day, 9) }));
		}
		expect(seen.size).toBeGreaterThan(1);
	});

	it("still greets a brand-new user with no history", () => {
		const line = pickGreeting({ now: at(2026, 8, 20, 9), lastChatAt: null });
		expect(["Morning", "Good morning"]).toContain(line);
	});

	it("never returns an empty string, even with no arguments", () => {
		expect(pickGreeting()).toBeTruthy();
		expect(pickGreeting({}).length).toBeGreaterThan(0);
	});

	it("carries no product name, so whitelabel and Jara are safe", () => {
		for (let h = 0; h < 24; h++) {
			const line = pickGreeting({ now: at(2026, 8, 20, h), lastChatAt: null });
			expect(line.toLowerCase()).not.toContain("jarvis");
			expect(line.toLowerCase()).not.toContain("jara");
		}
	});
});
