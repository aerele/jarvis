// What the receipt of a DECIDED server draft card says (State Table S6, S7, S8, S10). Pure,
// relative imports only: the phone imports this file too.
//
// A decided card is a tool row of `create_doc` / `update_doc` with a non-empty `action_outcome`.
// The row no longer carries the card (the server clears `pending_card` when it decides it), so
// which decided rows were draft cards is told by `isDraftReceiptRow`; every other row keeps the
// receipt it has always had (actionSummary.receiptView).
//
// draftReceipt(row) -> { text, tone: "ok" | "warn" | "error", open?: {doctype, name}, submit?: true,
//                        reason? } | null
// null for a row that is not a draft receipt and for the outcomes the State Table gives no
// receipt of its own (replaced by a newer draft, stopped, expired, unknown): the host keeps
// the row's existing chip for those.

const DRAFT_TOOLS = new Set(["create_doc", "update_doc"]);

export const CHANGED_TEXT = "Not saved: this record changed after the draft was made";
export const CANCELLED_TEXT = "Draft cancelled";

// The reason codes of a record that changed or was deleted after the draft was parked (the
// server's `stale` and `target_missing`), and the exact sentences the server writes for them.
const CHANGED_CODES = new Set(["stale", "target_missing"]);
const CHANGED_MESSAGES = new Set([
	"The record changed after this action was proposed.",
	"The record this action targets no longer exists.",
	"This record changed after the draft was made. Ask me to draft the update again.",
	"This record no longer exists, so the update was not saved.",
]);

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

function changedRecord(result) {
	const err = isObject(result.error) ? result.error : {};
	const codes = [result.reason_code, err.reason_code, err.code];
	return codes.some((c) => CHANGED_CODES.has(c)) || CHANGED_MESSAGES.has(text(err.message));
}

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
			if (changedRecord(result)) return { text: CHANGED_TEXT, tone: "error" };
			return {
				text: create ? "Not created" : "Not saved",
				tone: "error",
				reason: text(err.message),
			};
		default:
			return null;
	}
}

// Was this decided row a server draft card? The decided row carries no card and no mark of its
// own, so two things say so: the site stamps `draft_card` (a boolean) on every tool row of a
// chat when draft cards are on (true only while Pending, so false here), and the chat remembers
// the tokens of the draft cards it showed (`seenTokens`, a Set: for a row that arrives live
// without the stamp). With the site flag off a row has neither, and keeps its old receipt.
export function isDraftReceiptRow(row, seenTokens) {
	if (!isObject(row) || row.role !== "tool" || !row.action_outcome) return false;
	if (!DRAFT_TOOLS.has(String(row.tool_name || "").replace(/^jarvis__/, ""))) return false;
	if (row.draft_card === false) return true;
	return !!row.tool_call_id && !!seenTokens && seenTokens.has(row.tool_call_id);
}
