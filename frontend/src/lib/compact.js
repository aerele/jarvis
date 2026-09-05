// The composer shortcut: "/compact" or "/compact <what to keep>" runs the
// compact action instead of sending a turn. Case-insensitive, optional colon.
export function parseCompactCommand(text) {
	const m = /^\s*\/compact(?:\s*:\s*|\s+|$)(.*)$/i.exec(text || "");
	if (!m) return null;
	return { hint: (m[1] || "").trim() };
}

const COPY = {
	runtime_declined: "Nothing to compact yet",
	nothing_to_compact: "Nothing to compact yet",
	conversation_busy: "A reply is in progress, try again in a moment",
	already_compacting: "Already compacting this chat",
	macro_armed: "This chat is running a macro",
	bad_hint: "The hint cannot start with a slash",
	timeout: "Compacting took too long, try again",
	gateway_unreachable: "Could not reach your assistant, try again",
};
export function compactFailureCopy(reason) {
	return COPY[reason] || "Could not compact this chat";
}
