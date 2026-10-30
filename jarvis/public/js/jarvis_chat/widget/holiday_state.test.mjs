import { test } from "node:test";
import assert from "node:assert/strict";
import { REFRESH_MIN_MS } from "../../shared/holiday/store.mjs";
import { createHolidayState, exposeDebugView } from "./holiday_state.mjs";

const NOW = new Date("2026-11-08T06:00:00Z"); // 11:30 in Kolkata
const ART = { v: 1 };

// Vue doubles: enough for the store's reads.
const ref = (value) => ({ value });
const computed = (fn) => ({
  get value() {
    return fn();
  },
});

const answerOf = (extra = {}) => ({
  v: 1,
  date: "2026-11-08",
  tz: "Asia/Kolkata",
  kind: "day",
  design: "diwali",
  occasion: "diwali",
  year: 2026,
  worn: true,
  scene: true,
  may_play: true,
  ...extra,
});

const json = (body, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

function setup({
  boot = { jarvis_has_access: true, jarvis_holiday_touch: answerOf() },
  art = ART,
  fetches = [],
  clock = { at: NOW },
  name = "Priya Raman",
} = {}) {
  const listeners = [];
  const requests = [];
  const reports = [];
  const loads = [];
  const doc = {
    visibilityState: "visible",
    addEventListener: (type, fn) => listeners.push(["doc", type, fn]),
    removeEventListener() {},
  };
  const win = {
    addEventListener: (type, fn) => listeners.push(["win", type, fn]),
    removeEventListener() {},
    setInterval: () => 1,
    clearInterval() {},
    frappe: { csrf_token: "tok123" },
  };
  const queue = [...fetches];
  const state = createHolidayState({
    ref,
    computed,
    boot,
    doc,
    win,
    now: () => clock.at,
    storage: null,
    reduced: () => false,
    userName: () => name,
    report: (ctx) => reports.push(ctx),
    loader: {
      load: async () => {
        loads.push("load");
        return art;
      },
      art: () => (loads.length ? art : null),
    },
    fetchImpl: async (url, init) => {
      requests.push({ url, init });
      return queue.length ? queue.shift() : json({ message: { play: true } });
    },
  });
  return { state, listeners, requests, reports, loads, clock };
}

test("without the boot key nothing is built: no listener, no fetch, no art", async () => {
  const { state, listeners, requests, loads } = setup({
    boot: { jarvis_has_access: true },
  });
  state.start();
  await state.onPanelOpen();
  await state.ensureArt();
  assert.equal(state.enabled, false);
  assert.equal(state.view.value.kind, "none");
  assert.deepEqual([listeners, requests, loads], [[], [], []]);
  assert.deepEqual(await state.celebrate(), { play: false, reason: "off" });
});

test("without access, even a key builds nothing", () => {
  const { state, listeners } = setup({
    boot: { jarvis_has_access: false, jarvis_holiday_touch: answerOf() },
  });
  state.start();
  assert.equal(state.enabled, false);
  assert.deepEqual(listeners, []);
});

test("a day reads as heading and design; the item shows once the art is in", async () => {
  const { state } = setup();
  const before = state.view.value;
  assert.equal(before.heading, "Happy Diwali, Priya");
  assert.equal(before.leadupLine, null);
  assert.equal(before.design, "diwali");
  assert.equal(before.wear, false);
  await state.ensureArt();
  assert.equal(state.view.value.wear, true);
});

test("a lead-up has the advance wish and no heading", () => {
  const { state } = setup({
    boot: {
      jarvis_has_access: true,
      jarvis_holiday_touch: answerOf({ kind: "leadup", scene: false }),
    },
  });
  const view = state.view.value;
  assert.equal(view.heading, null);
  assert.equal(view.leadupLine, "Wishing you a happy Diwali in advance!");
});

test("a birthday heading", () => {
  const { state } = setup({
    boot: {
      jarvis_has_access: true,
      jarvis_holiday_touch: answerOf({
        kind: "birthday",
        design: "birthday",
        occasion: "birthday",
      }),
    },
  });
  assert.equal(state.view.value.heading, "Happy birthday, Priya");
});

test("a white-label answer keeps the words and never wears", async () => {
  const { state } = setup({
    boot: {
      jarvis_has_access: true,
      jarvis_holiday_touch: answerOf({ worn: false, scene: false }),
    },
  });
  await state.ensureArt();
  assert.equal(state.view.value.heading, "Happy Diwali, Priya");
  assert.equal(state.view.value.wear, false);
});

test("starting preloads the art only when the answer wears", async () => {
  const worn = setup();
  worn.state.start();
  assert.deepEqual(worn.loads, ["load"]);
  const plain = setup({
    boot: {
      jarvis_has_access: true,
      jarvis_holiday_touch: answerOf({ worn: false }),
    },
  });
  plain.state.start();
  assert.deepEqual(plain.loads, []);
});

test("opening the panel refetches only past the minimum gap (the answer survives a reopen)", async () => {
  const clock = { at: NOW };
  const { state, requests } = setup({
    clock,
    fetches: [json({ message: answerOf() })],
  });
  const first = state.view.value;
  await state.onPanelOpen();
  assert.equal(requests.length, 0);
  clock.at = new Date(NOW.getTime() + REFRESH_MIN_MS + 1000);
  await state.onPanelOpen();
  assert.equal(requests.length, 1);
  assert.match(requests[0].url, /holiday_touch\.api\.get_today$/);
  assert.equal(first.design, state.view.value.design);
});

test("a wake refetches only a stale answer (desk policy), never a fresh one", async () => {
  const clock = { at: NOW };
  const { state, listeners, requests } = setup({
    clock,
    fetches: [json({ message: answerOf({ date: "2026-11-09" }) })],
  });
  state.start();
  const wake = listeners.find(([, type]) => type === "visibilitychange")[2];
  clock.at = new Date(NOW.getTime() + REFRESH_MIN_MS + 1000); // past the gap, still today
  wake();
  assert.equal(requests.length, 0);
  clock.at = new Date("2026-11-08T20:00:00Z"); // 01:30 the next day in Kolkata
  wake();
  assert.equal(requests.length, 1);
  assert.match(requests[0].url, /get_today$/);
});

test("a server error is reported once with the fixed code; a gateway blip is not", async () => {
  for (const [status, reported] of [
    [500, 1],
    [502, 0],
    [503, 0],
    [504, 0],
  ]) {
    const clock = { at: NOW };
    const { state, reports } = setup({
      clock,
      fetches: [json({}, status), json({}, status)],
    });
    clock.at = new Date(NOW.getTime() + REFRESH_MIN_MS + 1000);
    await state.onPanelOpen();
    clock.at = new Date(NOW.getTime() + 2 * REFRESH_MIN_MS + 2000);
    await state.onPanelOpen();
    assert.equal(reports.length, reported, String(status));
    for (const ctx of reports) {
      assert.deepEqual(Object.keys(ctx).sort(), [
        "error_class",
        "error_code",
        "message",
      ]);
    }
  }
});

test("record_shown is a POST with the CSRF token and no body", async () => {
  const { state, requests } = setup({
    fetches: [json({ message: { play: true } })],
  });
  await state.celebrate();
  const { url, init } = requests.find((r) => /record_shown$/.test(r.url));
  assert.equal(init.method, "POST");
  assert.equal(init.headers["X-Frappe-CSRF-Token"], "tok123");
  assert.equal(init.credentials, "same-origin");
  assert.equal(init.body, undefined);
  assert.match(
    url,
    /^\/api\/method\/jarvis\.holiday_touch\.api\.record_shown$/
  );
});

test("celebrate asks once: play:true plays, then the answer is spent", async () => {
  const { state, requests } = setup({
    fetches: [json({ message: { play: true } })],
  });
  assert.equal((await state.celebrate()).play, true);
  assert.equal((await state.celebrate()).play, false);
  assert.equal(requests.filter((r) => /record_shown/.test(r.url)).length, 1);
});

test("a failed record_shown plays (a network error)", async () => {
  const { state } = setup({ fetches: [json({}, 500)] });
  const result = await state.celebrate();
  assert.deepEqual([result.play, result.reason], [true, "network"]);
});

test("the support view is read only and shows what the widget holds", async () => {
  const { state } = setup();
  const debug = state.debugView;
  assert.equal(debug.enabled, true);
  assert.equal(debug.answer.kind, "day");
  assert.equal(debug.art, "idle");
  await state.ensureArt();
  assert.equal(state.debugView.art, "ready");
  assert.throws(() => {
    "use strict";
    debug.enabled = false;
  });
});

test("the support view hangs off window as a getter, not a value", () => {
  const { state } = setup();
  const win = {};
  exposeDebugView(win, state);
  assert.equal(win.__jarvisWidgetHoliday.answer.design, "diwali");
  assert.equal(
    Object.getOwnPropertyDescriptor(win, "__jarvisWidgetHoliday").set,
    undefined
  );
});
