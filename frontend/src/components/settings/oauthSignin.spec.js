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
// errMessage stays real: which sentence a failure shows IS under test.
vi.mock("@/lib/errors", async (importOriginal) => ({
	...(await importOriginal()),
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

	it("shows the server's own sentence when the row cannot be created", async () => {
		// frappe-ui's Error puts "<method> <ExceptionClass>" in e.message and the
		// server's frappe.throw text in e.messages - the dialog must show the latter.
		const err = new Error("jarvis.chat.connectors_api.add_connector ValidationError");
		err.messages = ["We could not set up sign-in for this address."];

		const result = await signIn(() => Promise.reject(err), { label: "Vendor" });

		expect(result).toEqual({
			status: "error",
			message: "We could not set up sign-in for this address.",
		});
		expect(tab.close).toHaveBeenCalled();
		expect(api.connectOauth).not.toHaveBeenCalled();
	});

	it("keeps polling for a grace period after the tab closes, then resolves closed", async () => {
		api.connectOauth.mockResolvedValue({ ok: true, url: AUTH_URL, started_at: STARTED_AT });
		api.oauthSigninStatus.mockResolvedValue({ ok: true, connected: false });

		const pending = signIn("row-1", { label: "Vendor" });
		let settled = false;
		pending.then(() => {
			settled = true;
		});
		tab.closed = true; // the user closed it themselves before any poll ran

		await vi.advanceTimersByTimeAsync(2000); // first tick notices the closed tab
		expect(settled).toBe(false);
		await vi.advanceTimersByTimeAsync(4000); // still inside the 8 s grace
		expect(settled).toBe(false);
		const callsInGrace = api.oauthSigninStatus.mock.calls.length;
		expect(callsInGrace).toBeGreaterThan(2);

		await vi.advanceTimersByTimeAsync(4000); // grace is over
		expect(await pending).toEqual({ status: "closed" });
	});

	it("resolves connected when the sign-in lands during the grace period", async () => {
		api.connectOauth.mockResolvedValue({ ok: true, url: AUTH_URL, started_at: STARTED_AT });
		api.oauthSigninStatus.mockResolvedValue({ ok: true, connected: false });

		const pending = signIn("row-1", { label: "Vendor" });
		tab.closed = true;
		await vi.advanceTimersByTimeAsync(4000);

		api.oauthSigninStatus.mockResolvedValue({
			ok: true,
			connected: true,
			connected_at: "2026-01-01T00:00:05.000Z",
		});
		await vi.advanceTimersByTimeAsync(2000);

		expect(await pending).toEqual({ status: "connected" });
	});

	it("re-checks at once when the window becomes visible again", async () => {
		api.connectOauth.mockResolvedValue({ ok: true, url: AUTH_URL, started_at: STARTED_AT });
		api.oauthSigninStatus.mockResolvedValue({ ok: true, connected: false });

		const pending = signIn("row-1", { label: "Vendor" });
		tab.closed = true;
		await vi.advanceTimersByTimeAsync(2000);
		const calls = api.oauthSigninStatus.mock.calls.length;

		api.oauthSigninStatus.mockResolvedValue({
			ok: true,
			connected: true,
			connected_at: "2026-01-01T00:00:05.000Z",
		});
		document.dispatchEvent(new Event("visibilitychange"));
		await vi.advanceTimersByTimeAsync(0); // no poll tick has elapsed

		expect(api.oauthSigninStatus.mock.calls.length).toBe(calls + 1);
		expect(await pending).toEqual({ status: "connected" });
	});

	it("re-checks when the window regains focus, and stops listening once settled", async () => {
		api.connectOauth.mockResolvedValue({ ok: true, url: AUTH_URL, started_at: STARTED_AT });
		api.oauthSigninStatus.mockResolvedValue({
			ok: true,
			connected: true,
			connected_at: "2026-01-01T00:00:05.000Z",
		});

		const pending = signIn("row-1", { label: "Vendor" });
		await vi.advanceTimersByTimeAsync(0); // connect_oauth resolves, polling starts
		window.dispatchEvent(new Event("focus"));
		await vi.advanceTimersByTimeAsync(0);
		expect(await pending).toEqual({ status: "connected" });

		const calls = api.oauthSigninStatus.mock.calls.length;
		window.dispatchEvent(new Event("focus"));
		await vi.advanceTimersByTimeAsync(0);
		expect(api.oauthSigninStatus.mock.calls.length).toBe(calls);
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
