/**
 * Removes the agent's structured blocks from a reply before the remainder is
 * rendered as markdown.
 *
 * A reply is not only prose. The agent also emits fenced blocks that OTHER
 * surfaces render as real UI: the draft summary card (```jarvis-action), the
 * confirmation card (```confirm), the clarifying question (```jarvis-ask), the
 * suggestion cards, the skill/macro markers and the charts. Every one of them
 * must be cut out of the markdown body, or the same payload would be drawn
 * twice: once as its card, once as a raw code block underneath it.
 *
 * The subtle part is STREAMING. The closing fence is the last thing to arrive,
 * so for as long as a block is still being written there is nothing for a
 * `...```/` pattern to match, and marked treats an unterminated fence as a code
 * block running to the end of the text. The reader watches a wall of raw JSON
 * type itself out and then disappear when the block finally closes. That flash
 * is what makes a structured answer look sloppy next to a plain prose one.
 *
 * So a block is held back from the first character of its opener, not from its
 * closing fence, while `streaming` is true. Once the turn settles the rule is
 * dropped: a block the agent never closed is then shown as-is rather than
 * silently swallowed, because at that point the raw text is the only honest
 * thing left to render.
 *
 * Pure and dependency-free so the streaming behaviour is unit-testable without
 * mounting the view.
 */

/** Fenced blocks that other surfaces own. Order is irrelevant; all are cut. */
export const BLOCK_TAGS = [
	"jarvis-action",
	"confirm",
	"jarvis-ask",
	"jarvis-cards",
	"jarvis-skill",
	"jarvis-macro",
	"jarvis-chart",
];

// Completed blocks. `mermaid` is special: only the xychart-beta variant is a
// jarvis chart, every other mermaid block is a diagram the body should keep.
const COMPLETE = [
	...BLOCK_TAGS.map((tag) => new RegExp("```" + tag + "[ \\t]*\\n[\\s\\S]*?```", "g")),
	/```mermaid[ \t]*\n[ \t]*xychart-beta[\s\S]*?```/g,
];

// An opener with no closing fence yet, running to the end of the text. Only the
// trailing one can be unterminated: any earlier block already closed and was cut
// by COMPLETE above. `mermaid` is held whole here (not just xychart-beta) because
// half a diagram source is noise on screen either way, and the completed pass
// re-renders it correctly one frame later.
const OPEN = new RegExp(
	"\\n?```(?:" + [...BLOCK_TAGS, "mermaid"].join("|") + ")[ \\t]*(?:\\n[\\s\\S]*)?$"
);

// A fence whose language tag is still being typed ("```jarv"). No newline has
// arrived, so this cannot be a plain code block yet, and rendering it produces an
// empty grey slab that pops in and out. A real code block ("```js\n...") has its
// newline and streams normally, which is what a reader wants to watch.
const OPEN_PARTIAL = /\n?```[a-zA-Z-]*$/;

/**
 * @param {string} text raw assistant content
 * @param {boolean} streaming true while the reply is still being written
 * @returns {string} markdown body with structured blocks removed
 */
export function stripBlocks(text, streaming = false) {
	let out = text || "";
	for (const re of COMPLETE) out = out.replace(re, "");
	if (streaming) out = out.replace(OPEN, "").replace(OPEN_PARTIAL, "");
	return out.replace(/\n{3,}/g, "\n\n").trim();
}
