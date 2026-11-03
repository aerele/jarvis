import { test } from "node:test";
import assert from "node:assert/strict";
import {
  REDUCED_QUERY,
  prefersReduced,
  watchReducedMotion,
} from "./reduced_motion.mjs";

// A window double with one media query list the test can flip.
function fakeWindow(matches) {
  const listeners = new Set();
  const mql = {
    matches,
    addEventListener: (_, fn) => listeners.add(fn),
    removeEventListener: (_, fn) => listeners.delete(fn),
  };
  const asked = [];
  return {
    asked,
    listeners,
    flip(next) {
      mql.matches = next;
      [...listeners].forEach((fn) => fn({ matches: next }));
    },
    matchMedia: (query) => {
      asked.push(query);
      return mql;
    },
  };
}

test("prefersReduced asks the reduced-motion query", () => {
  const win = fakeWindow(true);
  assert.equal(prefersReduced(win), true);
  assert.deepEqual(win.asked, [REDUCED_QUERY]);
  assert.equal(prefersReduced(fakeWindow(false)), false);
});

test("prefersReduced is false where matchMedia is missing or throws", () => {
  assert.equal(prefersReduced({}), false);
  assert.equal(prefersReduced(undefined), false);
  assert.equal(
    prefersReduced({
      matchMedia() {
        throw new Error("blocked");
      },
    }),
    false
  );
});

test("watchReducedMotion reports each change until stopped", () => {
  const win = fakeWindow(false);
  const seen = [];
  const stop = watchReducedMotion((reduced) => seen.push(reduced), win);
  win.flip(true);
  win.flip(false);
  assert.deepEqual(seen, [true, false]);
  stop();
  assert.equal(win.listeners.size, 0);
  win.flip(true);
  assert.deepEqual(seen, [true, false]);
});

test("watchReducedMotion with nothing to watch is a no-op", () => {
  const stop = watchReducedMotion(() => assert.fail("never"), {});
  stop();
});
