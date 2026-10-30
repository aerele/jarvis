import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ART_ENTRY,
  ART_TIMEOUT_MS,
  ART_VERSION,
  createArtLoader,
} from "./holiday_art_loader.mjs";

const SRC = "/assets/jarvis/dist/js/jarvis_holiday_touch.bundle.ABC123.js";
const ART = { v: ART_VERSION, wornMarkup() {} };

// Page doubles: a head that remembers its script tags, a timer the test fires by hand, and
// a frappe whose freeze and require blow up if the loader ever touches them.
function setup({ assets = { [ART_ENTRY]: SRC }, art } = {}) {
  const scripts = [];
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
      boot: { assets_json: assets },
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
  });
  return { loader, scripts, reports, timers, win };
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

test("a load that never ends gives up after the timeout", async () => {
  const { loader, scripts, reports, timers, win } = setup();
  const promise = loader.load();
  assert.equal(timers[0].ms, ART_TIMEOUT_MS);
  timers[0].fn();
  assert.equal(await promise, null);
  assert.equal(scripts[0].removed, true);
  assert.equal(reports.length, 1);
  win.__jarvisHolidayArt = ART;
  scripts[0].onload(); // too late: the answer stays null, nothing more is reported
  assert.equal(loader.art(), null);
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
