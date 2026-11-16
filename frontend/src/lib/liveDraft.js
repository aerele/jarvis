/**
 * The one rule for which draft card is live. The server (Python), the phone app and
 * the desktop app all decide it the same way, proven by liveDraftCases.json.
 *
 * Rows are the conversation's messages in order; hidden and streaming rows are
 * skipped entirely. A draft row is an assistant row whose FIRST closed jarvis-action
 * fence is broken, or parses to a non-email action whose verb is missing, create or
 * update. The live draft is the last draft row, unless a later tool row is a decided
 * write (a non-empty action_outcome, or an old receipt: a completed create_doc /
 * update_doc row with no tool_call_id), a later tool row that is a parked tool draft
 * (draft_card true: the agent's own create_doc / update_doc card, still Pending), more than
 * MAX_USER_ROWS_AFTER_DRAFT user rows
 * follow it, or more than MAX_ROWS_AFTER_DRAFT rows of any role follow it. The tool rows
 * DIRECTLY after the draft row (up to the first later row that is not a tool row) are
 * the draft's own turn: the reply row takes its seq when the turn starts, so they do not
 * count toward MAX_ROWS_AFTER_DRAFT, though a decided write among them still retires it.
 *
 * Pure, relative imports only: the phone app's node tests load it.
 */
import { actionBlockState, closedActionBody, normaliseAction, GATED_VERBS } from "./chatAction.js";

export const MAX_USER_ROWS_AFTER_DRAFT = 5;
// Both apps load only the newest page of a chat (80 rows of any role, tool rows
// included, widened back to the start of its oldest turn: historyPaging.js). A draft
// with more later rows than this (its own turn's tool rows aside) is never live, so its
// card is not missing from a loaded page while the server calls it live.
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
 * A parked tool draft (draft_card true) or a decided write after a typed draft retires it:
 * the tool card is then the one card, and once it is decided its row has an action_outcome
 * and no draft_card, so the old typed draft never comes back. A decided write is a receipt (a non-empty action_outcome), or an
 * old receipt. A draft saved before 2026-07-17 left a completed create_doc / update_doc
 * row with no outcome AND no tool_call_id. An agent's tool call always carries its call
 * id, and a call that only staged a gated card is still "completed": it never retires a
 * draft this way (only through an outcome, once the card is decided).
 * @param {object} row
 * @returns {boolean}
 */
export function retiresDraft(row) {
	if (!row || row.role !== "tool") return false;
	if (row.draft_card === true) return true;
	if (typeof row.action_outcome === "string" && row.action_outcome.trim() !== "") return true;
	return WRITE_TOOLS.has(row.tool_name) && row.tool_status === "completed" && !row.tool_call_id;
}

/**
 * An assistant row with an action block of its own that is not a draft: an email, a
 * gated verb, an unknown verb. As the last message, its card is the one at the tail and
 * a kept draft hides behind it (the server's live_draft.tail_shows_its_own_card).
 * @param {{role?: string, content?: string}} row
 * @returns {boolean}
 */
export function isOwnActionRow(row) {
	return (
		!!row &&
		row.role === "assistant" &&
		actionBlockState(row.content) === "ok" &&
		!isDraftRow(row)
	);
}

/**
 * @param {Array<object>|null|undefined} rows messages in order
 * @returns {object|null} the live draft row, or null
 */
export function liveDraftRow(rows) {
	if (!Array.isArray(rows)) return null;
	let userRows = 0;
	// Rows that count toward MAX_ROWS_AFTER_DRAFT, and the run of tool rows walked since
	// the last row that was not one: that run is the draft's own turn if the draft is next.
	let counted = 0;
	let toolRun = 0;
	for (let i = rows.length - 1; i >= 0; i--) {
		const row = rows[i];
		if (skipped(row)) continue;
		if (isDraftRow(row)) return row;
		if (retiresDraft(row)) return null;
		if (row.role === "tool") {
			toolRun++;
			continue;
		}
		counted += toolRun + 1; // a row that is not a tool row: the run after it counts
		toolRun = 0;
		if (row.role === "user") userRows++;
		// Every row before this one has too many rows after it to be the live draft.
		if (userRows > MAX_USER_ROWS_AFTER_DRAFT || counted > MAX_ROWS_AFTER_DRAFT) return null;
	}
	return null;
}
