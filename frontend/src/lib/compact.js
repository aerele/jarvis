// The composer shortcut: "/compact" or "/compact <what to keep>" runs the
// compact action instead of sending a turn. Case-insensitive, optional colon.
export function parseCompactCommand(text) {
	const m = /^\s*\/compact(?:\s*:\s*|\s+|$)(.*)$/i.exec(text || "");
	if (!m) return null;
	return { hint: (m[1] || "").trim() };
}

// One entry per reason: the toast text and its type. "Nothing to compact" outcomes
// are a calm info note, everything else an error.
const COPY = {
	runtime_declined: { text: "Nothing to compact yet", type: "info" },
	runtime_failed: { text: "Could not compact this chat right now, try again later" },
	nothing_to_compact: { text: "Nothing new to compact yet", type: "info" },
	conversation_busy: { text: "A reply is in progress, try again in a moment" },
	already_compacting: { text: "Already compacting this chat" },
	macro_armed: { text: "This chat is running a macro" },
	bad_hint: { text: "The hint cannot start with a slash" },
	directive_refused: { text: "Model and runtime changes are not available in chat." },
	timeout: { text: "Compacting took too long, try again" },
	gateway_unreachable: { text: "Could not reach your assistant, try again" },
};
const FALLBACK = { text: "Could not compact this chat" };

/** Toast text and type ("info" | "error") for a compact outcome reason. */
export function compactOutcome(reason) {
	const e = COPY[reason] || FALLBACK;
	return { text: e.text, type: e.type || "error" };
}
export function compactFailureCopy(reason) {
	return compactOutcome(reason).text;
}
