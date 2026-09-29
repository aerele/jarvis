// Three backend producers (chart / dashboard / file canvases) each publish a
// `canvas` push with their OWN partial list of items for the turn, not the
// whole set. ChatView's realtime switch used to assign `cm.canvas = p.items`
// straight through, so a late push (e.g. a slow file render finishing after
// an earlier chart already landed) wiped out whatever an earlier push had
// already added — the reader lost a file card that was on screen a moment
// ago. Merge by `name` instead (the same key the template keys `v-for` on
// and the canvas cache uses, see ChatView.vue's cvOf/ensureCanvas): an
// incoming item sharing a name with an existing one replaces it IN PLACE (a
// re-render of the same file/chart), a new name appends, and an existing
// item absent from this payload is left alone (its own producer just hasn't
// reported again).
//
// Pure function, no Vue — the `case "canvas"` handler in ChatView.vue calls
// it directly (source-fenced in canvasMerge.spec.js).

/**
 * @param {Array<{name?: string}>|undefined|null} existing the row's current m.canvas
 * @param {Array<{name?: string}>|undefined|null} incoming this push's p.items
 * @returns {Array<{name?: string}>} a NEW array (never mutates `existing`)
 */
export function mergeCanvasItems(existing, incoming) {
	const prev = Array.isArray(existing) ? existing : [];
	const items = Array.isArray(incoming) ? incoming : [];
	const merged = prev.slice();
	for (const item of items) {
		// An item with no name can't be matched against what's already there
		// (nothing to key on) — append it rather than silently drop it.
		const at = item && item.name ? merged.findIndex((x) => x && x.name === item.name) : -1;
		if (at === -1) merged.push(item);
		else merged[at] = item;
	}
	return merged;
}
