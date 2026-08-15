import { describe, it, expect, beforeEach } from "vitest";
import {
	pendingDashboardPrefill,
	setDashboardPrefill,
	takeDashboardPrefill,
} from "./dashboardPrefill.js";

// The cross-view stash a ```jarvis-goto redirect hands to the Dashboards
// builder (jarvis#884), mirroring chatPrefill.js's one-shot contract exactly:
// set stashes a payload, take reads it and empties the slot in the same call,
// so a later plain visit to the page can never replay a stale prompt.
describe("dashboardPrefill", () => {
	beforeEach(() => {
		pendingDashboardPrefill.value = null;
	});

	it("starts empty", () => {
		expect(takeDashboardPrefill()).toBeNull();
	});

	it("returns what was set", () => {
		setDashboardPrefill({ text: "Monthly sales by territory", autoSend: true });
		expect(takeDashboardPrefill()).toEqual({
			text: "Monthly sales by territory",
			autoSend: true,
		});
	});

	it("is one-shot: a second take on the same mount finds nothing", () => {
		setDashboardPrefill({ text: "x", autoSend: true });
		takeDashboardPrefill();
		expect(takeDashboardPrefill()).toBeNull();
	});

	it("setDashboardPrefill(null/undefined) clears the slot", () => {
		setDashboardPrefill({ text: "x", autoSend: true });
		setDashboardPrefill(null);
		expect(takeDashboardPrefill()).toBeNull();
	});
});
