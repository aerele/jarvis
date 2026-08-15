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
