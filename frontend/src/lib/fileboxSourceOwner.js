// The owner-only conversation list is the client-side source of truth for
// displaying the source-selection control. The server repeats this check.
export function canSelectFileBoxSource(conversation, ownedConversations) {
	return !!(
		conversation &&
		Array.isArray(ownedConversations) &&
		ownedConversations.some((row) => row?.name === conversation)
	);
}
