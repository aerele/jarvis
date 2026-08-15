import { test } from "node:test";
import assert from "node:assert/strict";
import { greetingFor, greetingLine, suggestionsFor } from "./panel_welcome.mjs";

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
