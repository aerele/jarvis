/**
 * The one rule for which draft card is live. The server (Python), the phone app and
 * the desktop app all decide it the same way, proven by liveDraftCases.json.
 *
 * Rows are the conversation's messages in order; hidden and streaming rows are
 * skipped entirely. A draft row is an assistant row whose FIRST closed jarvis-action
 * fence is broken, or parses to a non-email action whose verb is missing, create or
 * update. The live draft is the last draft row, unless a later tool row carries a
 * non-empty action_outcome, or more than MAX_USER_ROWS_AFTER_DRAFT user rows follow.
 *
 * Pure, relative imports only: the phone app's node tests load it.
 */
import { actionBlockState, closedActionBody, normaliseAction, GATED_VERBS } from "./chatAction.js";

export const MAX_USER_ROWS_AFTER_DRAFT = 5;

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

const hasOutcome = (row) =>
	row.role === "tool" &&
	typeof row.action_outcome === "string" &&
	row.action_outcome.trim() !== "";

/**
 * @param {Array<object>|null|undefined} rows messages in order
 * @returns {object|null} the live draft row, or null
 */
export function liveDraftRow(rows) {
	if (!Array.isArray(rows)) return null;
	let userRows = 0;
	for (let i = rows.length - 1; i >= 0; i--) {
		const row = rows[i];
		if (skipped(row)) continue;
		if (isDraftRow(row)) return userRows > MAX_USER_ROWS_AFTER_DRAFT ? null : row;
		if (hasOutcome(row)) return null;
		if (row.role === "user") userRows++;
	}
	return null;
}
