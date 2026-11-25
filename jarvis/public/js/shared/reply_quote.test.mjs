import assert from "node:assert/strict";
import test from "node:test";

import {
  QUOTE_MAX,
  cleanQuote,
  quoteMarkdown,
  selectedReplyText,
  splitQuote,
  withQuote,
} from "./reply_quote.mjs";

test("cleanQuote trims, folds blank runs and drops trailing spaces", () => {
  assert.equal(cleanQuote("  a  \r\n\n\n\nb\u00a0c \n"), "a\n\nb c");
  assert.equal(cleanQuote(null), "");
});

test("cleanQuote cuts a long selection on a word with an ellipsis", () => {
  const long = "word ".repeat(QUOTE_MAX);
  const out = cleanQuote(long);
  assert.ok(out.length <= QUOTE_MAX + 1);
  assert.ok(out.endsWith("word…"));
});

test("quoteMarkdown prefixes every line and keeps paragraph breaks", () => {
  assert.equal(
    quoteMarkdown("Row 3: 1,200\n\nTotal"),
    "> Row 3: 1,200\n>\n> Total\n\n"
  );
  assert.equal(quoteMarkdown("   "), "");
});

test("splitQuote takes only the leading blockquote", () => {
  assert.deepEqual(splitQuote("> a\n>\n> b\n\nwhy?"), {
    quote: "a\n\nb",
    rest: "why?",
  });
  assert.deepEqual(splitQuote("why?\n> later"), {
    quote: "",
    rest: "why?\n> later",
  });
  assert.deepEqual(splitQuote(">not a quote"), {
    quote: "",
    rest: ">not a quote",
  });
  assert.deepEqual(splitQuote("> only"), { quote: "only", rest: "" });
});

test("quoteMarkdown and splitQuote round-trip", () => {
  const text = "line one\n\nline two";
  assert.equal(splitQuote(quoteMarkdown(text) + "q").quote, text);
});

test("withQuote puts the quote on top and replaces an unsent one", () => {
  assert.equal(withQuote("", "x"), "> x\n\n");
  assert.equal(withQuote("my question", "x"), "> x\n\nmy question");
  assert.equal(
    withQuote("> old\n\nmy question", "new"),
    "> new\n\nmy question"
  );
  assert.equal(withQuote("typed", "  "), "typed");
});

// A tiny DOM stand-in: closest() walks parents matching a class.
function el(cls, parent = null) {
  const node = { nodeType: 1, cls, parentElement: parent };
  node.closest = (sel) => {
    const want = sel.split(" ").pop().slice(1);
    for (let n = node; n; n = n.parentElement) if (n.cls === want) return n;
    return null;
  };
  return node;
}
function selection(start, end, text, collapsed = false) {
  return {
    isCollapsed: collapsed,
    rangeCount: 1,
    getRangeAt: () => ({ startContainer: start, endContainer: end }),
    toString: () => text,
  };
}

test("selectedReplyText reads a selection inside one reply only", () => {
  const root = el("root");
  root.contains = (n) => {
    for (let x = n; x; x = x.parentElement) if (x === root) return true;
    return false;
  };
  const bodyA = el("jv-md-body", el("jv-amsg", root));
  const bodyB = el("jv-md-body", el("jv-amsg", root));
  const textA = { nodeType: 3, parentElement: el("p", bodyA) };
  const textB = { nodeType: 3, parentElement: bodyB };
  const sel = ".jv-amsg .jv-md-body";
  assert.equal(
    selectedReplyText(selection(textA, textA, " Row 3 "), root, sel),
    "Row 3"
  );
  assert.equal(selectedReplyText(selection(textA, textB, "x"), root, sel), "");
  assert.equal(
    selectedReplyText(selection(textA, textA, "x", true), root, sel),
    ""
  );
  const outside = { nodeType: 3, parentElement: el("composer", root) };
  assert.equal(
    selectedReplyText(selection(outside, outside, "x"), root, sel),
    ""
  );
  const otherRoot = el("jv-md-body", el("elsewhere"));
  assert.equal(
    selectedReplyText(selection(otherRoot, otherRoot, "x"), root, sel),
    ""
  );
});
