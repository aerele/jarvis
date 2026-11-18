// Reply to a part of an earlier reply (aerele/jarvis#2191), shared by the desktop chat
// and the PWA.
//
// The quote travels INSIDE the message, as a Markdown blockquote at its top:
//
//   > the selected text
//   >
//   > its second paragraph
//
//   the user's question
//
// so the agent reads the exact text with no new field, protocol or backend change,
// the message history keeps it, and edit-and-resend, drafts and send recovery carry it
// like any other text. The user bubble draws the leading blockquote as a quote block
// (splitQuote); everywhere else it is plain Markdown.

// A quote longer than this is cut, so a whole page selected by accident cannot fill
// the message; the agent still has the full reply in its history.
export const QUOTE_MAX = 1500;

// The text worth quoting: trailing spaces off every line, runs of blank lines folded
// to one, trimmed, and cut at QUOTE_MAX (on a word when one is near) with "…".
export function cleanQuote(text) {
  let t = String(text || "")
    .replace(/\r\n?/g, "\n")
    .replace(/\u00a0/g, " ")
    .split("\n")
    .map((l) => l.replace(/\s+$/, ""))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  if (t.length > QUOTE_MAX) {
    const cut = t.slice(0, QUOTE_MAX);
    const space = cut.lastIndexOf(" ");
    t =
      (space > QUOTE_MAX - 80 ? cut.slice(0, space) : cut).replace(/\s+$/, "") +
      "…";
  }
  return t;
}

// `text` as a Markdown blockquote followed by a blank line, or "" when there is
// nothing to quote.
export function quoteMarkdown(text) {
  const t = cleanQuote(text);
  if (!t) return "";
  return (
    t
      .split("\n")
      .map((l) => (l ? `> ${l}` : ">"))
      .join("\n") + "\n\n"
  );
}

// The leading blockquote of `content` and the text after it:
// {quote: "the quoted text" | "", rest: "the question"}. Only a blockquote at the very
// top counts; one further down is the user's own Markdown and stays in `rest`.
export function splitQuote(content) {
  const s = String(content || "");
  const lines = s.split("\n");
  let i = 0;
  while (i < lines.length && /^>( |$)/.test(lines[i])) i++;
  if (!i) return { quote: "", rest: s };
  const quote = lines
    .slice(0, i)
    .map((l) => l.replace(/^> ?/, ""))
    .join("\n")
    .trim();
  if (!quote) return { quote: "", rest: s };
  return { quote, rest: lines.slice(i).join("\n").trim() };
}

// The composer text after "Reply" on `selected`: the new quote on top, then whatever
// the user had typed. A quote already at the top (an earlier Reply not sent yet) is
// replaced, not stacked.
export function withQuote(input, selected) {
  const quote = quoteMarkdown(selected);
  if (!quote) return String(input || "");
  return quote + splitQuote(input).rest;
}

// The text selected inside ONE reply under `root`, or "" when the selection is empty,
// spans more than one reply, or is outside the replies (`replySelector` names a
// reply's body, e.g. ".jv-amsg .jv-md-body").
export function selectedReplyText(selection, root, replySelector) {
  if (!selection || selection.isCollapsed || !selection.rangeCount || !root)
    return "";
  const range = selection.getRangeAt(0);
  const bodyOf = (node) => {
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    return el && el.closest ? el.closest(replySelector) : null;
  };
  const start = bodyOf(range.startContainer);
  if (!start || start !== bodyOf(range.endContainer) || !root.contains(start))
    return "";
  return cleanQuote(selection.toString());
}
