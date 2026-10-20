import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { reactive } from "vue";
import DraftCell from "./DraftCell.vue";

const F = (fieldname, fieldtype, extra = {}) => ({
	fieldname,
	label: fieldname,
	fieldtype,
	options: "",
	...extra,
});
const link = (over = {}) => ({ key: "", items: [], open: false, anchor: null, ...over });
// DraftLinkMenu has its own spec; here it is a stand-in, so only the cell's
// wiring to it is under test (jsdom has no layout for the real one).
const DraftLinkMenu = {
	name: "DraftLinkMenu",
	props: ["anchor", "items", "palette"],
	template: "<div />",
};
const cell = (field, row, props = {}) =>
	mount(DraftCell, {
		global: { stubs: { DraftLinkMenu } },
		props: { field, row, cellKey: "t:0:0:" + field.fieldname, draftLink: link(), ...props },
	});

describe("one editable value of a child row", () => {
	it("renders the right control for each fieldtype", () => {
		const row = reactive({
			d: "2026-01-02",
			dt: "2026-01-02T10:30",
			t: "10:30",
			n: "5",
			r: "0.6",
		});
		expect(cell(F("d", "Date"), row).find("input").attributes("type")).toBe("date");
		expect(cell(F("dt", "Datetime"), row).find("input").attributes("type")).toBe(
			"datetime-local"
		);
		expect(cell(F("t", "Time"), row).find("input").attributes("type")).toBe("time");
		expect(cell(F("n", "Currency"), row).find("input").attributes("type")).toBe("number");
		expect(cell(F("r", "Rating"), row).find("input").attributes("type")).toBe("number");
	});

	it("a Select keeps an unknown current value as an option; a Check is Yes/No", () => {
		const row = reactive({ s: "Odd", c: "1" });
		const sel = cell(F("s", "Select", { options: "A\nB" }), row);
		expect(sel.findAll("option").map((o) => o.text())).toEqual(["Odd", "A", "B"]);
		const chk = cell(F("c", "Check"), row);
		expect(chk.findAll("option").map((o) => [o.attributes("value"), o.text()])).toEqual([
			["1", "Yes"],
			["0", "No"],
		]);
	});

	it("a Check shows a proposed Yes/No/true/false as its 1/0 option, without rewriting the row", () => {
		const shown = (v) => {
			const row = reactive({ c: v });
			const sel = cell(F("c", "Check"), row).find("select");
			const out = [sel.element.value, row.c];
			return out;
		};
		for (const [raw, opt] of [
			["Yes", "1"],
			["yes", "1"],
			["true", "1"],
			[true, "1"],
			[1, "1"],
			["1", "1"],
			["No", "0"],
			["false", "0"],
			[false, "0"],
			[0, "0"],
			["0", "0"],
			["", "0"], // a blank cell (a new row) is unticked, as in Desk
			[null, "0"],
		]) {
			// the row keeps what the agent sent; only the display is normalised
			expect(shown(raw)).toEqual([opt, raw]);
		}
	});

	it("choosing a Check option writes 1 or 0", async () => {
		const row = reactive({ c: "Yes" });
		const sel = cell(F("c", "Check"), row).find("select");
		await sel.setValue("0");
		expect(row.c).toBe("0");
		await sel.setValue("1");
		expect(row.c).toBe("1");
	});

	it("long text is one line in the grid and a textarea in the row form", () => {
		const row = reactive({ x: "hello" });
		expect(cell(F("x", "Text Editor"), row).find("textarea").exists()).toBe(false);
		expect(cell(F("x", "Text Editor"), row, { wide: true }).find("textarea").exists()).toBe(
			true
		);
		expect(cell(F("x", "JSON"), row, { wide: true }).find("textarea").exists()).toBe(true);
	});

	it("read-only (by flag or the Read Only fieldtype) is a named, non-editable value", () => {
		const row = reactive({ a: "", b: "7", c: "0" });
		const ro = cell(F("b", "Read Only"), row, { controlId: "x-b" });
		expect(ro.find("input").exists()).toBe(false);
		expect(ro.find("#x-b").attributes("aria-readonly")).toBe("true");
		expect(cell(F("a", "Data"), row, { readOnly: true }).text()).toBe("-");
		expect(cell(F("c", "Check"), row, { readOnly: true }).text()).toBe("No");
	});

	it("names the control for assistive tech in the grid, and by id in the row form", () => {
		const row = reactive({ q: "" });
		expect(
			cell(F("q", "Data"), row, { label: "Qty" }).find("input").attributes("aria-label")
		).toBe("Qty");
		expect(
			cell(F("q", "Data"), row, { controlId: "id-q" }).find("input").attributes("id")
		).toBe("id-q");
	});

	it("reports link search, blur and pick, and the pick writes into the row", async () => {
		const row = reactive({ w: "" });
		const field = F("w", "Link", { options: "Warehouse" });
		// The menu positions itself against the field it belongs to.
		const anchor = document.createElement("input");
		anchor.getBoundingClientRect = () => ({ left: 0, top: 0, bottom: 20, width: 100 });
		document.body.append(anchor);
		const w = cell(field, row, {
			draftLink: link({
				open: true,
				key: "t:0:0:w",
				items: [{ value: "Stores", label: "" }],
				anchor,
			}),
		});
		await w.find("input").trigger("focus");
		const [key, getter, doctype] = w.emitted("link-search")[0];
		expect([key, getter(), doctype]).toEqual(["t:0:0:w", "", "Warehouse"]);
		await w.find("input").trigger("blur");
		expect(w.emitted("link-blur")).toHaveLength(1);
		w.findComponent({ name: "DraftLinkMenu" }).vm.$emit("pick", { value: "Stores" });
		const [setter, item] = w.emitted("link-pick")[0];
		setter(item.value);
		expect(row.w).toBe("Stores");
	});

	it("opens the suggestion menu only for its own key", () => {
		const row = reactive({ w: "" });
		const field = F("w", "Link", { options: "Warehouse" });
		const other = cell(field, row, {
			draftLink: link({ open: true, key: "t:0:0:w:form", items: [{ value: "A" }] }),
		});
		expect(other.findComponent({ name: "DraftLinkMenu" }).exists()).toBe(false);
	});
});

describe("a cell passes its description and required state to the control", () => {
	const row = reactive({ a: "", s: "", c: "", l: "", t: "", ro: "x" });
	const extra = { describedby: "d-1 d-2", required: true };
	it("sets aria-describedby and aria-required on every kind of control", () => {
		const kinds = [
			["a", "Data", "input"],
			["s", "Select", "select"],
			["c", "Check", "select"],
			["l", "Link", "input"],
			["t", "Text", "input"],
			["n", "Float", "input"],
			["d", "Date", "input"],
			["dt", "Datetime", "input"],
		];
		for (const [name, type, tag] of kinds) {
			const el = cell(F(name, type, { options: "A" }), row, extra).get(tag).element;
			expect(el.getAttribute("aria-describedby")).toBe("d-1 d-2");
			expect(el.getAttribute("aria-required")).toBe("true");
		}
		const area = cell(F("t", "Small Text"), row, { ...extra, wide: true }).get("textarea");
		expect(area.attributes("aria-describedby")).toBe("d-1 d-2");
		expect(area.attributes("aria-required")).toBe("true");
	});
	it("sets neither without the props", () => {
		const el = cell(F("a", "Data"), row).get("input").element;
		expect(el.hasAttribute("aria-describedby")).toBe(false);
		expect(el.hasAttribute("aria-required")).toBe(false);
	});
	it("a read-only value takes the description but is never aria-required", () => {
		const el = cell(F("ro", "Data"), row, { ...extra, readOnly: true }).get("span").element;
		expect(el.getAttribute("aria-describedby")).toBe("d-1 d-2");
		expect(el.hasAttribute("aria-required")).toBe(false);
	});
});
