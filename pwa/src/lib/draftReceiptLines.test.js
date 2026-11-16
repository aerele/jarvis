import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { receiptLines } from "./draftReceiptLines.js";

// A decided tool row as get_conversation hands it, stamped as a draft card's.
const row = (over = {}, result = null, args = { doctype: "Timesheet" }) => ({
	name: "m1",
	role: "tool",
	tool_name: "create_doc",
	tool_call_id: "tok-1",
	tool_status: "",
	pending_card: null,
	draft_card: false,
	draft_receipt: true,
	action_outcome: "confirmed",
	tool_args: JSON.stringify(args),
	tool_result: result === null ? null : JSON.stringify(result),
	...over,
});
const ok = (data) => ({ ok: true, data: { doctype: "Timesheet", name: "TS-0001", ...data } });
const OPEN_TS = { href: "/app/timesheet/TS-0001", label: "Open" };

// Each State Table receipt as the phone draws it: the line, its link, its Submit, its look.
// Break: a receipt loses its wording, tone, Open link or Submit step, the cancelled one stops
// being muted, or a state the table gives no line (S9 replaced) draws one.
const TABLE = [
	[
		"S6 created",
		row({}, ok()),
		{
			text: "Created Timesheet TS-0001",
			tone: "ok",
			muted: false,
			reason: "",
			open: OPEN_TS,
			submit: null,
		},
	],
	[
		"S6 created, submittable: Submit",
		row({}, ok({ suggested_next: { kind: "submit" } })),
		{
			text: "Created Timesheet TS-0001",
			tone: "ok",
			muted: false,
			reason: "",
			open: OPEN_TS,
			submit: {
				kind: "submit",
				doctype: "Timesheet",
				name: "TS-0001",
				key: "Timesheet|TS-0001",
				label: "Submit",
			},
		},
	],
	[
		"S6 updated",
		row({ tool_name: "update_doc" }, ok({ doctype: "Sales Order", name: "SO-9" })),
		{
			text: "Updated Sales Order SO-9",
			tone: "ok",
			muted: false,
			reason: "",
			open: { href: "/app/sales-order/SO-9", label: "Open" },
			submit: null,
		},
	],
	[
		"S8 cancelled",
		row({ action_outcome: "discarded" }),
		{ text: "Draft cancelled", tone: "ok", muted: true, reason: "", open: null, submit: null },
	],
	[
		"S7 failed create",
		row(
			{ action_outcome: "failed" },
			{ ok: false, error: { message: "Employee is required." } }
		),
		{
			text: "Not created",
			tone: "error",
			muted: false,
			reason: "Employee is required.",
			open: null,
			submit: null,
		},
	],
	[
		"S7 partly applied",
		row({ action_outcome: "partial" }, { ok: false, error: { message: "Row 2 failed." } }),
		{
			text: "Partly applied",
			tone: "warn",
			muted: false,
			reason: "Row 2 failed.",
			open: null,
			submit: null,
		},
	],
	[
		"S10 changed record",
		row({ tool_name: "update_doc", action_outcome: "failed", draft_receipt_reason: "stale" }),
		{
			text: "Not saved: this record changed after the draft was made",
			tone: "error",
			muted: false,
			reason: "",
			open: null,
			submit: null,
		},
	],
];
for (const [name, r, want] of TABLE)
	test(`receipt: ${name}`, () => {
		const lines = receiptLines([r]);
		assert.equal(lines.length, 1);
		const { key, stepKey, ...rest } = lines[0];
		assert.equal(key, "m1");
		assert.deepEqual(rest, want);
	});

// Break: a row the State Table gives no receipt, or one the server did not stamp, draws a line. A
// flag-off thread has no stamp on any row, so it draws nothing, exactly as before.
test("a row that is not a stamped decided draft card draws nothing", () => {
	const none = [
		["S9 replaced", row({ action_outcome: "superseded" })],
		["stopped / expired / unknown have no draft wording", row({ action_outcome: "expired" })],
		[
			"a pending draft card",
			row({
				action_outcome: "",
				draft_card: true,
				draft_receipt: undefined,
				tool_status: "pending",
			}),
		],
		[
			"a decided card the server did not stamp (flag off)",
			row({ draft_receipt: undefined }, ok()),
		],
		["stamp is not literally true", row({ draft_receipt: "yes" }, ok())],
		["a stamped row of another tool", row({ tool_name: "submit_doc" }, ok())],
		[
			"a read tool",
			{ name: "r", role: "tool", tool_name: "get_list", tool_status: "completed" },
		],
	];
	for (const [name, r] of none) assert.deepEqual(receiptLines([r]), [], name);
	assert.deepEqual(receiptLines(none.map((n) => n[1])), []);
	assert.deepEqual(receiptLines(undefined), []);
	assert.deepEqual(receiptLines([null, "x"]), []);
});

// Break: several decisions in one turn lose their order or their identity (the key is the row's).
test("several receipts keep the rows' order and own keys", () => {
	const lines = receiptLines([
		row({ name: "a" }, ok()),
		{ name: "x", role: "tool", tool_name: "get_list" },
		row({ name: "b", action_outcome: "discarded" }),
	]);
	assert.deepEqual(
		lines.map((l) => [l.key, l.text]),
		[
			["a", "Created Timesheet TS-0001"],
			["b", "Draft cancelled"],
		]
	);
});

// Break: Submit is offered again for a record whose submit was already done or refused for good.
test("a hidden step takes Submit away and keeps the receipt", () => {
	const r = row({}, ok({ suggested_next: { kind: "submit" } }));
	const [line] = receiptLines([r], { hiddenSteps: new Set(["Timesheet|TS-0001"]) });
	assert.equal(line.submit, null);
	assert.equal(line.text, "Created Timesheet TS-0001");
	assert.equal(line.stepKey, "Timesheet|TS-0001");
	assert.ok(receiptLines([r], { hiddenSteps: new Set(["Timesheet|OTHER"]) })[0].submit);
});

// Break: the placement drifts: the receipt must sit in the assistant item after the typed card and
// before the record cards, and an item with no assistant message (a truncated thread) must still
// show it, never silently drop a decided card.
test("ChatView draws the lines after the typed card, before the record cards, and for orphan rows", () => {
	const view = fs.readFileSync(new URL("../views/ChatView.vue", import.meta.url), "utf8");
	const actionCard = view.indexOf("<ActionCard");
	const receipt = view.indexOf("<DraftReceipt", actionCard);
	const records = view.indexOf("<RecordCards", actionCard);
	assert.ok(actionCard > 0 && receipt > actionCard && records > receipt);
	const orphan = view.indexOf("<template v-else>\n\t\t\t\t\t<DraftReceipt");
	assert.ok(orphan > records, "the orphan branch draws them too");
	assert.equal(view.split("<DraftReceipt").length - 1, 2);
});
