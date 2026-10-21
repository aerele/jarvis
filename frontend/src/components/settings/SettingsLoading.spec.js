import { describe, it, expect } from "vitest";

// Every settings pane shows JvSpinner while it loads (ConnectorsPane's markup),
// never a bare "Loading…" line. Inline sub-loads with their own words
// ("Loading template…") are not pane loads and are not matched.
const panes = import.meta.glob("./*Pane.vue", { query: "?raw", import: "default", eager: true });

describe("settings panes load behind one spinner", () => {
	it("finds the panes", () => {
		expect(Object.keys(panes).length).toBeGreaterThan(5);
	});

	it.each(Object.entries(panes))("%s has no bare Loading… text", (_path, src) => {
		expect(src).not.toMatch(/>\s*Loading…\s*</);
	});
});
