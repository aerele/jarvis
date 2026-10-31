import { test } from "node:test";
import assert from "node:assert/strict";
import { LOCAL_KEY } from "../../shared/holiday/cadence.mjs";
import { createCelebrator } from "./holiday_celebration.mjs";

const NOW = new Date("2026-11-08T06:00:00Z"); // 11:30 in Kolkata
const day = (extra = {}) => ({
  v: 1,
  date: "2026-11-08",
  tz: "Asia/Kolkata",
  kind: "day",
  design: "diwali",
  occasion: "diwali",
  worn: true,
  scene: true,
  may_play: true,
  ...extra,
});
const ART = { v: 1 };
// Let every promise step that can run, run. Deterministic: no clock involved.
const flush = () => new Promise((resolve) => setImmediate(resolve));

// A storage double that counts writes.
function memoryStorage() {
  const data = new Map();
  return {
    writes: [],
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem(k, v) {
      this.writes.push(k);
      data.set(k, v);
    },
  };
}

// A promise the test settles by hand.
function deferred() {
  const d = {};
  d.promise = new Promise((resolve, reject) => {
    d.resolve = resolve;
    d.reject = reject;
  });
  return d;
}

// `hold` makes record_shown (or the art) wait for the test; timers never run on their own:
// `expire()` fires the deadline.
function setup({
  current = day(),
  reply = { play: true },
  art = ART,
  reduced = false,
  deadlineMs = 2500,
  holdReply = false,
  holdArt = false,
} = {}) {
  const calls = [];
  const timers = [];
  const state = { answer: current, art, reply };
  const replies = [];
  const arts = [];
  const storage = memoryStorage();
  const celebrator = createCelebrator({
    answer: () => state.answer,
    ensureArt: () => {
      calls.push("art");
      if (!holdArt) return Promise.resolve(state.art);
      arts.push(deferred());
      return arts.at(-1).promise;
    },
    recordShown: () => {
      calls.push("record_shown");
      if (holdReply) {
        replies.push(deferred());
        return replies.at(-1).promise;
      }
      return state.reply instanceof Error
        ? Promise.reject(state.reply)
        : Promise.resolve(state.reply);
    },
    reduced: () => reduced,
    storage,
    now: () => NOW,
    deadlineMs,
    setTimer: (fn, ms) => timers.push({ fn, ms }),
    clearTimer: (id) => {
      timers[id - 1].cleared = true;
    },
  });
  const expire = () => timers.find((t) => !t.cleared)?.fn();
  return { celebrator, calls, state, storage, timers, expire, replies, arts };
}

test("the art loads first, then record_shown, then it plays", async () => {
  const { celebrator, calls } = setup();
  const result = await celebrator.celebrate();
  assert.deepEqual(calls, ["art", "record_shown"]);
  assert.equal(result.play, true);
});

test("an art that failed to load means no call and no play", async () => {
  const { celebrator, calls } = setup({ art: null });
  const result = await celebrator.celebrate();
  assert.deepEqual(calls, ["art"]);
  assert.equal(result.play, false);
  assert.equal(result.reason, "load-failed");
});

test("no call for lead-up, none, scene false, may_play false or reduced motion", async () => {
  const cases = {
    leadup: setup({ current: day({ kind: "leadup", scene: false }) }),
    none: setup({ current: { v: 1, kind: "none" } }),
    "scene false": setup({ current: day({ scene: false }) }),
    "may_play false": setup({ current: day({ may_play: false }) }),
    "reduced motion": setup({ reduced: true }),
  };
  for (const [label, { celebrator, calls }] of Object.entries(cases)) {
    const result = await celebrator.celebrate();
    assert.equal(result.play, false, label);
    assert.deepEqual(calls, [], label);
  }
});

test("a birthday celebrates like a day", async () => {
  const { celebrator } = setup({
    current: day({ kind: "birthday", design: "birthday" }),
  });
  assert.equal((await celebrator.celebrate()).play, true);
});

test("play:false is latched in memory for that answer and never written to storage", async () => {
  const { celebrator, calls, storage, state } = setup({
    reply: { play: false },
  });
  assert.equal((await celebrator.celebrate()).play, false);
  assert.deepEqual(storage.writes, []);
  calls.length = 0;
  const again = await celebrator.celebrate();
  assert.deepEqual(calls, []);
  assert.equal(again.reason, "latched");
  // A new answer object (a fresh get_today) is a new chance.
  state.answer = day();
  await celebrator.celebrate();
  assert.deepEqual(calls, ["art", "record_shown"]);
});

test("a network error plays and records the showing locally", async () => {
  const { celebrator, storage, state } = setup({ reply: new Error("500") });
  const result = await celebrator.celebrate();
  assert.equal(result.play, true);
  assert.deepEqual(storage.writes, [LOCAL_KEY]);
  // The same browser now remembers it: a fresh answer object does not play again today.
  state.answer = day();
  state.reply = { play: true };
  const next = await celebrator.celebrate();
  assert.equal(next.play, false);
  assert.equal(next.reason, "local");
});

test("a played day is spent: the same answer asks no second time", async () => {
  const { celebrator, calls } = setup();
  await celebrator.celebrate();
  calls.length = 0;
  assert.equal((await celebrator.celebrate()).play, false);
  assert.deepEqual(calls, []);
});

test("two requests at once make one call", async () => {
  const { celebrator, calls, replies } = setup({ holdReply: true });
  const both = Promise.all([celebrator.celebrate(), celebrator.celebrate()]);
  await flush();
  replies[0].resolve({ play: true });
  const [a, b] = await both;
  assert.equal(calls.filter((c) => c === "record_shown").length, 1);
  assert.equal(a.play, true);
  assert.equal(b.play, true);
});

test("one deadline from the ask covers the art and the call", async () => {
  const { celebrator, timers } = setup();
  await celebrator.celebrate();
  assert.deepEqual(
    timers.map((t) => t.ms),
    [2500]
  );
});

// ---- a reply that lands after the caller gave up ----

async function pastDeadline(options) {
  const rig = setup({ holdReply: true, ...options });
  const asked = rig.celebrator.celebrate();
  await flush();
  rig.expire();
  return { ...rig, result: await asked };
}

test("past the deadline: no play now, and a late play:false still sets the latch", async () => {
  const { celebrator, calls, result, replies } = await pastDeadline({});
  assert.equal(result.play, false);
  assert.equal(result.reason, "deadline");
  replies[0].resolve({ play: false });
  assert.deepEqual(await result.late, { play: false, reason: "declined" });
  calls.length = 0;
  assert.equal((await celebrator.celebrate()).reason, "latched");
  assert.deepEqual(calls, []);
});

test("past the deadline, a late play:true is handed back to play (the slot is spent)", async () => {
  const { celebrator, result, replies } = await pastDeadline({});
  replies[0].resolve({ play: true });
  assert.deepEqual(await result.late, { play: true, reason: "played" });
  assert.equal((await celebrator.celebrate()).reason, "latched");
});

test("a late network error neither plays nor records locally, and asks again next time", async () => {
  const { celebrator, storage, result, replies, calls } = await pastDeadline(
    {}
  );
  replies[0].reject(new Error("502"));
  assert.deepEqual(await result.late, { play: false, reason: "late-error" });
  assert.deepEqual(storage.writes, []);
  calls.length = 0;
  celebrator.celebrate();
  await flush();
  assert.deepEqual(calls, ["art", "record_shown"]);
});

test("a network error inside the deadline still plays and records", async () => {
  const { celebrator, replies, storage } = setup({ holdReply: true });
  const asked = celebrator.celebrate();
  await flush();
  replies[0].reject(new Error("500"));
  assert.deepEqual(await asked, { play: true, reason: "network" });
  assert.deepEqual(storage.writes, [LOCAL_KEY]);
});

test("a slow art past the deadline makes no call at all, and does not latch", async () => {
  const { celebrator, calls, arts, expire } = setup({ holdArt: true });
  const asked = celebrator.celebrate();
  await flush();
  expire();
  const result = await asked;
  assert.equal(result.reason, "deadline");
  assert.equal(result.late, undefined);
  arts[0].resolve(ART); // the art lands late: nothing asks the server
  await flush();
  assert.deepEqual(calls, ["art"]);
  const again = celebrator.celebrate();
  await flush();
  arts[1].resolve(ART);
  assert.equal((await again).play, true);
});

test("no timer is left running after a celebration", async () => {
  for (const options of [{}, { art: null }, { reply: new Error("x") }]) {
    const { celebrator, timers } = setup(options);
    await celebrator.celebrate();
    assert.ok(timers.length > 0);
    assert.ok(timers.every((t) => t.cleared));
  }
});
