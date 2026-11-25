// The desktop draft save (round 2, R2-9): stamping, the payload, and what the screen does.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
	applyActionPayload,
	draftActionKey,
	draftIsDirty,
	draftSaveOutcome,
	liveDraftMessage,
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

test("a panel is dirty once a value changes, and only then", () => {
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
	assert.equal(draftIsDirty(failed), false, "an error alone loses no typed work");
	assert.equal(
		draftIsDirty({ ...base(), fields: [], tables: [] }),
		false,
		"never opened as a panel"
	);
	assert.equal(draftIsDirty(null), false);
});

test("the live draft keeps its card through later replies, and only then", () => {
	const row = (name, role, content = "", action_outcome = "") => ({
		name,
		role,
		content,
		action_outcome,
	});
	const block = (body) => "Here:\n```jarvis-action\n" + body + "\n```";
	const draft = row("m2", "assistant", block('{"verb":"create","doctype":"ToDo"}'));
	const ack = row("m3", "assistant", "I'll wait.");
	assert.equal(liveDraftMessage([row("m1", "user"), draft, ack], ack), draft);
	const asked = [row("m1", "user"), draft, row("m4", "user"), ack];
	assert.equal(liveDraftMessage(asked, ack), draft, "a question does not retire it");
	const saved = [draft, row("t1", "tool", "", "created"), ack];
	assert.equal(liveDraftMessage(saved, ack), null, "a receipt retires it");
	const newDraft = row("m5", "assistant", block('{"verb":"update","doctype":"ToDo"}'));
	assert.equal(
		liveDraftMessage([draft, newDraft], newDraft),
		newDraft,
		"a newer one replaces it"
	);
	assert.equal(liveDraftMessage([draft, ack], null), null, "a turn is running");
});

test("the four reply kinds of a server draft card's save", () => {
	// saved (a repeat Save returns the same envelope)
	assert.deepEqual(
		draftSaveOutcome({ ok: true, verb: "create", name: "TS-1", doc_url: "/app/ts/TS-1" }),
		{
			kind: "saved",
		}
	);
	// fixable: the form stays open with the field marks; busy has no fields
	const fixable = draftSaveOutcome({
		ok: false,
		error: {
			code: "Invalid",
			message: "Hours is wrong.",
			fields: [{ fieldname: "hours", idx: 2 }],
		},
	});
	assert.equal(fixable.kind, "keep");
	assert.deepEqual(fixable.error.fields, [{ fieldname: "hours", idx: 2 }]);
	const busy = draftSaveOutcome({
		ok: false,
		error: { code: "DraftBusyError", message: "This draft is being handled.", fields: [] },
	});
	assert.equal(busy.kind, "keep");
	// closed, the card is gone: no cardOpen key at all (the old shape, exactly)
	assert.deepEqual(draftSaveOutcome({ ok: false, closed: true, error: { message: "Gone." } }), {
		kind: "closed",
		note: "Couldn't save this: Gone. The reply in the chat explains what to do.",
	});
	// closed, the card is still pending: keep it, and no sentence about the chat
	assert.deepEqual(
		draftSaveOutcome({
			ok: false,
			closed: true,
			card_open: true,
			assistant_told: false,
			error: { message: "Too big." },
		}),
		{ kind: "closed", note: "Couldn't save this: Too big.", cardOpen: true }
	);
});
