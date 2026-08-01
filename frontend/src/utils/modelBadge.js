// Per-reply model attribution (jarvis#560): extracted as a PLAIN, importable,
// unit-tested module because it decides whether the audit signal is VISIBLE at
// all, and a rule that hides too eagerly fails silently.
//
// Background. Jarvis lets a user change the model mid-conversation, and an
// UNPINNED conversation keeps openclaw's failover chain live, so a reply can
// come from a model the user never chose. The server now stamps `model` and
// `provider` on the assistant row at finalize, taken from the model the gateway
// says actually served the turn. Without a rendering rule that surfaces the
// interesting cases, that record exists only in the database.
//
// The rule: show the model on a reply ONLY when it differs from what the chat is
// set to RIGHT NOW. A steady thread therefore stays completely quiet (the header
// pill already answers "which model"), and the two cases worth seeing stand out:
//
//   * a reply written BEFORE a mid-thread switch: the user changed model, and
//     the older replies visibly did not come from the new one;
//   * a turn the pool failed over to a different model: the pill still says
//     what the user picked, and this is the only place the substitution shows.
//
// "Right now" has two definitions because the header pill has two modes. A PIN
// names a model outright, so that is the answer. "Auto" names none, so the best
// available statement of what Auto currently resolves to is the newest reply in
// the thread that carries an attribution. Under Auto, that makes the badge a
// change-marker: identical consecutive models are silent, and the turn where the
// model changed is the one that speaks.

/**
 * The model the conversation is running on right now.
 *
 * @param {Array<{role?: string, model?: string}>} messages transcript, in order
 * @param {string} modelOverride the conversation's pin ("" means Auto)
 * @returns {string} a model id, or "" when nothing in the thread names one
 */
export function currentThreadModel(messages, modelOverride) {
	if (modelOverride) return modelOverride;
	const list = Array.isArray(messages) ? messages : [];
	for (let i = list.length - 1; i >= 0; i--) {
		const m = list[i];
		if (m && m.role === "assistant" && m.model) return m.model;
	}
	return "";
}

/**
 * The model id to show on one reply, or "" to show nothing.
 *
 * Only assistant rows are ever attributed: the field is meaningless on a user or
 * tool row, and the server refuses to stamp one, so a value there would mean the
 * transcript disagrees with the database.
 *
 * @param {{role?: string, model?: string}} message
 * @param {string} currentModel result of currentThreadModel()
 * @returns {string}
 */
export function modelBadgeFor(message, currentModel) {
	if (!message || message.role !== "assistant" || !message.model) return "";
	return message.model === currentModel ? "" : message.model;
}

/**
 * Tooltip for a shown badge. Names the provider too when there is one: two
 * providers can serve the same model id, so the pair is the real identity for an
 * auditor reconstructing who wrote what.
 *
 * @param {{model?: string, provider?: string}} message
 * @returns {string}
 */
export function modelBadgeTitleFor(message) {
	if (!message || !message.model) return "";
	const via = message.provider ? ` via ${message.provider}` : "";
	return `This reply was written by ${message.model}${via}, not the model this chat is set to now`;
}
