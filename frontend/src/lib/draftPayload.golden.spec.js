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

// LCV source amounts are intentionally partial; the native readonly column must
// survive CREATE alongside the receipt's stable child-row reference.
describe("landed-cost create payload", () => {
	it("preserves explicit partial vendor cost and receipt keys through the real card builders", () => {
		const vendor = buildDraftTable({
			fieldname: "vendor_invoices",
			spec: {
				child_doctype: "Landed Cost Vendor Invoice",
				columns: [
					F("vendor_invoice", "Link", { options: "Purchase Invoice" }),
					F("amount", "Currency", { read_only: 1 }),
				],
			},
			verb: "create",
			proposed: [{ vendor_invoice: "PI-FREIGHT", amount: 30 }],
		});
		const items = buildDraftTable({
			fieldname: "items",
			spec: {
				child_doctype: "Landed Cost Item",
				columns: [
					F("item_code", "Link", { options: "Item" }),
					F("applicable_charges", "Currency"),
				],
				form_fields: [
					F("purchase_receipt_item", "Data", { read_only: 1 }),
					F("receipt_document", "Data", { read_only: 1 }),
					F("receipt_document_type", "Data", { read_only: 1 }),
				],
			},
			verb: "create",
			proposed: [
				{
					item_code: "STOCK-A",
					applicable_charges: 30,
					purchase_receipt_item: "native-pr-row",
					receipt_document: "PR-IMPORT",
					receipt_document_type: "Purchase Receipt",
				},
			],
		});
		const result = draftValues({ verb: "create", fields: [], tables: [vendor, items] });
		expect(result.vendor_invoices).toEqual([{ vendor_invoice: "PI-FREIGHT", amount: 30 }]);
		expect(result.items).toEqual([
			{
				item_code: "STOCK-A",
				applicable_charges: 30,
				purchase_receipt_item: "native-pr-row",
				receipt_document: "PR-IMPORT",
				receipt_document_type: "Purchase Receipt",
			},
		]);
	});
});
