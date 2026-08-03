import { test } from "node:test";
import assert from "node:assert/strict";
import {
  STORAGE_KEY,
  MIN_W,
  MIN_H,
  parseSavedSize,
  serializeSize,
  resizeFrom,
} from "./panel_size.mjs";
import { PANEL_W, PANEL_MAX_H } from "./panel_anchor.mjs";

test("MIN_* are the shipped default (the never-shrink floor)", () => {
  assert.equal(MIN_W, PANEL_W);
  assert.equal(MIN_H, PANEL_MAX_H);
});

test("STORAGE_KEY is stable", () => {
  assert.equal(STORAGE_KEY, "jarvis-panel-size");
});

// ---- parseSavedSize ----
test("parseSavedSize: a valid larger size round-trips", () => {
  assert.deepEqual(
    parseSavedSize(JSON.stringify({ width: 560, height: 720 })),
    {
      width: 560,
      height: 720,
    }
  );
});

test("parseSavedSize: floors a sub-default stored size at the default", () => {
  assert.deepEqual(
    parseSavedSize(JSON.stringify({ width: 100, height: 100 })),
    {
      width: MIN_W,
      height: MIN_H,
    }
  );
});

test("parseSavedSize: null / garbage / partial -> null", () => {
  assert.equal(parseSavedSize(null), null);
  assert.equal(parseSavedSize(""), null);
  assert.equal(parseSavedSize("not json"), null);
  assert.equal(parseSavedSize("[1,2]"), null); // array, no width/height
  assert.equal(parseSavedSize(JSON.stringify({ width: 500 })), null); // height missing
  assert.equal(
    parseSavedSize(JSON.stringify({ width: "x", height: 700 })),
    null
  );
});

// ---- serializeSize ----
test("serializeSize: floors at the default and round-trips through parse", () => {
  assert.deepEqual(parseSavedSize(serializeSize({ width: 10, height: 10 })), {
    width: MIN_W,
    height: MIN_H,
  });
  assert.deepEqual(parseSavedSize(serializeSize({ width: 700, height: 800 })), {
    width: 700,
    height: 800,
  });
});

// ---- resizeFrom ----
const START = { width: 400, height: 624 };

test("resizeFrom: left panel grows WIDER when dragged left (dx negative)", () => {
  assert.deepEqual(resizeFrom(START, "left", -50, 0), {
    width: 450,
    height: 624,
  });
});

test("resizeFrom: left panel shrink is floored at the default", () => {
  assert.deepEqual(resizeFrom(START, "left", 80, 0), {
    width: 400,
    height: 624,
  });
  assert.deepEqual(resizeFrom({ width: 500, height: 624 }, "left", 80, 0), {
    width: 420,
    height: 624,
  });
});

test("resizeFrom: right panel grows WIDER when dragged right (dx positive)", () => {
  assert.deepEqual(resizeFrom(START, "right", 60, 0), {
    width: 460,
    height: 624,
  });
});

test("resizeFrom: dragging UP grows height on both sides (dy negative)", () => {
  assert.deepEqual(resizeFrom(START, "left", 0, -40), {
    width: 400,
    height: 664,
  });
  assert.deepEqual(resizeFrom(START, "right", 0, -40), {
    width: 400,
    height: 664,
  });
});

test("resizeFrom: dragging DOWN shrinks height, floored at the default", () => {
  assert.deepEqual(resizeFrom(START, "left", 0, 100), {
    width: 400,
    height: 624,
  });
  assert.deepEqual(resizeFrom({ width: 400, height: 800 }, "left", 0, 100), {
    width: 400,
    height: 700,
  });
});

test("resizeFrom: rounds fractional pointer deltas", () => {
  assert.deepEqual(resizeFrom(START, "right", 10.4, -10.6), {
    width: 410,
    height: 635,
  });
});
