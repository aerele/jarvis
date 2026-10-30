import { test } from "node:test";
import assert from "node:assert/strict";
import { SMILE_MS, createWelcomeFx } from "./panel_welcome_fx.mjs";

const view = (extra = {}) => ({
  kind: "day",
  design: "diwali",
  worn: true,
  wear: true,
  ...extra,
});

function setup({
  v = view(),
  reply = { play: true },
  art = {},
  reduced = false,
} = {}) {
  const log = {
    celebrate: 0,
    ensureArt: 0,
    particles: [],
    cancels: 0,
    smiling: [],
  };
  const timers = new Map();
  let next = 1;
  const fxEl = { id: "svg" };
  const theArt = art && {
    particles: (el, design) => {
      log.particles.push([el, design]);
      return { cancel: () => log.cancels++ };
    },
    ...art,
  };
  const holiday = {
    view: { value: v },
    ensureArt: async () => {
      log.ensureArt++;
      return theArt;
    },
    celebrate: async () => {
      log.celebrate++;
      return reply;
    },
  };
  const fx = createWelcomeFx({
    holiday,
    getFxEl: () => fxEl,
    setSmiling: (on) => log.smiling.push(on),
    reduced: () => reduced,
    setTimer: (fn, ms) => {
      timers.set(next, { fn, ms });
      return next++;
    },
    clearTimer: (id) => timers.delete(id),
  });
  return { fx, log, timers, fxEl, holiday };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 5));

test("on the day the hero smiles with the item for 2.4 s, then rests", async () => {
  const { fx, log, timers } = setup();
  fx.open();
  await settle();
  assert.deepEqual(log.smiling, [true]);
  const [timer] = timers.values();
  assert.equal(timer.ms, SMILE_MS);
  assert.equal(SMILE_MS, 2400);
  timer.fn();
  assert.deepEqual(log.smiling, [true, false]);
});

test("a played day drops the particles once, into the welcome's svg", async () => {
  const { fx, log, fxEl } = setup();
  fx.open();
  await settle();
  assert.equal(log.celebrate, 1);
  assert.deepEqual(log.particles, [[fxEl, "diwali"]]);
});

test("play:false draws no particles but the hero still smiles", async () => {
  const { fx, log } = setup({ reply: { play: false } });
  fx.open();
  await settle();
  assert.deepEqual(log.particles, []);
  assert.deepEqual(log.smiling, [true]);
});

test("a lead-up smiles and never celebrates", async () => {
  const { fx, log } = setup({ v: view({ kind: "leadup" }) });
  fx.open();
  await settle();
  assert.equal(log.celebrate, 0);
  assert.deepEqual(log.smiling, [true]);
});

test("a birthday celebrates like a day", async () => {
  const { fx, log } = setup({
    v: view({ kind: "birthday", design: "birthday" }),
  });
  fx.open();
  await settle();
  assert.equal(log.celebrate, 1);
  assert.equal(log.particles[0][1], "birthday");
});

test("nothing to wear (none, white label, hold) does nothing at all", async () => {
  for (const v of [
    view({ kind: "none", worn: false }),
    view({ worn: false }),
  ]) {
    const { fx, log } = setup({ v });
    fx.open();
    await settle();
    assert.deepEqual([log.ensureArt, log.celebrate, log.smiling], [0, 0, []]);
  }
});

test("art that did not load: no smile, no particles", async () => {
  const { fx, log } = setup({ art: null, reply: { play: false } });
  fx.open();
  await settle();
  assert.deepEqual(log.smiling, []);
  assert.deepEqual(log.particles, []);
});

test("reduced motion: a held smiling frame, no timer, no particles", async () => {
  const { fx, log, timers } = setup({ reduced: true });
  fx.open();
  await settle();
  assert.deepEqual(log.smiling, [true]);
  assert.equal(timers.size, 0);
  assert.deepEqual(log.particles, []);
  fx.close();
  assert.deepEqual(log.smiling, [true, false]);
});

test("closing cancels the particles and leaves no timer", async () => {
  const { fx, log, timers } = setup();
  fx.open();
  await settle();
  assert.equal(timers.size, 1);
  fx.close();
  assert.equal(log.cancels, 1);
  assert.equal(timers.size, 0);
  assert.equal(log.smiling.at(-1), false);
});

test("a reply that lands after the welcome closed draws nothing", async () => {
  const { fx, log, holiday } = setup();
  let release;
  holiday.celebrate = () => new Promise((resolve) => (release = resolve));
  fx.open();
  await settle();
  fx.close();
  release({ play: true });
  await settle();
  assert.deepEqual(log.particles, []);
});

test("opening again starts over: the first open's particles are cancelled", async () => {
  const { fx, log } = setup();
  fx.open();
  await settle();
  fx.open();
  await settle();
  assert.equal(log.cancels, 1);
  assert.equal(log.particles.length, 2);
});

test("a replay plays the smile and particles again with no call", async () => {
  const { fx, log } = setup({ reply: { play: false } });
  fx.open();
  await settle();
  log.celebrate = 0;
  fx.replay();
  await settle();
  assert.equal(log.celebrate, 0);
  assert.equal(log.particles.length, 1);
  assert.deepEqual(log.smiling, [true, false, true]);
});

test("a replay on a lead-up smiles only; under reduced motion it holds the smile only", async () => {
  const a = setup({ v: view({ kind: "leadup" }) });
  a.fx.replay();
  await settle();
  assert.deepEqual(a.log.particles, []);
  assert.deepEqual(a.log.smiling, [true]);
  const b = setup({ reduced: true });
  b.fx.replay();
  await settle();
  assert.deepEqual(b.log.particles, []);
  assert.equal(b.timers.size, 0);
});

test("a replay with nothing to wear does nothing", async () => {
  const { fx, log } = setup({ v: view({ worn: false }) });
  fx.replay();
  await settle();
  assert.deepEqual(log.smiling, []);
});
