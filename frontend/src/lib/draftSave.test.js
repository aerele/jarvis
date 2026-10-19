// The desktop draft save (round 2, R2-9): stamping, the payload, and what the screen does.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
	applyActionPayload,
	draftActionKey,
	draftIsDirty,
	draftSaveOutcome,
	markDraftBaseline,
	openPanelFor,
	stampDraftModel,
} from "./draftSave.js";

const base = () => ({ verb: "create", doctype: "ToDo", docName: "", cont: 0 });

test("the open panel is stamped editable with its own draft and card", () => {
	const card = { verb: "create", doctype: "ToDo" };
	const m = stampDraftModel(base(), { messageKey: "msg-1", card, editable: true });
	const payload = applyActionPayload(m, { values: { a: 1 }, submit: 0, conversation: "c1" });
	assert.equal(payload.message, "msg-1");
	assert.equal(payload.card, card);
	assert.equal(payload.editable, 1);
});

test("the stamp wins over whatever draft is current at save time", () => {
	const m = stampDraftModel(base(), { messageKey: "msg-1", card: null, editable: true });
	const payload = applyActionPayload(m, {
		values: {},
		conversation: "c1",
		fallback: { messageKey: "msg-2-a-later-reply" },
	});
	assert.equal(payload.message, "msg-1");
});

test("the summary card is never editable", () => {
	const m = stampDraftModel(base(), { messageKey: "msg-1", card: null, editable: false });
	assert.equal(applyActionPayload(m, { values: {}, conversation: "c1" }).editable, 0);
	// a model nobody stamped (the summary card before this change) is not editable either
	assert.equal(applyActionPayload(base(), { values: {}, conversation: "c1" }).editable, 0);
});

test("closed closes with the note; anything else failed keeps the panel or card", () => {
	assert.deepEqual(
		draftSaveOutcome({ ok: false, closed: true, error: { message: "No stock." } }),
		{
			kind: "closed",
			note: "Couldn't save this: No stock. The reply in the chat explains what to do.",
		}
	);
	const kept = draftSaveOutcome({
		ok: false,
		error: { message: "Value missing", kind: "fixable" },
	});
	assert.equal(kept.kind, "keep");
	assert.equal(kept.error.message, "Value missing");
	assert.equal(draftSaveOutcome({ ok: true, parked: true, note: "Waiting." }).kind, "parked");
	assert.equal(draftSaveOutcome({ ok: true, name: "TD-1" }).kind, "saved");
});

test("R2-9: a fixable failure never leaves neither panel nor card", () => {
	// Whatever a fixable failure carries, unless the server closed it the screen keeps
	// what the person was looking at (the panel, or the summary card with its error).
	for (const error of [
		{ message: "Value missing", kind: "fixable", fields: [{ fieldname: "description" }] },
		{ message: "Bad link", kind: "fixable" },
		{ message: "Busy", kind: "retry_later" },
	]) {
		assert.equal(draftSaveOutcome({ ok: false, error }).kind, "keep", error.message);
	}
});

test("a proposal's key names what it would write", () => {
	const a = {
		verb: "create",
		doctype: "Sales Order",
		fields: [{ fieldname: "customer", value: "C1" }],
	};
	assert.equal(draftActionKey(a), draftActionKey({ ...a }));
	assert.equal(
		draftActionKey({ ...a, verb: undefined }),
		draftActionKey(a),
		"create is the default verb"
	);
	const changed = { ...a, fields: [{ fieldname: "customer", value: "C2" }] };
	assert.notEqual(draftActionKey(changed), draftActionKey(a));
});

test("the open panel is found only for the draft it edits", () => {
	const key = draftActionKey({ verb: "create", doctype: "ToDo" });
	const panel = stampDraftModel(base(), {
		messageKey: "m1",
		card: null,
		editable: true,
		actionKey: key,
	});
	assert.equal(openPanelFor(panel, key), panel);
	assert.equal(openPanelFor(panel, draftActionKey({ verb: "create", doctype: "Note" })), null);
	assert.equal(openPanelFor(null, key), null);
	// a panel built before the key existed, or a card without one, never matches
	assert.equal(openPanelFor(stampDraftModel(base(), { editable: true }), ""), null);
});

test("a panel is dirty once a value changes or a failed save is showing", () => {
	const field = (fieldname, value) => ({ fieldname, value, orig: value, control: "text" });
	const panel = () =>
		markDraftBaseline({ ...base(), fields: [field("description", "Call back")], tables: [] });
	const fresh = panel();
	assert.equal(draftIsDirty(fresh), false, "just opened");
	const edited = panel();
	edited.fields[0].value = "Call back tomorrow";
	assert.equal(draftIsDirty(edited), true);
	const failed = panel();
	failed.error = { message: "Value missing for Due Date" };
	assert.equal(draftIsDirty(failed), true, "its marked fields would be lost");
	assert.equal(
		draftIsDirty({ ...base(), fields: [], tables: [] }),
		false,
		"never opened as a panel"
	);
	assert.equal(draftIsDirty(null), false);
});
