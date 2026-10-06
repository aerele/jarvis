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
	cardBannerOf,
	cardWarningOf,
	diffLineView,
	diffTablesOf,
	personError,
	toolFailureCopy,
	failureReferenceOf,
	copyText,
	pendingExpiry,
	receiptView,
	isDestructivePlanStep,
	PLAN_STEP_CAP,
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

// #603: a required field the model left blank used to vanish from the card, so the
// person only learned about it when Confirm failed.
const draftField = (fieldname, label, over) => ({
	fieldname,
	label,
	value: "",
	reqd: 0,
	read_only: 0,
	proposed: true,
	...over,
});

test("summarize(create): a proposed required blank shows, flagged missing", () => {
	const model = {
		verb: "create",
		fields: [
			draftField("customer_name", "Customer Name", { value: "Acme", reqd: 1 }),
			draftField("account_manager", "Account Manager", { reqd: 1 }),
		],
		tables: [],
	};
	const action = { fields: [{ label: "Customer Name", value: "Acme" }] };
	assert.deepEqual(summarize(model, action).rows, [
		{ label: "Customer Name", value: "Acme" },
		{ label: "Account Manager", value: "", missing: true },
	]);
});

test("summarize(create): an unproposed required field is not flagged (controllers fill many)", () => {
	const model = {
		verb: "create",
		fields: [draftField("currency", "Currency", { reqd: 1, proposed: false })],
		tables: [],
	};
	assert.deepEqual(summarize(model, {}).rows, []);
});

test("summarize(create): an optional or read-only blank is not flagged", () => {
	const model = {
		verb: "create",
		fields: [
			draftField("notes", "Notes"),
			draftField("status", "Status", { reqd: 1, read_only: 1 }),
		],
		tables: [],
	};
	assert.deepEqual(summarize(model, {}).rows, []);
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

// P1 (skill approve-and-run, §3.5): `card.plan` is an ADDITIVE field on an
// EXISTING kind - CARD_KINDS itself must stay exactly as it was (a plan never
// needs its own kind), and pendingCardOf must pass `plan`/`approve_run`
// through untouched, same as any other card property.
test("CARD_KINDS parity is UNCHANGED - the plan/approve_run fields are additive, not a new kind", () => {
	// "skill" (create_custom_skill) is already whitelisted from the macro work;
	// a runnable-plan card rides an ORDINARY kind (e.g. "create") - it does not
	// add or need a 14th kind. The full whitelist is pinned by the "every kind
	// build_card emits is whitelisted" test above; this only pins that a plan
	// does not require touching that set.
	assert.equal(pendingCardOf({ preview: { card: { kind: "plan" } } }), null);
	const card = {
		kind: "create",
		doctype: "Sales Invoice",
		approve_run: true,
		skill_slug: "close-books",
		plan: { steps: [{ n: 1, verb: "create", doctype: "Sales Invoice", summary: "x" }] },
	};
	const out = pendingCardOf({ preview: { card } });
	assert.equal(out.approve_run, true);
	assert.equal(out.skill_slug, "close-books");
	assert.deepEqual(out.plan, card.plan);
});

test("isDestructivePlanStep: delete/cancel/amend are destructive, everything else is not", () => {
	for (const verb of ["delete", "cancel", "amend", "DELETE", "Cancel"]) {
		assert.equal(isDestructivePlanStep({ verb }), true, verb);
	}
	for (const verb of ["create", "update", "submit", "send_email", "run_method", ""]) {
		assert.equal(isDestructivePlanStep({ verb }), false, verb);
	}
	assert.equal(isDestructivePlanStep({}), false);
	assert.equal(isDestructivePlanStep(null), false);
});

test("PLAN_STEP_CAP: a positive display cap the outline caps against defensively", () => {
	assert.equal(typeof PLAN_STEP_CAP, "number");
	assert.ok(PLAN_STEP_CAP > 0);
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

test("receiptView: an auto_applied (armed macro) write is a DISTINCT receipt, not a plain confirmed", () => {
	const v = receiptView(
		"submit_doc",
		{ doctype: "Sales Order", name: "SO-0001" },
		{ ok: true, data: {} },
		"auto_applied"
	);
	assert.equal(v.icon, "auto_applied");
	assert.equal(v.tone, "success");
	assert.notEqual(v.icon, "confirmed");
	assert.match(v.title, /no confirmation/);
});

test("receiptView: auto_applied counts like a real execution (from data), not the args", () => {
	const v = receiptView(
		"create_doc",
		{ doctype: "Task" },
		{
			ok: true,
			data: {
				doctype: "Task",
				name: "TASK-9001",
				created: [{ doctype: "Task", name: "TASK-9001" }],
			},
		},
		"auto_applied"
	);
	assert.ok(v.targets[0].url.includes("TASK-9001"));
});

// P0b (§4.5 outcome table): every outcome gets its own honest chip, and the
// copy is fixed literal text (not verb/subject composed) per the plan. A
// truthy `result` proves each branch ignores it rather than trusting an
// unconfirmed/unverified result the way "confirmed" does.
const _HONEST_OUTCOMES = [
	["cancelled", "muted", "Cancelled — not performed"],
	["superseded", "muted", "Not confirmed — replaced by a newer proposal"],
	["expired", "muted", "Expired — not performed"],
	["unknown", "warning", "Outcome unknown — check before retrying"],
	["partial", "warning", "Partly applied — check before retrying"],
];
for (const [outcome, tone, title] of _HONEST_OUTCOMES) {
	test(`receiptView: ${outcome} renders its own honest chip, never confirmed`, () => {
		const v = receiptView(
			"create_doc",
			{ doctype: "Task", name: "TASK-1" },
			{ ok: true, data: { doctype: "Task", name: "TASK-1" } },
			outcome
		);
		assert.equal(v.outcome, outcome);
		assert.equal(v.icon, outcome);
		assert.equal(v.tone, tone);
		assert.equal(v.title, title);
		assert.notEqual(v.icon, "confirmed");
	});
}

test("receiptView: an unrecognised outcome value renders the NEUTRAL unknown chip, never confirmed", () => {
	// A future outcome this build predates, or a malformed row - must never
	// silently fall through to the confirmed/✓ path (the bug P0b closes).
	const v = receiptView(
		"create_doc",
		{ doctype: "Task", name: "TASK-1" },
		{ ok: true, data: { doctype: "Task", name: "TASK-1" } },
		"some_future_outcome"
	);
	assert.equal(v.icon, "unknown");
	assert.equal(v.tone, "warning");
	assert.notEqual(v.icon, "confirmed");
	assert.notEqual(v.title, "nothing changed");
});

test("receiptView: unknown/partial read names off the ARGS, not an unverified result", () => {
	// The result claims TASK-9001 was created; unknown/partial cannot trust
	// that (the write's real effect is unverified), so the chip must show
	// what was PROPOSED (args), not the unverified result.
	for (const outcome of ["unknown", "partial"]) {
		const v = receiptView(
			"create_doc",
			{ doctype: "Task", name: "TASK-1" },
			{ ok: true, data: { doctype: "Task", name: "TASK-9001" } },
			outcome
		);
		assert.deepEqual(
			v.targets.map((t) => t.name),
			["TASK-1"]
		);
	}
});

// CR-3: an update's table is its final set of rows, so a saved row the proposal
// does not name is deleted. The card must say which, never remove it silently.
test("lineItemSummary: an update lists the saved rows it would remove", () => {
	const s = lineItemSummary({
		fieldname: "items",
		label: "Items",
		columns: [
			{ fieldname: "item_code", label: "Item", fieldtype: "Link" },
			{ fieldname: "qty", label: "Qty", fieldtype: "Float" },
		],
		rows: [{ __name: "r1", item_code: "Widget A", qty: "5" }],
		origJson: JSON.stringify([
			{ name: "r1", item_code: "Widget A", qty: 2 },
			{ name: "r2", item_code: "Widget B", qty: 1 },
		]),
	});
	assert.equal(s.count, 1);
	assert.deepEqual(s.removed, ["Widget B"]);
});

test("summarize(update): a table emptied of saved rows still shows, with what it removes", () => {
	const model = {
		verb: "update",
		fields: [],
		tables: [
			{
				fieldname: "items",
				label: "Items",
				columns: [{ fieldname: "item_code", label: "Item", fieldtype: "Link" }],
				rows: [],
				origJson: JSON.stringify([{ name: "r1", item_code: "Widget A" }]),
			},
		],
	};
	const s = summarize(model, {});
	assert.equal(s.tables.length, 1);
	assert.deepEqual(s.tables[0].removed, ["Widget A"]);
});

test("lineItemSummary: a create (no saved rows) removes nothing", () => {
	const s = lineItemSummary(createModel.tables[0]);
	assert.deepEqual(s.removed, []);
});

// ── Risk banner + trial warning (round 2, J1-cards) ─────────────────────────

test("cardBannerOf: the server's risk line, for a sensitive card", () => {
	assert.deepEqual(
		cardBannerOf({
			kind: "create",
			risk: "sensitive",
			risk_line: "This runs code for every user.",
		}),
		{ risk: "sensitive", text: "This runs code for every user." }
	);
});

test("cardBannerOf: no banner for a structure card (structure writes are refused, never carded)", () => {
	assert.equal(cardBannerOf({ kind: "create", risk: "structure", risk_line: "x" }), null);
});

test("cardBannerOf: a sensitive card with no line falls back to plain words", () => {
	assert.equal(
		cardBannerOf({ risk: "sensitive", risk_line: "  " }).text,
		"This changes sensitive settings."
	);
});

test("cardBannerOf: no banner on an ordinary card, an old card, or junk", () => {
	for (const card of [
		{ kind: "create" },
		{},
		null,
		undefined,
		{ risk: "other" },
		{ risk_line: "x" },
	]) {
		assert.equal(cardBannerOf(card), null, JSON.stringify(card));
	}
});

test("cardWarningOf: the real job count", () => {
	assert.equal(
		cardWarningOf({ warning: { jobs: 500, rolled_back: false } }),
		"This will also start 500 background jobs."
	);
	assert.equal(
		cardWarningOf({ warning: { jobs: 1, rolled_back: false } }),
		"This will also start 1 background job."
	);
});

test("cardWarningOf: a hook that rolled back, alone or with jobs, in one line", () => {
	const rolled =
		"During the check, the record's own code stopped part-way, so the result may differ.";
	assert.equal(cardWarningOf({ warning: { jobs: 0, rolled_back: true } }), rolled);
	assert.equal(
		cardWarningOf({ warning: { jobs: 2, rolled_back: true } }),
		`${rolled} This will also start 2 background jobs.`
	);
});

test("cardWarningOf: nothing for no warning, an old card or bad values", () => {
	for (const card of [
		{},
		null,
		{ warning: null },
		{ warning: { jobs: 0, rolled_back: false } },
		{ warning: { jobs: "500" } },
		{ warning: { jobs: -1 } },
		{ warning: { jobs: 1.5 } },
		{ warning: { rolled_back: "yes" } },
	]) {
		assert.equal(cardWarningOf(card), "", JSON.stringify(card));
	}
});

test("diffLineView: one line of a sensitive update's line diff", () => {
	assert.deepEqual(diffLineView({ op: "+", text: "B" }), { kind: "add", text: "+ B" });
	assert.deepEqual(diffLineView({ op: "-", text: "b" }), { kind: "del", text: "- b" });
	assert.deepEqual(diffLineView({ op: " ", text: "a" }), { kind: "same", text: "  a" });
	assert.deepEqual(diffLineView({ op: "gap", text: "" }), { kind: "gap", text: "…" });
	assert.deepEqual(diffLineView({ op: "gap", text: "", count: 12 }), {
		kind: "gap",
		text: "… 12 unchanged lines",
	});
	assert.deepEqual(diffLineView({ op: "gap", text: "", count: 1 }), {
		kind: "gap",
		text: "… 1 unchanged line",
	});
	assert.deepEqual(diffLineView({ op: "?", text: 5 }), { kind: "same", text: "  5" });
	assert.deepEqual(diffLineView(null), { kind: "same", text: "  " });
	assert.deepEqual(diffLineView({ op: "toString", text: "x" }), { kind: "same", text: "  x" });
});

test("diffTablesOf: both sides, current first, an empty side named none", () => {
	const t = { rows: [{ cells: ["a"] }], count: 1 };
	const t3 = { rows: [{}, {}, {}], count: 3 };
	assert.deepEqual(diffTablesOf({ from_table: t3, to_table: t }), [
		{ name: "Current", table: t3, heading: "Current · 3 rows" },
		{ name: "New", table: t, heading: "New · 1 row" },
	]);
	assert.deepEqual(diffTablesOf({ to_table: t }), [
		{ name: "Current", table: null, heading: "Current · none" },
		{ name: "New", table: t, heading: "New · 1 row" },
	]);
	assert.deepEqual(diffTablesOf({ from_table: t3 }), [
		{ name: "Current", table: t3, heading: "Current · 3 rows" },
		{ name: "New", table: null, heading: "New · none" },
	]);
	assert.deepEqual(diffTablesOf({ from_table: "x", to_table: { rows: "no" } }), []);
	assert.deepEqual(diffTablesOf(null), []);
});

// A refusal the bench writes for the model ("Do not retry it with another tool;
// tell the user ...") can carry a plain twin for the person: error.person_message.
const REFUSAL = {
	code: "sensitive_refused",
	message:
		"Importing Customer records changes sensitive settings ... Do not retry it with another tool; tell the user to use Data Import in Desk.",
	hint: "Use Data Import in Desk: open /app/data-import/new.",
	person_message:
		"An import of Customer records changes sensitive settings and cannot be shown in full on a confirmation card, so it cannot be run from chat. Use Data Import in Desk.",
};

test("personError: the person's words when the bench wrote them, else the message", () => {
	assert.deepEqual(personError(REFUSAL), { ...REFUSAL, message: REFUSAL.person_message });
	assert.deepEqual(personError({ message: "Value missing" }), { message: "Value missing" });
	assert.deepEqual(personError({ message: "m", person_message: "  " }), {
		message: "m",
		person_message: "  ",
	});
	assert.deepEqual(personError(null), {});
	assert.deepEqual(personError("plain"), { message: "plain" });
});

test("toolFailureCopy: a refused tool row shows the person's words and the hint", () => {
	const row = toolFailureCopy(JSON.stringify({ ok: false, error: REFUSAL }));
	assert.equal(row.message, REFUSAL.person_message);
	assert.equal(row.hint, REFUSAL.hint);
	assert.ok(!row.message.includes("another tool"));
	assert.deepEqual(toolFailureCopy({ ok: false, error: { message: "Plain failure" } }), {
		message: "Plain failure",
		hint: "",
	});
	assert.deepEqual(toolFailureCopy("not json"), {
		message: "This step couldn't be completed.",
		hint: "",
	});
});

// J2b: a failed confirmation carries its own id (error.reference) for support.
test("failureReferenceOf: from an envelope, an error, or a stored JSON result", () => {
	const env = { ok: false, error: { message: "x", reference: "  pa-123  " } };
	assert.equal(failureReferenceOf(env), "pa-123");
	assert.equal(failureReferenceOf(env.error), "pa-123");
	assert.equal(failureReferenceOf(JSON.stringify(env)), "pa-123");
	assert.equal(failureReferenceOf({ ok: false, error: { message: "x" } }), "");
	assert.equal(failureReferenceOf({ ok: true, data: {} }), "");
	assert.equal(failureReferenceOf({ ok: false, error: { reference: 42 } }), "");
	assert.equal(failureReferenceOf("not json"), "");
	assert.equal(failureReferenceOf(null), "");
});

test("failureReferenceOf: a connector's own failure carries it beside its data", () => {
	const chip = { ok: true, data: { ok: false, error: { message: "down" } }, reference: "pa-c1" };
	assert.equal(failureReferenceOf(chip), "pa-c1");
	assert.equal(
		receiptView("call_connector", { connector: "c1" }, chip, "unknown").reference,
		"pa-c1"
	);
});

test("receiptView: a failed, partial or unknown chip carries the reference; a confirmed one never", () => {
	const res = { ok: false, error: { message: "Value missing", reference: "pa-9" } };
	for (const outcome of ["failed", "partial", "unknown"]) {
		assert.equal(
			receiptView("submit_doc", { doctype: "Task", name: "T-1" }, res, outcome).reference,
			"pa-9"
		);
	}
	const ok = { ok: true, data: { name: "T-1" }, error: { reference: "pa-9" } };
	assert.equal(
		receiptView("submit_doc", { doctype: "Task", name: "T-1" }, ok, "confirmed").reference,
		""
	);
	assert.equal(
		receiptView("submit_doc", { doctype: "Task", name: "T-1" }, null, "failed").reference,
		""
	);
});

test("copyText: true when the clipboard took it, false when it refused or is missing", async () => {
	const seen = [];
	assert.equal(await copyText("pa-1", { writeText: async (t) => seen.push(t) }), true);
	assert.deepEqual(seen, ["pa-1"]);
	assert.equal(
		await copyText("pa-1", {
			writeText: async () => {
				throw new Error("denied");
			},
		}),
		false
	);
	assert.equal(await copyText("pa-1", null), false);
	assert.equal(await copyText("", { writeText: async () => {} }), false);
});
