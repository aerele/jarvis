import { describe, it, expect, beforeEach, vi } from "vitest";

// The store is a module singleton that reads matchMedia at import (the 820px
// rail-collapse and 767px phone breakpoints). Stub it BEFORE import so both
// resolve to "wide, not phone" — the branch these tests exercise. vi.hoisted
// runs early enough; the ModelEffortPicker spec uses the same shim.
vi.hoisted(() => {
	const store = new Map();
	globalThis.localStorage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => store.set(k, String(v)),
		removeItem: (k) => store.delete(k),
		clear: () => store.clear(),
	};
	window.matchMedia = (q) => ({
		matches: false,
		media: q,
		onchange: null,
		addEventListener() {},
		removeEventListener() {},
		addListener() {},
		removeListener() {},
		dispatchEvent: () => false,
	});
});

// shell.js pulls in frappe-ui + @/api (a frappe-ui resource tree that does not
// resolve under vitest). None of it is exercised by these sidebar-state tests.
vi.mock("frappe-ui", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock("@/api", () => ({}));
vi.mock("@/lib/errors", () => ({ errHtml: (e) => String(e) }));
vi.mock("@/onboarding/readiness.js", () => ({ needsOnboarding: () => false }));

const { useShellStore } = await import("./shell");
const store = useShellStore();

describe("shell store: spacious-view auto-collapse (Dashboard Builder)", () => {
	beforeEach(() => {
		store.setSpaciousView(false);
		store.sidebarPref = "open";
	});

	it("collapses the rail while the view is active WITHOUT touching the saved preference", () => {
		expect(store.sidebarCollapsed).toBe(false);
		store.setSpaciousView(true);
		expect(store.sidebarCollapsed).toBe(true);
		// the persisted preference is never written by the request
		expect(store.sidebarPref).toBe("open");
	});

	it("restores the exact saved preference the moment the view is released", () => {
		store.setSpaciousView(true);
		expect(store.sidebarCollapsed).toBe(true);
		store.setSpaciousView(false);
		expect(store.sidebarCollapsed).toBe(false);
		expect(store.sidebarPref).toBe("open");
	});

	it("a saved 'collapsed' preference stays collapsed after the view releases", () => {
		store.sidebarPref = "collapsed";
		store.setSpaciousView(true);
		expect(store.sidebarCollapsed).toBe(true);
		store.setSpaciousView(false);
		expect(store.sidebarCollapsed).toBe(true);
		expect(store.sidebarPref).toBe("collapsed");
	});

	it("a manual peek while active is transient — the next visit re-collapses", () => {
		store.setSpaciousView(true);
		store.sidebarCollapsed = false; // user opens the rail to peek
		expect(store.sidebarCollapsed).toBe(false);
		expect(store.sidebarPref).toBe("open"); // peek does not persist
		store.setSpaciousView(false); // leave
		store.setSpaciousView(true); // come back
		expect(store.sidebarCollapsed).toBe(true); // auto-collapse re-applies
	});
});
