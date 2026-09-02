import { describe, it, expect } from "vitest";
import { agentsEmptyState, shouldProbeWholeCatalog } from "./agentsEmptyState";

/**
 * The bug this encodes: agent access is deny-by-default (jarvis#1062), so a
 * plain user with nothing granted lands on the catalog and sees an empty list.
 * The catalog opens on Featured, whose empty state said "No featured agents yet
 * / Browse the Available tab for the full catalog" - pointing them at a tab that
 * is just as empty, with a button that takes them there. The honest message has
 * to win on EVERY tab for that user, and the call to action has to go, because
 * there is nowhere to send them.
 *
 * The states it must NOT swallow are the reason this is a function and not an
 * `if`: an admin looking at a genuinely empty catalog, and a user who has agents
 * but none on the tab they happen to be on.
 */

const NO_ACCESS = "No agents have been made available to you yet. Ask your administrator.";

const state = (over = {}) => ({
	tab: "featured",
	filtersActive: false,
	canAdminister: false,
	wholeCatalogEmpty: null,
	...over,
});

describe("a non-admin with nothing granted", () => {
	it.each(["featured", "available", "installed"])(
		"shows the not-allowed copy on the %s tab",
		(tab) => {
			const s = agentsEmptyState(state({ tab, wholeCatalogEmpty: true }));
			expect(s.description).toBe(NO_ACCESS);
			expect(s.title).toBe("No agents available to you");
		}
	);

	it("offers no call to action, since every tab is equally empty", () => {
		// The regression itself: the Featured state's button sent them to Available.
		const s = agentsEmptyState(state({ tab: "featured", wholeCatalogEmpty: true }));
		expect(s.cta).toBe(false);
	});

	it("never mentions featured agents or browsing the catalog", () => {
		for (const tab of ["featured", "available", "installed"]) {
			const s = agentsEmptyState(state({ tab, wholeCatalogEmpty: true }));
			expect(`${s.title} ${s.description}`).not.toMatch(/featured|browse/i);
		}
	});
});

describe("a non-admin who has some agents, just not here", () => {
	it("keeps the per-tab installed copy", () => {
		const s = agentsEmptyState(state({ tab: "installed", wholeCatalogEmpty: false }));
		expect(s.title).toBe("You haven't installed any agents yet");
		expect(s.description).toBe("Browse the catalog and install one to get started.");
		expect(s.cta).toBe(true);
	});

	it("keeps the per-tab featured copy", () => {
		const s = agentsEmptyState(state({ tab: "featured", wholeCatalogEmpty: false }));
		expect(s.title).toBe("No featured agents yet");
		expect(s.cta).toBe(true);
	});
});

describe("an admin", () => {
	it("gets the real 'catalog is empty' message, never the access one", () => {
		// For an admin an empty catalog IS an empty catalog - telling them to ask
		// their administrator would be telling them to ask themselves.
		const s = agentsEmptyState(
			state({ tab: "available", canAdminister: true, wholeCatalogEmpty: true })
		);
		expect(s.title).toBe("No agents available");
		expect(s.description).toBe("The catalog is empty right now.");
	});

	it("keeps the per-tab states too", () => {
		const s = agentsEmptyState(
			state({ tab: "featured", canAdminister: true, wholeCatalogEmpty: true })
		);
		expect(s.title).toBe("No featured agents yet");
	});
});

describe("filters", () => {
	it("win over everything - an empty FILTERED view is about the filter", () => {
		const s = agentsEmptyState(
			state({ tab: "featured", filtersActive: true, wholeCatalogEmpty: true })
		);
		expect(s.title).toBe("No agents match");
		expect(s.cta).toBe(false);
	});
});

describe("before the probe answers", () => {
	it("falls back to the per-tab copy rather than guessing at access", () => {
		const s = agentsEmptyState(state({ tab: "featured", wholeCatalogEmpty: null }));
		expect(s.title).toBe("No featured agents yet");
	});
});

describe("shouldProbeWholeCatalog", () => {
	const probe = (over = {}) => ({
		tab: "featured",
		loading: false,
		rowCount: 0,
		filtersActive: false,
		canAdminister: false,
		wholeCatalogEmpty: null,
		probing: false,
		...over,
	});

	it("probes for a non-admin looking at an unfiltered empty tab", () => {
		expect(shouldProbeWholeCatalog(probe())).toBe(true);
	});

	it.each([
		["the list still has rows", { rowCount: 3 }],
		["the list is still loading", { loading: true }],
		["a filter is narrowing it", { filtersActive: true }],
		["the caller is an admin", { canAdminister: true }],
		["the answer is already known", { wholeCatalogEmpty: false }],
		["a probe is already in flight", { probing: true }],
		["the tab is not a catalog tab", { tab: "activity" }],
	])("does not probe when %s", (_why, over) => {
		expect(shouldProbeWholeCatalog(probe(over))).toBe(false);
	});
});
