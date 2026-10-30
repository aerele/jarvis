import { test } from "node:test";
import assert from "node:assert/strict";
import { DEADLINE_MS } from "./holiday_celebration.mjs";
import { SMILE_MS, createWelcomeFx } from "./panel_welcome_fx.mjs";

const view = (extra = {}) => ({
  kind: "day",
  design: "diwali",
  hasItem: true,
  itemReady: true,
  ...extra,
});

// holdArt / holdReply make the art and the celebration wait for the test (see `release`).
function setup({
  v = view(),
  reply = { play: true },
  art = {},
  reduced = false,
  holdArt = false,
  holdReply = false,
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
  const release = {};
  const holiday = {
    view: { value: v },
    ensureArt: () => {
      log.ensureArt++;
      if (!holdArt) return Promise.resolve(theArt);
      return new Promise((resolve) => (release.art = () => resolve(theArt)));
    },
    celebrate: () => {
      log.celebrate++;
      if (!holdReply) return Promise.resolve(reply);
      return new Promise((resolve) => (release.reply = resolve));
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
  // Fires the pending timer that was set for `ms`.
  const fire = (ms) => {
    const [id, timer] = [...timers].find(([, t]) => t.ms === ms) || [];
    timers.delete(id);
    timer.fn();
  };
  return { fx, log, timers, fxEl, holiday, release, fire };
}

// Let every promise step that can run, run. Deterministic: no clock involved.
const settle = () => new Promise((resolve) => setImmediate(resolve));

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
    view({ kind: "none", hasItem: false }),
    view({ hasItem: false }),
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
  const { fx, log, release } = setup({ holdReply: true });
  fx.open();
  await settle();
  fx.close();
  release.reply({ play: true });
  await settle();
  assert.deepEqual(log.particles, []);
  assert.deepEqual(log.smiling, []);
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
  const { fx, log } = setup({ v: view({ hasItem: false }) });
  fx.replay();
  await settle();
  assert.deepEqual(log.smiling, []);
});

// ---- the smile and the particles start together, inside one deadline ----

test("on a day the smile waits for the reply, so smile and particles start together", async () => {
  const { fx, log, release } = setup({ holdReply: true });
  fx.open();
  await settle();
  assert.deepEqual(log.smiling, []); // the art is in, the reply is not
  release.reply({ play: true });
  await settle();
  assert.deepEqual(log.smiling, [true]);
  assert.equal(log.particles.length, 1);
});

test("a decline still smiles once it arrives, with no particles", async () => {
  const { fx, log, release } = setup({ holdReply: true });
  fx.open();
  await settle();
  release.reply({ play: false, reason: "declined" });
  await settle();
  assert.deepEqual(log.smiling, [true]);
  assert.deepEqual(log.particles, []);
});

test("art that is not in by the deadline: no smile, then or later", async () => {
  const { fx, log, release, fire } = setup({ holdArt: true });
  fx.open();
  await settle();
  fire(DEADLINE_MS);
  await settle();
  release.art(); // too late
  await settle();
  assert.deepEqual(log.smiling, []);
  assert.deepEqual(log.particles, []);
});

test("a lead-up has the same deadline for its smile", async () => {
  const { fx, log, release, fire } = setup({
    v: view({ kind: "leadup" }),
    holdArt: true,
  });
  fx.open();
  await settle();
  fire(DEADLINE_MS);
  await settle();
  release.art();
  await settle();
  assert.deepEqual(log.smiling, []);
});

test("art that arrives in time clears the deadline timer", async () => {
  const { fx, timers, release } = setup({ holdArt: true });
  fx.open();
  await settle();
  assert.equal(timers.size, 1);
  release.art();
  await settle();
  // Only the smile's own timer is left.
  assert.deepEqual(
    [...timers.values()].map((t) => t.ms),
    [SMILE_MS]
  );
});

test("closing while it waits leaves no timer and starts nothing", async () => {
  const { fx, log, timers, release } = setup({ holdArt: true });
  fx.open();
  await settle();
  fx.close();
  assert.equal(timers.size, 0);
  release.art();
  await settle();
  assert.deepEqual(log.smiling, []);
});

// ---- a reply after the deadline ----

function lateReply() {
  let resolve;
  const late = new Promise((r) => (resolve = r));
  return { late, resolve };
}

test("a late play:true while the same welcome is open plays the particles", async () => {
  const { late, resolve } = lateReply();
  const { fx, log } = setup({
    reply: { play: false, reason: "deadline", late },
  });
  fx.open();
  await settle();
  assert.deepEqual(log.smiling, [true]);
  assert.deepEqual(log.particles, []);
  resolve({ play: true, reason: "played" });
  await settle();
  assert.equal(log.particles.length, 1);
});

test("a late play:true after the welcome closed, or a new one opened, draws nothing", async () => {
  const closed = lateReply();
  const a = setup({
    reply: { play: false, reason: "deadline", late: closed.late },
  });
  a.fx.open();
  await settle();
  a.fx.close();
  closed.resolve({ play: true });
  await settle();
  assert.deepEqual(a.log.particles, []);

  const reopened = lateReply();
  const b = setup({
    reply: { play: false, reason: "deadline", late: reopened.late },
  });
  b.fx.open();
  await settle();
  // A new welcome (its own answer from the server): the old reply belongs to the old one.
  b.holiday.celebrate = () =>
    Promise.resolve({ play: false, reason: "declined" });
  b.fx.open();
  await settle();
  const before = b.log.particles.length;
  reopened.resolve({ play: true });
  await settle();
  assert.equal(b.log.particles.length, before);
});

test("a late play:false, or a late play under reduced motion, draws nothing", async () => {
  const declined = lateReply();
  const a = setup({
    reply: { play: false, reason: "deadline", late: declined.late },
  });
  a.fx.open();
  await settle();
  declined.resolve({ play: false, reason: "declined" });
  await settle();
  assert.deepEqual(a.log.particles, []);

  const quiet = lateReply();
  const b = setup({
    reduced: true,
    reply: { play: false, reason: "deadline", late: quiet.late },
  });
  b.fx.open();
  await settle();
  quiet.resolve({ play: true });
  await settle();
  assert.deepEqual(b.log.particles, []);
});
