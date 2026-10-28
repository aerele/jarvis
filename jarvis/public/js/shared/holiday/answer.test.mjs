// Contract test for the holiday answer reader. The accept cases are the server's
// own file (jarvis/holiday_touch/answer_cases.json), so the two sides cannot drift.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DESIGNS, isStale, readAnswer } from "./answer.mjs";

const CASES = JSON.parse(
  readFileSync(
    new URL("../../../../holiday_touch/answer_cases.json", import.meta.url),
    "utf8"
  )
);
const NONE = { v: 1, kind: "none" };
const good = () =>
  structuredClone(CASES.find((c) => c.name === "day_diwali").payload);

test("the server cases file is non-empty and covers every kind", () => {
  const kinds = new Set(CASES.map((c) => c.payload.kind));
  for (const kind of ["none", "leadup", "day", "birthday"]) {
    assert.ok(kinds.has(kind), kind);
  }
});

test("readAnswer accepts every server case unchanged", () => {
  for (const { name, payload } of CASES) {
    assert.deepEqual(readAnswer(payload), payload, name);
  }
});

test("readAnswer never mutates its input", () => {
  const payload = good();
  const frozen = Object.freeze({ ...payload });
  assert.deepEqual(readAnswer(frozen), payload);
});

test("anything that is not an object reads as none", () => {
  for (const raw of [undefined, null, 0, 1, "day", true, [], [good()]]) {
    assert.deepEqual(readAnswer(raw), NONE, JSON.stringify(raw));
  }
});

test("a missing or unsupported v reads as none", () => {
  for (const v of [undefined, null, 0, -1, 1.5, "1", NaN]) {
    assert.deepEqual(readAnswer({ ...good(), v }), NONE, String(v));
  }
});

test("versioning is additive: a later v with the known fields is read", () => {
  const out = readAnswer({ ...good(), v: 2, extra: { anything: 1 } });
  assert.equal(out.kind, "day");
  assert.equal(out.v, 2);
});

test("unknown fields are dropped", () => {
  const out = readAnswer({
    ...good(),
    description: "Diwali",
    dob: "1990-01-01",
  });
  assert.deepEqual(out, good());
  assert.ok(!("description" in out) && !("dob" in out));
});

test("an unknown kind reads as none", () => {
  for (const kind of ["scene", "", null, 3, "DAY", undefined]) {
    assert.deepEqual(readAnswer({ ...good(), kind }), NONE, String(kind));
  }
});

test("an unknown design or occasion reads as none", () => {
  for (const design of [
    "ramadan",
    "",
    null,
    "republic",
    "toString",
    undefined,
  ]) {
    assert.deepEqual(readAnswer({ ...good(), design }), NONE, String(design));
  }
  for (const occasion of ["ramadan", 5, ""]) {
    assert.deepEqual(
      readAnswer({ ...good(), occasion }),
      NONE,
      String(occasion)
    );
  }
});

test("republic is an occasion on the independence design, not a design", () => {
  assert.ok(!DESIGNS.includes("republic"));
  const out = readAnswer({
    ...good(),
    design: "independence",
    occasion: "republic",
  });
  assert.equal(out.occasion, "republic");
});

test("a day without a usable date or tz reads as none", () => {
  for (const date of [undefined, "", "8 Nov", "2026-13-40", 20261108]) {
    assert.deepEqual(readAnswer({ ...good(), date }), NONE, String(date));
  }
  assert.deepEqual(readAnswer({ ...good(), tz: undefined }), NONE);
  assert.deepEqual(readAnswer({ ...good(), tz: 5 }), NONE);
});

test("flags must be real booleans to count", () => {
  const out = readAnswer({ ...good(), worn: "yes", scene: 1, may_play: "no" });
  assert.equal(out.worn, false);
  assert.equal(out.scene, false);
  assert.equal(out.may_play, true);
});

test("a none answer keeps its date and tz when they are valid, and survives without them", () => {
  assert.deepEqual(
    readAnswer({ v: 1, date: "2026-11-08", tz: "Asia/Kolkata", kind: "none" }),
    { v: 1, date: "2026-11-08", tz: "Asia/Kolkata", kind: "none" }
  );
  assert.deepEqual(readAnswer({ v: 1, kind: "none" }), NONE);
});

test("readAnswer never throws on hostile input", () => {
  const hostile = {
    get kind() {
      throw new Error("boom");
    },
  };
  assert.deepEqual(readAnswer(hostile), NONE);
  assert.doesNotThrow(() => readAnswer(Object.create(null)));
});

// 2026-11-08 00:30 in Kolkata is 2026-11-07 19:00 UTC: the two zones disagree on the date.
const KOLKATA_JUST_AFTER_MIDNIGHT = new Date("2026-11-07T19:00:00Z");

test("isStale compares the answer date with today in the answer's own zone", () => {
  const answer = good(); // date 2026-11-08, Asia/Kolkata
  assert.equal(isStale(answer, KOLKATA_JUST_AFTER_MIDNIGHT), false);
  assert.equal(
    isStale({ ...answer, tz: "UTC" }, KOLKATA_JUST_AFTER_MIDNIGHT),
    true
  );
  assert.equal(isStale(answer, new Date("2026-11-08T18:29:00Z")), false);
  assert.equal(isStale(answer, new Date("2026-11-08T18:31:00Z")), true);
});

test("isStale reads the date in the extreme zones too", () => {
  // 2026-11-08 12:00 UTC is already the 9th at UTC+14 and still the 7th at UTC-11.
  const noon = new Date("2026-11-08T12:00:00Z");
  const at = (tz, date) => isStale({ ...good(), tz, date }, noon);
  assert.equal(at("Pacific/Kiritimati", "2026-11-09"), false);
  assert.equal(at("Pacific/Kiritimati", "2026-11-08"), true);
  assert.equal(at("Pacific/Pago_Pago", "2026-11-08"), false);
  assert.equal(at("Pacific/Pago_Pago", "2026-11-09"), true);
});

test("isStale is true for a tab left open overnight", () => {
  assert.equal(isStale(good(), new Date("2026-11-09T05:00:00Z")), true);
});

test("isStale treats an invalid zone as stale instead of throwing", () => {
  const answer = { ...good(), tz: "Not/AZone" };
  assert.equal(isStale(answer, new Date("2026-11-08T05:00:00Z")), true);
});

test("a none answer with no date is never stale; with a date it follows the day", () => {
  assert.equal(isStale(NONE, new Date("2030-01-01T00:00:00Z")), false);
  const none = { v: 1, date: "2026-11-08", tz: "Asia/Kolkata", kind: "none" };
  assert.equal(isStale(none, new Date("2026-11-08T05:00:00Z")), false);
  assert.equal(isStale(none, new Date("2026-11-09T05:00:00Z")), true);
});

test("isStale tolerates a missing now by using the clock", () => {
  assert.equal(typeof isStale(good()), "boolean");
});
