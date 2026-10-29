import { describe, it, expect, vi, afterEach } from "vitest";
import { flushPromises } from "@vue/test-utils";

// The store reads the answer the page booted with, shows it only while it is still
// today's, and asks the server again when it is not. Time, the endpoint and the
// listeners are all injected, so nothing here touches the network or real timers.
vi.mock("@/api", () => ({ getHolidayToday: vi.fn() }));

import {
	createHolidayTouch,
	REFRESH_MIN_MS,
	STALE_RETRY_MAX_MS,
	STALE_RETRY_MS,
} from "./holidayTouch.js";

const DIWALI = {
	v: 1,
	date: "2026-11-08",
	tz: "Asia/Kolkata",
	kind: "day",
	design: "diwali",
	occasion: "diwali",
	year: null,
	worn: true,
	scene: true,
	may_play: true,
};
const NEXT_DAY = { v: 1, date: "2026-11-09", tz: "Asia/Kolkata", kind: "none" };
const MORNING = new Date("2026-11-08T04:00:00Z"); // 09:30 in Kolkata, 8 Nov
const NEXT_MORNING = new Date("2026-11-09T04:00:00Z"); // 9 Nov

function setup(opts = {}) {
	const { start = MORNING, fetchToday, canWear = () => true, win } = opts;
	// `boot: undefined` is a real case (no key on the page), so only a missing key defaults.
	const boot = "boot" in opts ? opts.boot : DIWALI;
	const clock = { t: start.getTime() };
	const fetch = fetchToday || vi.fn();
	const report = vi.fn();
	const loadArt = vi.fn();
	const store = createHolidayTouch({
		boot,
		fetchToday: fetch,
		now: () => new Date(clock.t),
		doc: document,
		win: win || window,
		report,
		loadArt,
		canWear,
	});
	return { store, fetch, clock, report, loadArt };
}
// A window whose interval the spec can fire by hand.
function fakeWindow() {
	const win = {
		fire: null,
		addEventListener: () => {},
		removeEventListener: () => {},
		setInterval: (fn) => {
			win.fire = fn;
			return 1;
		},
		clearInterval: () => {},
	};
	return win;
}
const status = (code) => Object.assign(new Error(`holiday touch ${code}`), { status: code });
function setVisibility(state) {
	Object.defineProperty(document, "visibilityState", { value: state, configurable: true });
	document.dispatchEvent(new Event("visibilitychange"));
}

describe("holidayTouch store", () => {
	let store;
	afterEach(() => {
		store?.stop();
		Object.defineProperty(document, "visibilityState", {
			value: "visible",
			configurable: true,
		});
	});

	it("shows today's boot answer and reads an unknown boot value as none", () => {
		({ store } = setup());
		expect(store.answer.value.kind).toBe("day");
		store.stop();
		({ store } = setup({ boot: null }));
		expect(store.answer.value.kind).toBe("none");
		store.stop();
		({ store } = setup({ boot: { v: 1, kind: "scene" } }));
		expect(store.answer.value.kind).toBe("none");
	});

	it("shows none the moment the answer is stale, with no network call yet", () => {
		const { store: s, fetch, clock } = setup();
		store = s;
		clock.t = NEXT_MORNING.getTime();
		store.tick();
		expect(store.answer.value.kind).toBe("none");
		expect(fetch).not.toHaveBeenCalled();
	});

	it("a stale answer plus visibilitychange refetches and shows the new answer", async () => {
		const fetchToday = vi.fn().mockResolvedValue({ ...DIWALI, date: "2026-11-09" });
		const { store: s, clock } = setup({ fetchToday });
		store = s;
		store.start();
		clock.t = NEXT_MORNING.getTime();
		setVisibility("visible");
		await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(1));
		await vi.waitFor(() => expect(store.answer.value.date).toBe("2026-11-09"));
		expect(store.answer.value.kind).toBe("day");
	});

	it("a failed refetch while stale shows none", async () => {
		const fetchToday = vi.fn().mockRejectedValue(new Error("offline"));
		const { store: s, clock } = setup({ fetchToday });
		store = s;
		store.start();
		clock.t = NEXT_MORNING.getTime();
		setVisibility("visible");
		await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(1));
		await flushPromises();
		expect(store.answer.value.kind).toBe("none");
	});

	it("a 403 means none, even for an answer that is not stale", async () => {
		const fetchToday = vi
			.fn()
			.mockRejectedValue(Object.assign(new Error("no"), { status: 403 }));
		const { store: s, clock } = setup({ fetchToday });
		store = s;
		store.start();
		clock.t += REFRESH_MIN_MS + 1;
		window.dispatchEvent(new Event("focus"));
		await vi.waitFor(() => expect(store.answer.value.kind).toBe("none"));
	});

	it("a failed refetch of a fresh answer keeps it: a network blip is not a reason to lose the greeting", async () => {
		const fetchToday = vi.fn().mockRejectedValue(new Error("offline"));
		const { store: s, clock } = setup({ fetchToday });
		store = s;
		store.start();
		clock.t += REFRESH_MIN_MS + 1;
		window.dispatchEvent(new Event("focus"));
		await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(1));
		await flushPromises();
		expect(store.answer.value.kind).toBe("day");
	});

	it("a fresh answer is refreshed at most once per 10 minutes", async () => {
		const fetchToday = vi.fn().mockResolvedValue(DIWALI);
		const { store: s, clock } = setup({ fetchToday });
		store = s;
		store.start();
		window.dispatchEvent(new Event("focus")); // too soon after boot
		setVisibility("visible");
		expect(fetchToday).not.toHaveBeenCalled();
		clock.t += REFRESH_MIN_MS + 1;
		window.dispatchEvent(new Event("focus"));
		setVisibility("visible"); // same instant: throttled
		await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(1));
		clock.t += 60_000;
		window.dispatchEvent(new Event("focus"));
		expect(fetchToday).toHaveBeenCalledTimes(1);
	});

	it("a hidden tab does not refetch", () => {
		const fetchToday = vi.fn().mockResolvedValue(DIWALI);
		const { store: s, clock } = setup({ fetchToday });
		store = s;
		store.start();
		clock.t = NEXT_MORNING.getTime();
		setVisibility("hidden");
		expect(fetchToday).not.toHaveBeenCalled();
	});

	it("a stale answer retries after a short gap, not on every focus", async () => {
		const fetchToday = vi.fn().mockRejectedValue(new Error("offline"));
		const { store: s, clock } = setup({ fetchToday });
		store = s;
		store.start();
		clock.t = NEXT_MORNING.getTime();
		window.dispatchEvent(new Event("focus"));
		await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(1));
		await flushPromises();
		window.dispatchEvent(new Event("focus"));
		expect(fetchToday).toHaveBeenCalledTimes(1);
		clock.t += STALE_RETRY_MS + 1;
		window.dispatchEvent(new Event("focus"));
		await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(2));
	});

	it("never runs two refetches at once", async () => {
		let release;
		const fetchToday = vi.fn(() => new Promise((resolve) => (release = resolve)));
		const { store: s, clock } = setup({ fetchToday });
		store = s;
		clock.t = NEXT_MORNING.getTime();
		const a = store.refresh();
		const b = store.refresh();
		release(NEXT_DAY);
		await Promise.all([a, b]);
		expect(fetchToday).toHaveBeenCalledTimes(1);
	});

	it("the personal switch off shows none; on again refetches", async () => {
		const fetchToday = vi.fn().mockResolvedValue(DIWALI);
		const { store: s } = setup({ fetchToday });
		store = s;
		store.setShown(false);
		expect(store.answer.value.kind).toBe("none");
		store.setShown(true);
		await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(1));
		expect(store.answer.value.kind).toBe("day");
	});

	it("start is idempotent and stop removes the listeners", () => {
		const fetchToday = vi.fn().mockResolvedValue(DIWALI);
		const { store: s, clock } = setup({ fetchToday });
		store = s;
		store.start();
		store.start();
		store.stop();
		clock.t = NEXT_MORNING.getTime();
		setVisibility("visible");
		expect(fetchToday).not.toHaveBeenCalled();
	});

	it("start fetches the art early when the page booted with something to wear", () => {
		const a = setup();
		store = a.store;
		store.start();
		expect(a.loadArt).toHaveBeenCalledTimes(1);
		store.stop();
		// Nothing to wear (ordinary day, worn:false, white label): no art fetch.
		for (const opts of [
			{ boot: { v: 1, kind: "none" } },
			{ boot: { ...DIWALI, worn: false } },
			{ canWear: () => false },
			{ boot: { ...DIWALI, date: "2026-11-07" } }, // yesterday's, so none
		]) {
			const b = setup(opts);
			store = b.store;
			store.start();
			expect(b.loadArt).not.toHaveBeenCalled();
			store.stop();
		}
	});

	it("a page that booted without the key asks at once, not after 10 minutes", async () => {
		const fetchToday = vi.fn().mockResolvedValue(NEXT_DAY);
		const { store: s } = setup({ boot: undefined, fetchToday });
		store = s;
		store.start();
		window.dispatchEvent(new Event("focus"));
		await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(1));
	});

	describe("polling", () => {
		it("backs off while the server's answer is still stale, doubling up to an hour", async () => {
			// The server keeps sending yesterday's date (a clock or zone disagreement).
			const fetchToday = vi.fn().mockResolvedValue({ ...DIWALI });
			const { store: s, clock } = setup({ fetchToday });
			store = s;
			store.start();
			clock.t = NEXT_MORNING.getTime();
			let gap = STALE_RETRY_MS;
			for (let i = 1; i <= 3; i++) {
				window.dispatchEvent(new Event("focus"));
				await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(i));
				await flushPromises();
				gap *= 2; // the next gap, after this still-stale answer
				clock.t += gap - 1;
				window.dispatchEvent(new Event("focus"));
				expect(fetchToday).toHaveBeenCalledTimes(i); // not yet
				clock.t += 2;
			}
			// The gap stops growing at the cap.
			for (let i = 0; i < 12; i++) {
				clock.t += STALE_RETRY_MAX_MS + 1;
				window.dispatchEvent(new Event("focus"));
				await flushPromises();
			}
			const calls = fetchToday.mock.calls.length;
			clock.t += STALE_RETRY_MAX_MS - 1000;
			window.dispatchEvent(new Event("focus"));
			expect(fetchToday).toHaveBeenCalledTimes(calls);
			clock.t += 2000;
			window.dispatchEvent(new Event("focus"));
			expect(fetchToday).toHaveBeenCalledTimes(calls + 1);
		});

		it("goes back to the short gap once the server sends today's answer", async () => {
			const fetchToday = vi
				.fn()
				.mockResolvedValueOnce({ ...DIWALI }) // still stale
				.mockResolvedValueOnce({ ...DIWALI, date: "2026-11-09" }) // today's
				.mockResolvedValue({ ...DIWALI, date: "2026-11-10" }); // tomorrow's
			const { store: s, clock } = setup({ fetchToday });
			store = s;
			store.start();
			clock.t = NEXT_MORNING.getTime();
			window.dispatchEvent(new Event("focus"));
			await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(1));
			await flushPromises();
			clock.t += 2 * STALE_RETRY_MS + 1;
			window.dispatchEvent(new Event("focus"));
			await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(2));
			await flushPromises();
			expect(store.answer.value.kind).toBe("day");
			// Next midnight: stale again, and the first retry is after the short gap.
			clock.t = new Date("2026-11-10T04:00:00Z").getTime();
			window.dispatchEvent(new Event("focus"));
			await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(3));
		});

		it("the timer does not refetch while the tab is hidden", async () => {
			const fetchToday = vi.fn().mockResolvedValue(NEXT_DAY);
			const win = fakeWindow();
			const { store: s, clock } = setup({ fetchToday, win });
			store = s;
			store.start();
			clock.t = NEXT_MORNING.getTime();
			Object.defineProperty(document, "visibilityState", {
				value: "hidden",
				configurable: true,
			});
			win.fire();
			await flushPromises();
			expect(fetchToday).not.toHaveBeenCalled();
			expect(store.answer.value.kind).toBe("none"); // the clock still moved
			Object.defineProperty(document, "visibilityState", {
				value: "visible",
				configurable: true,
			});
			win.fire();
			await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(1));
		});
	});

	describe("the personal switch and in-flight fetches", () => {
		it("a forced refresh that arrives while one is running runs once more after it", async () => {
			const releases = [];
			const fetchToday = vi.fn(() => new Promise((resolve) => releases.push(resolve)));
			const { store: s, clock } = setup({ fetchToday });
			store = s;
			clock.t = NEXT_MORNING.getTime();
			const first = store.refresh();
			const forced = store.refresh({ force: true });
			expect(fetchToday).toHaveBeenCalledTimes(1);
			releases[0](NEXT_DAY);
			await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(2));
			releases[1]({ ...DIWALI, date: "2026-11-09" });
			await Promise.all([first, forced]);
			expect(store.answer.value.kind).toBe("day");
			expect(fetchToday).toHaveBeenCalledTimes(2);
		});

		it("refresh does nothing while the personal switch is off", async () => {
			const fetchToday = vi.fn().mockResolvedValue(DIWALI);
			const { store: s, clock } = setup({ fetchToday });
			store = s;
			store.setShown(false);
			clock.t += 2 * REFRESH_MIN_MS;
			await store.refresh({ force: true });
			expect(fetchToday).not.toHaveBeenCalled();
		});

		it("a failed fetch after the switch came on does not hold the next wake back", async () => {
			const fetchToday = vi
				.fn()
				.mockRejectedValueOnce(new Error("offline"))
				.mockResolvedValue(DIWALI);
			const { store: s, clock } = setup({ fetchToday });
			store = s;
			store.start();
			store.setShown(false);
			store.setShown(true);
			await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(1));
			await flushPromises();
			clock.t += 1000; // far inside the 10 minute gap
			window.dispatchEvent(new Event("focus"));
			await vi.waitFor(() => expect(fetchToday).toHaveBeenCalledTimes(2));
		});
	});

	describe("failures nobody would otherwise see", () => {
		const wake = async (clock) => {
			clock.t = NEXT_MORNING.getTime();
			window.dispatchEvent(new Event("focus"));
			await flushPromises();
		};

		it("reports a server error once per session, with fixed text", async () => {
			const fetchToday = vi.fn().mockRejectedValue(status(502));
			const { store: s, clock, report } = setup({ fetchToday });
			store = s;
			store.start();
			await wake(clock);
			expect(report).toHaveBeenCalledTimes(1);
			const ctx = report.mock.calls[0][0];
			expect(ctx).toMatchObject({ error_class: "HolidayTodayFailed", error_code: "502" });
			// Not a message the reporter drops as a handled chunk error.
			expect(ctx.message).not.toMatch(/dynamically imported|module script|chunk/i);
			clock.t += STALE_RETRY_MS + 1;
			window.dispatchEvent(new Event("focus"));
			await flushPromises();
			expect(fetchToday).toHaveBeenCalledTimes(2);
			expect(report).toHaveBeenCalledTimes(1);
		});

		it("never reports a 403, a 4xx, a network error or a timeout", async () => {
			for (const error of [
				status(403),
				status(404),
				new TypeError("Failed to fetch"),
				Object.assign(new Error("timeout"), { name: "TimeoutError" }),
			]) {
				const {
					store: s,
					clock,
					report,
				} = setup({
					fetchToday: vi.fn().mockRejectedValue(error),
				});
				s.start();
				await wake(clock);
				s.stop();
				expect(report).not.toHaveBeenCalled();
			}
		});

		it("reports drift once: a kind the reader turns into none", async () => {
			const fetchToday = vi.fn().mockResolvedValue({ v: 1, kind: "day", design: "diwali" });
			const { store: s, clock, report } = setup({ fetchToday });
			store = s;
			store.start();
			await wake(clock);
			expect(report).toHaveBeenCalledTimes(1);
			expect(report.mock.calls[0][0].error_class).toBe("HolidayAnswerDrift");
			expect(store.answer.value.kind).toBe("none");
			clock.t += 2 * STALE_RETRY_MS;
			window.dispatchEvent(new Event("focus"));
			await flushPromises();
			expect(report).toHaveBeenCalledTimes(1);
		});

		it("an honest none, or a good answer, is not drift", async () => {
			for (const message of [NEXT_DAY, { ...DIWALI, date: "2026-11-09" }]) {
				const {
					store: s,
					clock,
					report,
				} = setup({
					fetchToday: vi.fn().mockResolvedValue(message),
				});
				s.start();
				await wake(clock);
				s.stop();
				expect(report).not.toHaveBeenCalled();
			}
		});

		it("a response with no answer in it is reported as drift and keeps the answer", async () => {
			const malformed = Object.assign(new Error("no answer"), {
				status: 200,
				malformed: true,
			});
			const {
				store: s,
				clock,
				report,
			} = setup({
				fetchToday: vi.fn().mockRejectedValue(malformed),
			});
			store = s;
			store.start();
			clock.t += REFRESH_MIN_MS + 1;
			window.dispatchEvent(new Event("focus"));
			await flushPromises();
			expect(report).toHaveBeenCalledTimes(1);
			expect(report.mock.calls[0][0].error_class).toBe("HolidayAnswerDrift");
			expect(store.answer.value.kind).toBe("day"); // the answer it already had
		});
	});

	describe("the support view", () => {
		it("shows what the store holds, and cannot change it", async () => {
			const fetchToday = vi.fn().mockRejectedValue(new Error("offline"));
			const { store: s, clock } = setup({ fetchToday });
			store = s;
			const view = store.debugView;
			expect(view.raw.kind).toBe("day");
			expect(view.answer.kind).toBe("day");
			expect(view.shown).toBe(true);
			expect(view.lastError).toBeNull();
			expect(view.lastFetchAt).toBeNull();
			clock.t += REFRESH_MIN_MS + 1;
			await store.refresh();
			expect(view.lastError).toBe("offline");
			store.setShown(false);
			expect(view.shown).toBe(false);
			expect(view.answer.kind).toBe("none");
			expect(() => {
				"use strict";
				view.raw = null;
			}).toThrow();
		});

		it("says why today's scene is or is not playing, as the stage reports it", () => {
			const { store: s } = setup();
			store = s;
			expect(store.debugView.sceneReason).toBeNull();
			store.setSceneReason("deadline");
			expect(store.debugView.sceneReason).toBe("deadline");
			store.setSceneReason(undefined);
			expect(store.debugView.sceneReason).toBeNull();
		});
	});
});
