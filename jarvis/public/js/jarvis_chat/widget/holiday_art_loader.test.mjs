import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ART_ENTRY,
  ART_TIMEOUT_MS,
  ART_VERSION,
  STALE_TAB_MS,
  createArtLoader,
} from "./holiday_art_loader.mjs";

const SRC = "/assets/jarvis/dist/js/jarvis_holiday_touch.bundle.ABC123.js";
const ART = { v: ART_VERSION, wornMarkup() {} };

// Page doubles: a head that remembers its script tags, a timer the test fires by hand, and
// a frappe whose freeze and require blow up if the loader ever touches them.
function setup({ assets = { [ART_ENTRY]: SRC }, art, boot = {} } = {}) {
  const scripts = [];
  const clock = { t: 0 };
  const reports = [];
  const timers = [];
  const doc = {
    createElement: () => ({
      remove() {
        this.removed = true;
      },
    }),
    head: { appendChild: (el) => scripts.push(el) },
  };
  const win = {
    frappe: {
      boot: { assets_json: assets, ...boot },
      require() {
        throw new Error("frappe.require freezes Desk");
      },
      dom: {
        freeze() {
          throw new Error("#freeze");
        },
      },
    },
    __jarvisHolidayArt: art,
    setTimeout: (fn, ms) => timers.push({ fn, ms }) && timers.length,
    clearTimeout: (id) => {
      timers[id - 1].cleared = true;
    },
  };
  const loader = createArtLoader({
    win,
    doc,
    report: (ctx) => reports.push(ctx),
    now: () => clock.t,
  });
  return { loader, scripts, reports, timers, win, clock };
}

test("loads the hashed bundle with its own script tag and resolves to the art", async () => {
  const { loader, scripts, reports, win } = setup();
  const promise = loader.load();
  assert.equal(scripts.length, 1);
  assert.equal(scripts[0].src, SRC);
  assert.equal(scripts[0].async, true);
  win.__jarvisHolidayArt = ART;
  scripts[0].onload();
  assert.equal(await promise, ART);
  assert.equal(loader.art(), ART);
  assert.deepEqual(reports, []);
});

test("a second load() shares the first: one tag, one result", async () => {
  const { loader, scripts, win } = setup();
  const first = loader.load();
  assert.equal(loader.load(), first);
  win.__jarvisHolidayArt = ART;
  scripts[0].onload();
  await first;
  assert.equal(await loader.load(), ART);
  assert.equal(scripts.length, 1);
});

test("art already on the page needs no tag", async () => {
  const { loader, scripts } = setup({ art: ART });
  assert.equal(await loader.load(), ART);
  assert.equal(scripts.length, 0);
});

test("a missing manifest entry resolves to null and reports once", async () => {
  const { loader, scripts, reports } = setup({ assets: {} });
  assert.equal(await loader.load(), null);
  assert.equal(await loader.load(), null);
  assert.equal(scripts.length, 0);
  assert.equal(reports.length, 1);
  assert.equal(loader.art(), null);
});

test("a script error resolves to null, reports once and removes the tag", async () => {
  const { loader, scripts, reports } = setup();
  const promise = loader.load();
  scripts[0].onerror();
  assert.equal(await promise, null);
  assert.equal(scripts[0].removed, true);
  assert.equal(reports.length, 1);
});

test("a load that never ends gives up after the timeout and says it was slow", async () => {
  const { loader, scripts, reports, timers } = setup();
  const promise = loader.load();
  assert.equal(timers[0].ms, ART_TIMEOUT_MS);
  timers[0].fn();
  assert.equal(await promise, null);
  assert.equal(reports.length, 1);
  assert.equal(reports[0].error_code, "holiday_widget:art_slow");
  // The tag stays: the file may still arrive, and a removed tag would hide that.
  assert.notEqual(scripts[0].removed, true);
});

test("art that arrives after the timeout is used from then on, without a second report", async () => {
  const { loader, scripts, reports, timers, win } = setup();
  const promise = loader.load();
  timers[0].fn();
  assert.equal(await promise, null);
  win.__jarvisHolidayArt = ART;
  scripts[0].onload();
  assert.equal(loader.art(), ART);
  assert.equal(await loader.load(), ART);
  assert.equal(reports.length, 1);
  assert.equal(scripts.length, 1);
});

test("a global that shows up with no load event is found by the next load()", async () => {
  const { loader, timers, win, scripts } = setup();
  const promise = loader.load();
  timers[0].fn();
  assert.equal(await promise, null);
  win.__jarvisHolidayArt = ART;
  assert.equal(await loader.load(), ART);
  assert.equal(loader.art(), ART);
  assert.equal(scripts.length, 1);
});

test("while it still fails, the next load() answers null without a new tag or report", async () => {
  const { loader, timers, scripts, reports } = setup();
  const promise = loader.load();
  timers[0].fn();
  await promise;
  assert.equal(await loader.load(), null);
  assert.equal(scripts.length, 1);
  assert.equal(reports.length, 1);
});

test("a load that ends in time clears the timer", async () => {
  const { loader, scripts, timers, win } = setup();
  const promise = loader.load();
  win.__jarvisHolidayArt = ART;
  scripts[0].onload();
  await promise;
  assert.equal(timers[0].cleared, true);
});

test("a missing or wrong-version global is a failed load", async () => {
  for (const bad of [undefined, null, { v: ART_VERSION + 1 }, {}, "x"]) {
    const { loader, scripts, reports, win } = setup();
    const promise = loader.load();
    win.__jarvisHolidayArt = bad;
    scripts[0].onload();
    assert.equal(await promise, null, String(bad));
    assert.equal(reports.length, 1, String(bad));
  }
});

test("a report carries fixed words only: no url, no design, no date", async () => {
  const { loader, scripts, reports } = setup();
  const promise = loader.load();
  scripts[0].onerror();
  await promise;
  const [ctx] = reports;
  assert.equal(ctx.error_code, "holiday_widget:art");
  assert.equal(ctx.error_class, "HolidayArtLoadFailed");
  assert.deepEqual(Object.keys(ctx).sort(), [
    "error_class",
    "error_code",
    "message",
  ]);
  assert.doesNotMatch(ctx.message, /assets|\.js|20\d\d/);
});

test("the loader never calls frappe.require or freezes Desk", async () => {
  const { loader, scripts, win } = setup();
  const promise = loader.load();
  win.__jarvisHolidayArt = ART;
  scripts[0].onload();
  await promise; // the doubles throw on any call
});

test("a thrown report cannot break the loader", async () => {
  const { scripts, win } = setup();
  const loader = createArtLoader({
    win,
    doc: {
      createElement: () => ({}),
      head: { appendChild: (el) => scripts.push(el) },
    },
    report: () => {
      throw new Error("reporter down");
    },
  });
  const promise = loader.load();
  scripts[0].onerror();
  assert.equal(await promise, null);
});

// ---- told apart: a page left open across a deploy fetches an old hash that no longer exists ----

test("a script error on a page open for hours is reported as a stale page", async () => {
  const { loader, scripts, reports, clock } = setup();
  clock.t = STALE_TAB_MS;
  const promise = loader.load();
  scripts[0].onerror();
  await promise;
  assert.equal(reports[0].error_code, "holiday_widget:art_stale");
  assert.doesNotMatch(reports[0].message, /assets|\.js|20\d\d/);
});

test("a script error on a fresh page is the plain art code", async () => {
  const { loader, scripts, reports, clock } = setup();
  clock.t = STALE_TAB_MS - 1;
  const promise = loader.load();
  scripts[0].onerror();
  await promise;
  assert.equal(reports[0].error_code, "holiday_widget:art");
});

test("a changed build version marks the page stale, however young", async () => {
  const boot = { build_version: "2026-10-01" };
  const { loader, scripts, reports, win } = setup({ boot });
  win.frappe.boot.build_version = "2026-10-31";
  const promise = loader.load();
  scripts[0].onerror();
  await promise;
  assert.equal(reports[0].error_code, "holiday_widget:art_stale");
});

test("a page with no build version is not marked stale by that", async () => {
  const { loader, scripts, reports } = setup();
  const promise = loader.load();
  scripts[0].onerror();
  await promise;
  assert.equal(reports[0].error_code, "holiday_widget:art");
});
