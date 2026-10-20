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
	it("marks a field a failed save named as missing, and a changed field", () => {
		const w = mount(DraftRowForm, {
			props: {
				table,
				row: reactive({
					__key: 2,
					item_code: "I",
					discount_percentage: "9",
					description: "",
					__missing: ["discount_percentage", "description"],
				}),
				tableIndex: 0,
				rowIndex: 2,
				changedFields: new Set(["discount_percentage"]),
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
		});
		const field = w
			.find('label[for="jv-rowform-0-2-discount_percentage"]')
			.element.closest(".jv-rowform-field");
		const blank = w
			.find('label[for="jv-rowform-0-2-description"]')
			.element.closest(".jv-rowform-field");
		expect(blank.classList.contains("jv-rowform-missing")).toBe(true); // named and still blank
		// Named by a failed save but already filled: not missing; still changed.
		expect(field.classList.contains("jv-rowform-missing")).toBe(false);
		expect(field.classList.contains("jv-rowform-changed")).toBe(true);
	});

	it("shows the fields the grid does not, with labels tied to their controls", () => {
		const w = mountForm(
			reactive({
				__key: 2,
				item_code: "I",
				discount_percentage: "5",
				is_free_item: "0",
				amount: "10",
			})
		);
		expect(w.text()).toContain("Pricing");
		const label = w.find('label[for="jv-rowform-0-2-discount_percentage"]');
		expect(label.exists()).toBe(true);
		expect(w.find("#jv-rowform-0-2-discount_percentage").element.value).toBe("5");
		expect(w.find("textarea#jv-rowform-0-2-description").exists()).toBe(true);
		// Read-only: shown, not editable, and still named by its label.
		const amount = w.find("#jv-rowform-0-2-amount");
		expect(amount.element.tagName).toBe("SPAN");
		expect(amount.attributes("aria-readonly")).toBe("true");
		expect(amount.text()).toBe("10");
	});

	it("shows a depends_on field as soon as the row meets its rule", async () => {
		const row = reactive({ __key: 2, item_code: "I", is_free_item: "0", free_reason: "" });
		const w = mountForm(row);
		expect(w.find("#jv-rowform-0-2-free_reason").exists()).toBe(false);
		row.is_free_item = "1";
		await nextTick();
		expect(w.find("#jv-rowform-0-2-free_reason").exists()).toBe(true);
	});

	it("edits the row itself; a required blank field has a star, flagged only once a save names it", async () => {
		const row = reactive({ __key: 2, item_code: "I", discount_percentage: "", warehouse: "" });
		const w = mountForm(row);
		await w.find("#jv-rowform-0-2-discount_percentage").setValue("12");
		expect(String(row.discount_percentage)).toBe("12"); // a number input, as in the grid
		// ERPNext may fill it on save, so a blank required field alone is not flagged.
		const label = w.find('label[for="jv-rowform-0-2-warehouse"]');
		expect(label.find(".jv-rowform-req").exists()).toBe(true);
		expect(w.find(".jv-rowform-missing").exists()).toBe(false);
		row.__missing = ["warehouse"];
		await nextTick();
		expect(w.find(".jv-rowform-missing label").text()).toContain("warehouse");
	});

	it("asks the panel to search a Link field with the row's key", async () => {
		const w = mountForm(reactive({ __key: 2, item_code: "I", warehouse: "" }));
		await w.find("#jv-rowform-0-2-warehouse").trigger("focus");
		const [key, getter, doctype] = w.emitted("link-search")[0];
		expect(key).toBe("t:0:2:warehouse:form"); // its own key: the grid's copy has another
		expect(doctype).toBe("Warehouse");
		expect(getter()).toBe("");
	});
});

// a changed field says what it was, and a rule-hidden value stays hidden an unchanged value a rule hides.
describe("a changed field shows its saved value under the control", () => {
	const mountSaved = (row, savedRow, changed, parent = {}) =>
		mount(DraftRowForm, {
			props: {
				table,
				row,
				parent,
				tableIndex: 0,
				rowIndex: 2,
				savedRow,
				changedFields: new Set(changed),
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
		});
	const was = (w, name) => {
		const el = w
			.find(`label[for="jv-rowform-0-2-${name}"]`)
			.element.closest(".jv-rowform-field")
			.querySelector(".jv-rowform-was");
		return el && el.textContent.trim();
	};
	it("reads 'was X' through the shared formatter: 0, blank and a Check included", () => {
		const row = reactive({
			__key: 2,
			item_code: "I",
			discount_percentage: "0",
			is_free_item: "0",
			warehouse: "",
		});
		const saved = {
			name: "r1",
			item_code: "I",
			discount_percentage: 10,
			is_free_item: 1,
			warehouse: "Stores",
		};
		const w = mountSaved(row, saved, ["discount_percentage", "is_free_item", "warehouse"]);
		expect(was(w, "discount_percentage")).toBe("was 10");
		expect(was(w, "is_free_item")).toBe("was Yes");
		expect(was(w, "warehouse")).toBe("was Stores");
		// blank before
		const w2 = mountSaved(
			reactive({ __key: 2, item_code: "I", warehouse: "Stores" }),
			{ name: "r1", item_code: "I", warehouse: "" },
			["warehouse"]
		);
		expect(was(w2, "warehouse")).toBe("was (empty)");
	});
	it("says nothing under an unchanged field, or on a row with no saved row", () => {
		const row = reactive({ __key: 2, item_code: "I", discount_percentage: "10" });
		const saved = { name: "r1", item_code: "I", discount_percentage: 10 };
		expect(was(mountSaved(row, saved, []), "discount_percentage")).toBeNull();
		const fresh = mount(DraftRowForm, {
			props: {
				table,
				row,
				parent: {},
				tableIndex: 0,
				rowIndex: 2,
				changedFields: new Set(["discount_percentage"]),
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
		});
		expect(fresh.find(".jv-rowform-was").exists()).toBe(false);
	});
	it("with a saved row, an unchanged value its rule hides is not laid out; a changed one is", async () => {
		const row = reactive({
			__key: 2,
			item_code: "I",
			is_free_item: "0",
			free_reason: "promo",
		});
		const saved = { name: "r1", item_code: "I", is_free_item: 0, free_reason: "promo" };
		const w = mountSaved(row, saved, []);
		expect(w.find("#jv-rowform-0-2-free_reason").exists()).toBe(false);
		const c = mountSaved(reactive({ __key: 2, ...row, free_reason: "new reason" }), saved, [
			"free_reason",
		]);
		expect(c.find("#jv-rowform-0-2-free_reason").exists()).toBe(true);
		// no saved row: the old show-if-valued rule stays
		const n = mountForm(
			reactive({ __key: 2, item_code: "I", is_free_item: "0", free_reason: "promo" })
		);
		expect(n.find("#jv-rowform-0-2-free_reason").exists()).toBe(true);
	});
});

// a read-only field is named by its visible label, through aria-labelledby only.
describe("a read-only field has an accessible name equal to its label", () => {
	const ro = {
		columns: [],
		formFields: [
			F("amount", "Currency", { label: "Amount", read_only: 1 }),
			F("net_rate", "Currency", { label: "Net Rate", read_only_depends_on: "eval:doc.qty" }),
			F("qty", "Float", { label: "Quantity" }),
			F("notes", "Data", { label: "Notes" }),
			F("kind", "Read Only", { label: "Kind" }),
		],
	};
	const mountRo = (row) =>
		mount(DraftRowForm, {
			props: {
				table: ro,
				row: reactive({ __key: 4, ...row }),
				parent: {},
				tableIndex: 1,
				rowIndex: 0,
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
			attachTo: document.body,
		});
	// the name a browser computes for an element named by aria-labelledby
	const nameOf = (el) =>
		el
			.getAttribute("aria-labelledby")
			.split(/\s+/)
			.map((id) => document.getElementById(id).textContent)
			.join(" ")
			.replace(/\s+/g, " ")
			.trim();

	it("names each read-only span by its label's id, and passes no aria-label as well", () => {
		const w = mountRo({ amount: "10", net_rate: "5", qty: "2", notes: "", kind: "x" });
		for (const [name, label] of [
			["amount", "Amount"],
			["net_rate", "Net Rate"], // read-only by a rule
			["kind", "Kind"], // a Read Only fieldtype
		]) {
			const span = w.get(`[id$="-${name}"]`).element;
			expect(span.tagName).toBe("SPAN");
			expect(span.getAttribute("role")).toBe("textbox");
			expect(span.hasAttribute("aria-label")).toBe(false); // labelledby ONLY
			expect(nameOf(span)).toBe(label);
		}
		w.unmount();
	});
	it("an editable control keeps its <label for> and has no aria-labelledby", () => {
		const w = mountRo({ amount: "10", net_rate: "5", qty: "2", notes: "", kind: "x" });
		for (const [name, label] of [
			["qty", "Quantity"],
			["notes", "Notes"],
		]) {
			const input = w.get(`[id$="-${name}"]`).element;
			expect(input.hasAttribute("aria-labelledby")).toBe(false);
			const lab = document.querySelector(`label[for="${input.id}"]`);
			expect(lab.textContent.replace(/\s+/g, " ").trim()).toBe(label);
		}
		w.unmount();
	});
	it("a field that turns read-only as the row changes is named too", async () => {
		const row = reactive({
			__key: 4,
			amount: "",
			net_rate: "5",
			qty: "",
			notes: "",
			kind: "",
		});
		const w = mount(DraftRowForm, {
			props: {
				table: ro,
				row,
				parent: {},
				tableIndex: 1,
				rowIndex: 0,
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
			attachTo: document.body,
		});
		expect(w.get('[id$="-net_rate"]').element.tagName).toBe("INPUT");
		row.qty = "3";
		await nextTick();
		const span = w.get('[id$="-net_rate"]').element;
		expect(span.tagName).toBe("SPAN");
		expect(nameOf(span)).toBe("Net Rate");
		w.unmount();
	});
	it("a label id is unique to its row, so two open rows do not share one", () => {
		const a = mountRo({ __key: 4, amount: "1" });
		const b = mount(DraftRowForm, {
			props: {
				table: ro,
				row: reactive({ __key: 9, amount: "2" }),
				parent: {},
				tableIndex: 1,
				rowIndex: 1,
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
			attachTo: document.body,
		});
		const ids = [...document.querySelectorAll("label[id]")].map((l) => l.id);
		expect(new Set(ids).size).toBe(ids.length);
		a.unmount();
		b.unmount();
	});
});

// a failed save's missing marks are words, and a name with no field is listed.
describe("missing fields are named in words", () => {
	const t = {
		columns: [
			F("item_code", "Link", { options: "Item" }),
			F("cost_center", "Link", { label: "Cost Center" }),
		],
		formFields: [
			F("item_code", "Link", { options: "Item" }),
			F("asset_category", "Link", {
				label: "Asset Category",
				depends_on: "eval:doc.is_fixed_asset",
			}),
			F("remarks", "Data", { label: "Remarks" }),
		],
	};
	const mountMissing = (row) =>
		mount(DraftRowForm, {
			props: {
				table: t,
				row: reactive({
					__key: 3,
					item_code: "I",
					asset_category: "",
					remarks: "",
					...row,
				}),
				parent: {},
				tableIndex: 0,
				rowIndex: 0,
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
		});

	it("a blank missing field says 'Required' in text, and a filled one does not", () => {
		const w = mountMissing({ __missing: ["remarks"] });
		const field = w
			.get('label[for="jv-rowform-0-3-remarks"]')
			.element.closest(".jv-rowform-field");
		expect(field.classList.contains("jv-rowform-missing")).toBe(true);
		expect(field.querySelector(".jv-rowform-reqtext").textContent.trim()).toBe("Required");
		const filled = mountMissing({ __missing: ["remarks"], remarks: "ok" });
		expect(filled.find(".jv-rowform-reqtext").exists()).toBe(false);
		expect(mountMissing({}).find(".jv-rowform-reqtext").exists()).toBe(false);
	});
	it("a required blank field (by meta) has its star but no 'Required' note until a save names it", () => {
		const w = mount(DraftRowForm, {
			props: {
				table: {
					columns: [],
					formFields: [F("warehouse", "Link", { options: "W", reqd: 1 })],
				},
				row: reactive({ __key: 3, warehouse: "" }),
				tableIndex: 0,
				rowIndex: 0,
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
		});
		expect(w.find(".jv-rowform-req").exists()).toBe(true);
		expect(w.find(".jv-rowform-reqtext").exists()).toBe(false);
	});
	it("a rule-hidden field a failed save named is shown, as Required", () => {
		const w = mountMissing({ __missing: ["asset_category"] });
		const ctl = w.find("#jv-rowform-0-3-asset_category");
		expect(ctl.exists()).toBe(true);
		expect(ctl.element.closest(".jv-rowform-field").textContent).toContain("Required");
		expect(w.text()).not.toContain("Also needed"); // it has a field, so no extra line
	});
	it("a name with no form field is listed as 'Also needed: <label or fieldname>'", () => {
		const w = mountMissing({ __missing: ["cost_center", "tax_template", "remarks"] });
		const lines = w.findAll(".jv-rowform-needed").map((e) => e.text());
		// cost_center is a grid column only (its label); tax_template is unknown (fieldname)
		expect(lines).toEqual(["Also needed: Cost Center", "Also needed: tax_template"]);
	});
	it("lists nothing when no name lacks a field, and the empty note is not shown over it", () => {
		expect(
			mountMissing({ __missing: ["remarks"] })
				.find(".jv-rowform-needed")
				.exists()
		).toBe(false);
		const only = mount(DraftRowForm, {
			props: {
				table: { columns: [], formFields: [] },
				row: reactive({ __key: 3, __missing: ["tax_template"] }),
				tableIndex: 0,
				rowIndex: 0,
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
		});
		expect(only.get(".jv-rowform-needed").text()).toBe("Also needed: tax_template");
	});
});

describe("a named field the form shows read-only is listed under 'Also needed'", () => {
	const t = {
		columns: [],
		formFields: [
			F("locked", "Data", { label: "Locked", read_only: 1 }),
			F("ruled", "Data", { label: "Ruled", read_only_depends_on: "eval:1" }),
			F("kind", "Read Only", { label: "Kind" }),
			F("plain", "Data", { label: "Plain" }),
		],
	};
	const mountNamed = (row) =>
		mount(DraftRowForm, {
			props: {
				table: t,
				row: reactive({ __key: 5, locked: "", ruled: "", kind: "", plain: "", ...row }),
				parent: {},
				tableIndex: 0,
				rowIndex: 0,
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
		});
	const lines = (w) => w.findAll(".jv-rowform-needed").map((e) => e.text());
	it("lists a blank read-only field (static, by rule, or a Read Only type), not an editable one", () => {
		const w = mountNamed({ __missing: ["locked", "ruled", "kind", "plain"] });
		expect(lines(w)).toEqual([
			"Also needed: Locked",
			"Also needed: Ruled",
			"Also needed: Kind",
		]);
		// the editable one says Required beside its control instead; the others do not
		expect(w.findAll(".jv-rowform-reqtext")).toHaveLength(1);
	});
	it("stops listing it once the row has a value, and lists a name only once", () => {
		expect(lines(mountNamed({ __missing: ["locked"], locked: "x" }))).toEqual([]);
		const twice = mountNamed({ __missing: ["locked", "locked", "nofield", "nofield"] });
		expect(lines(twice)).toEqual(["Also needed: Locked", "Also needed: nofield"]);
	});
});

describe("'was X' is clamped to 200 characters with the full text in title", () => {
	const long = "x".repeat(300);
	const mountWas = (was) =>
		mount(DraftRowForm, {
			props: {
				table,
				row: reactive({ __key: 2, item_code: "I", warehouse: "new" }),
				parent: {},
				tableIndex: 0,
				rowIndex: 2,
				savedRow: { name: "r1", item_code: "I", warehouse: was },
				changedFields: new Set(["warehouse"]),
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
		});
	it("clips a long saved value with an ellipsis (U+2026) and titles the full text", () => {
		const el = mountWas(long).get(".jv-rowform-was").element;
		expect(el.textContent.trim()).toBe("was " + "x".repeat(195) + "…");
		expect(el.textContent.trim()).toHaveLength(200);
		expect(el.getAttribute("title")).toBe("was " + long);
	});
	it("leaves a short value whole", () => {
		const el = mountWas("Stores").get(".jv-rowform-was").element;
		expect(el.textContent.trim()).toBe("was Stores");
		expect(el.textContent).not.toContain("…");
	});
});

describe("sections are keyed by a stable id, not their position", () => {
	const t = {
		columns: [],
		formFields: [
			F("lead"),
			{
				fieldname: "stock_sec",
				fieldtype: "Section Break",
				label: "Stock",
				depends_on: "eval:doc.show",
			},
			F("bin"),
			{ fieldname: "other_sec", fieldtype: "Section Break", label: "Other" },
			F("note"),
		],
	};
	it("the section below a hidden one keeps its DOM node", async () => {
		const row = reactive({ __key: 1, show: "1", lead: "x", bin: "b", note: "n" });
		const w = mount(DraftRowForm, {
			props: {
				table: t,
				row,
				parent: {},
				tableIndex: 0,
				rowIndex: 0,
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
		});
		const noteSection = () => w.get("#jv-rowform-0-1-note").element.closest("section");
		const before = noteSection();
		row.show = "";
		row.bin = "";
		await nextTick();
		expect(w.findAll("section")).toHaveLength(2);
		expect(noteSection()).toBe(before);
	});
});

describe("'Required' and 'was X' are tied to their control for a screen reader", () => {
	const mountA11y = (row, over = {}) =>
		mount(DraftRowForm, {
			props: {
				table,
				row: reactive({ __key: 2, item_code: "I", ...row }),
				parent: {},
				tableIndex: 0,
				rowIndex: 2,
				draftLink: { key: "", items: [], open: false, anchor: null },
				...over,
			},
			attachTo: document.body,
		});
	const textOf = (ctl) =>
		ctl
			.getAttribute("aria-describedby")
			.split(/\s+/)
			.map((id) => document.getElementById(id).textContent.trim())
			.join(" | ");
	it("a blank required field is aria-required, and described by 'Required' once a save names it", () => {
		const blank = mountA11y({ warehouse: "" });
		const ctl = blank.get("#jv-rowform-0-2-warehouse").element;
		expect(ctl.getAttribute("aria-required")).toBe("true");
		expect(ctl.hasAttribute("aria-describedby")).toBe(false);
		blank.unmount();
		const named = mountA11y({ warehouse: "", __missing: ["warehouse"] });
		expect(textOf(named.get("#jv-rowform-0-2-warehouse").element)).toBe("Required");
		named.unmount();
	});
	it("a filled required field is still aria-required, with nothing to describe", () => {
		const w = mountA11y({ warehouse: "Stores" });
		const ctl = w.get("#jv-rowform-0-2-warehouse").element;
		expect(ctl.getAttribute("aria-required")).toBe("true");
		expect(ctl.hasAttribute("aria-describedby")).toBe(false);
		w.unmount();
	});
	it("a field that is not required has neither", () => {
		const w = mountA11y({ discount_percentage: "5" });
		const ctl = w.get("#jv-rowform-0-2-discount_percentage").element;
		expect(ctl.hasAttribute("aria-required")).toBe(false);
		expect(ctl.hasAttribute("aria-describedby")).toBe(false);
		w.unmount();
	});
	it("a changed field is described by its 'was X' text, and by both when a save also named it", () => {
		const w = mountA11y(
			{ discount_percentage: "9", warehouse: "", __missing: ["warehouse"] },
			{
				savedRow: { name: "r1", item_code: "I", discount_percentage: 5, warehouse: "Old" },
				changedFields: new Set(["discount_percentage", "warehouse"]),
			}
		);
		expect(textOf(w.get("#jv-rowform-0-2-discount_percentage").element)).toBe("was 5");
		expect(textOf(w.get("#jv-rowform-0-2-warehouse").element)).toBe("Required | was Old");
		w.unmount();
	});
	it("a named read-only field is not marked required (it has no control to fill)", () => {
		const w = mountA11y({ amount: "", __missing: ["amount"] });
		expect(w.get("#jv-rowform-0-2-amount").element.hasAttribute("aria-required")).toBe(false);
		w.unmount();
	});
});

describe("sections", () => {
	const t = {
		columns: [F("item_code", "Link", { options: "Item" })],
		formFields: [
			F("item_code", "Link", { options: "Item" }),
			{ fieldname: "qty_sec", fieldtype: "Section Break", label: "Quantity and Rate" },
			F("qty", "Float"),
			{
				fieldname: "disc_sec",
				fieldtype: "Section Break",
				label: "Discount",
				collapsible: 1,
			},
			F("discount_percentage", "Percent"),
		],
	};
	const mountT = (row) =>
		mount(DraftRowForm, {
			props: {
				table: t,
				row: reactive({ __key: 1, item_code: "I", qty: "", ...row }),
				tableIndex: 0,
				rowIndex: 0,
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
		});

	it("a plain section is a titled group; a collapsible one a closed details with its heading", () => {
		const w = mountT({ discount_percentage: "" });
		const titled = w.findAll("section.jv-rowform-section");
		expect(titled.map((s) => s.attributes("aria-label"))).toContain("Quantity and Rate");
		const fold = w.get("details.jv-rowform-section");
		expect(fold.get("summary").text()).toBe("Discount");
		expect(fold.element.open).toBe(false);
	});
	it("a collapsible section holding a value starts open", () => {
		const w = mountT({ discount_percentage: "5" });
		expect(w.get("details.jv-rowform-section").element.open).toBe(true);
	});
});
