// Formats the tail of a chat transcript as plain text for the "copy this chat
// into your ticket?" prompt (ChatView's openSupport). Pure (no Vue, no store)
// so it is unit-testable on its own, the same reasoning as lib/supportBody.js's
// compose-side helpers.
export const SUPPORT_COPY_COUNT = 4;
export const SUPPORT_COPY_CHARS = 400;

// `messages`: chat history entries ({role, content}); `agentName`: how the
// assistant's turns are labelled. Only user/assistant turns with non-blank
// content count as "visible" - tool calls, empty placeholders, etc. are noise
// support staff reading the ticket don't need.
export function formatRecentMessagesForSupport(
	messages,
	agentName,
	count = SUPPORT_COPY_COUNT,
	chars = SUPPORT_COPY_CHARS
) {
	const visible = (messages || []).filter(
		(m) => m && (m.role === "user" || m.role === "assistant") && (m.content || "").trim()
	);
	return visible.slice(-count).map((m) => {
		const who = m.role === "user" ? "You" : agentName;
		const text = m.content.trim();
		// Code-point-aware clip: text.slice() indexes UTF-16 code units, which
		// can cut an astral character (e.g. an emoji) in half and leave an
		// unpaired surrogate in both the preview and, if copied, the ticket
		// body support staff read. The spread operator iterates by code point.
		const codePoints = [...text];
		const clipped =
			codePoints.length > chars ? codePoints.slice(0, chars).join("") + "…" : text;
		return `${who}: ${clipped}`;
	});
}
