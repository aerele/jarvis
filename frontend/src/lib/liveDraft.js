/**
 * The one rule for which draft card is live. The server (Python), the phone app and
 * the desktop app all decide it the same way, proven by liveDraftCases.json.
 *
 * Rows are the conversation's messages in order; hidden and streaming rows are
 * skipped entirely. A draft row is an assistant row whose FIRST closed jarvis-action
 * fence is broken, or parses to a non-email action whose verb is missing, create or
 * update. The live draft is the last draft row, unless a later tool row is a decided
 * write (a non-empty action_outcome, or a completed create_doc / update_doc call),
 * more than MAX_USER_ROWS_AFTER_DRAFT user rows follow it, or more than
 * MAX_ROWS_AFTER_DRAFT rows of any role follow it.
 *
 * Pure, relative imports only: the phone app's node tests load it.
 */
import { actionBlockState, closedActionBody, normaliseAction, GATED_VERBS } from "./chatAction.js";

export const MAX_USER_ROWS_AFTER_DRAFT = 5;
// Both apps load only the newest page of a chat (80 rows of any role, tool rows
// included: historyPaging.js). A draft further back than this is never live, so its
// card can never be missing from a loaded page while the server calls it live.
export const MAX_ROWS_AFTER_DRAFT = 60;

const skipped = (row) => !row || !!row.hidden || !!row.streaming;

/**
 * @param {{role?: string, content?: string}} row
 * @returns {boolean}
 */
export function isDraftRow(row) {
	if (!row || row.role !== "assistant") return false;
	const state = actionBlockState(row.content);
	if (state === "none") return false;
	if (state === "bad") return true;
	const action = normaliseAction(JSON.parse(closedActionBody(row.content).trim()));
	if (!action || action.kind === "email") return false;
	return !action.verb || (!GATED_VERBS.has(action.verb) && isEditVerb(action.verb));
}

const isEditVerb = (verb) => verb === "create" || verb === "update";

const WRITE_TOOLS = new Set(["create_doc", "update_doc"]);

/**
 * A decided write after a draft retires it: a receipt (a non-empty action_outcome), or
 * a create / update call that completed. Receipts written before 2026-07-17 carry no
 * outcome, so a draft saved on such a build is retired by its completed call.
 * @param {object} row
 * @returns {boolean}
 */
export function retiresDraft(row) {
	if (!row || row.role !== "tool") return false;
	if (typeof row.action_outcome === "string" && row.action_outcome.trim() !== "") return true;
	return WRITE_TOOLS.has(row.tool_name) && row.tool_status === "completed";
}

/**
 * @param {Array<object>|null|undefined} rows messages in order
 * @returns {object|null} the live draft row, or null
 */
export function liveDraftRow(rows) {
	if (!Array.isArray(rows)) return null;
	let userRows = 0;
	let laterRows = 0;
	for (let i = rows.length - 1; i >= 0; i--) {
		const row = rows[i];
		if (skipped(row)) continue;
		if (isDraftRow(row)) return row;
		if (retiresDraft(row)) return null;
		if (row.role === "user") userRows++;
		laterRows++;
		// Every row before this one has too many rows after it to be the live draft.
		if (userRows > MAX_USER_ROWS_AFTER_DRAFT || laterRows > MAX_ROWS_AFTER_DRAFT) return null;
	}
	return null;
}
