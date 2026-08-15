import { test } from "node:test";
import assert from "node:assert/strict";
import {
  FAB_SIZE,
  EDGE_INSET,
  TAP_THRESHOLD_PX,
  SNAP_MS,
  IDLE_FADE_MS,
  IDLE_OPACITY,
  STORAGE_KEY,
  chooseSide,
  clampY,
  xForSide,
  yToRatio,
  ratioToY,
  parseSavedPosition,
  serializePosition,
  resolvePosition,
  dragStart,
  dragMove,
  dragEnd,
  createIdleTimer,
} from "./fab_position.mjs";

// A viewport with a clean, hand-checkable dockable band: y ranges [0, 550].
const VIEWPORT = {
  vw: 300,
  vh: 600,
  topInset: 0,
  bottomInset: 0,
  edgeInset: 20,
  fabSize: 50,
};
// A viewport too short to fit the fab: the band degenerates to topInset.
const DEGENERATE_VIEWPORT = {
  vw: 300,
  vh: 100,
  topInset: 50,
  bottomInset: 20,
  edgeInset: 20,
  fabSize: 54,
};

test("constants match the spec verbatim", () => {
  assert.equal(FAB_SIZE, 54);
  assert.equal(EDGE_INSET, 22);
  assert.equal(TAP_THRESHOLD_PX, 6);
  assert.equal(SNAP_MS, 250);
  assert.equal(IDLE_FADE_MS, 8000);
  assert.equal(IDLE_OPACITY, 0.4);
  assert.equal(STORAGE_KEY, "jarvis-fab-pos");
});

test("chooseSide: left below the midpoint, right at and above it", () => {
  assert.equal(chooseSide(149, 300), "left");
  assert.equal(chooseSide(150, 300), "right"); // exact midpoint counts as right
  assert.equal(chooseSide(151, 300), "right");
});

test("clampY: below range, above range, and a degenerate (too-short) viewport", () => {
  assert.equal(clampY(-100, VIEWPORT), 0); // below min -> topInset
  assert.equal(clampY(9999, VIEWPORT), 550); // above max -> vh - bottomInset - fabSize
  assert.equal(clampY(300, VIEWPORT), 300); // inside range -> unchanged
  // vh - bottomInset - fabSize = 100 - 20 - 54 = 26 < topInset(50) -> band collapses to topInset
  assert.equal(clampY(0, DEGENERATE_VIEWPORT), 50);
  assert.equal(clampY(9999, DEGENERATE_VIEWPORT), 50);
});

test("xForSide: edge-inset x for each side; unrecognized side falls back to right", () => {
  assert.equal(xForSide("left", VIEWPORT), 20);
  assert.equal(xForSide("right", VIEWPORT), 230); // 300 - 20 - 50
  assert.equal(xForSide("up", VIEWPORT), 230);
});

test("yToRatio / ratioToY round-trip through the dockable band", () => {
  for (const ratio of [0, 0.25, 0.5, 0.75, 1]) {
    const y = ratioToY(ratio, VIEWPORT);
    assert.equal(yToRatio(y, VIEWPORT), ratio);
  }
  assert.equal(ratioToY(0.5, VIEWPORT), 275);
  assert.equal(yToRatio(275, VIEWPORT), 0.5);
});

test("yToRatio / ratioToY clamp non-finite and out-of-range input", () => {
  assert.equal(yToRatio(NaN, VIEWPORT), 0);
  assert.equal(ratioToY(NaN, VIEWPORT), 0); // topInset
  assert.equal(ratioToY(-5, VIEWPORT), 0); // clamps to ratio 0
  assert.equal(ratioToY(5, VIEWPORT), 550); // clamps to ratio 1
  // degenerate band: any ratio resolves to the single reachable point
  assert.equal(yToRatio(50, DEGENERATE_VIEWPORT), 0);
});

test("parseSavedPosition: garbage in, null out; finite out-of-range yRatio clamps", () => {
  const cases = [
    [null, null],
    [undefined, null],
    ["", null],
    ["not json", null],
    ["123", null],
    ['"a string"', null],
    ["[]", null],
    ["{}", null],
    ['{"side":"up","yRatio":0.5}', null], // invalid side
    ['{"side":"left"}', null], // missing yRatio
    ['{"side":"left","yRatio":"0.5"}', null], // wrong type
    ['{"side":"left","yRatio":null}', null],
    ['{"side":"left","yRatio":NaN}', null], // not valid JSON
    ['{"side":"right","yRatio":0.5}', { side: "right", yRatio: 0.5 }],
    ['{"side":"left","yRatio":1.5}', { side: "left", yRatio: 1 }], // clamp above
    ['{"side":"left","yRatio":-0.5}', { side: "left", yRatio: 0 }], // clamp below
  ];
  for (const [raw, expected] of cases) {
    assert.deepEqual(parseSavedPosition(raw), expected, `raw=${raw}`);
  }
});

test("serializePosition round-trips through parseSavedPosition", () => {
  const pos = { side: "left", yRatio: 0.42 };
  assert.equal(serializePosition(pos), '{"side":"left","yRatio":0.42}');
  assert.deepEqual(parseSavedPosition(serializePosition(pos)), pos);
});

test("resolvePosition: invalid/missing saved falls back to bottom-right", () => {
  assert.deepEqual(resolvePosition(null, VIEWPORT), {
    side: "right",
    yRatio: 1,
  });
  assert.deepEqual(resolvePosition(undefined, VIEWPORT), {
    side: "right",
    yRatio: 1,
  });
  assert.deepEqual(resolvePosition({ side: "up", yRatio: 0.5 }, VIEWPORT), {
    side: "right",
    yRatio: 1,
  });
  assert.deepEqual(resolvePosition({ side: "left", yRatio: NaN }, VIEWPORT), {
    side: "right",
    yRatio: 1,
  });
});

test("resolvePosition: valid saved passes through (clamped)", () => {
  assert.deepEqual(resolvePosition({ side: "left", yRatio: 0.3 }, VIEWPORT), {
    side: "left",
    yRatio: 0.3,
  });
  assert.deepEqual(resolvePosition({ side: "right", yRatio: 3 }, VIEWPORT), {
    side: "right",
    yRatio: 1,
  });
});

test("drag machine: a 5px move stays under threshold -> tap:true, never latched", () => {
  let session = dragStart(100, 200, 10, 10);
  session = dragMove(session, 13, 14, TAP_THRESHOLD_PX); // dx=3, dy=4, dist=5 < 6
  assert.equal(session.dragging, false);
  const result = dragEnd(session, VIEWPORT);
  assert.equal(result.tap, true);
});

test("drag machine: moving past the threshold latches dragging permanently, even back at origin", () => {
  let session = dragStart(100, 200, 10, 10);
  session = dragMove(session, 16, 18, TAP_THRESHOLD_PX); // dx=6, dy=8, dist=10 >= 6 -> latches
  assert.equal(session.dragging, true);
  session = dragMove(session, 10, 10, TAP_THRESHOLD_PX); // back to the exact origin, dist=0
  assert.equal(session.dragging, true, "dragging must not un-latch");
  const result = dragEnd(session, VIEWPORT);
  assert.equal(result.tap, false);
});

test("dragEnd: resolves side, snapped x, clamped y and yRatio from the drag delta", () => {
  let session = dragStart(100, 225, 500, 500);
  session = dragMove(session, 520, 550, TAP_THRESHOLD_PX); // dx=20, dy=50
  const result = dragEnd(session, VIEWPORT);
  // fabX = 120, center = 145 < vw/2(150) -> left; fabY = 275 -> mid-band
  assert.deepEqual(result, {
    tap: false,
    side: "left",
    x: 20,
    y: 275,
    yRatio: 0.5,
  });
});

test("dragEnd: a tap (no move) still resolves a consistent position", () => {
  const session = dragStart(230, 0, 300, 300);
  const result = dragEnd(session, VIEWPORT);
  assert.equal(result.tap, true);
  assert.equal(result.side, "right"); // centerX = 230+25=255 >= 150
  assert.equal(result.y, 0);
});

// Hand-rolled fake clock so createIdleTimer's injectable setTimeoutFn/clearTimeoutFn
// can be driven deterministically without real wall-clock waits.
function makeFakeClock() {
  let now = 0;
  let nextId = 1;
  const pending = new Map();
  return {
    setTimeoutFn(fn, ms) {
      const id = nextId++;
      pending.set(id, { fn, at: now + ms });
      return id;
    },
    clearTimeoutFn(id) {
      pending.delete(id);
    },
    advance(ms) {
      now += ms;
      for (const [id, timer] of [...pending.entries()]) {
        if (timer.at <= now) {
          pending.delete(id);
          timer.fn();
        }
      }
    },
  };
}

test("createIdleTimer: fires onIdle once the delay elapses", () => {
  const clock = makeFakeClock();
  let fired = 0;
  createIdleTimer({
    delayMs: 1000,
    onIdle: () => fired++,
    setTimeoutFn: clock.setTimeoutFn,
    clearTimeoutFn: clock.clearTimeoutFn,
  });
  clock.advance(999);
  assert.equal(fired, 0);
  clock.advance(1);
  assert.equal(fired, 1);
});

test("createIdleTimer: poke() restarts the countdown", () => {
  const clock = makeFakeClock();
  let fired = 0;
  const timer = createIdleTimer({
    delayMs: 1000,
    onIdle: () => fired++,
    setTimeoutFn: clock.setTimeoutFn,
    clearTimeoutFn: clock.clearTimeoutFn,
  });
  clock.advance(600);
  timer.poke();
  clock.advance(600); // 1200ms since start, but only 600ms since poke
  assert.equal(fired, 0);
  clock.advance(400); // 1000ms since poke
  assert.equal(fired, 1);
});

test("createIdleTimer: dispose() cancels the pending fire and further pokes are no-ops", () => {
  const clock = makeFakeClock();
  let fired = 0;
  const timer = createIdleTimer({
    delayMs: 1000,
    onIdle: () => fired++,
    setTimeoutFn: clock.setTimeoutFn,
    clearTimeoutFn: clock.clearTimeoutFn,
  });
  timer.dispose();
  clock.advance(10000);
  assert.equal(fired, 0);
  timer.poke();
  clock.advance(10000);
  assert.equal(fired, 0);
});
