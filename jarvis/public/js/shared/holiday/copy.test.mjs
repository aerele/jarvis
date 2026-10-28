// Contract test for the holiday words. Plain node:test, no framework:
// `node --test jarvis/public/js/shared/holiday/*.test.mjs`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { FEST, OCCASIONS, headingFor, leadupLineFor } from "./copy.mjs";

// The approved wording, word for word.
const LEADUP = {
  diwali: "Wishing you a happy Diwali in advance!",
  pongal: "Wishing you a happy Pongal in advance!",
  eid: "Eid Mubarak in advance!",
  christmas: "Wishing you a merry Christmas in advance!",
  newyear: "Wishing you a happy New Year in advance!",
  onam: "Wishing you a happy Onam in advance!",
  holi: "Wishing you a happy Holi in advance!",
  independence: "Wishing you a happy Independence Day in advance!",
  republic: "Wishing you a happy Republic Day in advance!",
};
const HEADING = {
  diwali: "Happy Diwali, Priya",
  pongal: "Happy Pongal, Priya",
  eid: "Eid Mubarak, Priya",
  christmas: "Merry Christmas, Priya",
  newyear: "Happy New Year, Priya",
  onam: "Happy Onam, Priya",
  holi: "Happy Holi, Priya",
  independence: "Happy Independence Day, Priya",
  republic: "Happy Republic Day, Priya",
  birthday: "Happy birthday, Priya",
};

// Built from the code point so this file holds no literal em-dash itself.
const EM_DASH = String.fromCharCode(0x2014);

test("lead-up lines match the approved table", () => {
  for (const [occasion, line] of Object.entries(LEADUP)) {
    assert.equal(leadupLineFor(occasion), line, occasion);
  }
});

test("birthday has no lead-up line", () => {
  assert.equal(leadupLineFor("birthday"), null);
});

test("day headings match the approved table", () => {
  for (const [occasion, heading] of Object.entries(HEADING)) {
    assert.equal(headingFor(occasion, "Priya"), heading, occasion);
  }
});

test("a long or unusual first name is kept as given", () => {
  assert.equal(
    headingFor("diwali", "Venkatasubramanian"),
    "Happy Diwali, Venkatasubramanian"
  );
});

test("a blank first name drops the comma instead of leaving one dangling", () => {
  assert.equal(headingFor("diwali", ""), "Happy Diwali");
  assert.equal(headingFor("diwali", "   "), "Happy Diwali");
  assert.equal(headingFor("diwali", undefined), "Happy Diwali");
});

test("an unknown or inherited key gives nothing", () => {
  for (const key of [
    "",
    "ramadan",
    "toString",
    "__proto__",
    null,
    undefined,
    7,
  ]) {
    assert.equal(headingFor(key, "Priya"), null, String(key));
    assert.equal(leadupLineFor(key), null, String(key));
  }
});

test("no copy names the product or uses an em-dash", () => {
  const all = [
    ...Object.values(LEADUP),
    ...Object.values(HEADING),
    ...Object.values(FEST).flat(),
  ].join("\n");
  assert.ok(!all.includes(EM_DASH), "em-dash");
  assert.ok(!/jarvis/i.test(all), "product name");
});

test("OCCASIONS lists every design plus republic and birthday, once each", () => {
  assert.deepEqual([...OCCASIONS].sort(), [...Object.keys(HEADING)].sort());
  assert.equal(new Set(OCCASIONS).size, OCCASIONS.length);
});

test("FEST has a card for every holiday occasion and none for birthday", () => {
  for (const occasion of Object.keys(LEADUP)) {
    const [title, prompt] = FEST[occasion];
    assert.ok(title && prompt, occasion);
  }
  assert.equal(FEST.birthday, undefined);
  assert.match(FEST.republic[0], /Republic Day/);
});
