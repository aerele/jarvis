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
