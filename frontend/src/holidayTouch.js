// The holiday touch the desktop shows today.
//
// The page boots with `window.holiday_touch` (jarvis/www/jarvis.py). That answer is
// for one day in one time zone, so a tab left open overnight must stop showing it:
// an answer that is no longer today's reads as "none" at once, and the store asks
// `get_today` again when the tab wakes up (visible or focused). A refetch that fails
// while the answer is stale leaves "none" in place, and a 403 means none.
//
// Polling is gentle on purpose. A fresh answer is refreshed at most every 10 minutes.
// A stale one retries every 30 seconds, but when the server keeps answering with a date
// this browser still calls stale (a wrong clock or an unknown zone) the gap doubles up
// to an hour, and a hidden tab never polls on its timer.
//
// Two kinds of failure are reported once per session, because nobody would notice them
// otherwise: the server erroring (status 500 and up), and the server sending an answer
// this reader turns into "none" (the two sides have drifted apart). A 403, an offline
// browser and a timeout are ordinary and never reported.
import { computed, ref } from "vue";
import { isStale, readAnswer } from "@jsshared/holiday/answer.mjs";
import { getHolidayToday } from "@/api";
import { brand } from "@/branding";
import { report as reportError } from "@/lib/errorReporter";
import { loadWorn } from "@/lib/holidayWorn";

// A fresh answer is refreshed at most this often (admin may flip a switch).
export const REFRESH_MIN_MS = 10 * 60 * 1000;
// A stale answer retries sooner, but not on every focus event while offline.
export const STALE_RETRY_MS = 30 * 1000;
// The retry gap for a stale answer doubles each time the server's answer is still stale,
// up to this.
export const STALE_RETRY_MAX_MS = 60 * 60 * 1000;
// How often a tab left in the foreground checks whether midnight has passed.
const TICK_MS = 60 * 1000;

const NONE = { v: 1, kind: "none" };

export function createHolidayTouch({
	boot,
	fetchToday,
	now = () => new Date(),
	doc = document,
	win = window,
	report = reportError,
	// Start fetching the worn art early when the page boots with something to wear.
	loadArt = loadWorn,
	canWear = () => !brand.isWhitelabeled,
}) {
	const raw = ref(readAnswer(boot));
	const clock = ref(now());
	const shown = ref(true); // the personal "Show holiday touches" switch
	// A page that booted without the key (dev server, a custom shell) has nothing yet, so
	// its first refresh is not held back by the usual gap.
	let lastAttempt = boot === undefined ? 0 : now().getTime();
	let staleGap = STALE_RETRY_MS;
	let inFlight = null;
	let rerun = false; // a forced refresh arrived while one was running
	let timer = null;
	let started = false;
	const reported = new Set();

	// What the screens read: today's answer, or none.
	const answer = computed(() =>
		shown.value && !isStale(raw.value, clock.value) ? raw.value : NONE
	);

	// For support: what the store holds, readable from the console as
	// window.__holidayTouch. Read only; nothing here can change the store.
	const state = { lastError: null, lastFetchAt: null };
	const debugView = {
		get raw() {
			return raw.value;
		},
		get answer() {
			return answer.value;
		},
		get shown() {
			return shown.value;
		},
		get lastError() {
			return state.lastError;
		},
		get lastFetchAt() {
			return state.lastFetchAt;
		},
	};

	function reportOnce(key, ctx) {
		if (reported.has(key)) return;
		reported.add(key);
		report(ctx);
	}

	function tick() {
		clock.value = now();
	}

	function refresh({ force = false } = {}) {
		if (!shown.value) return Promise.resolve();
		if (inFlight) {
			// A forced refresh (the switch just came on) must not be lost to a fetch that
			// started before it: run once more when this one ends.
			if (force) rerun = true;
			return inFlight;
		}
		tick();
		const stale = isStale(raw.value, clock.value);
		const gap = stale ? staleGap : REFRESH_MIN_MS;
		if (!force && clock.value.getTime() - lastAttempt < gap) return Promise.resolve();
		lastAttempt = clock.value.getTime();
		// Cleared here, not inside the loop: a fetchToday that throws at once would otherwise
		// clear it before this assignment and leave a settled promise stuck in inFlight.
		inFlight = loadAll(force).finally(() => (inFlight = null));
		return inFlight;
	}

	async function loadAll(forced) {
		do {
			rerun = false;
			await load(forced);
		} while (rerun && shown.value);
	}

	async function load(forced) {
		try {
			const message = await fetchToday();
			const next = readAnswer(message);
			state.lastError = null;
			state.lastFetchAt = now().toISOString();
			if (message && message.kind && message.kind !== "none" && next.kind === "none") {
				reportOnce("drift", {
					error_class: "HolidayAnswerDrift",
					error_code: "holiday_drift",
					message:
						"The holiday answer from the server was not understood and was shown as none",
				});
			}
			raw.value = next;
			tick();
			// Still stale after a good answer: the server and this browser disagree about
			// the date. Ask less and less often rather than every 30 seconds for ever.
			staleGap = isStale(next, clock.value)
				? Math.min(staleGap * 2, STALE_RETRY_MAX_MS)
				: STALE_RETRY_MS;
		} catch (error) {
			state.lastError = String((error && error.message) || error);
			if (error && error.status === 403) {
				// No access any more: none.
				raw.value = { ...NONE };
			} else if (error && error.malformed) {
				reportOnce("drift", {
					error_class: "HolidayAnswerDrift",
					error_code: "holiday_drift",
					message: "The holiday answer from the server had no answer in it",
				});
			} else if (error && error.status >= 500) {
				reportOnce("server", {
					error_class: "HolidayTodayFailed",
					error_code: String(error.status),
					message: "The holiday touch endpoint answered with a server error",
				});
			}
			// Any other failure keeps a fresh answer; a stale one already reads as none, so
			// it stays none and is retried after the stale gap. A failed forced fetch (the
			// switch just came on) must not hold the next wake back for 10 minutes.
			if (forced) lastAttempt = 0;
			tick();
		}
	}

	function onWake() {
		if (doc.visibilityState === "hidden") return;
		refresh();
	}
	function onTimer() {
		tick();
		if (doc.visibilityState === "hidden") return;
		if (isStale(raw.value, clock.value)) refresh();
	}

	function start() {
		if (started) return;
		started = true;
		doc.addEventListener("visibilitychange", onWake);
		win.addEventListener("focus", onWake);
		timer = win.setInterval(onTimer, TICK_MS);
		// The art is a separate chunk; with something to wear today, fetch it now so it is
		// ready by the first blink instead of after it.
		const first = answer.value;
		if (first.kind !== "none" && first.worn && canWear()) loadArt();
	}

	function stop() {
		if (!started) return;
		started = false;
		doc.removeEventListener("visibilitychange", onWake);
		win.removeEventListener("focus", onWake);
		win.clearInterval(timer);
		timer = null;
	}

	// The personal switch. Turning it on asks the server: while it was off the page may
	// have booted with none.
	function setShown(value) {
		const next = !!value;
		if (next === shown.value) return;
		shown.value = next;
		if (next) refresh({ force: true });
	}

	return { answer, refresh, start, stop, tick, setShown, debugView };
}

export const holidayTouch = createHolidayTouch({
	boot: window.holiday_touch,
	// Looked up at call time, so a spec that mocks @/api without it still imports this.
	fetchToday: () => getHolidayToday(),
});

// Read only, for support (see the store). Not part of the app.
Object.defineProperty(window, "__holidayTouch", {
	get: () => ({ ...holidayTouch.debugView }),
	configurable: true,
});
