// Golden Apply payloads for the record card's child tables, built through the real
// buildDraftTable + draftValues. The expected values were recorded before any review fix
// to the row form, so a later change to how the card builds rows (hidden rule fields,
// row keys, preview helpers) is proven not to change what Apply sends.
import { describe, expect, it } from "vitest";
import { draftValues } from "./draftApply";
import { buildDraftTable } from "./draftTables";

const F = (fieldname, fieldtype = "Data", extra = {}) => ({
	fieldname,
	label: fieldname,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});

// Sales Order Item-like meta: a grid of five columns, the rest only in the row form.
const spec = {
	label: "Items",
	child_doctype: "Sales Order Item",
	columns: [
		F("item_code", "Link", { options: "Item", reqd: 1 }),
		F("qty", "Float", { reqd: 1 }),
		F("rate", "Currency"),
		F("amount", "Currency", { read_only: 1 }),
		F("delivery_date", "Date"),
	],
	extra_columns: [
		F("discount_percentage", "Percent"),
		F("warehouse", "Link", { options: "Warehouse" }),
		F("is_free_item", "Check"),
		F("description", "Small Text"),
	],
	form_fields: [
		F("item_code", "Link", { options: "Item", reqd: 1 }),
		F("qty", "Float", { reqd: 1 }),
		F("rate", "Currency"),
		F("amount", "Currency", { read_only: 1 }),
		F("delivery_date", "Date"),
		{ fieldname: "", fieldtype: "Section Break", label: "Details" },
		F("discount_percentage", "Percent"),
		F("warehouse", "Link", { options: "Warehouse" }),
		F("is_free_item", "Check"),
		F("description", "Small Text"),
	],
};

const savedRows = () => [
	{
		name: "r1",
		item_code: "ITEM-A",
		qty: 2,
		rate: 100,
		amount: 200,
		delivery_date: "2026-11-01",
		discount_percentage: 10,
		warehouse: "Stores - X",
		is_free_item: 0,
		description: "First item",
	},
	{
		name: "r2",
		item_code: "ITEM-B",
		qty: 1,
		rate: 50,
		amount: 50,
		delivery_date: "2026-11-02",
		discount_percentage: 0,
		warehouse: "Stores - X",
		is_free_item: 1,
		description: "Second item",
	},
];

const mainField = { fieldname: "customer", value: "C1", orig: "C1", control: "text" };

describe("v15 landed-cost charge provenance", () => {
	const source = "custom_jarvis_source_invoice";
	const chargeSpec = {
		label: "Charges",
		child_doctype: "Landed Cost Taxes and Charges",
		columns: [F("expense_account", "Link"), F("amount", "Currency")],
		extra_columns: [F(source, "Link", { options: "Purchase Invoice" })],
		form_fields: [F(source, "Link", { options: "Purchase Invoice" })],
	};
	it("keeps a preview's invoice link in the confirmed create payload", () => {
		const row = { expense_account: "Freight - X", amount: 120, [source]: "PINV-TEST" };
		const table = buildDraftTable({
			fieldname: "taxes",
			spec: chargeSpec,
			required: true,
			proposed: [row],
			verb: "create",
		});
		expect(draftValues({ verb: "create", fields: [], tables: [table] })).toEqual({
			taxes: [row],
		});
	});
	it("updates the saved row by name without clearing its source link", () => {
		const row = {
			name: "charge-1",
			expense_account: "Freight - X",
			amount: 120,
			[source]: "PINV-TEST",
		};
		const table = buildDraftTable({
			fieldname: "taxes",
			spec: chargeSpec,
			saved: [row],
			verb: "update",
		});
		table.rows[0].amount = "100";
		expect(draftValues({ verb: "update", fields: [], tables: [table] })).toEqual({
			taxes: [{ name: "charge-1", amount: 100 }],
		});
	});
});

function payload(verb, { proposed, saved, mutate, withSpec = spec } = {}) {
	const t = buildDraftTable({
		fieldname: "items",
		spec: withSpec,
		required: verb === "create",
		proposed,
		saved,
		verb,
	});
	if (mutate) mutate(t);
	return draftValues({
		verb,
		fields: [{ ...mainField }],
		tables: t ? [t] : [],
	});
}

describe("golden Apply payloads (recorded before the review fixes)", () => {
	it("create: every filled row, every non-blank cell, Check as 0/1, number as number", () => {
		const values = payload("create", {
			proposed: [
				{
					item_code: "ITEM-A",
					qty: "3",
					rate: 25.5,
					delivery_date: "2026-12-01",
					discount_percentage: "5",
					warehouse: "Stores - X",
					is_free_item: "Yes",
				},
				{ item_code: "ITEM-B", qty: 1, is_free_item: false },
			],
		});
		expect(JSON.parse(JSON.stringify(values))).toEqual({
			customer: "C1",
			items: [
				{
					item_code: "ITEM-A",
					qty: 3,
					rate: 25.5,
					delivery_date: "2026-12-01",
					discount_percentage: 5,
					warehouse: "Stores - X",
					is_free_item: 1,
				},
				{ item_code: "ITEM-B", qty: 1, is_free_item: 0 },
			],
		});
	});

	it("update, nothing changed: no table key at all", () => {
		const values = payload("update", { saved: savedRows() });
		expect(values).toEqual({});
		expect("items" in values).toBe(false);
	});

	it("update, one cell edited: that row is {name, cell} only", () => {
		const values = payload("update", {
			saved: savedRows(),
			mutate: (t) => {
				t.rows[0].warehouse = "Finished Goods - X";
			},
		});
		expect(values).toEqual({
			items: [{ name: "r1", warehouse: "Finished Goods - X" }, { name: "r2" }],
		});
	});

	it("update, a proposal that changes one cell outside the grid", () => {
		const values = payload("update", {
			saved: savedRows(),
			proposed: [{ name: "r1", discount_percentage: 15 }, { name: "r2" }],
		});
		expect(values).toEqual({
			items: [{ name: "r1", discount_percentage: 15 }, { name: "r2" }],
		});
	});

	it("update, a row removed: only the kept row is sent, by name", () => {
		const values = payload("update", {
			saved: savedRows(),
			mutate: (t) => {
				t.rows.splice(0, 1);
			},
		});
		expect(values).toEqual({ items: [{ name: "r2" }] });
	});

	it("update, a cleared cell goes out as null; a new row sends its filled cells", () => {
		const values = payload("update", {
			saved: savedRows(),
			mutate: (t) => {
				t.rows[1].description = "";
				t.rows.push({
					...t.rows[1],
					__name: undefined,
					item_code: "ITEM-C",
					qty: "4",
					description: "",
					warehouse: "",
					is_free_item: "0",
					delivery_date: "",
					rate: "",
					amount: "",
					discount_percentage: "",
				});
				delete t.rows[2].__name;
			},
		});
		expect(JSON.parse(JSON.stringify(values))).toEqual({
			items: [
				{ name: "r1" },
				{ name: "r2", description: null },
				{ item_code: "ITEM-C", qty: 4, is_free_item: 0 },
			],
		});
	});

	it("rows carry a __key that no payload carries, create or update", () => {
		for (const verb of ["create", "update"]) {
			let seen;
			const values = payload(verb, {
				proposed: [{ item_code: "ITEM-N", qty: 2, warehouse: "W" }],
				saved: verb === "update" ? savedRows() : undefined,
				mutate: (t) => {
					t.rows.push({ ...t.rows[0], __name: undefined, item_code: "ADDED", qty: "5" });
					seen = t.rows.map((r) => r.__key);
				},
			});
			expect(seen.every((k) => Number.isInteger(k))).toBe(true);
			expect(JSON.stringify(values)).toContain("ADDED"); // the rows are in the payload
			expect(JSON.stringify(values)).not.toContain("__key");
			expect(JSON.stringify(values)).not.toMatch(/"__/);
		}
	});

	it("no serialised key starts with __", () => {
		const values = payload("update", {
			saved: savedRows(),
			mutate: (t) => {
				t.rows[0].qty = "9";
			},
		});
		expect(JSON.stringify(values)).not.toMatch(/"__/);
	});
});

// the hidden fields the row form's rules read (rule_fields, __rule) are display
// state only. Built with them, every case above sends exactly what it sends without.
describe("golden Apply payloads are unchanged by rule_fields and __rule", () => {
	const ruleSpec = {
		...spec,
		rule_fields: [
			{
				fieldname: "is_fixed_asset",
				fieldtype: "Check",
				default: "0",
				fetch_from: "item_code.is_fixed_asset",
			},
			{ fieldname: "is_stock_item", fieldtype: "Check", default: "1", fetch_from: "" },
			{ fieldname: "item_group", fieldtype: "Data", default: "", fetch_from: "" },
		],
	};
	// saved rows that also hold the rule fields, as load_doc returns them
	const savedWithRules = () =>
		savedRows().map((r, i) => ({
			...r,
			is_fixed_asset: i,
			is_stock_item: 1,
			item_group: "Products",
		}));
	const cases = {
		"a create": [
			"create",
			{
				proposed: [
					{ item_code: "ITEM-A", qty: "3", is_free_item: "Yes" },
					{ item_code: "ITEM-B", qty: 1, is_free_item: false },
				],
			},
		],
		"an unchanged update": ["update", {}],
		"a one-cell update": [
			"update",
			{
				mutate: (t) => {
					t.rows[0].warehouse = "Finished Goods - X";
				},
			},
		],
		"a proposal outside the grid": [
			"update",
			{ proposed: [{ name: "r1", discount_percentage: 15 }, { name: "r2" }] },
		],
		"a changed item_code": [
			"update",
			{ proposed: [{ name: "r1", item_code: "ITEM-Z" }, { name: "r2" }] },
		],
		"a removed row": [
			"update",
			{
				mutate: (t) => {
					t.rows.splice(0, 1);
				},
			},
		],
	};
	for (const [name, [verb, opts]] of Object.entries(cases)) {
		it(`${name}: same payload with and without rule fields, no __ keys`, () => {
			const saved = verb === "update" ? savedWithRules() : undefined;
			const plain = payload(verb, { ...opts, saved: saved && savedRows() });
			const ruled = payload(verb, { ...opts, saved, withSpec: ruleSpec });
			expect(ruled).toEqual(plain);
			expect(JSON.stringify(ruled)).not.toMatch(/"__/);
			expect(JSON.stringify(ruled)).not.toMatch(/is_fixed_asset|is_stock_item|item_group/);
		});
	}

	it("editing a rule value on the row changes nothing that is sent", () => {
		const values = payload("update", {
			saved: savedWithRules(),
			withSpec: ruleSpec,
			mutate: (t) => {
				t.rows[0].__rule.is_fixed_asset = 1;
				t.rows[1].__rule = { is_fixed_asset: 0, is_stock_item: 0, item_group: "x" };
			},
		});
		expect(values).toEqual({});
	});

	it("a create row's default rule values are not sent", () => {
		const values = payload("create", {
			proposed: [{ item_code: "ITEM-A", qty: 1 }],
			withSpec: ruleSpec,
		});
		expect(values).toEqual({ customer: "C1", items: [{ item_code: "ITEM-A", qty: 1 }] });
	});
});
