import { test } from "node:test";
import assert from "node:assert/strict";
import { WORN_GAP_PX, WORN_OVERHANG } from "../../shared/holiday/worn-room.mjs";
import {
  HERO_PX,
  WELCOME_PAD_TOP_PX,
  greetingFor,
  greetingLine,
  heroTopRoom,
  isWelcomeOpen,
  suggestionsFor,
  welcomeLines,
} from "./panel_welcome.mjs";

test("greetingFor: splits the day at noon and 5pm", () => {
  assert.equal(greetingFor(0), "Good morning");
  assert.equal(greetingFor(11), "Good morning");
  assert.equal(greetingFor(12), "Good afternoon");
  assert.equal(greetingFor(16), "Good afternoon");
  assert.equal(greetingFor(17), "Good evening");
  assert.equal(greetingFor(23), "Good evening");
});

test("greetingFor: a junk hour still greets", () => {
  assert.equal(greetingFor(undefined), "Good afternoon");
  assert.equal(greetingFor(NaN), "Good afternoon");
});

test("greetingLine: uses the first name only", () => {
  assert.equal(greetingLine(18, "Kavin Raj"), "Good evening, Kavin");
  assert.equal(greetingLine(9, "Administrator"), "Good morning, Administrator");
});

test("greetingLine: drops the comma when there is no name", () => {
  assert.equal(greetingLine(18, ""), "Good evening");
  assert.equal(greetingLine(18, null), "Good evening");
  assert.equal(greetingLine(18, "   "), "Good evening");
});

test("suggestionsFor: a record gets record-scoped prompts", () => {
  const s = suggestionsFor({ doctype: "Sales Invoice", name: "INV-0001" });
  assert.equal(s.length, 3);
  assert.equal(s[0].title, "Explain this record");
  assert.ok(s.every((x) => x.prompt && x.title));
});

test("suggestionsFor: a list or report gets collection-scoped prompts", () => {
  assert.equal(
    suggestionsFor({ doctype: "Sales Invoice" })[0].title,
    "Analyse data"
  );
  assert.equal(
    suggestionsFor({ report_name: "Accounts Receivable" })[0].title,
    "Analyse data"
  );
  assert.equal(
    suggestionsFor({ doctype: "Sales Invoice" })[0].prompt,
    "Which of these need my attention first?"
  );
});

test("suggestionsFor: no context falls back to the general starting points", () => {
  for (const ctx of [null, undefined, {}, "junk"]) {
    const s = suggestionsFor(ctx);
    assert.equal(s.length, 3);
    assert.equal(s[0].prompt, "Which sales orders are overdue this month?");
  }
});

test("suggestionsFor: every prompt is a full sentence the agent can act on", () => {
  for (const ctx of [null, { doctype: "X" }, { doctype: "X", name: "Y" }]) {
    for (const s of suggestionsFor(ctx)) {
      assert.ok(s.prompt.length > 10, `too short: ${s.prompt}`);
      assert.ok(/[.?]$/.test(s.prompt), `no terminal punctuation: ${s.prompt}`);
    }
  }
});

// ---- holiday touches in the welcome ----

test("heroTopRoom: the hat's reach plus the gap, less the padding already there", () => {
  const reach = (WORN_OVERHANG.christmas * HERO_PX) / 56;
  assert.equal(
    heroTopRoom("christmas"),
    Math.ceil(reach + WORN_GAP_PX - WELCOME_PAD_TOP_PX)
  );
  assert.ok(heroTopRoom("christmas") > heroTopRoom("holi"));
});

test("heroTopRoom: no room for a design whose item stays beside the tile, or none", () => {
  assert.equal(heroTopRoom("diwali"), 0);
  assert.equal(heroTopRoom(null), 0);
  assert.equal(heroTopRoom("toString"), 0);
});

test("heroTopRoom is never negative", () => {
  for (const design of Object.keys(WORN_OVERHANG)) {
    assert.ok(heroTopRoom(design) >= 0, design);
  }
});

test("welcomeLines: the day heading replaces the greeting, a lead-up adds the wish", () => {
  const plain = { heading: null, leadupLine: null };
  assert.deepEqual(welcomeLines("Good evening, Priya", plain), {
    greeting: "Good evening, Priya",
    leadupLine: null,
  });
  assert.deepEqual(
    welcomeLines("Good evening, Priya", {
      heading: "Happy Diwali, Priya",
      leadupLine: null,
    }),
    { greeting: "Happy Diwali, Priya", leadupLine: null }
  );
  assert.deepEqual(
    welcomeLines("Good evening, Priya", {
      heading: null,
      leadupLine: "Wishing you a happy Diwali in advance!",
    }),
    {
      greeting: "Good evening, Priya",
      leadupLine: "Wishing you a happy Diwali in advance!",
    }
  );
});

test("welcomeLines: a hold (maintenance) shows the normal greeting", () => {
  const view = { heading: "Happy Diwali, Priya", leadupLine: "x" };
  assert.deepEqual(welcomeLines("Good evening", view, { hold: true }), {
    greeting: "Good evening",
    leadupLine: null,
  });
});

// ---- when does a welcome count as open? (the panel's celebration trigger) ----

// The state in which a welcome is open: the panel is open and settled, the workspace is set
// up, and there is nothing in the thread.
const OPEN = Object.freeze({
  open: true,
  settled: true,
  enabled: true,
  maintenance: false,
  readiness: "ready",
  loading: false,
  hasMessages: false,
  loadError: false,
  streaming: false,
  thinking: false,
});

test("isWelcomeOpen: an open panel with an empty thread is a welcome", () => {
  assert.equal(isWelcomeOpen(OPEN), true);
});

test("isWelcomeOpen: each condition alone closes it", () => {
  const closers = {
    open: false,
    settled: false,
    enabled: false,
    maintenance: true,
    loading: true,
    hasMessages: true,
    loadError: true,
    streaming: true,
    thinking: true,
  };
  for (const [field, value] of Object.entries(closers)) {
    assert.equal(isWelcomeOpen({ ...OPEN, [field]: value }), false, field);
  }
});

test("isWelcomeOpen: needs a resolved readiness that is not the setup gate", () => {
  assert.equal(isWelcomeOpen({ ...OPEN, readiness: "ready" }), true);
  assert.equal(isWelcomeOpen({ ...OPEN, readiness: "degraded" }), true);
  // Not known yet: an unset-up workspace must not count as a welcome before the verdict.
  assert.equal(isWelcomeOpen({ ...OPEN, readiness: null }), false);
  assert.equal(isWelcomeOpen({ ...OPEN, readiness: "gate" }), false);
  assert.equal(isWelcomeOpen({ ...OPEN, readiness: "something-new" }), false);
});

test("isWelcomeOpen: a missing field never opens it", () => {
  assert.equal(isWelcomeOpen({}), false);
  assert.equal(isWelcomeOpen({ ...OPEN, enabled: undefined }), false);
});
