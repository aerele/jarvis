import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick, reactive } from "vue";
import DraftRowForm from "./DraftRowForm.vue";

const F = (fieldname, fieldtype = "Data", extra = {}) => ({
	fieldname,
	label: fieldname,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});
const table = {
	columns: [F("item_code", "Link", { options: "Item" })],
	formFields: [
		F("item_code", "Link", { options: "Item" }),
		{ fieldname: "", fieldtype: "Section Break", label: "Pricing" },
		F("discount_percentage", "Percent"),
		F("is_free_item", "Check"),
		F("free_reason", "Data", { depends_on: "eval:doc.is_free_item" }),
		F("description", "Text Editor"),
		F("amount", "Currency", { read_only: 1 }),
		F("warehouse", "Link", { options: "Warehouse", reqd: 1 }),
	],
};
const mountForm = (row) =>
	mount(DraftRowForm, {
		props: {
			table,
			row,
			parent: {},
			tableIndex: 0,
			rowIndex: 2,
			draftLink: { key: "", items: [], open: false, anchor: null },
		},
	});

describe("a child row's full form in the draft panel", () => {
	it("shows the fields the grid does not, with labels tied to their controls", () => {
		const w = mountForm(
			reactive({ item_code: "I", discount_percentage: "5", is_free_item: "0", amount: "10" })
		);
		expect(w.text()).toContain("Pricing");
		const label = w.find('label[for="jv-rowform-0-2-discount_percentage"]');
		expect(label.exists()).toBe(true);
		expect(w.find("#jv-rowform-0-2-discount_percentage").element.value).toBe("5");
		expect(w.find("textarea#jv-rowform-0-2-description").exists()).toBe(true);
		expect(w.find(".jv-cell-ro").text()).toBe("10"); // read-only shown, not editable
		expect(w.find("#jv-rowform-0-2-amount").exists()).toBe(false);
	});

	it("shows a depends_on field as soon as the row meets its rule", async () => {
		const row = reactive({ item_code: "I", is_free_item: "0", free_reason: "" });
		const w = mountForm(row);
		expect(w.find("#jv-rowform-0-2-free_reason").exists()).toBe(false);
		row.is_free_item = "1";
		await nextTick();
		expect(w.find("#jv-rowform-0-2-free_reason").exists()).toBe(true);
	});

	it("edits the row itself, and marks a required blank field", async () => {
		const row = reactive({ item_code: "I", discount_percentage: "", warehouse: "" });
		const w = mountForm(row);
		await w.find("#jv-rowform-0-2-discount_percentage").setValue("12");
		expect(String(row.discount_percentage)).toBe("12"); // a number input, as in the grid
		expect(w.find(".jv-rowform-missing label").text()).toContain("warehouse");
	});

	it("asks the panel to search a Link field with the row's key", async () => {
		const w = mountForm(reactive({ item_code: "I", warehouse: "" }));
		await w.find("#jv-rowform-0-2-warehouse").trigger("focus");
		const [key, getter, doctype] = w.emitted("link-search")[0];
		expect(key).toBe("t:0:2:warehouse");
		expect(doctype).toBe("Warehouse");
		expect(getter()).toBe("");
	});
});
