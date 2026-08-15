// Agent replies are GFM: bold, bullets, pipe tables, fenced code. Rendering
// them as plain text is how the panel ended up showing literal ** and | rows.
//
// The renderer is the SPA's own (frontend/src/markdown.js) rather than a third
// implementation — it is dependency-free and already tuned for these replies,
// and it escapes HTML before emitting, which is what makes v-html safe here.
import { renderMarkdown } from "../../../../../frontend/src/markdown.js";

// Pure interactive control fences: NOT viewable content. The panel surfaces
// actions / confirmations through its own event handlers (action:pending etc.),
// not the transcript, so these are removed outright.
const STRIP_FENCES = [
  "jarvis-action",
  "confirm",
  "jarvis-ask",
  "jarvis-skill",
  "jarvis-macro",
];

// Rich CONTENT this text-only mini panel cannot draw: record cards, data charts,
// and mermaid diagrams. Rather than drop them silently (or, for mermaid, leak the
// raw source), each block becomes a sentinel that renderReply turns into a chip
// linking to the full chat, where the SPA renders it. Private-use chars so the
// sentinel can never collide with real text and survives markdown rendering.
const M0 = "\uE000";
const M1 = "\uE001";
const sentinel = (kind) => `\n\n${M0}${kind}${M1}\n\n`;

export function stripControlBlocks(src) {
  let t = String(src == null ? "" : src);
  for (const name of STRIP_FENCES) {
    t = t.replace(
      new RegExp("```" + name + "[ \\t]*\\n[\\s\\S]*?```", "g"),
      ""
    );
  }
  // Content blocks → "view in full chat" sentinels (kept, not dropped).
  t = t.replace(/```jarvis-cards[ \t]*\n[\s\S]*?```/g, sentinel("cards"));
  t = t.replace(/```jarvis-chart[ \t]*\n[\s\S]*?```/g, sentinel("chart"));
  // Mermaid: xychart-beta is a DATA chart; anything else is a diagram.
  t = t.replace(
    /```mermaid[ \t]*\n[ \t]*xychart-beta[\s\S]*?```/g,
    sentinel("chart")
  );
  t = t.replace(/```mermaid[ \t]*\n[\s\S]*?```/g, sentinel("diagram"));
  return t.replace(/\n{3,}/g, "\n\n").trim();
}

// Per-kind chip: an icon, a label, and a "Open in full chat" affordance. Clicks
// are caught by delegation in Panel.vue (the chip lives inside a v-html bubble,
// so it cannot bind a Vue handler directly) and emit "open-full".
const CHIP = {
  diagram: {
    label: "Diagram",
    icon:
      '<rect x="3" y="3" width="7" height="6" rx="1"></rect>' +
      '<rect x="14" y="15" width="7" height="6" rx="1"></rect>' +
      '<path d="M6.5 9v4a2 2 0 0 0 2 2H14"></path>',
  },
  chart: {
    label: "Chart",
    icon: '<path d="M4 20V10M10 20V4M16 20v-7M2 20h20"></path>',
  },
  cards: {
    label: "Records",
    icon:
      '<rect x="3" y="4" width="18" height="5" rx="1"></rect>' +
      '<rect x="3" y="13" width="18" height="5" rx="1"></rect>',
  },
};

function chip(kind) {
  const c = CHIP[kind] || {
    label: "Content",
    icon: '<circle cx="12" cy="12" r="9"></circle>',
  };
  return (
    '<button type="button" class="jvp-view-chip" data-open-full>' +
    '<svg class="jvp-view-chip-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    c.icon +
    "</svg>" +
    '<span class="jvp-view-chip-tx"><span class="jvp-view-chip-t">' +
    c.label +
    "</span>" +
    '<span class="jvp-view-chip-s">Open in full chat</span></span>' +
    '<svg class="jvp-view-chip-go" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 17 17 7M8 7h9v9"></path></svg>' +
    "</button>"
  );
}

const MERMAID_DIV_RE = /<div class="jv-mermaid">[\s\S]*?<\/div>/g;
// The renderer wraps a lone line in <p class="jv-md-p"> — match any attributes.
const SENT_P_RE = new RegExp(
  "<p[^>]*>\\s*" + M0 + "(\\w+)" + M1 + "\\s*</p>",
  "g"
);
const SENT_RE = new RegExp(M0 + "(\\w+)" + M1, "g");

export function renderReply(src) {
  let html = renderMarkdown(stripControlBlocks(src));
  // Defensive: if any mermaid slipped past stripControlBlocks, renderMarkdown
  // emits an undrawn `.jv-mermaid` div — treat it as a diagram chip too.
  html = html.replace(MERMAID_DIV_RE, chip("diagram"));
  // Sentinels normally come out wrapped in their own <p>; unwrap that first so
  // the chip is a clean block, then catch any inline stragglers.
  html = html.replace(SENT_P_RE, (_m, k) => chip(k));
  html = html.replace(SENT_RE, (_m, k) => chip(k));
  return html;
}
