// The inputs, the fake server and the recorder for draftModel.spec.js (not part of the app bundle).
//
// Both the view's old in-line builder and the shared one (lib/draftModel.js) are driven through
// `runCases` with these cases. A case is an action (or a short script of builds) plus the answers
// the fake server gives; its record is the built model (or the error, or undefined), the ordered
// log of API calls the build made, and, for the timing cases, the model again after the slow
// answer landed. `__key` (a counter that runs for the page's life) is replaced by the order of
// first appearance, so only its identity structure is compared.
import { FROM_CARD } from "./toolDraftPanel.js";

const F = (fieldname, label, fieldtype = "Data", extra = {}) => ({
	fieldname,
	label,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});

const ITEM_COLUMNS = [
	F("item_code", "Item Code", "Link", { options: "Item", reqd: 1 }),
	F("qty", "Qty", "Float"),
	F("rate", "Rate", "Currency"),
	F("amount", "Amount", "Currency", { read_only: 1 }),
];
const ITEMS = {
	label: "Items",
	child_doctype: "Sales Order Item",
	columns: ITEM_COLUMNS,
	extra_columns: [F("description", "Description", "Small Text"), F("brand", "Brand")],
	form_fields: [
		...ITEM_COLUMNS,
		F("description", "Description", "Small Text"),
		F("brand", "Brand"),
		F("warehouse", "Warehouse", "Link", { options: "Warehouse" }),
	],
	rule_fields: [
		{
			fieldname: "is_fixed_asset",
			fieldtype: "Check",
			default: "0",
			fetch_from: "item_code.is_fixed_asset",
		},
	],
};
const TAXES = {
	label: "Taxes",
	child_doctype: "Sales Taxes",
	columns: [
		F("charge_type", "Type", "Select", { options: "On Net Total\nActual" }),
		F("rate", "Rate", "Percent"),
	],
	extra_columns: [],
	form_fields: [],
};

// What get_doctype_form_meta answers, per doctype. A fresh copy on every call (the server's JSON),
// because the builder stamps its lookup keys onto the fields it is given.
const METAS = {
	"Sales Order": {
		ok: true,
		title_field: "customer_name",
		is_submittable: 1,
		fields: [
			F("customer", "Customer", "Link", { options: "Customer", reqd: 1 }),
			F("customer_name", "Customer Name", "Data", { read_only: 1 }),
			F("transaction_date", "Date", "Date", { reqd: 1, default: "Today" }),
			F("delivery_date", "Delivery Date", "Date"),
			F("status", "Status", "Select", { options: "Draft\nOpen\nClosed", default: "Draft" }),
			F("is_internal", "Is Internal", "Check", { default: "0" }),
			F("po_no", "PO No"),
			F("po_date", "PO Date", "Date"),
			F("company", "Company", "Link", { options: "Company" }),
			F("remarks", "Remarks", "Small Text"),
			F("total", "Total", "Currency", { read_only: 1 }),
			F("items", "Items", "Table", { options: "Sales Order Item", reqd: 1 }),
			F("taxes", "Taxes", "Table", { options: "Sales Taxes" }),
		],
		tables: { items: ITEMS, taxes: TAXES },
	},
	// A label that is another field's fieldname, and the other way round.
	Memo: {
		ok: true,
		title_field: "",
		is_submittable: 0,
		fields: [
			F("posting_date", "Date", "Date"),
			F("date", "Posting Date", "Date"),
			F("subject", "Subject", "Data", { reqd: 1 }),
			F("body", "Body", "Text Editor"),
		],
		tables: {},
	},
	Task: {
		ok: true,
		title_field: "subject",
		is_submittable: 0,
		fields: [
			F("subject", "Subject", "Data", { reqd: 1 }),
			F("description", "Description", "Small Text"),
			F("is_milestone", "Is Milestone", "Check"),
			F("priority", "Priority", "Select", { options: "Low\nMedium\nHigh" }),
			F("status", "Status", "Select", { options: "Open\nWorking\nCompleted" }),
		],
		tables: {},
	},
};

// What load_doc answers: the saved values, the saved child rows (each with its name) and docstatus.
const DOCS = {
	"Sales Order/SO-1": {
		values: {
			customer: "ACME",
			customer_name: "ACME Ltd",
			transaction_date: "2026-11-01",
			status: "Draft",
			is_internal: 1,
			remarks: "first",
		},
		tables: {
			items: [
				{
					name: "soi-1",
					idx: 1,
					item_code: "A",
					qty: 1,
					rate: 10,
					amount: 10,
					description: "Item A",
					warehouse: "W1",
					is_fixed_asset: 1,
				},
				{
					name: "soi-2",
					idx: 2,
					item_code: "B",
					qty: 2,
					rate: 4,
					amount: 8,
					is_fixed_asset: 0,
				},
			],
			taxes: [{ name: "tax-1", idx: 1, charge_type: "On Net Total", rate: 18 }],
		},
		docstatus: 0,
	},
	"Task/TASK-1": {
		values: { subject: "Old", is_milestone: 1, priority: "Low", status: "Open" },
		tables: {},
		docstatus: 0,
	},
};

const typed = (doctype, verb, fields = [], tables = [], extra = {}) => ({
	kind: "doc",
	verb,
	doctype,
	fields,
	tables,
	...extra,
});
const card = (...args) => ({ ...typed(...args), [FROM_CARD]: true });
const lab = (label, value) => ({ label, value });
const key = (fieldname, value) => ({ fieldname, label: fieldname, value });

const RULE_ANSWER = {
	ok: true,
	values: { item_code: { A: { is_fixed_asset: 1 }, B: { is_fixed_asset: 0 } } },
};

// `serve`: how the fake server answers: meta "ok" | "notok" | "throws"; doc "ok" | "throws";
// rules "answers" | "none" | "throws" | "slow" (held until the script says so).
export const CASES = [
	// ---- typed actions
	{
		name: "typed create, fields by label, fieldname and fuzzy",
		serve: { rules: "none" },
		actions: [
			typed("Sales Order", "create", [
				lab("Customer", "ACME"),
				lab("delivery_date", "2026-12-01"),
				lab("Is Internal", "Yes"),
				lab("Remarks", "line one\nline two"),
				lab("Status", "Shipped"),
			]),
		],
	},
	{
		name: "typed create with a table, rule values answered, an extra column key",
		serve: { rules: "answers" },
		actions: [
			typed(
				"Sales Order",
				"create",
				[lab("Customer", "ACME")],
				[
					{
						fieldname: "items",
						rows: [
							{ item_code: "A", qty: 2, rate: 5, brand: "Zed" },
							{ item_code: "B", qty: 1, description: "Second" },
						],
					},
				]
			),
		],
	},
	{
		name: "typed update with a stored record and a proposed table",
		serve: { rules: "answers" },
		actions: [
			typed(
				"Sales Order",
				"update",
				[lab("Status", "Open"), lab("Remarks", "second"), lab("Is Internal", "No")],
				[
					{
						fieldname: "items",
						rows: [
							{ name: "soi-1", qty: 5 },
							{ item_code: "C", qty: 1, rate: 3 },
						],
					},
				],
				{ name: "SO-1", title: "Change SO-1" }
			),
		],
	},
	{
		name: "typed update with no proposed table shows the stored tables",
		serve: { rules: "none" },
		actions: [typed("Sales Order", "update", [lab("Remarks", "only")], [], { name: "SO-1" })],
	},
	{
		name: "typed update, a Check field on a stored record",
		serve: {},
		actions: [
			typed("Task", "update", [lab("Is Milestone", "No"), lab("Priority", "High")], [], {
				name: "TASK-1",
			}),
		],
	},
	{
		name: "typed update with no field changes",
		serve: {},
		actions: [typed("Task", "update", [], [], { name: "TASK-1" })],
	},
	{
		name: "typed action whose field has an extra fieldname key still resolves by label",
		serve: {},
		actions: [
			typed("Task", "create", [{ label: "Subject", fieldname: "description", value: "Hi" }]),
		],
	},
	{
		name: "label resolution: unique containment",
		serve: {},
		actions: [
			typed("Sales Order", "create", [
				lab("delivery", "2026-12-24"),
				lab("comp", "Acme Co"),
			]),
		],
	},
	{
		name: "label resolution: ambiguous with no required hit is dropped",
		serve: {},
		actions: [typed("Sales Order", "create", [lab("po", "PO-77"), lab("Customer", "C")])],
	},
	{
		name: "label resolution: ambiguous with exactly one required hit",
		serve: {},
		actions: [typed("Sales Order", "create", [lab("cust", "ACME")])],
	},
	{
		name: "label resolution: no match, a table field and an empty label are left out",
		serve: {},
		actions: [
			typed("Sales Order", "create", [
				lab("zzz", "x"),
				lab("Items", "not a field"),
				lab("", "blank"),
				lab("Customer", "ACME"),
			]),
		],
	},
	{
		name: "label resolution: a label that is another field's fieldname",
		serve: {},
		actions: [
			typed("Memo", "create", [
				lab("Date", "2026-01-01"),
				lab("Posting Date", "2026-02-02"),
				lab("subject", "S"),
			]),
			typed("Memo", "create", [
				lab("posting_date", "2026-03-03"),
				lab("date", "2026-04-04"),
			]),
		],
	},
	{
		name: "typed create of a prompt-named record carries its name and the plan flag",
		serve: {},
		actions: [
			typed("Memo", "create", [lab("Name", "MEMO-1"), lab("Subject", "S")], [], {
				continue: 1,
				title: "Memo draft",
			}),
		],
	},
	{
		name: "typed update, stored record unloadable, no tables: a create-style view",
		serve: { doc: "throws" },
		actions: [typed("Task", "update", [lab("Subject", "New")], [], { name: "TASK-1" })],
	},
	{
		name: "typed update, stored record unloadable, with tables: refused",
		serve: { doc: "throws" },
		actions: [
			typed(
				"Sales Order",
				"update",
				[lab("Status", "Open")],
				[{ fieldname: "items", rows: [{ qty: 1 }] }],
				{
					name: "SO-1",
				}
			),
		],
	},
	{
		name: "refusals: a docs batch, an empty create, and a non-doc action make no call",
		serve: {},
		actions: [
			typed("Task", "create", [lab("Subject", "x")], [], { docs: [{}] }),
			typed("Task", "update", [], [], { name: "TASK-1", docs: [{}] }),
			typed("Task", "create", [], []),
			{ kind: "verb", verb: "submit", doctype: "Task" },
			typed("", "create", [lab("Subject", "x")]),
			null,
		],
	},
	{
		name: "a create with only a table is legal",
		serve: { rules: "none" },
		actions: [
			typed(
				"Sales Order",
				"create",
				[],
				[{ fieldname: "items", rows: [{ item_code: "A" }] }]
			),
		],
	},
	{
		name: "form meta that is not ok, or that throws, builds nothing and is asked again",
		serve: { metaSeq: ["notok", "throws", "ok"] },
		actions: [
			typed("Task", "create", [lab("Subject", "a")]),
			typed("Task", "create", [lab("Subject", "b")]),
			typed("Task", "create", [lab("Subject", "c")]),
		],
	},
	{
		name: "the form meta is read once per doctype, the saved record every time",
		serve: {},
		actions: [
			typed("Task", "update", [lab("Subject", "a")], [], { name: "TASK-1" }),
			typed("Task", "update", [lab("Subject", "b")], [], { name: "TASK-1" }),
			typed("Memo", "create", [lab("Subject", "c")]),
		],
	},
	{
		name: "rule values that throw leave the defaults",
		serve: { rules: "throws" },
		actions: [
			typed(
				"Sales Order",
				"create",
				[lab("Customer", "ACME")],
				[{ fieldname: "items", rows: [{ item_code: "A" }] }]
			),
		],
	},
	{
		name: "rule values slower than the wait: the card is built without them and the late answer still writes __rule",
		serve: { rules: "slow" },
		slow: true,
		actions: [
			typed(
				"Sales Order",
				"create",
				[lab("Customer", "ACME")],
				[{ fieldname: "items", rows: [{ item_code: "A" }, { item_code: "B" }] }]
			),
		],
	},
	// ---- card actions (FROM_CARD): a field is the one its fieldname names
	{
		name: "card create, simple",
		serve: {},
		actions: [
			card("Sales Order", "create", [
				key("customer", "ACME"),
				key("remarks", "hello"),
				key("is_internal", 1),
				key("status", "Open"),
			]),
		],
	},
	{
		name: "card create with a table",
		serve: { rules: "answers" },
		actions: [
			card(
				"Sales Order",
				"create",
				[key("customer", "ACME"), key("transaction_date", "2026-12-08")],
				[
					{
						fieldname: "items",
						rows: [
							{ item_code: "A", qty: 3, rate: 2, brand: "Zed" },
							{ item_code: "B", qty: 1.5 },
						],
					},
				]
			),
		],
	},
	{
		name: "card update with the stored record overlaid",
		serve: { rules: "answers" },
		actions: [
			card(
				"Sales Order",
				"update",
				[key("status", "Open"), key("remarks", "changed")],
				[
					{
						fieldname: "items",
						rows: [
							{ name: "soi-1", qty: 9 },
							{ qty: 2, item_code: "D" },
						],
					},
				],
				{ name: "SO-1" }
			),
		],
	},
	{
		name: "card: two keys that look alike each keep their own field; an unknown and a table key are left out",
		serve: {},
		actions: [
			card("Memo", "create", [
				key("date", "2026-12-09"),
				key("posting_date", "2026-12-08"),
				key("subj", "guessed?"),
				key("subject", "S"),
				key("body", "text"),
			]),
			card("Sales Order", "create", [
				key("customer", "C"),
				key("items", "x"),
				key("deliv", "y"),
			]),
		],
	},
	{
		name: "card: a prompt-named create keeps its name",
		serve: {},
		actions: [card("Memo", "create", [key("name", "MEMO-9"), key("subject", "S")])],
	},
	{
		name: "card update, stored record unloadable: shown without it and marked, tables or not",
		serve: { doc: "throws" },
		actions: [
			card("Task", "update", [key("subject", "New")], [], { name: "TASK-1" }),
			card(
				"Sales Order",
				"update",
				[key("status", "Open")],
				[{ fieldname: "items", rows: [{ item_code: "A", qty: 1 }] }],
				{ name: "SO-1" }
			),
		],
	},
];

// ---- the fake server and the recorder

const clone = (v) => JSON.parse(JSON.stringify(v));

function fakeDeps(c, calls, held) {
	const serve = c.serve || {};
	let metaCall = 0;
	return {
		getDoctypeFormMeta: async (doctype) => {
			calls.push(["getDoctypeFormMeta", doctype]);
			const mode = (serve.metaSeq || [])[metaCall++] || serve.meta || "ok";
			if (mode === "throws") throw new Error("meta down");
			if (mode === "notok") return { ok: false };
			return clone(METAS[doctype]);
		},
		loadDocForEdit: async (doctype, name) => {
			calls.push(["loadDocForEdit", doctype, name]);
			if (serve.doc === "throws") throw new Error("not permitted");
			return clone(DOCS[`${doctype}/${name}`]);
		},
		ruleValues: async (doctype, table, links) => {
			calls.push(["ruleValues", doctype, table, clone(links)]);
			const mode = serve.rules || "none";
			if (mode === "throws") throw new Error("rules down");
			if (mode === "slow")
				return new Promise((resolve) => {
					held.push(() => resolve(clone(RULE_ANSWER)));
				});
			return mode === "answers" ? clone(RULE_ANSWER) : { ok: false };
		},
	};
}

// JSON-safe, with what JSON would hide made visible, and `__key` renumbered by first appearance.
function plain(value) {
	const keys = new Map();
	const walk = (v) => {
		if (v === undefined) return "$undefined";
		if (typeof v === "function") return "$function";
		if (typeof v === "symbol") return "$symbol";
		if (v === null || typeof v !== "object") return Number.isNaN(v) ? "$nan" : v;
		if (Array.isArray(v)) return v.map(walk);
		const out = {};
		for (const k of Object.keys(v)) {
			if (k === "__key") {
				if (!keys.has(v[k])) keys.set(v[k], `key#${keys.size + 1}`);
				out[k] = keys.get(v[k]);
			} else out[k] = walk(v[k]);
		}
		return out;
	};
	return walk(value);
}

// Builds one action. `live` is the model object itself (to watch a late write); `shown` is its
// JSON-safe copy at this moment.
async function attempt(build, action) {
	try {
		const model = await build(action);
		return model === undefined
			? { out: { undefined: true } }
			: { live: model, out: { model: plain(model) } };
	} catch (e) {
		return {
			out: { error: { message: e.message, jvUserMessage: e.jvUserMessage ?? "$undefined" } },
		};
	}
}

// `makeBuilder(deps)` -> a function `build(action)` (the builder under test). `timers.advance(ms)`
// moves fake time and lets promises settle. Returns { [case name]: record }.
export async function runCases(makeBuilder, timers) {
	const record = {};
	for (const c of CASES) {
		const calls = [];
		const held = [];
		const build = makeBuilder(fakeDeps(c, calls, held));
		const builds = [];
		for (const action of c.actions) {
			if (!c.slow) {
				builds.push((await attempt(build, action)).out);
				continue;
			}
			// The wait runs out with the answers still held, so the card is built without them;
			// then the answers land and the same model object is read again.
			const pending = attempt(build, action);
			await timers.advance(1500);
			const { live, out } = await pending;
			held.splice(0).forEach((answer) => answer());
			await timers.advance(0);
			builds.push({ built: out, afterLateAnswer: live ? plain(live) : "$none" });
		}
		record[c.name] = { builds, calls: plain(calls) };
	}
	return record;
}
