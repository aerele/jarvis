import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// Same vi.hoisted + vi.mock("@/api", () => api) idiom as UsagePane.spec.js /
// PlanBillingPane.spec.js - the mock factory runs before imports are
// evaluated, so the tracked fns have to come from vi.hoisted rather than a
// plain top-level const.
const api = vi.hoisted(() => ({
	connectOauth: vi.fn(),
	oauthSigninStatus: vi.fn(),
}));
vi.mock("@/api", () => api);

// The interim tab's cosmetic HTML only needs a document that accepts
// write()/close() - escapeHtml's real implementation isn't under test here.
vi.mock("@/lib/errors", () => ({
	escapeHtml: (s) => s,
}));

import { signIn, nav } from "./oauthSignin";

const STARTED_AT = "2026-01-01T00:00:00.000Z";
const AUTH_URL = "https://vendor.example/authorize";

function fakeTab() {
	const tab = {
		closed: false,
		opener: {},
		document: { write: vi.fn(), close: vi.fn() },
		// An own, writable location - assigning `.href` on it must not throw
		// the way assigning `window.location.href` does under jsdom.
		location: {},
	};
	tab.close = vi.fn(() => {
		tab.closed = true;
	});
	return tab;
}

describe("oauthSignin", () => {
	let tab;
	let openSpy;

	beforeEach(() => {
		vi.useFakeTimers();
		api.connectOauth.mockReset();
		api.oauthSigninStatus.mockReset();
		tab = fakeTab();
		openSpy = vi.spyOn(window, "open").mockReturnValue(tab);
		nav.go = vi.fn();
	});

	afterEach(() => {
		openSpy.mockRestore();
		vi.useRealTimers();
	});

	it("resolves connected once connected_at is strictly newer than started_at", async () => {
		api.connectOauth.mockResolvedValue({ ok: true, url: AUTH_URL, started_at: STARTED_AT });
		api.oauthSigninStatus.mockResolvedValue({
			ok: true,
			connected: true,
			connected_at: "2026-01-01T00:00:05.000Z",
		});

		const pending = signIn("row-1", { label: "Vendor", agentName: "Jarvis" });
		const result = await advancePoll(pending);

		expect(result).toEqual({ status: "connected" });
		expect(tab.close).toHaveBeenCalled();
	});

	it("does not resolve connected when connected_at only equals started_at", async () => {
		api.connectOauth.mockResolvedValue({ ok: true, url: AUTH_URL, started_at: STARTED_AT });
		api.oauthSigninStatus.mockResolvedValue({
			ok: true,
			connected: true,
			connected_at: STARTED_AT,
		});

		await expectStillPending(signIn("row-1", { label: "Vendor" }));
	});

	it("does not resolve connected when connected_at is older than started_at", async () => {
		api.connectOauth.mockResolvedValue({
			ok: true,
			url: AUTH_URL,
			started_at: "2026-01-01T00:00:10.000Z",
		});
		api.oauthSigninStatus.mockResolvedValue({
			ok: true,
			connected: true,
			connected_at: STARTED_AT,
		});

		await expectStillPending(signIn("row-1", { label: "Vendor" }));
	});

	it("resolves error with a one-shot parked error string", async () => {
		api.connectOauth.mockResolvedValue({ ok: true, url: AUTH_URL, started_at: STARTED_AT });
		api.oauthSigninStatus.mockResolvedValue({ ok: true, error: "Access was denied." });

		const pending = signIn("row-1", { label: "Vendor" });
		const result = await advancePoll(pending);

		expect(result).toEqual({ status: "error", message: "Access was denied." });
	});

	it("resolves error with the envelope's message on an ok:false status", async () => {
		api.connectOauth.mockResolvedValue({ ok: true, url: AUTH_URL, started_at: STARTED_AT });
		api.oauthSigninStatus.mockResolvedValue({
			ok: false,
			error: { code: "forbidden", message: "You can't use this connector." },
		});

		const pending = signIn("row-1", { label: "Vendor" });
		const result = await advancePoll(pending);

		expect(result).toEqual({ status: "error", message: "You can't use this connector." });
	});

	it("resolves closed once the tab is gone and one final check finds nothing", async () => {
		api.connectOauth.mockResolvedValue({ ok: true, url: AUTH_URL, started_at: STARTED_AT });
		api.oauthSigninStatus.mockResolvedValue({ ok: true, connected: false });

		const pending = signIn("row-1", { label: "Vendor" });
		tab.closed = true; // the user closed it themselves before any poll ran
		const result = await advancePoll(pending);

		expect(result).toEqual({ status: "closed" });
	});

	it("cancel() resolves cancelled and closes the tab, without waiting on the vendor", async () => {
		api.connectOauth.mockImplementation(() => new Promise(() => {})); // never settles
		const pending = signIn("row-1", { label: "Vendor" });

		pending.cancel();
		const result = await pending;

		expect(result).toEqual({ status: "cancelled" });
		expect(tab.close).toHaveBeenCalled();
	});

	it("navigates this tab and resolves navigated when the popup is blocked", async () => {
		openSpy.mockReturnValue(null);
		api.connectOauth.mockResolvedValue({ ok: true, url: AUTH_URL, started_at: STARTED_AT });

		const result = await signIn("row-1", { label: "Vendor" });

		expect(result).toEqual({ status: "navigated" });
		expect(nav.go).toHaveBeenCalledWith(AUTH_URL);
	});
});

// Drives exactly one poll tick (POLL_INTERVAL_MS = 2000) - vitest's async
// timer advance also flushes the real promise microtasks in between
// (connectOauth's own resolution, then oauthSigninStatus's), so this covers
// both the initial connect_oauth call and the first status check.
async function advancePoll(pending) {
	await vi.advanceTimersByTimeAsync(2000);
	return pending;
}

// Confirms `pending` has NOT settled after one poll tick, then cleans it up
// via cancel() so the test doesn't leak a dangling timer.
async function expectStillPending(pending) {
	let settled = false;
	pending.then(() => {
		settled = true;
	});
	await vi.advanceTimersByTimeAsync(2000);
	expect(settled).toBe(false);
	pending.cancel();
	await pending;
}
