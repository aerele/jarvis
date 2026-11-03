// Loads the Desk holiday art bundle (jarvis_holiday_touch.bundle.js) on demand.
//
// Our own <script> tag, not frappe.require: frappe.require freezes Desk while it loads and
// cannot tell a failed script from a loaded one (on v15 a failed load can leave Desk frozen).
// A failure of any kind (no manifest entry, a script error, 10 s with no answer, a global
// that is missing or of another version) resolves to null, reports once, and leaves Desk
// untouched; the widget then shows no item. A bundle that lands after a timeout is still
// used: the next load() finds it. Three report codes tell the causes apart: `art` (broken),
// `art_slow` (no answer in time) and `art_stale` (a page left open since before a deploy asks
// for a hashed file that no longer exists). Static text only: this file must not import
// worn.mjs, stage.mjs, scene-geometry.mjs or scenes/ (holiday_bundle_guard.test.mjs).

export const ART_ENTRY = "jarvis_holiday_touch.bundle.js";
// The art global's version, as shared/holiday/art.mjs publishes it.
export const ART_VERSION = 1;
export const ART_TIMEOUT_MS = 10000;
// A tab open this long has probably seen a deploy since it loaded.
export const STALE_TAB_MS = 6 * 60 * 60 * 1000;

// Fixed words per reason: a report never carries the URL, the design or the date.
const MESSAGES = {
  entry: "The holiday art bundle is not in the asset manifest",
  load: "The holiday art bundle failed to load",
  timeout: "The holiday art bundle did not load in time",
  global: "The holiday art bundle loaded without the expected art",
  stale:
    "The holiday art bundle failed to load on a page that has been open since before an update",
};

export function createArtLoader({
  win = window,
  doc = document,
  report = (ctx) => win.jarvisReportError?.(ctx),
  timeoutMs = ART_TIMEOUT_MS,
  now = () => Date.now(),
} = {}) {
  let pending = null;
  let art = null;
  let reported = false;
  let failed = false;
  const openedAt = now();
  const builtAs = win.frappe?.boot?.build_version;

  // Has this page likely outlived the build it loaded from? Long open, or a build version
  // that has moved on (stock Frappe does not set one; a site that does gets the sharper signal).
  function pageIsStale() {
    const current = win.frappe?.boot?.build_version;
    return (
      now() - openedAt >= STALE_TAB_MS || (!!builtAs && current !== builtAs)
    );
  }

  // The report code for a failure: slowness is its own cause, then a stale page, then the rest.
  function codeFor(reason) {
    if (reason === "timeout") return { code: "art_slow", key: "timeout" };
    if (reason !== "entry" && pageIsStale())
      return { code: "art_stale", key: "stale" };
    return { code: "art", key: reason };
  }

  function readGlobal() {
    const found = win.__jarvisHolidayArt;
    return found && typeof found === "object" && found.v === ART_VERSION
      ? found
      : null;
  }

  function fail(reason) {
    failed = true;
    if (!reported) {
      reported = true;
      const { code, key } = codeFor(reason);
      try {
        report({
          error_class: "HolidayArtLoadFailed",
          error_code: `holiday_widget:${code}`,
          message: MESSAGES[key],
        });
      } catch (_) {
        // The reporter is down; the load has failed all the same.
      }
    }
    return null;
  }

  function run() {
    const ready = readGlobal();
    if (ready) return Promise.resolve(adopt(ready));
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
        if (found) adopt(found);
        else {
          // A timed-out tag stays: the file may yet arrive, and onload below will take it.
          if (reason !== "timeout") script.remove?.();
          fail(reason || "global");
        }
        resolve(found);
      };
      script.async = true;
      script.src = src;
      script.onload = () => {
        settle(null);
        if (!art) adopt(readGlobal()); // arrived after the timeout
      };
      script.onerror = () => settle("load");
      timer = win.setTimeout(() => settle("timeout"), timeoutMs);
      doc.head.appendChild(script);
    });
  }

  // The art is in: remember it, forget an earlier failure, and answer later load()s with it.
  function adopt(found) {
    if (!found) return found;
    art = found;
    failed = false;
    pending = Promise.resolve(found);
    return found;
  }

  return {
    // One load per page: a second call shares the first, failure included. After a failure it
    // looks at the page once more, since the bundle may have landed since.
    load() {
      if (failed) adopt(readGlobal());
      pending = pending || run();
      return pending;
    },
    // The art if it has loaded, else null. Never starts a load.
    art: () => art,
  };
}
