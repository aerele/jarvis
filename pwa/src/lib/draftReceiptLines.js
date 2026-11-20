// The receipt lines of the decided server draft cards in one assistant item's tool rows. Pure:
// the words and tones come from the shared draftReceipt.js (the desktop draws the same ones); this
// only decides which rows draw a line and what the line carries for the phone.
//
// A line is drawn ONLY for a row the server stamped as a decided draft card (`draft_receipt`
// === true, isDraftReceiptRow). Every other tool row stays invisible on the phone, exactly as
// before: with the site flag off no row is stamped, so a thread draws no line at all. A decided row
// the State Table gives no line (S9 replaced by a newer draft, and outcomes with no draft wording)
// is skipped as well.
import {
	CANCELLED_TEXT,
	draftReceipt,
	isDraftReceiptRow,
} from "../../../frontend/src/lib/draftReceipt.js";
import { docUrl } from "../../../frontend/src/lib/actionSummary.js";

export const OPEN_LABEL = "Open";
export const SUBMIT_LABEL = "Submit";

// The key a next step is told apart by: the record it acts on ("Doctype|name", the shape
// chatCardActions.nextStepActedKeys uses).
export const stepKey = (doctype, name) => `${doctype}|${name}`;

// line = { key, stepKey, text, tone: "ok"|"warn"|"error", muted, reason,
//          open: {href, label} | null, submit: {kind, doctype, name, key, label} | null }
// `stepKey` is where the line's Submit state (busy, note) is kept, whether or not Submit is offered.
// `hiddenSteps`: the keys whose Submit is not offered any more (already done in the thread, or
// refused for good).
export function receiptLines(tools, { hiddenSteps } = {}) {
	const hidden = hiddenSteps || new Set();
	const lines = [];
	for (const row of Array.isArray(tools) ? tools : []) {
		if (!isDraftReceiptRow(row)) continue;
		const r = draftReceipt(row);
		if (!r) continue;
		// the record the line is about ("" when the receipt names none)
		const key = r.open ? stepKey(r.open.doctype, r.open.name) : "";
		lines.push({
			key: row.name || row.tool_call_id || `${lines.length}`,
			stepKey: key,
			text: r.text,
			tone: r.tone,
			muted: r.text === CANCELLED_TEXT,
			reason: r.reason || "",
			open: r.open ? { href: docUrl(r.open.doctype, r.open.name), label: OPEN_LABEL } : null,
			submit:
				r.submit && r.open && !hidden.has(key)
					? {
							kind: "submit",
							doctype: r.open.doctype,
							name: r.open.name,
							key,
							label: SUBMIT_LABEL,
					  }
					: null,
		});
	}
	return lines;
}
