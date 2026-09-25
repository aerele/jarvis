// Typed yes/no on chat action cards (decisions 6, 13, 14).
//
// A bare or sweep phrase ("go ahead", "no", "confirm all") binds only the cards
// parked since the user's latest message; the server marks those `recent`. An
// older card is labelled "Earlier" and only a number ("confirm 1", "discard 1")
// binds it, so the stack never advertises a bare phrase for it.
//
// The SPA and PWA are separate builds, so this file mirrors frontend/src/lib.

export function isRecentCard(card) {
	return !!card && card.recent !== false;
}

// The composer hint shown under the stack. The selective example numbers track
// the real count so a copied example never names a card that is not there.
export function typedApprovalHint(cards) {
	const n = (cards || []).length;
	if (!n) return "";
	const allRecent = cards.every(isRecentCard);
	if (n > 1)
		return allRecent
			? `or type "confirm all", or "confirm 1 and ${n}"`
			: `or type "confirm 1 and ${n}"`;
	return allRecent ? 'or type "go ahead"' : 'or type "confirm 1"';
}

// The tokens a typed "no" discarded (send_message's `typed_rejection`).
export function discardedTokens(res) {
	const d = res && res.typed_rejection && res.typed_rejection.discarded;
	return new Set((Array.isArray(d) ? d : []).map((x) => x && x.token).filter(Boolean));
}

// An accepted send means the user just spoke: every card still on screen for that
// conversation is now older than their latest message.
export function markCardsEarlier(cards, conversation) {
	for (const c of cards || []) if (c && c.conversation === conversation) c.recent = false;
}
