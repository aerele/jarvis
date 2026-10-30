// The holiday store core (store.mjs), run with bare `node --test`: no Vue, no DOM. Time,
// the endpoint, the listeners and `ref`/`computed` are all injected.
import test from "node:test";
import assert from "node:assert/strict";

import {
  REFRESH_MIN_MS,
  STALE_RETRY_MAX_MS,
  STALE_RETRY_MS,
  createHolidayStore,
  fetchToday,
} from "./store.mjs";

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
const NONE_TODAY = {
  v: 1,
  date: "2026-11-08",
  tz: "Asia/Kolkata",
  kind: "none",
};
const MORNING = new Date("2026-11-08T04:00:00Z").getTime(); // 09:30 in Kolkata, 8 Nov
const NEXT_MORNING = new Date("2026-11-09T04:00:00Z").getTime(); // 9 Nov

// Minimal stand-ins for Vue's ref and computed.
const ref = (value) => ({ value });
const computed = (fn) => ({
  get value() {
    return fn();
  },
});

function fakeTarget() {
  const target = { handlers: {}, intervals: [], visibilityState: "visible" };
  target.addEventListener = (name, fn) => (target.handlers[name] = fn);
  target.removeEventListener = (name) => delete target.handlers[name];
<<<<<<< HEAD
  target.cleared = [];
  target.setInterval = (fn) => {
    target.intervals.push(fn);
    return target.intervals.length;
  };
  target.clearInterval = (id) => {
    target.cleared.push(id);
  };
=======
  target.setInterval = (fn) => (
    target.intervals.push(fn), target.intervals.length
  );
  target.clearInterval = () => {};
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
  return target;
}

function setup(opts = {}) {
  const boot = "boot" in opts ? opts.boot : DIWALI;
  const clock = { t: opts.start ?? MORNING };
  const target = fakeTarget();
  const calls = [];
  const fetch = opts.fetchToday || (async () => NONE_TODAY);
  const reports = [];
  const store = createHolidayStore({
    ref,
    computed,
    boot,
<<<<<<< HEAD
    fetchToday: async () => {
      calls.push(1);
      return fetch();
    },
=======
    fetchToday: async () => (calls.push(1), fetch()),
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
    now: () => new Date(clock.t),
    doc: target,
    win: target,
    report: (ctx) => reports.push(ctx),
    refreshPolicy: opts.refreshPolicy,
    reportGateway: opts.reportGateway,
  });
  return { store, clock, target, calls, reports };
}
const status = (code) =>
  Object.assign(new Error(`holiday touch ${code}`), { status: code });
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("reads the boot answer and shows it while it is today's", () => {
  assert.equal(setup().store.answer.value.kind, "day");
  assert.equal(
    setup({ boot: { v: 1, kind: "scene" } }).store.answer.value.kind,
    "none"
  );
});

<<<<<<< HEAD
// A small dependency tracker with Vue 3.3's computed rule (the Frappe 15 Desk): a computed
// tells its readers whenever one of ITS inputs is set, without comparing its own result. A
// ref only tells them when the value it holds actually changes.
function tracked() {
  let active = null;
  const track = (subs) => active && subs.add(active);
  const ref = (initial) => {
    const subs = new Set();
    let held = initial;
    return {
      get value() {
        track(subs);
        return held;
      },
      set value(next) {
        if (Object.is(next, held)) return;
        held = next;
        [...subs].forEach((run) => run());
      },
    };
  };
  const computed = (fn) => {
    const subs = new Set();
    let dirty = true;
    let cached;
    const invalidate = () => {
      dirty = true;
      [...subs].forEach((run) => run());
    };
    return {
      get value() {
        track(subs);
        if (dirty) {
          const outer = active;
          active = invalidate;
          cached = fn();
          active = outer;
          dirty = false;
        }
        return cached;
      },
    };
  };
  const watch = (read) => {
    const run = () => {
      const outer = active;
      active = run;
      read();
      active = outer;
    };
    run();
  };
  return { ref, computed, watch };
}

test("a tick that changes nothing wakes nobody; midnight wakes them once", () => {
  const reactive = tracked();
  const clock = { t: MORNING };
  const target = fakeTarget();
  const store = createHolidayStore({
    ref: reactive.ref,
    computed: reactive.computed,
    boot: DIWALI,
    now: () => new Date(clock.t),
    doc: target,
    win: target,
  });
  let runs = 0;
  reactive.watch(() => {
    runs++;
    return store.answer.value;
  });
  for (const minutes of [1, 2, 3, 4]) {
    clock.t = MORNING + minutes * 60000;
    store.tick();
  }
  assert.equal(runs, 1);
  clock.t = new Date("2026-11-08T18:30:00Z").getTime(); // 00:00 on 9 Nov in Kolkata
  store.tick();
  assert.equal(runs, 2);
  assert.equal(store.answer.value.kind, "none");
});

=======
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
test("at midnight in the answer's own time zone it reads as none, with no call", () => {
  const { store, clock, calls } = setup();
  clock.t = new Date("2026-11-08T18:29:00Z").getTime(); // 23:59 in Kolkata
  store.tick();
  assert.equal(store.answer.value.kind, "day");
  clock.t = new Date("2026-11-08T18:30:00Z").getTime(); // 00:00 on 9 Nov in Kolkata
  store.tick();
  assert.equal(store.answer.value.kind, "none");
  assert.equal(calls.length, 0);
});

test("spa: a fresh answer is refetched on wake once REFRESH_MIN_MS has passed", async () => {
  const { store, clock, target, calls } = setup({ refreshPolicy: "spa" });
  store.start();
  clock.t += REFRESH_MIN_MS - 1;
  target.handlers.focus();
  await settle();
  assert.equal(calls.length, 0);
  clock.t += 2;
  target.handlers.focus();
  await settle();
  assert.equal(calls.length, 1);
});

test("spa is the default policy", async () => {
  const { store, clock, target, calls } = setup();
  store.start();
  clock.t += REFRESH_MIN_MS + 1;
  target.handlers.focus();
  await settle();
  assert.equal(calls.length, 1);
});

test("desk: a fresh answer is never refetched on wake, a fresh none included", async () => {
  for (const boot of [DIWALI, NONE_TODAY]) {
    const { store, clock, target, calls } = setup({
      boot,
      refreshPolicy: "desk",
    });
    store.start();
    clock.t += REFRESH_MIN_MS * 3;
    target.handlers.focus();
    target.handlers.visibilitychange();
    await settle();
    assert.equal(calls.length, 0, `kind ${boot.kind}`);
  }
});

test("desk: a stale answer is refetched on wake and shows the new answer", async () => {
  const next = { ...DIWALI, date: "2026-11-09" };
  const { store, clock, target, calls } = setup({
    refreshPolicy: "desk",
    fetchToday: async () => next,
  });
  store.start();
  clock.t = NEXT_MORNING;
  target.handlers.focus();
  await settle();
  assert.equal(calls.length, 1);
  assert.equal(store.answer.value.date, "2026-11-09");
});

test("desk: a non-forced refresh() is capped by REFRESH_MIN_MS", async () => {
  const { store, clock, calls } = setup({ refreshPolicy: "desk" });
  await store.refresh();
  assert.equal(calls.length, 0);
  clock.t += REFRESH_MIN_MS;
  await store.refresh();
  assert.equal(calls.length, 1);
  await store.refresh();
  assert.equal(calls.length, 1);
});

test("a stale answer retries after STALE_RETRY_MS, then doubles the gap while it stays stale", async () => {
  // The server keeps answering yesterday's date: a wrong clock on this side.
  const { store, clock, calls } = setup({
    start: NEXT_MORNING,
    boot: DIWALI,
    fetchToday: async () => DIWALI,
  });
  // The boot answer is stale: the first retry comes after the plain stale gap.
  let gap = STALE_RETRY_MS;
  while (gap <= STALE_RETRY_MAX_MS) {
    const before = calls.length;
    clock.t += gap - 1;
    await store.refresh();
    assert.equal(calls.length, before, `no call inside ${gap} ms`);
    clock.t += 2;
    await store.refresh();
    assert.equal(calls.length, before + 1, `a call after ${gap} ms`);
    if (gap === STALE_RETRY_MAX_MS) break;
    gap = Math.min(gap * 2, STALE_RETRY_MAX_MS);
  }
  assert.equal(gap, STALE_RETRY_MAX_MS);
});

test("a 403 means none, even for a fresh answer", async () => {
  const { store, clock } = setup({
    fetchToday: async () => {
      throw status(403);
    },
  });
  clock.t += REFRESH_MIN_MS + 1;
  await store.refresh();
  assert.equal(store.answer.value.kind, "none");
});

test("a failed refetch keeps a fresh answer and reports nothing for an offline error", async () => {
  const { store, clock, reports } = setup({
    fetchToday: async () => {
      throw new Error("offline");
    },
  });
  clock.t += REFRESH_MIN_MS + 1;
  await store.refresh();
  assert.equal(store.answer.value.kind, "day");
  assert.equal(reports.length, 0);
});

test("a 500 is reported once; 502, 503 and 504 too by default", async () => {
  for (const code of [500, 502, 503, 504]) {
    const { store, reports } = setup({
      fetchToday: async () => {
        throw status(code);
      },
    });
    await store.refresh({ force: true });
    await store.refresh({ force: true });
    assert.equal(reports.length, 1, `status ${code}`);
    assert.equal(reports[0].error_code, String(code));
  }
});

test("reportGateway false skips 502, 503 and 504 but still reports a 500", async () => {
  for (const [code, expected] of [
    [500, 1],
    [502, 0],
    [503, 0],
    [504, 0],
  ]) {
    const { store, reports } = setup({
      reportGateway: false,
      fetchToday: async () => {
        throw status(code);
      },
    });
    await store.refresh({ force: true });
    assert.equal(reports.length, expected, `status ${code}`);
  }
});

test("an answer the reader turns into none is reported as drift, once", async () => {
  const { store, reports } = setup({
    fetchToday: async () => ({ v: 1, kind: "scene" }),
  });
  await store.refresh({ force: true });
  await store.refresh({ force: true });
  assert.equal(reports.length, 1);
  assert.equal(reports[0].error_code, "holiday_drift");
});

test("a malformed response is reported as drift and keeps the answer", async () => {
  const { store, reports } = setup({
    fetchToday: async () => {
      throw Object.assign(new Error("no answer"), {
        malformed: true,
        status: 200,
      });
    },
  });
  await store.refresh({ force: true });
  assert.equal(reports[0].error_code, "holiday_drift");
  assert.equal(store.answer.value.kind, "day");
});

test("the personal switch off reads as none and stops fetching; on forces a refresh", async () => {
  const { store, calls } = setup();
  store.setShown(false);
  assert.equal(store.answer.value.kind, "none");
  await store.refresh({ force: true });
  assert.equal(calls.length, 0);
  store.setShown(true);
  await settle();
  assert.equal(calls.length, 1);
});

<<<<<<< HEAD
async function wakeAfterMidnight(visibility, wake) {
  const ctx = setup({ start: NEXT_MORNING - 3_600_000 });
  ctx.store.start();
  ctx.target.visibilityState = visibility;
  ctx.clock.t = NEXT_MORNING;
  wake(ctx.target);
  await settle();
  return ctx.calls.length;
}
const onFocus = (target) => target.handlers.focus();
const onTimer = (target) => target.intervals[0]();

test("a visible tab refetches a stale answer on wake and on its timer", async () => {
  assert.equal(await wakeAfterMidnight("visible", onFocus), 1);
  assert.equal(await wakeAfterMidnight("visible", onTimer), 1);
});

test("a hidden tab never refreshes on wake or on its timer", async () => {
  assert.equal(await wakeAfterMidnight("hidden", onFocus), 0);
  assert.equal(await wakeAfterMidnight("hidden", onTimer), 0);
});

test("the timer leaves a fresh answer alone", async () => {
  const { store, clock, target, calls } = setup();
  store.start();
  clock.t += REFRESH_MIN_MS * 3;
=======
test("a hidden tab never refreshes on wake or on its timer", async () => {
  const { store, clock, target, calls } = setup({
    start: NEXT_MORNING - 3_600_000,
  });
  store.start();
  target.visibilityState = "hidden";
  clock.t = NEXT_MORNING;
  target.handlers.focus();
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
  target.intervals[0]();
  await settle();
  assert.equal(calls.length, 0);
});

test("start loads the art once for a worn answer, and stop removes the listeners", () => {
  let loads = 0;
  const target = fakeTarget();
  const store = createHolidayStore({
    ref,
    computed,
    boot: DIWALI,
    fetchToday: async () => NONE_TODAY,
    now: () => new Date(MORNING),
    doc: target,
    win: target,
    loadArt: () => loads++,
  });
  store.start();
  store.start();
  assert.equal(loads, 1);
  assert.equal(target.intervals.length, 1);
  store.stop();
  assert.deepEqual(Object.keys(target.handlers), []);
<<<<<<< HEAD
  assert.deepEqual(target.cleared, [1]);
=======
>>>>>>> d4a977c (feat(holiday): shared framework-free holiday store and worn-room module)
});

test("canWear false keeps the art from loading", () => {
  let loads = 0;
  const target = fakeTarget();
  createHolidayStore({
    ref,
    computed,
    boot: DIWALI,
    fetchToday: async () => NONE_TODAY,
    now: () => new Date(MORNING),
    doc: target,
    win: target,
    loadArt: () => loads++,
    canWear: () => false,
  }).start();
  assert.equal(loads, 0);
});

test("fetchToday: GET with cookie, no cache; returns the message", async () => {
  let seen;
  const message = { v: 1, kind: "none" };
  const got = await fetchToday(async (url, init) => {
    seen = { url, init };
    return { ok: true, status: 200, json: async () => ({ message }) };
  });
  assert.equal(seen.url, "/api/method/jarvis.holiday_touch.api.get_today");
  assert.equal(seen.init.method, "GET");
  assert.equal(seen.init.credentials, "same-origin");
  assert.equal(seen.init.cache, "no-store");
  assert.deepEqual(got, message);
});

test("fetchToday: a non-2xx rejects with its status; a bad body rejects as malformed", async () => {
  await assert.rejects(
    fetchToday(async () => ({
      ok: false,
      status: 403,
      json: async () => ({}),
    })),
    { status: 403 }
  );
  for (const message of [undefined, null, "x", [1]]) {
    await assert.rejects(
      fetchToday(async () => ({
        ok: true,
        status: 200,
        json: async () => ({ message }),
      })),
      { malformed: true, status: 200 }
    );
  }
});
