// Saving a draft from the desktop chat (round 2, R2-9): the open draft panel and the
// read-only summary card both go through apply_action. Pure, so the rules are
// pinned without mounting ChatView.
//
// - A model is stamped when it is built with the draft's message (actionFor goes
//   null while a turn runs and moves on with the next reply, so read at save time
//   it could name another draft: the server tells the assistant "the user is fixing
//   it in the panel" once per draft) and the card it came from.
// - Only the open panel is `editable`: after a fixable failure the person fixes it
//   there. The summary card cannot be edited, so it sends `editable: 0` and the
//   server never tells the assistant someone is fixing it (the card then stays, with
//   its error, instead of the person being left with neither card nor panel).
import { draftFailureOf } from "./draftFailure.js";
import { parkedNoteOf } from "./draftParked.js";

export function stampDraftModel(model, { messageKey, card, editable }) {
	model.messageKey = messageKey || "";
	model.card = card || null;
	model.editable = !!editable;
	return model;
}

// The apply_action payload for `model`. `fallback` ({messageKey, card}) covers a
// model built before the stamp existed.
export function applyActionPayload(model, { values, submit, conversation, fallback = {} }) {
	return {
		verb: model.verb,
		doctype: model.doctype,
		name: model.docName || "",
		values,
		submit: submit ? 1 : 0,
		conversation: conversation || "",
		continue: model.cont ? 1 : 0,
		// which card this confirms: a File Box chat parks exactly this one
		message: model.messageKey || fallback.messageKey || "",
		card: model.card || fallback.card || null,
		editable: model.editable ? 1 : 0,
	};
}

// What the screen does with apply_action's answer:
// closed (close the panel, toast `note`), keep (keep the panel or card open with
// `error`), parked (a confirmation card now waits, `note`) or saved.
export function draftSaveOutcome(r, agent) {
	const failed = draftFailureOf(r, agent);
	if (failed && failed.closed) return { kind: "closed", note: failed.note };
	if (failed) return { kind: "keep", error: failed.error };
	const parked = parkedNoteOf(r);
	if (parked) return { kind: "parked", note: parked };
	return { kind: "saved" };
}
