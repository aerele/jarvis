import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import DraftPreview from "./DraftPreview.vue";

const F = (fieldname, fieldtype = "Data", extra = {}) => ({
	fieldname,
	label: fieldname,
	fieldtype,
	options: "",
	...extra,
});
const model = (rows, over = {}) => ({
	verb: "update",
	doctype: "Sales Order",
	docName: "SO-1",
	fields: [],
	linkContext: { update_stock: 1 },
	tables: [
		{
			fieldname: "items",
			label: "Items",
			columns: [F("item_code", "Link"), F("qty", "Float")],
			formFields: [
				F("item_code", "Link"),
				F("qty", "Float"),
				F("discount_percentage", "Percent"),
				F("warehouse", "Link", { depends_on: "eval:parent.update_stock" }),
				F("note"),
			],
			rows,
		},
	],
	...over,
});
const mountPreview = (m) =>
	mount(DraftPreview, { props: { model: m }, global: { stubs: { transition: false } } });

describe("the read-only preview lists a row's values outside the grid", () => {
	it("does not repeat an unchanged proposed column beneath the preview grid", () => {
		const m = model([{ item_code: "I", qty: "2", description: "Bank charges" }], {
			verb: "create",
		});
		const t = m.tables[0];
		t.gridFields = ["item_code", "qty"];
		t.columns.push(F("description", "Small Text"));
		t.formFields.push(F("description", "Small Text"));
		const w = mountPreview(m);
		expect(w.findAll(".dp-grid td").map((c) => c.text())).toContain("Bank charges");
		expect(w.findAll(".dp-rowmore")).toHaveLength(0);
	});

	it("keeps the old and new values for a changed proposed column", () => {
		const m = model([{ __name: "r1", item_code: "I", qty: "2", description: "New charges" }]);
		const t = m.tables[0];
		t.gridFields = ["item_code", "qty"];
		t.columns.push(F("description", "Small Text"));
		t.formFields.push(F("description", "Small Text"));
		t.origJson = JSON.stringify([
			{ name: "r1", item_code: "I", qty: 2, description: "Old charges" },
		]);
		const w = mountPreview(m);
		expect(w.find(".dp-rowmore").text()).toContain("Old charges");
		expect(w.find(".dp-rowmore").text()).toContain("New charges");
	});

	it("shows a disclosure per row with such values, worded by count", () => {
		const w = mountPreview(
			model([
				{
					__name: "r1",
					item_code: "I",
					qty: "2",
					discount_percentage: "5",
					warehouse: "",
					note: "",
				},
				{
					__name: "r2",
					item_code: "J",
					qty: "1",
					discount_percentage: "5",
					warehouse: "Stores",
					note: "",
				},
				{
					__name: "r3",
					item_code: "K",
					qty: "1",
					discount_percentage: "0",
					warehouse: "",
					note: "",
				},
			])
		);
		const summaries = w
			.findAll(".dp-rowmore summary")
			.map((s) => s.text().replace(/\s+/g, " "));
		expect(summaries).toEqual(["Row 1: 1 more field", "Row 2: 2 more fields"]); // row 3: zeros only
		const items = w
			.findAll(".dp-rowmore")[1]
			.findAll("dt")
			.map((d) => d.text());
		expect(items).toEqual(["discount_percentage", "warehouse"]);
	});

	it("lists a filled field even when its rule would hide it, so nothing saved is unseen", () => {
		const off = mountPreview(
			model(
				[
					{
						__name: "r1",
						item_code: "I",
						qty: "1",
						discount_percentage: "",
						warehouse: "Stores",
						note: "",
					},
				],
				{
					linkContext: { update_stock: 0 },
				}
			)
		);
		expect(off.findAll(".dp-rowmore dt").map((d) => d.text())).toEqual(["warehouse"]);
	});
});

// on an update the preview leads with what changed, in the card's `orig -> new` form.
describe("the preview of an update leads with the changed fields", () => {
	const saved = [
		{ name: "r1", item_code: "I", qty: 2, discount_percentage: 10, warehouse: "A", note: "" },
		{ name: "r2", item_code: "J", qty: 1, discount_percentage: 5, warehouse: "A", note: "n" },
	];
	const withSaved = (rows, over = {}) => {
		const m = model(rows, over);
		m.tables[0].origJson = JSON.stringify(saved);
		return m;
	};
	const row = (over = {}) => ({
		__name: "r1",
		item_code: "I",
		qty: "2",
		discount_percentage: "10",
		warehouse: "A",
		note: "",
		...over,
	});
	// "label: value" for each dt/dd pair, in order
	const pairs = (el) => {
		const dts = el.findAll("dt");
		const dds = el.findAll("dd");
		return dts.map((d, i) => `${d.text()}: ${dds[i].text().replace(/\s+/g, " ")}`);
	};
	const lines = (w) =>
		w.findAll(".dp-rowmore").map((d) => ({
			summary: d.find("summary").text().replace(/\s+/g, " "),
			open: d.element.open,
			text: d.text().replace(/\s+/g, " "),
		}));

	it("shows a warehouse change as A -> B", () => {
		const w = mountPreview(withSaved([row({ warehouse: "B" })]));
		const [d] = lines(w);
		expect(pairs(w.find(".dp-rowmore"))[0]).toBe("warehouse: A -> B");
		expect(d.summary).toBe("Row 1: 1 changed, 1 more field");
		expect(d.open).toBe(true);
	});
	it("shows a discount going 10 to 0 as 10 -> 0", () => {
		const w = mountPreview(withSaved([row({ discount_percentage: "0" })]));
		expect(pairs(w.find(".dp-rowmore"))).toContain("discount_percentage: 10 -> 0");
	});
	it("shows a value cleared as (empty), and a new value over an empty saved one", () => {
		const w = mountPreview(
			withSaved([row({ warehouse: "" }), row({ __name: "r2", item_code: "J", note: "hi" })])
		);
		const [a] = lines(w);
		expect(a.text).toContain("Row 1");
		const [da, db] = w.findAll(".dp-rowmore");
		expect(pairs(da)).toContain("warehouse: A -> (empty)");
		expect(pairs(db)).toContain("note: n -> hi");
	});
	it("lists the changed fields first, then the other filled values under 'Also on this row'", () => {
		const w = mountPreview(withSaved([row({ warehouse: "B" })]));
		const d = w.find(".dp-rowmore");
		expect(pairs(d)).toEqual(["warehouse: A -> B", "discount_percentage: 10"]);
		expect(d.text()).toContain("Also on this row");
		expect(d.text().indexOf("A -> B")).toBeLessThan(d.text().indexOf("Also on this row"));
	});
	it("changed rows open by default only when 3 or fewer changed, like the card", () => {
		const many = (n) => {
			const rows = Array.from({ length: n }, (_, i) => ({
				...row({ __name: `r${i + 1}` }),
				warehouse: "B",
			}));
			const m = model(rows);
			m.tables[0].origJson = JSON.stringify(
				rows.map((r, i) => ({ ...saved[0], name: `r${i + 1}`, warehouse: "A" }))
			);
			return lines(mountPreview(m));
		};
		expect(many(3).map((d) => d.open)).toEqual([true, true, true]);
		const four = many(4);
		expect(four).toHaveLength(4);
		expect(four.map((d) => d.open)).toEqual([false, false, false, false]);
		expect(four[0].summary).toBe("Row 1: 1 changed, 1 more field"); // still listed
	});
	it("an unchanged row keeps its collapsed 'more fields' line, with no 'Also' heading", () => {
		const w = mountPreview(withSaved([row()]));
		const [d] = lines(w);
		expect(d.summary).toBe("Row 1: 2 more fields");
		expect(d.open).toBe(false);
		expect(d.text).not.toContain("Also on this row");
	});
	it("reads the record's values for the rules: a field its rule hides and that did not change is unlisted", () => {
		// warehouse needs parent.update_stock; with it off an unchanged saved value is hidden
		const hidden = mountPreview(withSaved([row()], { linkContext: { update_stock: 0 } }));
		expect(hidden.find(".dp-rowmore").text()).not.toContain("warehouse");
		// ... but a changed one is always listed
		const changed = mountPreview(
			withSaved([row({ warehouse: "B" })], { linkContext: { update_stock: 0 } })
		);
		expect(pairs(changed.find(".dp-rowmore"))).toContain("warehouse: A -> B");
	});
	it("a Check reads Yes/No in a change", () => {
		const m = withSaved([row({ is_free: "No" })]);
		m.tables[0].formFields.push(F("is_free", "Check"));
		m.tables[0].origJson = JSON.stringify([{ ...saved[0], is_free: 1 }]);
		expect(pairs(mountPreview(m).find(".dp-rowmore"))).toContain("is_free: Yes -> No");
	});
	it("has no 'Show all rows' control", () => {
		const w = mountPreview(withSaved([row({ warehouse: "B" })]));
		expect(w.text()).not.toContain("Show all rows");
	});
	it("a main field change uses the same wording: blank is (empty), 0 stays 0", () => {
		const w = mountPreview(
			model([], {
				fields: [
					{
						fieldname: "a",
						label: "A",
						value: "",
						orig: "5",
						changed: true,
						control: "text",
					},
					{
						fieldname: "b",
						label: "B",
						value: "0",
						orig: "",
						changed: true,
						control: "number",
					},
					{
						fieldname: "c",
						label: "C",
						value: "No",
						orig: "Yes",
						changed: true,
						control: "check",
					},
				],
			})
		);
		expect(pairs(w.find(".dp-fields"))).toEqual([
			"A: 5 -> (empty)",
			"B: (empty) -> 0",
			"C: Yes -> No",
		]);
	});
});

describe("a long 'was' value is clamped, with the full text in title", () => {
	const long = "y".repeat(300);
	const clipped = "y".repeat(199) + "…";
	it("clamps a main field's old value", () => {
		const w = mountPreview({
			verb: "update",
			doctype: "Sales Order",
			docName: "SO-1",
			fields: [
				{
					fieldname: "remarks",
					label: "Remarks",
					value: "new",
					orig: long,
					changed: true,
				},
			],
			tables: [],
		});
		const old = w.get(".dp-fields .dp-old");
		expect(old.text()).toBe(clipped);
		expect(old.attributes("title")).toBe(long);
		expect(w.get(".dp-fields .dp-new").text()).toBe("new");
	});
	it("clamps a row's old value", () => {
		const m = model([{ __name: "r1", item_code: "I", qty: "1", note: "new" }]);
		m.tables[0].origJson = JSON.stringify([
			{ name: "r1", item_code: "I", qty: 1, note: long },
		]);
		const w = mountPreview(m);
		const old = w.get(".dp-rowmore-list .dp-old");
		expect(old.text()).toBe(clipped);
		expect(old.attributes("title")).toBe("y".repeat(300));
	});
	it("leaves a short value whole", () => {
		const w = mountPreview({
			verb: "update",
			doctype: "Sales Order",
			docName: "SO-1",
			fields: [
				{
					fieldname: "remarks",
					label: "Remarks",
					value: "new",
					orig: "short",
					changed: true,
				},
			],
			tables: [],
		});
		expect(w.get(".dp-fields .dp-old").text()).toBe("short");
	});
});
