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
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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

function setup({
  current = day(),
  reply = { play: true },
  art = ART,
  reduced = false,
  deadlineMs = 2500,
  replyAfter = 0,
} = {}) {
  const calls = [];
  const state = { answer: current, art, reply };
  const storage = memoryStorage();
  const celebrator = createCelebrator({
    answer: () => state.answer,
    ensureArt: async () => {
      calls.push("art");
      return state.art;
    },
    recordShown: async () => {
      calls.push("record_shown");
      if (replyAfter) await wait(replyAfter);
      if (state.reply instanceof Error) throw state.reply;
      return state.reply;
    },
    reduced: () => reduced,
    storage,
    now: () => NOW,
    deadlineMs,
  });
  return { celebrator, calls, state, storage };
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
  const { celebrator, calls } = setup({ replyAfter: 10 });
  const [a, b] = await Promise.all([
    celebrator.celebrate(),
    celebrator.celebrate(),
  ]);
  assert.equal(calls.filter((c) => c === "record_shown").length, 1);
  assert.equal(a.play, true);
  assert.equal(b.play, true);
});

test("past the deadline: no play, and a late play:false still sets the latch", async () => {
  const { celebrator, calls, state } = setup({
    reply: { play: false },
    deadlineMs: 15,
    replyAfter: 60,
  });
  const result = await celebrator.celebrate();
  assert.equal(result.play, false);
  assert.equal(result.reason, "deadline");
  await wait(80); // the reply lands after the panel gave up
  calls.length = 0;
  assert.equal((await celebrator.celebrate()).reason, "latched");
  assert.deepEqual(calls, []);
  assert.ok(state.answer);
});

test("a late network error still records locally and does not play", async () => {
  const { celebrator, storage } = setup({
    reply: new Error("502"),
    deadlineMs: 15,
    replyAfter: 60,
  });
  const result = await celebrator.celebrate();
  assert.equal(result.play, false);
  await wait(80);
  assert.deepEqual(storage.writes, [LOCAL_KEY]);
});

test("a slow art past the deadline makes no call at all", async () => {
  const calls = [];
  const celebrator = createCelebrator({
    answer: () => day(),
    ensureArt: () =>
      new Promise((resolve) => setTimeout(() => resolve(ART), 60)),
    recordShown: async () => {
      calls.push("record_shown");
      return { play: true };
    },
    reduced: () => false,
    storage: memoryStorage(),
    now: () => NOW,
    deadlineMs: 15,
  });
  const result = await celebrator.celebrate();
  assert.equal(result.reason, "deadline");
  await wait(80);
  assert.deepEqual(calls, []);
});

test("a slow art past the deadline does not latch the answer", async () => {
  let delay = 60;
  const calls = [];
  const celebrator = createCelebrator({
    answer: () => day(),
    ensureArt: () =>
      new Promise((resolve) => setTimeout(() => resolve(ART), delay)),
    recordShown: async () => {
      calls.push("record_shown");
      return { play: true };
    },
    reduced: () => false,
    storage: memoryStorage(),
    now: () => NOW,
    deadlineMs: 15,
  });
  await celebrator.celebrate();
  delay = 0;
  assert.equal((await celebrator.celebrate()).play, true);
  assert.deepEqual(calls, ["record_shown"]);
});

test("no timer is left running after a celebration", async () => {
  const { celebrator } = setup({ reply: { play: true }, deadlineMs: 60000 });
  await celebrator.celebrate(); // a 60 s deadline timer left behind would hold the process open
});
