// The holiday touch store core, shared by the desktop SPA and the Desk widget. No framework
// import: the caller injects Vue's `ref` and `computed`, so `node --test` runs it bare.
//
// The page boots with an answer for one day in one time zone, so a tab left open overnight
// must stop showing it: an answer that is no longer today's reads as "none" at once, and the
// store asks `get_today` again when the tab wakes up (visible or focused). A refetch that
// fails while the answer is stale leaves "none" in place, and a 403 means none.
//
// Polling is gentle on purpose. A fresh answer is refreshed at most every 10 minutes.
// A stale one retries every 30 seconds, but when the server keeps answering with a date
// this browser still calls stale (a wrong clock or an unknown zone) the gap doubles up
// to an hour, and a hidden tab never polls on its timer.
//
// refreshPolicy: "spa" refreshes a fresh answer on every wake (after the 10 minute gap);
// "desk" refetches only when the answer is stale, or when the caller asks with a
// non-forced refresh() (the panel opening), still capped by the gap.
//
// Failures reported once per session, because nobody would notice them otherwise: the
// server erroring (status 500 and up; with reportGateway false, not 502/503/504, which are
// proxy blips) and the server sending an answer this reader turns into "none" (the two
// sides have drifted apart). A 403, an offline browser and a timeout are never reported.
import { isStale, readAnswer } from "./answer.mjs";

// A fresh answer is refreshed at most this often (admin may flip a switch).
export const REFRESH_MIN_MS = 10 * 60 * 1000;
// A stale answer retries sooner, but not on every focus event while offline.
export const STALE_RETRY_MS = 30 * 1000;
// The retry gap for a stale answer doubles each time the server's answer is still stale,
// up to this.
export const STALE_RETRY_MAX_MS = 60 * 60 * 1000;
// How often a tab left in the foreground checks whether midnight has passed.
const TICK_MS = 60 * 1000;
const GATEWAY_CODES = [502, 503, 504];
const NONE = { v: 1, kind: "none" };

/**
 * What the holiday touch shows the session user today (jarvis.holiday_touch.api). The method
 * is GET-only server-side, so this is a plain fetch. A non-2xx rejects with .status; a 200
 * whose message is not an object rejects too (malformed: true), so the store keeps the
 * answer it already has. Gives up after 10 s and never reads the HTTP cache.
 */
export async function fetchToday(fetchImpl = globalThis.fetch) {
  const response = await fetchImpl(
    "/api/method/jarvis.holiday_touch.api.get_today",
    {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal:
        typeof AbortSignal.timeout === "function"
          ? AbortSignal.timeout(10000)
          : undefined,
    }
  );
  if (!response.ok) {
    throw Object.assign(new Error(`holiday touch ${response.status}`), {
      status: response.status,
    });
  }
  const message = (await response.json()).message;
  if (!message || typeof message !== "object" || Array.isArray(message)) {
    throw Object.assign(new Error("holiday touch: no answer in the response"), {
      status: response.status,
      malformed: true,
    });
  }
  return message;
}

export function createHolidayStore({
  ref,
  computed,
  boot,
  fetchToday: fetchAnswer = fetchToday,
  now = () => new Date(),
  doc = globalThis.document,
  win = globalThis.window,
  report = () => {},
  // Start fetching the worn art early when the page boots with something to wear.
  loadArt = () => {},
  canWear = () => true,
  refreshPolicy = "spa",
  reportGateway = true,
}) {
  const raw = ref(readAnswer(boot));
<<<<<<< HEAD
  // The time of the last tick, kept out of the reactive graph on purpose: a ref holding a new
  // Date every minute would wake everything that reads `answer` on every tick (a Vue 3.3 computed
  // tells its readers whenever an input is set). Only whether the answer is stale is reactive,
  // and a ref holding the same boolean tells nobody.
  let clock = now();
  const stale = ref(isStale(raw.value, clock));
=======
  const clock = ref(now());
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
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
<<<<<<< HEAD
    shown.value && !stale.value ? raw.value : NONE
=======
    shown.value && !isStale(raw.value, clock.value) ? raw.value : NONE
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
  );

  // For support: what the store holds. Read only; nothing here can change the store.
  const state = { lastError: null, lastFetchAt: null, sceneReason: null };
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
    // Why today's scene is or is not playing (loaded | load-failed | deadline | declined |
    // threw | hidden), set by the welcome stage; null before it has an answer.
    get sceneReason() {
      return state.sceneReason;
    },
  };

  function setSceneReason(reason) {
    state.sceneReason = reason || null;
  }

  function reportOnce(key, ctx) {
    if (reported.has(key)) return;
    reported.add(key);
    report(ctx);
  }

  function tick() {
<<<<<<< HEAD
    clock = now();
    stale.value = isStale(raw.value, clock);
=======
    clock.value = now();
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
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
<<<<<<< HEAD
    const gap = stale.value ? staleGap : REFRESH_MIN_MS;
    if (!force && clock.getTime() - lastAttempt < gap) return Promise.resolve();
    lastAttempt = clock.getTime();
=======
    const stale = isStale(raw.value, clock.value);
    const gap = stale ? staleGap : REFRESH_MIN_MS;
    if (!force && clock.value.getTime() - lastAttempt < gap)
      return Promise.resolve();
    lastAttempt = clock.value.getTime();
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
    // Cleared here, not inside the loop: a fetchAnswer that throws at once would otherwise
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
      const message = await fetchAnswer();
      const next = readAnswer(message);
      state.lastError = null;
      state.lastFetchAt = now().toISOString();
      if (
        message &&
        message.kind &&
        message.kind !== "none" &&
        next.kind === "none"
      ) {
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
<<<<<<< HEAD
      staleGap = isStale(next, clock)
=======
      staleGap = isStale(next, clock.value)
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
        ? Math.min(staleGap * 2, STALE_RETRY_MAX_MS)
        : STALE_RETRY_MS;
    } catch (error) {
      onLoadError(error, forced);
    }
  }

  function onLoadError(error, forced) {
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
    } else if (error && error.status >= 500 && isReportable(error.status)) {
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

  function isReportable(code) {
    return reportGateway || !GATEWAY_CODES.includes(code);
  }

  function onWake() {
    if (doc.visibilityState === "hidden") return;
    if (refreshPolicy === "desk") {
      tick();
<<<<<<< HEAD
      if (!stale.value) return;
=======
      if (!isStale(raw.value, clock.value)) return;
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
    }
    refresh();
  }
  function onTimer() {
    tick();
    if (doc.visibilityState === "hidden") return;
<<<<<<< HEAD
    if (stale.value) refresh();
=======
    if (isStale(raw.value, clock.value)) refresh();
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
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

  return {
    answer,
    refresh,
    start,
    stop,
    tick,
    setShown,
    setSceneReason,
    debugView,
  };
}
