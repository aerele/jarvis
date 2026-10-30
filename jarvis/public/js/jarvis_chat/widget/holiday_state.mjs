// What the Desk widget knows about today's holiday touch: the answer, the words, whether
// the art is in, and whether the welcome may celebrate. Framework-free (Vue's `ref` and
// `computed` are injected), so `node --test` runs it bare.
//
// Built only for a user with Jarvis access whose page booted with the key
// (`bootinfo.jarvis_holiday_touch`, written server-side only for such a user). Anyone else
// gets an inert state: no store, no listener, no request, no art.
//
// Text only: this file and the modules it imports are in the always-loaded widget bundle.
// The art (worn item, particles) arrives through the loader, never by import
// (holiday_bundle_guard.test.mjs).
import { leadupLineFor, headingFor } from "../../shared/holiday/copy.mjs";
import { createHolidayStore, fetchToday } from "../../shared/holiday/store.mjs";
import { createCelebrator } from "./holiday_celebration.mjs";

const RECORD_SHOWN = "/api/method/jarvis.holiday_touch.api.record_shown";
const NONE_VIEW = Object.freeze({
  kind: "none",
  design: null,
  occasion: null,
  scene: false,
  worn: false,
  wear: false,
  heading: null,
  leadupLine: null,
});

const firstWord = (name) =>
  String(name || "")
    .trim()
    .split(/\s+/)[0];

export function createHolidayState({
  ref,
  computed,
  boot,
  doc = globalThis.document,
  win = globalThis.window,
  now = () => new Date(),
  storage = null,
  reduced = () => false,
  userName = () => "",
  report = () => {},
  loader, // { load(): Promise<art|null> }  (holiday_art_loader.mjs)
  fetchImpl = (...args) => globalThis.fetch(...args),
  deadlineMs,
}) {
  const key =
    boot && boot.jarvis_has_access === true
      ? boot.jarvis_holiday_touch
      : undefined;
  const enabled = key !== undefined && key !== null;
  const art = ref(null);
  let artStatus = "idle"; // idle | loading | ready | failed
  let lastCelebration = null;

  // The art loads at most once; a failure leaves the item off for the session.
  function ensureArt() {
    if (!enabled) return Promise.resolve(null);
    if (artStatus === "idle") artStatus = "loading";
    return loader.load().then((loaded) => {
      art.value = loaded || null;
      if (loaded && typeof loaded.installWornCss === "function")
        loaded.installWornCss(doc);
      artStatus = loaded ? "ready" : "failed";
      return art.value;
    });
  }

  const store = enabled
    ? createHolidayStore({
        ref,
        computed,
        boot: key,
        fetchToday: () => fetchToday(fetchImpl),
        now,
        doc,
        win,
        report,
        loadArt: () => void ensureArt(),
        refreshPolicy: "desk",
        reportGateway: false,
      })
    : null;

  const view = computed(() => {
    const a = store ? store.answer.value : null;
    if (!a || a.kind === "none") return NONE_VIEW;
    return {
      kind: a.kind,
      design: a.design,
      occasion: a.occasion,
      scene: a.scene === true,
      worn: a.worn === true,
      wear: a.worn === true && !!art.value,
      heading:
        a.kind === "leadup"
          ? null
          : headingFor(a.occasion, firstWord(userName())),
      leadupLine: a.kind === "leadup" ? leadupLineFor(a.occasion) : null,
    };
  });

  async function recordShown() {
    const response = await fetchImpl(RECORD_SHOWN, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        "X-Frappe-CSRF-Token": (win.frappe && win.frappe.csrf_token) || "",
      },
    });
    if (!response.ok) {
      throw Object.assign(new Error(`record_shown ${response.status}`), {
        status: response.status,
      });
    }
    return (await response.json()).message;
  }

  const celebrator = enabled
    ? createCelebrator({
        answer: () => store.answer.value,
        ensureArt,
        recordShown,
        reduced,
        storage,
        now,
        ...(deadlineMs ? { deadlineMs } : {}),
      })
    : null;

  const debugView = {
    get enabled() {
      return enabled;
    },
    get answer() {
      return store ? store.answer.value : NONE_VIEW;
    },
    get view() {
      return view.value;
    },
    get art() {
      return artStatus;
    },
    get lastCelebration() {
      return lastCelebration;
    },
    get store() {
      return store ? store.debugView : null;
    },
  };

  return {
    enabled,
    view,
    debugView,
    ensureArt,
    start: () => store && store.start(),
    stop: () => store && store.stop(),
    // The panel opened: ask again only if the gap since the last ask allows it, so the
    // answer object (and the decline latch on it) survives a reopen.
    onPanelOpen: () => (store ? store.refresh() : Promise.resolve()),
    async celebrate() {
      if (!celebrator) return { play: false, reason: "off" };
      lastCelebration = await celebrator.celebrate();
      return lastCelebration;
    },
  };
}

/** window.__jarvisWidgetHoliday: for support; a getter, so nothing can write to it. */
export function exposeDebugView(win, state) {
  Object.defineProperty(win, "__jarvisWidgetHoliday", {
    get: () => ({ ...state.debugView }),
    configurable: true,
  });
}
