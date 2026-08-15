import { describe, it, expect } from "vitest";
import { formatDuration, firstResponseBadge, resolutionBadge } from "@/lib/supportSla";

// Pure logic — inputs are epoch-ms numbers with an injected `nowMs`, so these are
// deterministic and need no frappe-ui / timezone setup (the panel does the site-tz
// -> ms conversion via @/utils/datetime before calling in). Each badge is computed
// PER STAGE from its own deadline fields; `agreementStatus` only supplies the
// "Paused" (awaiting-customer / on-hold) override.

const NOW = 1_700_000_000_000; // fixed "now"
const HOUR = 3_600_000;
const DAY = 86_400_000;

describe("formatDuration", () => {
	it("renders seconds -> the largest two units", () => {
		expect(formatDuration(0)).toBe("0s");
		expect(formatDuration(45)).toBe("45s");
		expect(formatDuration(90)).toBe("1m");
		expect(formatDuration(3661)).toBe("1h 1m");
		expect(formatDuration(90000)).toBe("1d 1h");
	});
	it("floors negatives and junk to 0s", () => {
		expect(formatDuration(-5)).toBe("0s");
		expect(formatDuration(undefined)).toBe("0s");
	});
});

describe("firstResponseBadge", () => {
	it("Due (orange) when unanswered and the target is still ahead", () => {
		expect(firstResponseBadge({ responseBy: NOW + 2 * HOUR }, NOW)).toEqual({
			label: "Due in 2h 0m",
			theme: "orange",
		});
	});
	it("Fulfilled (green) with time-to-first-response when answered before target", () => {
		const creation = NOW;
		const b = firstResponseBadge(
			{ firstRespondedOn: creation + 300_000, responseBy: creation + HOUR, creation },
			NOW
		);
		expect(b).toEqual({ label: "Fulfilled in 5m", theme: "green" });
	});
	it("Failed (red) when the response came after the target", () => {
		const b = firstResponseBadge(
			{ firstRespondedOn: NOW + 2 * HOUR, responseBy: NOW + HOUR, creation: NOW },
			NOW
		);
		expect(b).toEqual({ label: "Failed", theme: "red" });
	});
	it("Failed (red) when unanswered and the target has passed", () => {
		expect(firstResponseBadge({ responseBy: NOW - HOUR }, NOW)).toEqual({
			label: "Failed",
			theme: "red",
		});
	});
	it("On hold (gray) when unanswered and the SLA is Paused (awaiting the customer)", () => {
		// Would otherwise read Failed on the stale past deadline — the bug this fixes.
		expect(
			firstResponseBadge({ responseBy: NOW - HOUR, agreementStatus: "Paused" }, NOW)
		).toEqual({ label: "On hold", theme: "gray" });
	});
	it("null when there is no first-response SLA at all", () => {
		expect(firstResponseBadge({}, NOW)).toBeNull();
		expect(firstResponseBadge(null, NOW)).toBeNull();
	});
});

describe("resolutionBadge", () => {
	it("Due (orange) when unresolved and the target is still ahead", () => {
		expect(resolutionBadge({ resolutionBy: NOW + DAY }, NOW)).toEqual({
			label: "Due in 1d 0h",
			theme: "orange",
		});
	});
	it("Fulfilled (green) with resolution_time when resolved before the target", () => {
		const b = resolutionBadge(
			{ resolutionDate: NOW - HOUR, resolutionBy: NOW, resolutionTime: 7200 },
			NOW
		);
		expect(b).toEqual({ label: "Fulfilled in 2h 0m", theme: "green" });
	});
	it("Failed (red) when resolved AFTER the target", () => {
		const b = resolutionBadge({ resolutionDate: NOW, resolutionBy: NOW - HOUR }, NOW);
		expect(b).toEqual({ label: "Failed", theme: "red" });
	});
	it("Failed (red) when unresolved and the target has passed", () => {
		expect(resolutionBadge({ resolutionBy: NOW - HOUR }, NOW)).toEqual({
			label: "Failed",
			theme: "red",
		});
	});
	it("On hold (gray), NOT Failed, for an awaiting-customer (Paused) ticket — the High-2 fix", () => {
		// "Replied / Awaiting you" freezes resolution_by in the past with no
		// resolution_date; the old code rendered a false red "Failed" (blaming us for
		// the customer's delay). Paused must read as On hold.
		expect(
			resolutionBadge(
				{ resolutionBy: NOW - DAY, resolutionDate: null, agreementStatus: "Paused" },
				NOW
			)
		).toEqual({ label: "On hold", theme: "gray" });
	});
	it("null when there is no resolution SLA at all", () => {
		expect(resolutionBadge({}, NOW)).toBeNull();
		expect(resolutionBadge(null, NOW)).toBeNull();
	});
});

describe("SLA boundary (Helpdesk: on-the-dot is Fulfilled)", () => {
	it("treats a response/resolution landing EXACTLY on the deadline as Fulfilled, not Failed", () => {
		expect(
			firstResponseBadge(
				{ firstRespondedOn: NOW, responseBy: NOW, creation: NOW - 60_000 },
				NOW
			)
		).toMatchObject({ theme: "green" });
		expect(
			resolutionBadge({ resolutionDate: NOW, resolutionBy: NOW, resolutionTime: 60 }, NOW)
		).toMatchObject({ theme: "green" });
	});
});
