// Loads the Desk holiday art bundle (jarvis_holiday_touch.bundle.js) on demand.
//
// Our own <script> tag, not frappe.require: frappe.require freezes Desk while it loads and
// cannot tell a failed script from a loaded one (on v15 a failed load can leave Desk frozen).
// A failure of any kind (no manifest entry, a script error, 10 s with no answer, a global
// that is missing or of another version) resolves to null, reports once, and leaves Desk
// untouched; the widget then shows no item. Static text only: this file must not import
// worn.mjs, stage.mjs, scene-geometry.mjs or scenes/ (holiday_bundle_guard.test.mjs).

export const ART_ENTRY = "jarvis_holiday_touch.bundle.js";
// The art global's version, as shared/holiday/art.mjs publishes it.
export const ART_VERSION = 1;
export const ART_TIMEOUT_MS = 10000;

// Fixed words per reason: a report never carries the URL, the design or the date.
const MESSAGES = {
  entry: "The holiday art bundle is not in the asset manifest",
  load: "The holiday art bundle failed to load",
  timeout: "The holiday art bundle did not load in time",
  global: "The holiday art bundle loaded without the expected art",
};

export function createArtLoader({
  win = window,
  doc = document,
  report = (ctx) => win.jarvisReportError?.(ctx),
  timeoutMs = ART_TIMEOUT_MS,
} = {}) {
  let pending = null;
  let art = null;
  let reported = false;

  function readGlobal() {
    const found = win.__jarvisHolidayArt;
    return found && typeof found === "object" && found.v === ART_VERSION
      ? found
      : null;
  }

  function fail(reason) {
    if (!reported) {
      reported = true;
      try {
        report({
          error_class: "HolidayArtLoadFailed",
          error_code: "holiday_widget:art",
          message: MESSAGES[reason],
        });
      } catch (_) {
        // The reporter is down; the load has failed all the same.
      }
    }
    return null;
  }

  function run() {
    const ready = readGlobal();
    if (ready) return Promise.resolve((art = ready));
    const src = win.frappe?.boot?.assets_json?.[ART_ENTRY];
    if (!src) return Promise.resolve(fail("entry"));
    return new Promise((resolve) => {
      const script = doc.createElement("script");
      let timer = null;
      let settled = false;
      const settle = (reason) => {
        if (settled) return;
        settled = true;
        win.clearTimeout(timer);
        const found = reason ? null : readGlobal();
        if (found) art = found;
        else {
          script.remove?.();
          fail(reason || "global");
        }
        resolve(found);
      };
      script.async = true;
      script.src = src;
      script.onload = () => settle(null);
      script.onerror = () => settle("load");
      timer = win.setTimeout(() => settle("timeout"), timeoutMs);
      doc.head.appendChild(script);
    });
  }

  return {
    // One load per page: a second call shares the first, failure included.
    load() {
      pending = pending || run();
      return pending;
    },
    // The art if it has loaded, else null. Never starts a load.
    art: () => art,
  };
}
