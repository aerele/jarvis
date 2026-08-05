// Pure sidebar nav-order logic, split out of Sidebar.vue so the reconcile + the
// drag-move rules are unit-testable without mounting the component (the "a stale
// label can never hide a nav item" guarantee and the drop-to-end / drop-into-an-
// emptied-group cases were prose before). Def objects are passed through opaque;
// only their `label` is read.

// Apply a saved {top, more} order across all defs. Items may move between the
// two groups. Unknown labels are dropped; any def the saved order didn't place
// is appended to its DEFAULT group, so a code change (a new nav item, a renamed
// label) can never hide or dead-link an entry. Returns fresh {top, more} arrays.
export function reconcileOrder(saved, topDefs, moreDefs) {
	const byLabel = new Map([...topDefs, ...moreDefs].map((d) => [d.label, d]));
	const used = new Set();
	const resolve = (labels) => {
		const out = [];
		for (const lbl of Array.isArray(labels) ? labels : []) {
			const d = byLabel.get(lbl);
			if (d && !used.has(lbl)) {
				out.push(d);
				used.add(lbl);
			}
		}
		return out;
	};
	const top = resolve(saved && saved.top);
	const more = resolve(saved && saved.more);
	const appendUnplaced = (defs, into) => {
		for (const d of defs)
			if (!used.has(d.label)) {
				into.push(d);
				used.add(d.label);
			}
	};
	appendUnplaced(topDefs, top);
	appendUnplaced(moreDefs, more);
	return { top, more };
}

// Move an item between (or within) the two ordered groups. `toIndex` is the
// insert position in the TARGET group; pass the target group's length to append.
// Appending is how an item reaches the LAST slot or an emptied group — positions
// no item-drop-target can express, since items insert BEFORE themselves. Returns
// fresh {top, more} arrays; the inputs are never mutated. A no-op (bad index)
// returns the original arrays unchanged.
export function moveOrderItem(top, more, fromGroup, fromIndex, toGroup, toIndex) {
	const from = (fromGroup === "top" ? top : more).slice();
	const [moved] = from.splice(fromIndex, 1);
	if (!moved) return { top, more };
	if (fromGroup === toGroup) {
		// `from` already lost the moved item, so any target past it shifts left one.
		const to = fromIndex < toIndex ? toIndex - 1 : toIndex;
		from.splice(to, 0, moved);
		return fromGroup === "top" ? { top: from, more } : { top, more: from };
	}
	const to = (toGroup === "top" ? top : more).slice();
	to.splice(toIndex, 0, moved);
	return fromGroup === "top" ? { top: from, more: to } : { top: to, more: from };
}
