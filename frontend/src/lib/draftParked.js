// Draft panel / phone card -> gated card (round 2, write-risk guard).
//
// Sensitive configuration (scripts, webhooks, mail, users and permissions,
// sign-in settings) is never saved by the draft panel: apply_action turns it into
// the gated confirmation card and answers `{ok: true, parked: true, note}`.
// Nothing is saved yet, so neither client may show it as done, and the draft it
// came from must not be applied a second time (it would park a second card).

export const PARKED_FALLBACK =
	"This needs its own confirmation card, now waiting in the chat. Nothing is saved until it is confirmed.";

// The note to show when an apply_action response parked a card, else "".
export function parkedNoteOf(r) {
	if (!r || r.ok === false || !r.parked) return "";
	return String(r.note || PARKED_FALLBACK);
}

// A copy of `parked` (Map of draft key -> note) with `key` marked parked.
export function markParked(parked, key, note) {
	const next = new Map(parked || []);
	if (key) next.set(key, note || PARKED_FALLBACK);
	return next;
}

// The parked note for a draft key, or "" when that draft can still be applied.
export function parkedNoteFor(parked, key) {
	return (key && parked && parked.get(key)) || "";
}

// The note for a draft whose gated card is still waiting, read from the pending
// cards themselves: the server stamps the draft's message on the preview AND on
// the card (a reload seeds the card from its message row, which carries only the
// card), so the waiting state survives a reload. "" when there is none.
export function parkedByCard(pending, key) {
	if (!key || !Array.isArray(pending)) return "";
	const hit = pending.some((pa) => {
		const preview = (pa && pa.preview) || {};
		return preview.from_draft === key || (preview.card && preview.card.from_draft === key);
	});
	return hit ? PARKED_FALLBACK : "";
}
