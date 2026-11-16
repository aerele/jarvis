// The chat's view of a pending confirmation that is a SERVER draft card (the tool path): is
// it one, and what the shared DraftCard is told about it. Pure, relative imports only.

const isObject = (v) => !!v && typeof v === "object" && !Array.isArray(v);

// The card of a pending item, from either of the two paths the server uses: a pending-list
// item or realtime push (`pa.preview.card`) or a tool row of `get_conversation`
// (`pa.pending_card`). null when it has none.
export function cardOfItem(pa) {
	if (!isObject(pa)) return null;
	if (isObject(pa.preview) && isObject(pa.preview.card)) return pa.preview.card;
	return isObject(pa.pending_card) ? pa.pending_card : null;
}

// True when the item's card has a `draft` key and no `risk`. An unreadable draft counts: the
// card draws it with only Cancel. A sensitive card (`risk`) and every other gated card keep
// the existing confirmation card.
export function isToolDraftItem(pa) {
	const card = cardOfItem(pa);
	return !!card && Object.prototype.hasOwnProperty.call(card, "draft") && !card.risk;
}

// The card view's busy flag: Cancel stays "cancel"; Create and a Save from the edit panel are
// both "create" (every button disabled, the spinner on the primary one). An item marked busy
// with no kind is still busy but nobody knows which button was pressed: "busy" locks every
// button and puts the spinner on none (draftCardView spins only "create" and "cancel").
// `state` is where the busy flag lives: the item itself (the desktop keeps `busy` / `busyKind` on
// the pending item) or whatever the host reads it from (the phone keeps them by token, because
// its pending list replaces the item objects on every read).
function busyOf(state) {
	const kind = state.busyKind || "";
	if (kind === "cancel") return "cancel";
	if (kind === "create" || kind === "save") return "create";
	return state.busy ? "busy" : "";
}

// The `ctx` of draftCardView for one pending item. `building`: the edit form or the preview of
// THIS item is being built (a click that has not opened anything yet): every button is locked,
// none spins, so a second click cannot start a second build. `surface` is "desktop" (default) or
// "phone" (the card shows fewer table rows); any other value is the desktop. `busyState`
// ({busy, busyKind}) replaces the item's own flags when the host keeps them elsewhere. `readOnly`
// draws Preview and Edit disabled (a host that cannot open them yet), `"preview"` only Preview;
// it is in the ctx only when true or "preview", so every other host's ctx is exactly what it was.
export function toolDraftCtx({
	pa,
	editingToken,
	streaming,
	building,
	surface = "desktop",
	busyState,
	readOnly = false,
}) {
	const item = isObject(pa) ? pa : {};
	return {
		earlier: item.recent === false,
		busy: busyOf(busyState || item) || (building ? "busy" : ""),
		editing: !!editingToken && editingToken === item.token,
		streaming: !!streaming,
		surface: surface === "phone" ? "phone" : "desktop",
		...(readOnly === true || readOnly === "preview" ? { readOnly } : {}),
	};
}
