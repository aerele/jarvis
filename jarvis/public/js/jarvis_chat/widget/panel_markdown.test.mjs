import { test } from "node:test";
import assert from "node:assert/strict";
import { stripControlBlocks, renderReply } from "./panel_markdown.mjs";

test("strips the jarvis-skill fence the agent appends to replies", () => {
  const src = "Here is the summary.\n\n```jarvis-skill\nerpnext-accounts\n```";
  assert.equal(stripControlBlocks(src), "Here is the summary.");
});

test("removes pure control fences outright, keeping the prose", () => {
  for (const name of [
    "jarvis-action",
    "confirm",
    "jarvis-ask",
    "jarvis-skill",
    "jarvis-macro",
  ]) {
    const src = `Before.\n\n\`\`\`${name}\n{"x":1}\n\`\`\`\n\nAfter.`;
    const out = stripControlBlocks(src);
    assert.ok(!out.includes(name), `${name} leaked`);
    assert.ok(!out.includes('"x"'), `${name} body leaked`);
    assert.ok(out.includes("Before.") && out.includes("After."));
  }
});

const gotoBlock = (page, prompt = "Show the cash view") =>
  `\`\`\`jarvis-goto\n${JSON.stringify({ page, prompt })}\n\`\`\``;

test("a Dashboards goto becomes one generic chip: no JSON, no prompt text", () => {
  const src = `Opening that for you.\n\n${gotoBlock(
    "dashboards"
  )}\n\nAnything else?`;
  const html = renderReply(src);
  assert.equal(html.split("data-open-full").length - 1, 1, "want one chip");
  assert.ok(html.includes(">Continue in full chat</span>"), "title");
  assert.ok(html.includes(">to open Dashboards</span>"), "subline");
  assert.ok(!html.includes("Open in full chat"), "generic subline leaked");
  assert.ok(!html.includes("jarvis-goto"), "fence leaked");
  assert.ok(!html.includes("cash view"), "prompt leaked");
  assert.ok(!html.includes("&quot;page&quot;"), "json leaked");
  assert.ok(html.includes("Opening that for you."), "prose dropped");
  assert.ok(html.includes("Anything else?"), "prose dropped");
});

test("two goto blocks give one chip, from the first valid one", () => {
  const src = `A.\n\n${gotoBlock("dashboards")}\n\nB.\n\n${gotoBlock(
    "dashboards",
    "Second ask"
  )}`;
  const html = renderReply(src);
  assert.equal(html.split("data-open-full").length - 1, 1);
  assert.ok(!html.includes("Second ask"), "second block leaked");
});

test("a Payment Calendar goto gets no chip and no raw text", () => {
  const src = `Opening that for you.\n\n${gotoBlock(
    "payment-calendar"
  )}\n\nAnything else?`;
  assert.equal(
    stripControlBlocks(src),
    "Opening that for you.\n\nAnything else?"
  );
  const html = renderReply(src);
  assert.ok(!html.includes("data-open-full"), "calendar must not chip");
  assert.ok(!html.includes("jarvis-goto"), "fence leaked");
  assert.ok(!html.includes("cash view"), "prompt leaked");
});

test("a malformed or incomplete goto block gets no chip and no raw text", () => {
  for (const body of [
    "{not json",
    '{"page":"dashboards"}',
    '{"page":"dashboards","prompt":"  "}',
    '{"page":"nowhere","prompt":"x"}',
    "[]",
  ]) {
    const html = renderReply(
      `Hi.\n\n\`\`\`jarvis-goto\n${body}\n\`\`\`\n\nBye.`
    );
    assert.ok(!html.includes("data-open-full"), `${body} chipped`);
    assert.ok(!html.includes("jarvis-goto"), `${body} fence leaked`);
    assert.ok(!html.includes("nowhere") && !html.includes("not json"), body);
  }
});

test("only the FIRST goto counts: a calendar goto before a Dashboards one gives no chip", () => {
  const src = `${gotoBlock("payment-calendar")}\n\n${gotoBlock("dashboards")}`;
  assert.ok(!renderReply(src).includes("data-open-full"));
});

test("an unclosed jarvis-goto fence is stripped while streaming: no <pre>, no JSON", () => {
  for (const tail of [
    '```jarvis-goto\n{"page":"dashboards","prompt":"x"}',
    '```jarvis-goto\n{"page":"dashbo',
    "```jarvis-goto\n",
    "```jarvis-goto",
  ]) {
    const src = `Wait.\n\n${tail}`;
    assert.equal(stripControlBlocks(src), "Wait.", JSON.stringify(tail));
    const html = renderReply(src);
    assert.ok(!html.includes("<pre"), `${tail} drew a code block`);
    assert.ok(!html.includes("page"), `${tail} leaked json`);
    assert.ok(!html.includes("data-open-full"), `${tail} chipped early`);
    assert.ok(html.includes("Wait."));
  }
});

test("an unclosed goto tail whose prompt holds backticks (or a half-typed closing fence) is stripped too", () => {
  for (const tail of [
    '```jarvis-goto\n{"page":"dashboards","prompt":"use `x` here"}',
    '```jarvis-goto\n{"page":"dashboards","prompt":"use `x` he',
    '```jarvis-goto\n{"page":"dashboards","prompt":"a ``b`` c"',
    '```jarvis-goto\n{"page":"dashboards","prompt":"x"}\n``',
    '```jarvis-goto\n{"page":"dashboards","prompt":"x"}\n`',
  ]) {
    const src = `Wait.\n\n${tail}`;
    assert.equal(stripControlBlocks(src), "Wait.", JSON.stringify(tail));
    const html = renderReply(src);
    assert.ok(!html.includes("<pre"), `${tail} drew a code block`);
    assert.ok(!html.includes("page"), `${tail} leaked json`);
    assert.ok(!html.includes("`"), `${tail} leaked a backtick`);
    assert.ok(!html.includes("data-open-full"), `${tail} chipped early`);
  }
});

test("a closed goto followed by a streaming unclosed one: one chip, no raw tail", () => {
  const src = `${gotoBlock("dashboards")}\n\nMore.\n\n\`\`\`jarvis-goto\n{"pa`;
  const html = renderReply(src);
  assert.equal(html.split("data-open-full").length - 1, 1);
  assert.ok(!html.includes("<pre"));
});

test("other chips keep the generic subline", () => {
  const html = renderReply("```mermaid\nflowchart TD\nA-->B\n```");
  assert.ok(html.includes(">Open in full chat</span>"));
  assert.ok(!html.includes("to open Dashboards"));
});

test("keeps ordinary code fences — those are content", () => {
  const src = "Run this:\n\n```bash\nbench migrate\n```";
  assert.ok(stripControlBlocks(src).includes("bench migrate"));
});

test("collapses the blank runs a stripped fence leaves behind", () => {
  const src = "A.\n\n```jarvis-skill\nx\n```\n\n\n\nB.";
  assert.equal(stripControlBlocks(src), "A.\n\nB.");
});

test("is safe on empty and non-string input", () => {
  assert.equal(stripControlBlocks(""), "");
  assert.equal(stripControlBlocks(null), "");
  assert.equal(stripControlBlocks(undefined), "");
});

// ---- content the panel can't draw -> an "open in full chat" chip ----

test("renderReply: a mermaid flowchart becomes a Diagram chip, not raw source", () => {
  const html = renderReply("The flow:\n\n```mermaid\nflowchart TD\nA-->B\n```");
  assert.ok(html.includes('class="jvp-view-chip"'), "no chip");
  assert.ok(html.includes(">Diagram</span>"), "wrong label");
  assert.ok(!html.includes("flowchart TD"), "raw mermaid leaked");
  assert.ok(html.includes("The flow:"), "prose dropped");
});

test("renderReply: jarvis-chart and mermaid xychart both become a Chart chip", () => {
  for (const src of [
    'Sales:\n\n```jarvis-chart\n{"type":"bar"}\n```',
    "Sales:\n\n```mermaid\nxychart-beta\n  bar [1,2,3]\n```",
  ]) {
    const html = renderReply(src);
    assert.ok(html.includes('class="jvp-view-chip"'), "no chip");
    assert.ok(html.includes(">Chart</span>"), "wrong label");
    assert.ok(
      !html.includes("xychart") && !html.includes('"type"'),
      "raw spec leaked"
    );
  }
});

test("renderReply: jarvis-cards becomes a Records chip", () => {
  const html = renderReply(
    'Matches:\n\n```jarvis-cards\n[{"name":"SO-1"}]\n```'
  );
  assert.ok(html.includes('class="jvp-view-chip"'), "no chip");
  assert.ok(html.includes(">Records</span>"), "wrong label");
  assert.ok(!html.includes("SO-1"), "raw card data leaked");
});

test("renderReply: every chip carries the open-full hook and the hint", () => {
  const html = renderReply("```mermaid\nflowchart TD\nA-->B\n```");
  assert.ok(html.includes("data-open-full"));
  assert.ok(html.includes("Open in full chat"));
});

test("renderReply: plain markdown (a table) stays inline, no chip", () => {
  const html = renderReply("| A | B |\n|---|---|\n| 1 | 2 |");
  assert.ok(!html.includes("jvp-view-chip"), "spurious chip");
  assert.ok(html.includes("jv-md-table"), "table not rendered");
});
