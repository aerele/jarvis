// Human copy for a rejected send ({ ok: false, reason }). The server sends
// machine codes for the gates it owns and plain sentences for one-off guards;
// a code the SPA does not know must never reach a toast. The copy is plain text:
// a caller that renders HTML (a frappe-ui toast) escapes the whole message.
const FALLBACK = "Couldn't send your message.";

// A usage_limit envelope names its window (limit_period) only when the
// aggregate day/week/month cap blocked; the all-time and per-model caps stay
// period-neutral, so the copy can never promise a reset that will not come.
const WINDOW_COPY = {
	Daily: "You've reached today's usage limit. It resets at midnight.",
	Weekly: "You've reached this week's usage limit. It resets on Sunday.",
	Monthly: "You've reached this month's usage limit. It resets on the 1st.",
};

export function sendRejectionCopy(reason, agentName, envelope, fallback = FALLBACK) {
	const window = WINDOW_COPY[envelope && envelope.limit_period];
	const known = {
		maintenance: {
			message: `${agentName} is temporarily unavailable for maintenance. Try again shortly.`,
			type: "warning",
		},
		usage_limit: {
			message:
				window ||
				`You've reached your usage limit. Ask your ${agentName} admin to raise it.`,
			type: "error",
		},
		llm_not_configured: {
			message: "No AI model is connected. Connect one in Settings → AI models.",
			type: "warning",
		},
		workspace_resetting: {
			message: `${agentName} is being reset. Chat will be back in a few minutes.`,
			type: "warning",
		},
		release_update_required: {
			message: `${agentName} is being updated. Reload the page to continue.`,
			type: "error",
		},
		queue_full: {
			message: `${agentName} is busy right now. Wait a moment, then send it again.`,
			type: "warning",
		},
		already_claimed: {
			message:
				"That message was already sent. Check the conversation before sending it again.",
			type: "warning",
		},
		subscription_suspended: {
			message: "Your subscription has lapsed. Renew it to keep chatting.",
			type: "error",
		},
	};
	if (reason && known[reason]) return known[reason];
	const sentence = typeof reason === "string" && reason.includes(" ") ? reason : "";
	return { message: sentence || fallback, type: "error" };
}

// A refused send/retry envelope ({ ok: false, reason, message }) to user copy. A known code
// wins; otherwise the server's own sentence (message, then reason); a bare machine code
// ("queue_full", "macro_armed") never shows, the plain fallback does.
export function rejectionCopy(r, agentName, fallback = FALLBACK) {
	const reason = r && r.reason;
	if (reason && typeof reason === "string") {
		const known = sendRejectionCopy(reason, agentName, r, "");
		if (known.message) return known;
	}
	const message = r && typeof r.message === "string" && r.message.includes(" ") ? r.message : "";
	return { message: message || fallback, type: "error" };
}
