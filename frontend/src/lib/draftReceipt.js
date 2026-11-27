// What the receipt of a DECIDED server draft card says (State Table S6, S7, S8, S10). Pure,
// relative imports only: the phone imports this file too.
//
// A decided card is a tool row of `create_doc` / `update_doc` with a non-empty `action_outcome`.
// The row no longer carries the card (the server clears `pending_card` when it decides it), so the
// server stamps the decided row that WAS a draft card: `draft_receipt: true`, and for a record that
// changed or went missing after the draft `draft_receipt_reason: "stale" | "target_missing"`
// (jarvis/chat/draft_card.py decided_mark; only with the site flag on). `isDraftReceiptRow` reads
// that stamp; every other row keeps the receipt it has always had (actionSummary.receiptView).
//
// draftReceipt(row) -> { text, tone: "ok" | "warn" | "error", open?: {doctype, name}, submit?: true,
//                        reason? } | null
// null for a row that is not a draft receipt and for the outcomes the State Table gives no
// receipt of its own (replaced by a newer draft, stopped, expired, unknown): the host keeps
// the row's existing chip for those.

const DRAFT_TOOLS = new Set(["create_doc", "update_doc"]);

export const CHANGED_TEXT = "Not saved: this record changed after the draft was made";
export const CANCELLED_TEXT = "Draft cancelled";

// The stamp's reasons for a record that changed or was deleted after the draft was parked. A stale
// failure stores no message and no code of its own (`{ok: false, error: {reference}}`), so this
// stamp is the only place the server says why.
const CHANGED_REASONS = new Set(["stale", "target_missing"]);

const isObject = (v) => !!v && typeof v === "object" && !Array.isArray(v);
function parse(v) {
	if (isObject(v)) return v;
	if (typeof v !== "string") return {};
	try {
		const p = JSON.parse(v);
		return isObject(p) ? p : {};
	} catch {
		return {};
	}
}
const text = (v) => (typeof v === "string" ? v.trim() : "");

export function draftReceipt(row) {
	if (!isObject(row) || row.role !== "tool" || !row.action_outcome) return null;
	const tool = String(row.tool_name || "").replace(/^jarvis__/, "");
	if (!DRAFT_TOOLS.has(tool)) return null;
	const create = tool === "create_doc";
	const result = parse(row.tool_result);
	const args = parse(row.tool_args);
	const data = isObject(result.data) ? result.data : {};
	const doctype = text(data.doctype) || text(args.doctype) || "record";
	const name = text(data.name) || text(args.name);
	const err = isObject(result.error) ? result.error : {};
	switch (row.action_outcome) {
		case "confirmed": {
			const out = {
				text: `${create ? "Created" : "Updated"} ${doctype}${name ? ` ${name}` : ""}`,
				tone: "ok",
			};
			if (name) out.open = { doctype, name };
			const next = isObject(data.suggested_next) ? data.suggested_next : null;
			if (create && next && next.kind === "submit") out.submit = true;
			return out;
		}
		case "discarded":
			return { text: CANCELLED_TEXT, tone: "ok" };
		case "partial":
			return { text: "Partly applied", tone: "warn", reason: text(err.message) };
		case "failed":
			if (CHANGED_REASONS.has(row.draft_receipt_reason))
				return { text: CHANGED_TEXT, tone: "error" };
			return {
				text: create ? "Not created" : "Not saved",
				tone: "error",
				reason: text(err.message),
			};
		default:
			return null;
	}
}

// Was this decided row a server draft card? The server says so: `draft_receipt === true` (never
// guessed from `draft_card`, which is false on every decided row, a sensitive card's included). A
// row the site did not stamp (flag off, a sensitive card, any other tool) keeps its old receipt.
export function isDraftReceiptRow(row) {
	if (!isObject(row) || row.role !== "tool" || !row.action_outcome) return false;
	if (!DRAFT_TOOLS.has(String(row.tool_name || "").replace(/^jarvis__/, ""))) return false;
	return row.draft_receipt === true;
}
