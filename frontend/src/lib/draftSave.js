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
import { draftValues } from "./draftApply.js";
import { draftFailureOf } from "./draftFailure.js";
import { parkedNoteOf } from "./draftParked.js";

export function stampDraftModel(model, { messageKey, card, editable, actionKey }) {
	model.messageKey = messageKey || "";
	model.card = card || null;
	model.editable = !!editable;
	model.actionKey = actionKey || "";
	return model;
}

// What a proposed create/update would write. Two actions with the same key propose the
// same record, so the panel open on one is the panel for the other.
export function draftActionKey(a) {
	return JSON.stringify([
		a.verb || "create",
		a.doctype,
		a.name || "",
		a.fields || [],
		a.tables || [],
	]);
}

// The open panel when it edits the draft `actionKey` names, else null. The summary
// card's Confirm, Preview and Edit act on it, so a value the person changed there is
// never bypassed (Confirm saving the card's copy) or dropped (a rebuild from the
// agent's proposal). An empty key never matches.
export function openPanelFor(panel, actionKey) {
	return panel && actionKey && panel.actionKey === actionKey ? panel : null;
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

// What the panel would save when it opened, so a click outside it can tell whether
// closing would throw the person's work away.
export function markDraftBaseline(model) {
	model.baseline = JSON.stringify(draftValues(model));
	return model;
}

// Whether closing the panel would lose the person's work: a value changed since it
// opened. A model with no baseline (never opened as a panel) is never dirty.
export function draftIsDirty(model) {
	if (!model || model.baseline === undefined) return false;
	return JSON.stringify(draftValues(model)) !== model.baseline;
}
