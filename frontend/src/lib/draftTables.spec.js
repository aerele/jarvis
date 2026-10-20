import { describe, expect, it, vi } from "vitest";
import { allRowFields, coerceRow } from "./draftApply";
import { typedRow } from "./rowForm";
import {
	applyAutoOpen,
	applyRuleValues,
	autoOpenRows,
	blankRow,
	buildDraftTable,
	changedSummary,
	markMissingRows,
	openChangedRows,
	parentDefaults,
	rowChangeSets,
	ruleFetchLinks,
	savedRowFor,
	toggleLabel,
	toggleParts,
} from "./draftTables";

const F = (fieldname, fieldtype = "Data", extra = {}) => ({
	fieldname,
	label: fieldname,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});
const spec = {
	label: "Items",
	child_doctype: "Sales Order Item",
	columns: [F("item_code", "Link"), F("qty", "Float")],
	extra_columns: [F("discount_percentage", "Percent"), F("warehouse", "Link")],
	form_fields: [
		F("item_code", "Link"),
		F("qty", "Float"),
		{ fieldname: "", fieldtype: "Section Break", label: "More" },
		F("discount_percentage", "Percent"),
		F("warehouse", "Link"),
	],
};

describe("buildDraftTable: one child table of the card", () => {
	it("is left out when not required, not proposed and empty", () => {
		expect(
			buildDraftTable({ fieldname: "items", spec, required: false, verb: "create" })
		).toBeNull();
	});

	it("a saved row carries its values outside the grid (the reported bug)", () => {
		const t = buildDraftTable({
			fieldname: "items",
			spec,
			saved: [
				{
					name: "r1",
					item_code: "I",
					qty: 2,
					discount_percentage: 5,
					warehouse: "Stores",
				},
			],
			verb: "update",
		});
		expect(t.columns.map((c) => c.fieldname)).toEqual(["item_code", "qty"]);
		expect(t.formFields).toBe(spec.form_fields);
		expect(t.rows[0]).toMatchObject({
			__name: "r1",
			discount_percentage: "5",
			warehouse: "Stores",
		});
		expect(JSON.parse(t.origJson)[0].name).toBe("r1");
	});

	it("a key the agent proposed outside the grid becomes a typed column (#655)", () => {
		const t = buildDraftTable({
			fieldname: "items",
			spec,
			proposed: [{ item_code: "I", discount_percentage: 10 }],
			verb: "create",
		});
		const col = t.columns.find((c) => c.fieldname === "discount_percentage");
		expect(col.fieldtype).toBe("Percent");
		expect(t.rows[0].warehouse).toBe(""); // every form field is on the row
	});

	it("grid columns come from the agent's raw keys, never from the saved row", () => {
		// The agent proposes only qty over a saved row that also holds extra_columns
		// fields and a hidden field: the overlay must not turn them into grid columns.
		const gridSpec = { ...spec, columns: [F("item_code", "Link")] };
		const saved = [
			{
				name: "r1",
				item_code: "I",
				qty: 1,
				discount_percentage: 5,
				warehouse: "Stores",
				description: "d",
				is_fixed_asset: 1,
			},
		];
		const build = (extra) =>
			buildDraftTable({
				fieldname: "items",
				spec: gridSpec,
				proposed: [{ name: "r1", qty: 2 }],
				saved: saved.map((r) => ({ ...r, ...extra })),
				verb: "update",
			});
		const t = build({});
		expect(t.columns.map((c) => c.fieldname)).toEqual(["item_code", "qty"]);
		// identical whether or not the saved rows carry more keys
		expect(build({ cost_center: "CC", is_stock_item: 1 }).columns).toEqual(t.columns);
		// the overlaid row still carries the saved values for the row form
		expect(t.rows[0]).toMatchObject({ __name: "r1", qty: "2", warehouse: "Stores" });
	});

	it("a name-only proposal adds no grid column", () => {
		const t = buildDraftTable({
			fieldname: "items",
			spec,
			proposed: [{ name: "r1" }],
			saved: [
				{ name: "r1", item_code: "I", qty: 1, warehouse: "Stores", is_fixed_asset: 1 },
			],
			verb: "update",
		});
		expect(t.columns.map((c) => c.fieldname)).toEqual(["item_code", "qty"]);
	});

	it("an empty required table starts with one blank row with every form field", () => {
		const t = buildDraftTable({ fieldname: "items", spec, required: true, verb: "create" });
		expect(t.rows).toEqual([
			{
				item_code: "",
				qty: "",
				discount_percentage: "",
				warehouse: "",
				__key: expect.any(Number),
			},
		]);
		const { __key, ...rest } = blankRow(t);
		expect(__key).toEqual(expect.any(Number));
		expect(rest).toEqual({ item_code: "", qty: "", discount_percentage: "", warehouse: "" });
	});

	it("works without form fields (an older server): rows carry the columns only", () => {
		const t = buildDraftTable({
			fieldname: "items",
			spec: { ...spec, form_fields: undefined },
			required: true,
			verb: "create",
		});
		expect(t.formFields).toEqual([]);
		expect(Object.keys(t.rows[0]).filter((k) => !k.startsWith("__"))).toEqual([
			"item_code",
			"qty",
		]);
	});
});

describe("savedRowFor / parentDefaults / markMissingRows", () => {
	const t = buildDraftTable({
		fieldname: "items",
		spec,
		saved: [
			{ name: "r1", item_code: "I", qty: 2 },
			{ name: "r2", item_code: "J", qty: 1 },
		],
		verb: "update",
	});

	it("finds a row's saved row by name, and none for a new row", () => {
		expect(savedRowFor(t, t.rows[1]).item_code).toBe("J");
		expect(savedRowFor(t, { item_code: "new" })).toBeNull();
	});

	it("takes the plain defaults of the record's own fields, never a table", () => {
		expect(
			parentDefaults([
				{ fieldname: "update_stock", fieldtype: "Check", default: "1" },
				{ fieldname: "is_return", fieldtype: "Check", default: "0" },
				{ fieldname: "currency", fieldtype: "Link", default: "INR" },
				{ fieldname: "items", fieldtype: "Table", default: "x" },
				{ fieldname: "title", fieldtype: "Data", default: "" },
			])
		).toEqual({ update_stock: 1, is_return: 0, currency: "INR" }); // a "0" is falsy, as in Desk
	});

	it("counts only the rows the save sent: a blank new row is skipped", () => {
		const create = buildDraftTable({
			fieldname: "items",
			spec,
			proposed: [{ item_code: "" }, { item_code: "A" }, { item_code: "B" }],
			verb: "create",
		});
		create.rows[0] = { item_code: "", qty: "", discount_percentage: "", warehouse: "" };
		markMissingRows({ verb: "create", tables: [create] }, [
			{ parentfield: "items", idx: 2, fieldname: "warehouse" },
		]);
		expect(create.rows[2].__missing).toEqual(["warehouse"]); // the 2nd SENT row is panel row 3
		expect(create.rows[1].__missing).toBeUndefined();
	});

	it("marks and opens the rows a failed save names, and clears old marks", () => {
		const model = { verb: "update", tables: [t] };
		t.rows[0].__missing = ["stale"];
		const n = markMissingRows(model, [
			{ parentfield: "items", idx: 2, fieldname: "warehouse" },
			{ fieldname: "customer" }, // a main field: not a row
			{ parentfield: "items", idx: 9, fieldname: "x" }, // no such row
		]);
		expect(n).toBe(1);
		expect(t.rows[0].__missing).toBeUndefined();
		expect(t.rows[1].__missing).toEqual(["warehouse"]);
		expect(t.rows[1].__open).toBe(true);
	});
});

describe("markMissingRows skips an entry with no usable field name", () => {
	const make = () => {
		const t = buildDraftTable({
			fieldname: "items",
			spec,
			saved: [{ name: "r1", item_code: "I", qty: 2 }],
			verb: "update",
		});
		return { model: { verb: "update", tables: [t] }, t };
	};
	it("an empty, missing or non-string name marks nothing and opens nothing", () => {
		for (const fieldname of ["", "  ", undefined, null, 7, { a: 1 }, ["x"]]) {
			const { model, t } = make();
			expect(markMissingRows(model, [{ parentfield: "items", idx: 1, fieldname }])).toBe(0);
			expect(t.rows[0].__missing).toBeUndefined();
			expect(t.rows[0].__open).toBeUndefined();
		}
	});
	it("a good name beside a bad one on the same row is still marked, once", () => {
		const { model, t } = make();
		const n = markMissingRows(model, [
			{ parentfield: "items", idx: 1, fieldname: "" },
			{ parentfield: "items", idx: 1, fieldname: "warehouse" },
		]);
		expect(n).toBe(1);
		expect(t.rows[0].__missing).toEqual(["warehouse"]);
	});
});

describe("buildDraftTable: the hidden fields the row form's rules read (rule_fields)", () => {
	const flag = {
		fieldname: "is_fixed_asset",
		fieldtype: "Check",
		default: "0",
		fetch_from: "item_code.is_fixed_asset",
	};
	const withRules = {
		...spec,
		rule_fields: [
			flag,
			{ ...flag, fieldname: "stock_kind", fieldtype: "Data", default: "", fetch_from: "" },
		],
	};
	const saved = [
		{ name: "r1", item_code: "A", qty: 1, is_fixed_asset: 1, stock_kind: "Stock" },
		{ name: "r2", item_code: "B", qty: 1, is_fixed_asset: 0, stock_kind: "" },
	];
	const build = (over = {}) =>
		buildDraftTable({ fieldname: "items", spec: withRules, saved, verb: "update", ...over });

	it("keeps table.ruleFields; a spec without them (an older server) has none", () => {
		expect(build().ruleFields).toEqual(withRules.rule_fields);
		expect(
			buildDraftTable({ fieldname: "items", spec, saved, verb: "update" }).ruleFields
		).toEqual([]);
	});

	it("a saved row carries the saved values under __rule, plus the saved link values", () => {
		const t = build();
		expect(t.rows[0].__rule).toEqual({ is_fixed_asset: 1, stock_kind: "Stock" });
		expect(t.rows[1].__rule).toEqual({ is_fixed_asset: 0, stock_kind: "" });
		expect(t.rows[0].__ruleSrc).toEqual({ item_code: "A" });
	});

	it("a new row and a blank row carry the plain defaults", () => {
		const t = build({
			proposed: [{ name: "r1" }, { item_code: "C", qty: 3 }],
		});
		expect(t.rows[1].__rule).toEqual({ is_fixed_asset: "0", stock_kind: "" });
		expect(t.rows[1].__ruleSrc).toBeUndefined(); // nothing saved to compare with
		expect(blankRow(t).__rule).toEqual({ is_fixed_asset: "0", stock_kind: "" });
		const empty = buildDraftTable({
			fieldname: "items",
			spec: withRules,
			required: true,
			verb: "create",
		});
		expect(empty.rows[0].__rule).toEqual({ is_fixed_asset: "0", stock_kind: "" });
	});

	it("ruleFetchLinks asks for the links whose rule values are neither fetched nor saved", () => {
		const t = build({
			proposed: [{ name: "r1" }, { item_code: "C", qty: 3 }, { item_code: "C" }],
		});
		// r1 keeps its saved item A, so its saved value stands; the new rows need C, once
		expect(ruleFetchLinks(t)).toEqual({ item_code: ["C"] });
		t.rows[0].item_code = "D"; // moved off the saved item
		expect(ruleFetchLinks(t)).toEqual({ item_code: ["D", "C"] });
	});

	it("a fetched value is the row's rule value, read as current until the link changes", () => {
		const t = buildDraftTable({
			fieldname: "items",
			spec: withRules,
			proposed: [{ item_code: "C", qty: 1 }],
			verb: "create",
		});
		const flagOf = (row) =>
			typedRow(allRowFields(t), row, { ruleFields: t.ruleFields }).is_fixed_asset;
		expect(flagOf(t.rows[0])).toBe(0); // the default, before the fetch
		applyRuleValues(t, { item_code: { C: { is_fixed_asset: 1 }, Z: { is_fixed_asset: 1 } } });
		expect(t.rows[0].__rule.is_fixed_asset).toBe(1);
		expect(t.rows[0].__ruleSrc).toEqual({ item_code: "C" });
		expect(flagOf(t.rows[0])).toBe(1);
		expect(ruleFetchLinks(t)).toEqual({});
		t.rows[0].item_code = "E"; // another item, not fetched yet: the default again
		expect(flagOf(t.rows[0])).toBe(0);
		expect(ruleFetchLinks(t)).toEqual({ item_code: ["E"] });
	});

	it("rule values never reach the save payload", () => {
		const t = buildDraftTable({
			fieldname: "items",
			spec: withRules,
			proposed: [{ item_code: "C", qty: 1 }],
			verb: "create",
		});
		applyRuleValues(t, { item_code: { C: { is_fixed_asset: 1 } } });
		expect(coerceRow(t, t.rows[0], "create")).not.toHaveProperty("is_fixed_asset");
	});

	it("a value the agent proposed for a rule field is used", () => {
		const t = build({ proposed: [{ name: "r1", stock_kind: "Service" }], saved });
		expect(t.rows[0].__rule.stock_kind).toBe("Service");
	});

	it("a changed source link makes a fetch_from field read its default", () => {
		const t = build({ proposed: [{ name: "r1", item_code: "Z" }, { name: "r2" }] });
		expect(t.rows[0].__rule).toEqual({ is_fixed_asset: "0", stock_kind: "Stock" }); // only the fetched one
		expect(t.rows[1].__rule.is_fixed_asset).toBe(0); // unchanged link: the saved value
	});

	it("rule fields never join the row's fields, columns or sent payload (invariant)", () => {
		const t = build({ proposed: [{ name: "r1", qty: 5 }] });
		const names = allRowFields(t).map((f) => f.fieldname);
		expect(names).not.toContain("is_fixed_asset");
		expect(names).not.toContain("stock_kind");
		expect(t.columns.map((c) => c.fieldname)).not.toContain("is_fixed_asset");
		const sent = coerceRow(t, t.rows[0], "update");
		expect(
			Object.keys(sent).some(
				(k) => k.startsWith("__") || k in { is_fixed_asset: 1, stock_kind: 1 }
			)
		).toBe(false);
		const created = coerceRow(
			buildDraftTable({
				fieldname: "items",
				spec: withRules,
				required: true,
				proposed: [{ item_code: "C" }],
				verb: "create",
			}),
			{ item_code: "C", __rule: { is_fixed_asset: "0" } },
			"create"
		);
		expect(created).toEqual({ item_code: "C" });
	});
});

// a 20-row update where the agent changes fields outside the grid.
describe("changed rows outside the grid", () => {
	const wh = F("warehouse", "Link", { label: "Warehouse" });
	const disc = F("discount_percentage", "Percent", { label: "Discount" });
	const big = {
		label: "Items",
		child_doctype: "Sales Order Item",
		columns: [F("item_code", "Link"), F("qty", "Float")],
		extra_columns: [wh, disc],
		form_fields: [F("item_code", "Link"), F("qty", "Float"), wh, disc],
	};
	const saved = Array.from({ length: 20 }, (_, i) => ({
		name: `r${i + 1}`,
		item_code: `I${i + 1}`,
		qty: 1,
		warehouse: "A",
		discount_percentage: 10,
	}));
	// the agent's changes: {rowNumber: {field: value}}; every other row is left as saved
	const build = (changes) =>
		buildDraftTable({
			fieldname: "items",
			spec: big,
			saved,
			verb: "update",
			proposed: Object.entries(changes).map(([n, c]) => ({ name: `r${n}`, ...c })),
		});
	// a proposal REPLACES the rows, so keep the rest by naming them
	const withRest = (changes) => {
		const t = build(Object.fromEntries(saved.map((_, i) => [i + 1, changes[i + 1] || {}])));
		return t;
	};

	it("rows 7 and 12 changed: both auto-open, and the summary names them", () => {
		const t = withRest({ 7: { warehouse: "B" }, 12: { warehouse: "B" } });
		const sets = rowChangeSets(t);
		expect(autoOpenRows(t, sets)).toEqual([6, 11]);
		const sum = changedSummary(t, sets);
		expect(sum.text).toBe("Changed outside the grid: rows 7, 12");
		expect(sum.rows).toEqual([6, 11]);
		expect(sum.grouped).toBe(false);
	});
	it("the summary gives the card its lead words and the grouped button's label", () => {
		const few = withRest({ 7: { warehouse: "B" }, 12: { warehouse: "B" } });
		const s = changedSummary(few, rowChangeSets(few));
		expect(s.lead).toBe("Changed outside the grid: rows"); // the numbers follow as buttons
		expect(s.text).toBe(`${s.lead} 7, 12`);
		expect(s.openLabel).toBeUndefined();
		const all = withRest(Object.fromEntries(saved.map((_, i) => [i + 1, { warehouse: "B" }])));
		const g = changedSummary(all, rowChangeSets(all));
		expect(g.openLabel).toBe("Open 20 changed rows");
		const four = withRest({
			1: { warehouse: "B" },
			2: { warehouse: "B" },
			3: { warehouse: "B" },
			4: { warehouse: "B" },
		});
		expect(changedSummary(four, rowChangeSets(four)).openLabel).toBe("Open 4 changed rows");
	});
	it("one row reads 'row 7'", () => {
		const t = withRest({ 7: { warehouse: "B" } });
		expect(changedSummary(t, rowChangeSets(t)).text).toBe("Changed outside the grid: row 7");
	});
	it("all 20 rows changed: nothing auto-opens, the summary groups by field and offers the button", () => {
		const all = Object.fromEntries(saved.map((_, i) => [i + 1, { warehouse: "B" }]));
		const t = withRest(all);
		const sets = rowChangeSets(t);
		expect(autoOpenRows(t, sets)).toEqual([]);
		const sum = changedSummary(t, sets);
		expect(sum.text).toBe("Warehouse changed on 20 rows");
		expect(sum.grouped).toBe(true);
		expect(sum.rows).toHaveLength(20);
	});
	it("more than 3 rows groups by field, the busiest first", () => {
		// warehouse comes first in the row's field order but is the less common change
		const ch = { 1: { warehouse: "B" }, 2: { warehouse: "B" } };
		for (let n = 1; n <= 5; n++) ch[n] = { ...ch[n], discount_percentage: 5 };
		const t = withRest(ch);
		expect(changedSummary(t, rowChangeSets(t)).text).toBe(
			"Discount changed on 5 rows; Warehouse on 2 rows"
		);
		// one row of a field reads "1 row"
		const one = {};
		for (let n = 1; n <= 4; n++) one[n] = { warehouse: "B" };
		one[1].discount_percentage = 0; // 10 -> 0
		const u = withRest(one);
		expect(changedSummary(u, rowChangeSets(u)).text).toBe(
			"Warehouse changed on 4 rows; Discount on 1 row"
		);
	});
	it("exactly 3 rows still names them and auto-opens; 4 does not", () => {
		const three = withRest({
			1: { warehouse: "B" },
			2: { warehouse: "B" },
			3: { warehouse: "B" },
		});
		expect(autoOpenRows(three, rowChangeSets(three))).toEqual([0, 1, 2]);
		const four = withRest({
			1: { warehouse: "B" },
			2: { warehouse: "B" },
			3: { warehouse: "B" },
			4: { warehouse: "B" },
		});
		expect(autoOpenRows(four, rowChangeSets(four))).toEqual([]);
	});
	it("a discount going 10 to 0 counts as a change", () => {
		const t = withRest({ 3: { discount_percentage: 0 } });
		expect(changedSummary(t, rowChangeSets(t)).rows).toEqual([2]);
	});
	it("a change inside the grid only, an unchanged update and a create report nothing", () => {
		const grid = withRest({ 4: { qty: 9 } });
		expect(changedSummary(grid, rowChangeSets(grid))).toBeNull();
		expect(autoOpenRows(grid, rowChangeSets(grid))).toEqual([]);
		const none = withRest({});
		expect(changedSummary(none, rowChangeSets(none))).toBeNull();
		const create = buildDraftTable({
			fieldname: "items",
			spec: big,
			required: true,
			proposed: [{ item_code: "I", warehouse: "B" }],
			verb: "create",
		});
		expect(changedSummary(create, rowChangeSets(create))).toBeNull();
	});
	it("a new row among saved ones is not a 'changed saved row'", () => {
		const t = withRest({ 2: { warehouse: "B" } });
		t.rows.push({ item_code: "NEW", qty: "1", warehouse: "C", discount_percentage: "" });
		expect(changedSummary(t, rowChangeSets(t)).rows).toEqual([1]);
	});

	it("a table with no row form (formFields) reports nothing outside the grid", () => {
		const t = withRest({ 7: { warehouse: "B" }, 12: { warehouse: "B" } });
		t.formFields = [];
		const sets = rowChangeSets(t);
		expect(autoOpenRows(t, sets)).toEqual([]);
		expect(changedSummary(t, sets)).toBeNull();
		const m = { verb: "update", doctype: "Sales Order", docName: "SO-1", tables: [t] };
		expect(applyAutoOpen(m, null)).toEqual([]);
		expect(t.rows.some((r) => r.__open)).toBe(false);
		// the PWA builds a table without the list at all
		delete t.formFields;
		expect(changedSummary(t, rowChangeSets(t))).toBeNull();
	});

	describe("first build, or a changed re-emit; the same re-emit opens nothing", () => {
		const modelOf = (t, over = {}) => ({
			verb: "update",
			doctype: "Sales Order",
			docName: "SO-1",
			tables: [t],
			...over,
		});
		it("opens the changed rows on the first build, with no focus side effects", () => {
			const t = withRest({ 7: { warehouse: "B" }, 12: { warehouse: "B" } });
			const before = document.activeElement;
			const focus = vi.spyOn(HTMLElement.prototype, "focus");
			const opened = applyAutoOpen(modelOf(t), null);
			expect(opened).toEqual([{ table: 0, rows: [6, 11] }]);
			expect(t.rows.map((r, i) => (r.__open ? i : -1)).filter((i) => i >= 0)).toEqual([
				6, 11,
			]);
			expect(focus).not.toHaveBeenCalled();
			expect(document.activeElement).toBe(before);
			focus.mockRestore();
		});
		// A model as the card has it once built: auto-open has run and stamped its key.
		const shown = (t, over) => {
			const m = modelOf(t, over);
			applyAutoOpen(m, null);
			return m;
		};
		it("a re-emit of the same changes (same record, panel already open) opens nothing", () => {
			const same = { 7: { warehouse: "B" }, 12: { warehouse: "B" } };
			const prev = shown(withRest(same));
			const t = withRest(same);
			expect(applyAutoOpen(modelOf(t), prev)).toEqual([]);
			expect(t.rows.some((r) => r.__open)).toBe(false);
		});
		it("the same value A to B then A to C on one row opens that row again", () => {
			const prev = shown(withRest({ 7: { warehouse: "B" } }));
			const t = withRest({ 7: { warehouse: "C" } });
			expect(applyAutoOpen(modelOf(t), prev)).toEqual([{ table: 0, rows: [6] }]);
			expect(t.rows[6].__open).toBe(true);
		});
		it("a different field on the same row opens it again", () => {
			const prev = shown(withRest({ 7: { warehouse: "B" } }));
			const t = withRest({ 7: { discount_percentage: 5 } });
			expect(applyAutoOpen(modelOf(t), prev)).toEqual([{ table: 0, rows: [6] }]);
		});
		it("the identical re-emit after the user edited the open panel opens nothing", () => {
			const same = { 7: { warehouse: "B" } };
			const prev = shown(withRest(same));
			// the user types over the agent's value, and over another field
			prev.tables[0].rows[6].warehouse = "Z";
			prev.tables[0].rows[6].discount_percentage = "99";
			prev.tables[0].rows[2].warehouse = "Q";
			const t = withRest(same);
			expect(applyAutoOpen(modelOf(t), prev)).toEqual([]);
			// and the user putting a row back to the saved value changes nothing either
			prev.tables[0].rows[6].warehouse = "A";
			expect(applyAutoOpen(modelOf(withRest(same)), prev)).toEqual([]);
		});
		it("a previous panel with no stamp counts as a different proposal", () => {
			const prev = modelOf(withRest({ 7: { warehouse: "B" } }));
			const t = withRest({ 7: { warehouse: "B" } });
			expect(applyAutoOpen(modelOf(t), prev)).toEqual([{ table: 0, rows: [6] }]);
		});
		it("a different change to the same record auto-opens by the normal rule", () => {
			const prev = shown(withRest({ 7: { warehouse: "B" }, 12: { warehouse: "B" } }));
			const t = withRest({ 3: { warehouse: "B" } });
			expect(applyAutoOpen(modelOf(t), prev)).toEqual([{ table: 0, rows: [2] }]);
			expect(t.rows[2].__open).toBe(true);
			// more than 3 changed rows still opens nothing
			const many = withRest({
				1: { warehouse: "B" },
				2: { warehouse: "B" },
				3: { warehouse: "B" },
				4: { warehouse: "B" },
			});
			expect(applyAutoOpen(modelOf(many), prev)).toEqual([]);
		});
		it("a different record, or a create, is a first build", () => {
			const prev = shown(withRest({}), { docName: "SO-2" });
			const t = withRest({ 7: { warehouse: "B" } });
			expect(applyAutoOpen(modelOf(t), prev)).toHaveLength(1);
			const c = withRest({ 7: { warehouse: "B" } });
			expect(applyAutoOpen(modelOf(c, { verb: "create" }), null)).toEqual([]);
		});
		it("'Open changed rows' opens every changed row, without moving focus", () => {
			const all = Object.fromEntries(saved.map((_, i) => [i + 1, { warehouse: "B" }]));
			const t = withRest(all);
			const sum = changedSummary(t, rowChangeSets(t));
			const before = document.activeElement;
			openChangedRows(t, sum);
			expect(t.rows.every((r) => r.__open)).toBe(true);
			expect(document.activeElement).toBe(before);
		});
	});
});

describe("the row toggle's text and label", () => {
	it("is short text for what applies, both when both apply", () => {
		expect(toggleParts({ missing: 0, changed: 0 })).toEqual([]);
		expect(toggleParts({ missing: 0, changed: 3 })).toEqual([
			{ kind: "changed", text: "3 changed" },
		]);
		expect(toggleParts({ missing: 2, changed: 0 })).toEqual([
			{ kind: "missing", text: "2 required" },
		]);
		expect(toggleParts({ missing: 2, changed: 3 }).map((p) => p.text)).toEqual([
			"2 required",
			"3 changed",
		]);
	});
	it("the label is in full words, for both, and equals what the title shows", () => {
		expect(toggleLabel(7, { missing: 0, changed: 0 })).toBe("All fields of row 7");
		expect(toggleLabel(7, { missing: 1, changed: 1 })).toBe(
			"All fields of row 7, 1 required field blank, 1 field changed"
		);
		expect(toggleLabel(7, { missing: 2, changed: 3 })).toBe(
			"All fields of row 7, 2 required fields blank, 3 fields changed"
		);
	});
});

// every panel row has a stable id, so a link menu, a form id and a v-for key keep
// pointing at the same row after another row is removed.
describe("rows carry a stable __key", () => {
	const build = (extra = {}) =>
		buildDraftTable({
			fieldname: "items",
			spec,
			verb: "update",
			saved: [
				{ name: "r1", item_code: "A", qty: 1 },
				{ name: "r2", item_code: "B", qty: 2 },
				{ name: "r3", item_code: "C", qty: 3 },
			],
			...extra,
		});
	it("gives every built row, proposed row and added row its own key", () => {
		const keys = [
			...build().rows,
			...build({ proposed: [{ name: "r1" }, { item_code: "NEW", qty: 1 }] }).rows,
			blankRow(build()),
			...buildDraftTable({ fieldname: "items", spec, required: true, verb: "create" }).rows,
		].map((r) => r.__key);
		expect(keys.every((k) => Number.isInteger(k))).toBe(true);
		expect(new Set(keys).size).toBe(keys.length);
	});
	it("keeps a row's key when another row is removed, and never reuses one", () => {
		const t = build();
		const [k1, k2, k3] = t.rows.map((r) => r.__key);
		t.rows.splice(0, 1);
		expect(t.rows.map((r) => r.__key)).toEqual([k2, k3]);
		const added = blankRow(t);
		t.rows.push(added);
		expect([k1, k2, k3]).not.toContain(added.__key);
	});
	it("is never a column, a form field or part of the payload", () => {
		const t = build();
		expect(allRowFields(t).map((f) => f.fieldname)).not.toContain("__key");
		expect(JSON.stringify(coerceRow(t, t.rows[0], "update"))).not.toContain("__key");
		const created = buildDraftTable({
			fieldname: "items",
			spec,
			required: true,
			proposed: [{ item_code: "X", qty: 1 }],
			verb: "create",
		});
		expect(JSON.stringify(coerceRow(created, created.rows[0], "create"))).not.toContain(
			"__key"
		);
	});
});
