// The desktop chat's decisions about a server draft card (a tool draft item), kept pure so
// they run under vitest as data tables. ChatView.vue only wires them: no Vue, no DOM, no
// API. Relative imports only.
import { isToolDraftItem, toolDraftCtx } from "./toolDraft.js";

// One card always: while the pending list holds a tool draft item, no typed draft is live.
// `items` is the list the screen draws (visiblePendingActions), so another chat's card never
// counts. A sensitive card is not a tool draft item and does not count.
export function hasParkedDraft(items) {
	return Array.isArray(items) && items.some(isToolDraftItem);
}

// What DraftCard is told about an item at this commit. Preview and Edit are drawn but
// disabled (`readOnly`) until the edit panel lands; `editing` is therefore always false.
export function toolDraftHostCtx({ pa, streaming }) {
	return { ...toolDraftCtx({ pa, editingToken: "", streaming }), readOnly: true };
}

// Run one card action (Create -> the confirm handler, Cancel -> the discard handler) with
// the item's `busyKind` set, so the card shows the working state with the spinner on the
// pressed button, and a second press on the same card does nothing while one is in flight.
// The kind is cleared in a `finally`: a refused, failed or thrown action leaves the card
// usable again; a successful one has already taken the item off the list. Resolves to true
// when `run` was started. The handlers keep their own `busy` / `inflightTokens` guards.
export async function withBusyKind(pa, kind, run) {
	if (!pa || pa.busy || pa.busyKind) return false;
	pa.busyKind = kind;
	try {
		await run(pa);
		return true;
	} finally {
		pa.busyKind = "";
	}
}

// Shown inside a typed edit panel that a server draft card has overtaken.
export const DRAFT_STALE_TEXT =
	"This draft changed while you were editing. Your edits were not saved.";

// The typed edit panel's "stale" flag. A panel is stale once a tool draft item is parked
// while it is open, and stays stale until it closes (the card that retired the typed draft
// may itself be decided later, but the panel's save has no draft to act on by then). A panel
// that is a different object from the last one starts from the current parked state.
// This is the TYPED panel's rule: a panel that edits a server draft card itself (commit 3) is
// not stale because a draft is parked, so its host must not feed this reducer.
//   state  = the flag now
//   change = { panelOpen, panelChanged, parked }
export function nextPanelStale(state, { panelOpen, panelChanged, parked }) {
	if (!panelOpen) return false;
	return (panelChanged ? false : !!state) || !!parked;
}
