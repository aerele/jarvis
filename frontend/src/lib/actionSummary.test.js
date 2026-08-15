// frontend/src/lib/actionSummary.test.js
import { test } from "node:test";
import assert from "node:assert/strict";
import {
	proposedFields,
	changedFields,
	lineItemSummary,
	summarize,
	batchFromPreview,
	pendingCardOf,
	verbSentence,
	pendingExpiry,
	receiptView,
} from "./actionSummary.js";

const createAction = {
	kind: "doc",
	verb: "create",
	doctype: "Sales Order",
	summary: "Sales Order - Acme, 2 items, total 1,100",
	fields: [
		{ label: "Customer", value: "Acme Corp" },
		{ label: "Delivery Date", value: "2026-08-01" },
		{ label: "Note", value: "" },
	],
	tables: [{ fieldname: "items", rows: [{ item_code: "Widget A" }, { item_code: "Widget B" }] }],
};
const createModel = {
	verb: "create",
	doctype: "Sales Order",
	fields: [
		{ fieldname: "customer", label: "Customer", value: "Acme Corp", orig: "", changed: false },
		{
			fieldname: "delivery_date",
			label: "Delivery Date",
			value: "2026-08-01",
			orig: "",
			changed: false,
		},
	],
	tables: [
		{
			fieldname: "items",
			label: "Items",
			columns: [
				{ fieldname: "item_code", label: "Item", fieldtype: "Link" },
				{ fieldname: "qty", label: "Qty", fieldtype: "Float" },
				{ fieldname: "rate", label: "Rate", fieldtype: "Currency" },
			],
			rows: [
				{ item_code: "Widget A", qty: 10, rate: 50 },
				{ item_code: "Widget B", qty: 5, rate: 120 },
			],
		},
	],
};

test("proposedFields: the fields the model set, non-empty, as label -> value", () => {
	const rows = proposedFields(createAction);
	assert.deepEqual(rows, [
		{ label: "Customer", value: "Acme Corp" },
		{ label: "Delivery Date", value: "2026-08-01" },
	]); // empty Note dropped, no schema-driven ranking
});

test("proposedFields: empty or missing fields array yields empty list", () => {
	assert.deepEqual(proposedFields({}), []);
	assert.deepEqual(proposedFields({ fields: [] }), []);
});

test("changedFields: only changed fields, as from -> to (update diff, unchanged)", () => {
	const rows = changedFields({
		verb: "update",
		fields: [
			{ label: "Status", value: "Closed", orig: "Open", changed: true },
			{ label: "Customer", value: "Acme", orig: "Acme", changed: false },
		],
	});
	assert.deepEqual(rows, [{ label: "Status", from: "Open", to: "Closed" }]);
});

test("lineItemSummary: count + compact rows + column labels, and NO total field", () => {
	const s = lineItemSummary(createModel.tables[0]);
	assert.equal(s.count, 2);
	assert.equal(s.rows.length, 2);
	assert.equal(s.rows[0].cells[0], "Widget A");
	assert.deepEqual(s.columns, ["Item", "Qty", "Rate"]);
	assert.ok(!("total" in s)); // no code-computed total any more
});

test("lineItemSummary: missing cell values render as empty string", () => {
	const s = lineItemSummary({
		fieldname: "items",
		label: "Items",
		columns: [
			{ fieldname: "item_code", label: "Item", fieldtype: "Link" },
			{ fieldname: "qty", label: "Qty", fieldtype: "Float" },
		],
		rows: [{ item_code: "Widget A" }],
	});
	assert.equal(s.rows[0].cells[1], "");
});

test("summarize(create): kind=create, headline from action.summary, rows from proposed fields", () => {
	const out = summarize(createModel, createAction);
	assert.equal(out.kind, "create");
	assert.equal(out.headline, "Sales Order - Acme, 2 items, total 1,100");
	assert.deepEqual(out.rows, [
		{ label: "Customer", value: "Acme Corp" },
		{ label: "Delivery Date", value: "2026-08-01" },
	]);
	assert.equal(out.tables[0].count, 2);
	assert.ok(!("total" in out.tables[0]));
});

test("summarize(create): headline is empty string when the model provides no summary", () => {
	const out = summarize(createModel, { ...createAction, summary: undefined });
	assert.equal(out.headline, "");
	assert.ok(out.rows.length >= 1); // proposed fields still render (graceful default)
});

test("summarize(update): kind=update, mechanical diff, headline optional", () => {
	const out = summarize(
		{
			verb: "update",
			fields: [{ label: "Status", value: "Closed", orig: "Open", changed: true }],
			tables: [],
		},
		{ verb: "update", doctype: "Sales Order", fields: [], summary: "Closing SO-0001" }
	);
	assert.equal(out.kind, "update");
	assert.equal(out.headline, "Closing SO-0001");
	assert.deepEqual(out.diff, [{ label: "Status", from: "Open", to: "Closed" }]);
});

// THE trust-boundary fix, on the FALLBACK path - the one that runs when the server
// built no card, including when build_card FAILS. `notes` is a tool argument the
// MODEL writes; rendered as unattributed bullets it reads as system truth, so a
// prompt-injected agent could caption its own confirmation.
test("batchFromPreview: created records -> action lines; model-authored notes are NOT returned", () => {
	const out = batchFromPreview({
		would: {
			created: [
				{ doctype: "Item", name: "Widget" },
				{ doctype: "Customer", name: "Acme" },
			],
			notes: ["these already exist - confirming changes nothing"],
		},
	});
	assert.deepEqual(out.actions, [
		{ doctype: "Item", name: "Widget" },
		{ doctype: "Customer", name: "Acme" },
	]);
	assert.equal(out.notes, undefined);
	assert.equal(JSON.stringify(out).includes("confirming changes nothing"), false);
});

test("batchFromPreview: non-batch or empty preview -> null", () => {
	assert.equal(batchFromPreview({ would: "SomeDocName" }), null);
	assert.equal(batchFromPreview({ described: true }), null);
	assert.equal(batchFromPreview(null), null);
});

test("pendingCardOf: returns the structured card for a known kind", () => {
	const card = {
		kind: "update",
		doctype: "ToDo",
		diff: [{ label: "Priority", from: "Medium", to: "Low" }],
	};
	assert.deepEqual(pendingCardOf({ preview: { card } }), card);
});

// CARD_KINDS IS THE GATE. pendingCardOf returns null for a kind not in the set and
// the SPA silently falls back to the raw preview, so a kind build_card emits but the
// whitelist omits ships as a no-op: the card renders exactly as before, every test
// stays green, and nothing says so. One assertion per kind the server can emit.
test("pendingCardOf: every kind build_card emits is whitelisted", () => {
	for (const kind of [
		"create",
		"update",
		"bulk_update",
		"verb",
		"email",
		"method",
		"batch_create",
		"bulk_email",
		"share",
		"assign",
		"skill",
		"wiki",
		"import",
	]) {
		assert.ok(pendingCardOf({ preview: { card: { kind } } }), `${kind} is not in CARD_KINDS`);
	}
});

test("pendingCardOf: null for missing / unknown-kind / non-object cards", () => {
	assert.equal(pendingCardOf({ preview: {} }), null);
	assert.equal(pendingCardOf({ preview: { card: { kind: "wat" } } }), null);
	assert.equal(pendingCardOf({ preview: { card: "nope" } }), null);
	assert.equal(pendingCardOf({}), null);
	assert.equal(pendingCardOf(null), null);
});

test("verbSentence: single names the record, bulk counts + pluralizes", () => {
	assert.equal(
		verbSentence({
			kind: "verb",
			verb: "submit",
			doctype: "Sales Order",
			count: 1,
			targets: ["SO-1"],
		}),
		"Will submit this Sales Order SO-1"
	);
	assert.equal(
		verbSentence({
			kind: "verb",
			verb: "cancel",
			doctype: "Sales Order",
			count: 6,
			targets: ["SO-1"],
		}),
		"Will cancel 6 Sales Orders"
	);
	assert.equal(
		verbSentence({
			kind: "verb",
			verb: "apply",
			action: "Approve",
			doctype: "Leave Application",
			count: 1,
			targets: ["LA-1"],
		}),
		'Will apply "Approve" to this Leave Application LA-1'
	);
});

test("pendingExpiry: expired vs live vs no-stamp", () => {
	const now = 1_000_000_000_000; // Date.now() ms
	assert.deepEqual(pendingExpiry(now / 1000 + 120, now), { expired: false, secondsLeft: 120 });
	assert.deepEqual(pendingExpiry(now / 1000 - 5, now), { expired: true, secondsLeft: -5 });
	assert.deepEqual(pendingExpiry(null, now), { expired: false, secondsLeft: null });
	assert.deepEqual(pendingExpiry(0, now), { expired: false, secondsLeft: null });
});

test("receiptView: a CONFIRMED delete offers no open link - the record is gone", () => {
	// The shortcut would 404. Every non-batch, non-email receipt used to linkify.
	const v = receiptView(
		"delete_doc",
		{ doctype: "Task", name: "TASK-0001" },
		{ data: { deleted: true, doctype: "Task", name: "TASK-0001" } },
		"confirmed"
	);
	assert.equal(v.targets.length, 1);
	assert.equal(v.targets[0].name, "TASK-0001");
	assert.equal(v.targets[0].url, "");
});

test("receiptView: a DISCARDED delete keeps its link - nothing ran, the record lives", () => {
	const v = receiptView("delete_doc", { doctype: "Task", name: "TASK-0001" }, {}, "discarded");
	assert.ok(v.targets[0].url, "a record that still exists must stay openable");
});

test("receiptView: a create links the record it made", () => {
	const v = receiptView(
		"create_doc",
		{ doctype: "Task" },
		{ data: { doctype: "Task", name: "TASK-0002" } },
		"confirmed"
	);
	assert.ok(v.targets[0].url.includes("TASK-0002"));
});
