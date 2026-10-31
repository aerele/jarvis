// Contract test for the client cadence rule. The cases are the server's own file
// (jarvis/holiday_touch/cadence_cases.json), so the two sides cannot drift.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  LOCAL_KEY,
  addShowing,
  mayPlay,
  mayPlayLocal,
  readLocal,
  recordLocal,
} from "./cadence.mjs";

const CASES = JSON.parse(
  readFileSync(
    new URL("../../../../holiday_touch/cadence_cases.json", import.meta.url),
    "utf8"
  )
);

const memory = () => {
  const data = new Map();
  return {
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => void data.set(k, String(v)),
  };
};

test("the shared case table is not empty", () => {
  assert.ok(CASES.length >= 20);
});

for (const { name, tz, now, times, expect } of CASES) {
  test(`mayPlay: ${name}`, () => {
    assert.equal(mayPlay(now, times, tz), expect);
  });
}

test("an unknown zone throws, as the Python side does", () => {
  assert.throws(
    () => mayPlay("2026-10-28T09:00:00Z", [], "Not/AZone"),
    RangeError
  );
});

test("showings from another local day are ignored, unreadable ones too", () => {
  const times = ["2026-10-27T09:00:00+00:00", "junk", null];
  assert.equal(mayPlay("2026-10-28T09:00:00+00:00", times, "UTC"), true);
});

test("addShowing keeps today's showings only and appends now", () => {
  const state = {
    date: "x",
    times: ["2026-10-27T09:00:00+00:00", "2026-10-28T08:00:00+00:00", "junk"],
  };
  assert.deepEqual(addShowing(state, "2026-10-28T09:30:00+00:00", "UTC"), {
    date: "2026-10-28",
    times: ["2026-10-28T08:00:00.000Z", "2026-10-28T09:30:00.000Z"],
  });
  assert.deepEqual(addShowing(null, "2026-10-28T09:30:00+00:00", "UTC").times, [
    "2026-10-28T09:30:00.000Z",
  ]);
});

test("a showing recorded in the browser blocks the next one in the same slot", () => {
  const storage = memory();
  const now = "2026-10-28T09:00:00+00:00";
  assert.equal(mayPlayLocal(storage, now, "UTC"), true);
  recordLocal(storage, now, "UTC");
  assert.ok(storage.getItem(LOCAL_KEY));
  assert.equal(
    mayPlayLocal(storage, "2026-10-28T10:00:00+00:00", "UTC"),
    false
  );
  assert.equal(mayPlayLocal(storage, "2026-10-28T13:00:00+00:00", "UTC"), true);
  assert.equal(readLocal(storage).times.length, 1);
});

test("blocked storage never throws, and an unknown zone does not play", () => {
  const blocked = {
    getItem() {
      throw new Error("denied");
    },
    setItem() {
      throw new Error("denied");
    },
  };
  assert.doesNotThrow(() =>
    recordLocal(blocked, "2026-10-28T09:00:00Z", "UTC")
  );
  assert.equal(mayPlayLocal(blocked, "2026-10-28T09:00:00Z", "UTC"), true);
  assert.equal(
    mayPlayLocal(memory(), "2026-10-28T09:00:00Z", "Not/AZone"),
    false
  );
});
