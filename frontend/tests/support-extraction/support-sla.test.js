import { describe, it, expect } from "vitest";
import { formatDuration, firstResponseBadge, resolutionBadge } from "@/lib/supportSla";

// Pure logic — inputs are epoch-ms numbers with an injected `nowMs`, so these are
// deterministic and need no frappe-ui / timezone setup (the panel does the site-tz
// -> ms conversion via @/utils/datetime before calling in).

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
		const b = firstResponseBadge({ firstRespondedOn: null, responseBy: NOW + 2 * HOUR }, NOW);
		expect(b).toEqual({ label: "Due in 2h 0m", theme: "orange" });
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
		const b = firstResponseBadge({ firstRespondedOn: null, responseBy: NOW - HOUR }, NOW);
		expect(b).toEqual({ label: "Failed", theme: "red" });
	});
	it("null when there is no first-response SLA at all (no false Failed)", () => {
		expect(firstResponseBadge({ firstRespondedOn: null, responseBy: null }, NOW)).toBeNull();
		expect(firstResponseBadge(null, NOW)).toBeNull();
	});
});

describe("resolutionBadge", () => {
	it("Due (orange) when unresolved and the target is still ahead", () => {
		const b = resolutionBadge({ resolutionDate: null, resolutionBy: NOW + DAY }, NOW);
		expect(b).toEqual({ label: "Due in 1d 0h", theme: "orange" });
	});
	it("Fulfilled (green) with resolution_time (seconds) when agreement is Fulfilled", () => {
		const b = resolutionBadge({ agreementStatus: "Fulfilled", resolutionTime: 7200 }, NOW);
		expect(b).toEqual({ label: "Fulfilled in 2h 0m", theme: "green" });
	});
	it("Failed (red) when the agreement failed", () => {
		const b = resolutionBadge(
			{
				resolutionDate: NOW - HOUR,
				resolutionBy: NOW - 2 * HOUR,
				agreementStatus: "Failed",
			},
			NOW
		);
		expect(b).toEqual({ label: "Failed", theme: "red" });
	});
	it("null when there is no resolution SLA at all", () => {
		expect(resolutionBadge({}, NOW)).toBeNull();
		expect(resolutionBadge(null, NOW)).toBeNull();
	});
});
