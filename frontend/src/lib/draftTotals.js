// The edit form's totals row, as a pure function of the draft model (no Vue, no DOM), so
// the desktop panel and the phone sheet compute the same figures.
import { allRowFields } from "./draftApply";

// Auto-derived, read-only totals for the edit panel: sum the line-item qty and
// qty×rate across every child table that has a `qty` column. Returns a
// structured object (not a string) so the UI can render each figure as its own
// read-only field; null when there's nothing to total.
export function draftTotals(p) {
	if (!p) return null;
	let qty = 0,
		amt = 0,
		hasQty = false,
		hasAmt = false;
	for (const t of p.tables) {
		// Any field of the row, not only the grid's columns: a user's own grid columns
		// may leave qty or rate out, and the totals must still add up.
		const q = allRowFields(t).find((c) => c.fieldname === "qty");
		const r = allRowFields(t).find((c) => c.fieldname === "rate");
		if (!q) continue;
		hasQty = true;
		for (const row of t.rows) {
			const n = parseFloat(row.qty) || 0;
			qty += n;
			if (r) {
				hasAmt = true;
				amt += n * (parseFloat(row.rate) || 0);
			}
		}
	}
	if (!hasQty) return null;
	return {
		qty: qty.toLocaleString("en-IN"),
		amount: amt.toLocaleString("en-IN", {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		}),
		hasAmt,
	};
}
