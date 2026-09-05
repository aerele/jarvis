// Human copy for a rejected send ({ ok: false, reason }). The server sends
// machine codes for the gates it owns and plain sentences for one-off guards;
// a code the SPA does not know must never reach a toast.
const FALLBACK = "Couldn't send your message.";

export function sendRejectionCopy(reason, agentName) {
	const known = {
		usage_limit: {
			message: `You've reached your usage limit. Ask your ${agentName} admin to raise it.`,
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
		subscription_suspended: {
			message: "Your subscription has lapsed. Renew it to keep chatting.",
			type: "error",
		},
	};
	if (reason && known[reason]) return known[reason];
	const sentence = typeof reason === "string" && reason.includes(" ") ? reason : "";
	return { message: sentence || FALLBACK, type: "error" };
}
