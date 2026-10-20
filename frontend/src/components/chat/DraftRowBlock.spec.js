// A row's open form block, with the keyboard behaviour around it and the
// link menu's row identity. The view is too big to mount, so a small harness
// wires the REAL DraftRowBlock / DraftRowForm / DraftCell to the same helpers ChatView
// calls (lib/rowFormFocus.js, draftLinkSearch) and the specs drive it with real
// keyboard events on the focused element.
import { afterEach, describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick, reactive } from "vue";
import DraftRowBlock from "./DraftRowBlock.vue";
import { draftLinkSearch } from "@/lib/draftLinkFilters";
import { blankRow, buildDraftTable } from "@/lib/draftTables";
import {
	closeRowForm,
	rowFormId,
	rowToggleId,
	showRowForm,
	toggleRowForm,
} from "@/lib/rowFormFocus";

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
	child_doctype: "Row",
	columns: [F("item_code", "Link", { options: "Item" }), F("currency")],
	extra_columns: [],
	form_fields: [
		F("item_code", "Link", { options: "Item" }),
		F("currency"),
		{ fieldname: "", fieldtype: "Section Break", label: "More" },
		F("warehouse", "Link", {
			options: "Warehouse",
			link_filters: [["Warehouse", "currency", "=", "eval:doc.currency"]],
		}),
		F("remarks"),
	],
};
const saved = () =>
	["r1", "r2", "r3"].map((name, i) => ({
		name,
		item_code: "I" + i,
		currency: ["USD", "EUR", "GBP"][i],
		warehouse: "",
		remarks: "",
	}));

// jsdom has no layout, so the real menu is a stand-in that can pick its first item.
const DraftLinkMenu = {
	name: "DraftLinkMenu",
	props: ["anchor", "items", "palette"],
	emits: ["pick", "close"],
	template: '<button class="menu-pick" @click="$emit(\'pick\', items[0])">pick</button>',
};

let wrapper;
afterEach(() => wrapper && wrapper.unmount());

function setup() {
	const table = reactive(
		buildDraftTable({ fieldname: "items", spec, verb: "update", saved: saved() })
	);
	const model = {
		doctype: "Order",
		docName: "O-1",
		linkContext: {},
		fields: [],
		tables: [table],
	};
	const searches = [];
	const draftLink = reactive({ key: "", items: [], open: false, anchor: null });
	const Harness = {
		components: { DraftRowBlock },
		setup() {
			return {
				table,
				draftLink,
				rowToggleId,
				toggleRowForm,
				closeRowForm,
				remove: (ri) => table.rows.splice(ri, 1),
				// ChatView's onDraftLink / pickDraftLink, reduced to what they do with the key
				search(key, target, doctype, ev) {
					const args = draftLinkSearch(model, key);
					searches.push({ key, filters: args.filters });
					Object.assign(draftLink, {
						key,
						open: true,
						items: [{ value: "WH-1" }],
						anchor: ev.target,
					});
				},
				pick(setter, item) {
					setter(item.value);
					Object.assign(draftLink, { key: "", open: false, items: [] });
				},
			};
		},
		template: `<div>
			<div v-for="(r, ri) in table.rows" :key="r.__key" class="grid-row">
				<button :id="rowToggleId(0, r.__key)" class="toggle" @click="toggleRowForm(0, r)">row {{ ri + 1 }}</button>
				<button class="remove" @click="remove(ri)">remove {{ ri + 1 }}</button>
			</div>
			<template v-for="(r, ri) in table.rows" :key="'form-' + r.__key">
				<DraftRowBlock
					v-if="r.__open"
					:table="table" :row="r" :table-index="0" :row-index="ri"
					:draft-link="draftLink"
					@close="closeRowForm(0, r)"
					@link-search="search"
					@link-pick="pick"
					@link-close="draftLink.open = false"
				/>
			</template>
		</div>`,
	};
	wrapper = mount(Harness, {
		attachTo: document.body,
		global: { stubs: { DraftLinkMenu } },
	});
	return { table, searches, draftLink };
}
// A real key event on whatever has focus, as the browser delivers it.
const press = (key) =>
	document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
const toggles = () => wrapper.findAll(".toggle");
const active = () => document.activeElement;

describe("opening a row's form moves focus into it", () => {
	it("a toggle click focuses the form section, which is focusable but not a tab stop", async () => {
		const { table } = setup();
		await toggles()[1].trigger("click");
		await nextTick();
		const section = document.getElementById(rowFormId(0, table.rows[1].__key));
		expect(section).not.toBeNull();
		expect(section.getAttribute("tabindex")).toBe("-1");
		expect(active()).toBe(section);
	});
	it("a row that opened by itself (auto-open, 'Open changed rows') takes no focus", async () => {
		const { table } = setup();
		toggles()[0].element.focus();
		table.rows[1].__open = true; // what applyAutoOpen / openChangedRows do
		await nextTick();
		await nextTick();
		expect(wrapper.find(".jv-rowform-block").exists()).toBe(true);
		expect(active()).toBe(toggles()[0].element);
	});
	it("closing with the toggle does not steal focus from it", async () => {
		const { table } = setup();
		await toggles()[0].trigger("click");
		await nextTick();
		await toggles()[0].trigger("click");
		await nextTick();
		expect(table.rows[0].__open).toBe(false);
		expect(active()).not.toBe(document.getElementById(rowFormId(0, table.rows[0].__key)));
	});
});

describe("Done and Escape close the form and return focus to the row's toggle", () => {
	it("Done", async () => {
		const { table } = setup();
		await toggles()[2].trigger("click");
		await nextTick();
		await wrapper.get(".jv-rowform-close").trigger("click");
		await nextTick();
		expect(table.rows[2].__open).toBe(false);
		expect(wrapper.find(".jv-rowform-block").exists()).toBe(false);
		expect(active()).toBe(toggles()[2].element);
	});
	it("Escape from the section itself, and from a field inside it", async () => {
		const { table } = setup();
		await toggles()[0].trigger("click");
		await nextTick();
		press("Escape"); // focus is on the section
		await nextTick();
		await nextTick();
		expect(table.rows[0].__open).toBe(false);
		expect(active()).toBe(toggles()[0].element);

		await toggles()[0].trigger("click");
		await nextTick();
		wrapper.get("#jv-rowform-0-" + table.rows[0].__key + "-remarks").element.focus();
		press("Escape");
		await nextTick();
		await nextTick();
		expect(table.rows[0].__open).toBe(false);
		expect(active()).toBe(toggles()[0].element);
	});
	it("a plain Escape elsewhere on the page does nothing to an open form", async () => {
		const { table } = setup();
		await toggles()[0].trigger("click");
		await nextTick();
		toggles()[1].element.focus();
		press("Escape");
		await nextTick();
		expect(table.rows[0].__open).toBe(true);
	});
	it("Escape does not also reach the page's own Escape handler", async () => {
		setup();
		await toggles()[0].trigger("click");
		await nextTick();
		let seen = false;
		const onKey = () => (seen = true);
		window.addEventListener("keydown", onKey);
		press("Escape");
		window.removeEventListener("keydown", onKey);
		expect(seen).toBe(false);
	});
	it("focus goes to that row's toggle even after a row above it was removed", async () => {
		const { table } = setup();
		const k2 = table.rows[1].__key;
		await toggles()[1].trigger("click");
		await nextTick();
		await wrapper.findAll(".remove")[0].trigger("click"); // row 1 goes; row 2 is now first
		await nextTick();
		expect(table.rows[0].__key).toBe(k2);
		document.getElementById(rowFormId(0, k2)).focus();
		press("Escape");
		await nextTick();
		await nextTick();
		expect(active().id).toBe(rowToggleId(0, k2));
		expect(active()).toBe(toggles()[0].element); // by position it would be the 2nd toggle
	});
});

describe("Escape in a Link input with its menu open closes only the menu", () => {
	it("the first Escape closes the menu, the second closes the form and focuses the toggle", async () => {
		const { table, draftLink } = setup();
		const k = table.rows[1].__key;
		await toggles()[1].trigger("click");
		await nextTick();
		const input = wrapper.get(`#jv-rowform-0-${k}-warehouse`);
		input.element.focus();
		await input.trigger("focus"); // the panel opens the menu on focus
		await nextTick();
		expect(draftLink.open).toBe(true);
		expect(wrapper.find(".menu-pick").exists()).toBe(true);

		press("Escape");
		await nextTick();
		expect(draftLink.open).toBe(false); // the menu is closed...
		expect(wrapper.find(".menu-pick").exists()).toBe(false);
		expect(table.rows[1].__open).toBe(true); // ...and the form is still open
		expect(active()).toBe(input.element); // with focus where it was

		press("Escape");
		await nextTick();
		await nextTick();
		expect(table.rows[1].__open).toBe(false);
		expect(active()).toBe(toggles()[1].element);
	});
	it("a Link input whose menu is closed lets Escape through to close the form", async () => {
		const { table, draftLink } = setup();
		await toggles()[0].trigger("click");
		await nextTick();
		const input = wrapper.get(`#jv-rowform-0-${table.rows[0].__key}-item_code`);
		input.element.focus(); // focusing opens the menu...
		await nextTick();
		draftLink.open = false; // ...which the user has dismissed (or it timed out)
		await nextTick();
		press("Escape");
		await nextTick();
		await nextTick();
		expect(table.rows[0].__open).toBe(false);
	});
});

describe("a link menu stays with its row when a row above is removed", () => {
	it("the search and the picked value still go to the original row 2", async () => {
		const { table, searches, draftLink } = setup();
		const [, row2, row3] = table.rows;
		await toggles()[1].trigger("click");
		await nextTick();
		const input = wrapper.get(`#jv-rowform-0-${row2.__key}-warehouse`);
		await input.trigger("focus");
		expect(searches.at(-1).filters[0][3]).toBe("EUR"); // row 2's currency
		expect(draftLink.open).toBe(true);

		await wrapper.findAll(".remove")[0].trigger("click"); // remove row 1 with the menu open
		await nextTick();
		expect(table.rows.map((r) => r.__name)).toEqual(["r2", "r3"]);

		// the next search from the same input is still row 2's (by position it would be row 3's)
		await input.trigger("input");
		expect(searches.at(-1).filters[0][3]).toBe("EUR");
		expect(searches.at(-1).key).toBe(`t:0:${row2.__key}:warehouse:form`);

		await wrapper.get(".menu-pick").trigger("click");
		expect(row2.warehouse).toBe("WH-1");
		expect(row3.warehouse).toBe("");
	});
	it("a row added later gets its own key, so no menu key repeats", () => {
		const { table } = setup();
		const added = blankRow(table);
		table.rows.push(added);
		const keys = table.rows.map((r) => r.__key);
		expect(new Set(keys).size).toBe(keys.length);
	});
});

describe("the block is named by its visible title, and ignores an IME Escape", () => {
	it("aria-labelledby points at the title; there is no aria-label", async () => {
		const { table } = setup();
		await toggles()[1].trigger("click");
		await nextTick();
		const section = document.getElementById(rowFormId(0, table.rows[1].__key));
		expect(section.hasAttribute("aria-label")).toBe(false);
		const title = document.getElementById(section.getAttribute("aria-labelledby"));
		expect(title).not.toBeNull();
		expect(section.contains(title)).toBe(true);
		expect(title.textContent.replace(/\s+/g, " ").trim()).toBe("Row 2 · I1");
	});
	it("an Escape that is part of an IME composition does not close the form", async () => {
		const { table } = setup();
		await toggles()[0].trigger("click");
		await nextTick();
		const ev = new KeyboardEvent("keydown", {
			key: "Escape",
			isComposing: true,
			bubbles: true,
			cancelable: true,
		});
		document.activeElement.dispatchEvent(ev);
		await nextTick();
		expect(table.rows[0].__open).toBe(true);
		expect(ev.defaultPrevented).toBe(false); // the IME keeps its own Escape
		// a keyCode 229 Escape (the IME's own, in browsers that omit isComposing) is left alone too
		const ime = new KeyboardEvent("keydown", {
			key: "Escape",
			keyCode: 229,
			bubbles: true,
			cancelable: true,
		});
		document.activeElement.dispatchEvent(ime);
		await nextTick();
		expect(table.rows[0].__open).toBe(true);
		expect(ime.defaultPrevented).toBe(false);
		press("Escape"); // a plain one still closes it
		await nextTick();
		expect(table.rows[0].__open).toBe(false);
	});
});

describe("a row number in the summary opens that row and moves focus to its form", () => {
	it("opens a closed row and focuses its form section", async () => {
		const { table } = setup();
		expect(table.rows[2].__open).toBeFalsy();
		showRowForm(0, table.rows[2]);
		await nextTick();
		await nextTick();
		expect(table.rows[2].__open).toBe(true);
		expect(active()).toBe(document.getElementById(rowFormId(0, table.rows[2].__key)));
	});
	it("on a row that is already open it keeps it open and focuses its form", async () => {
		const { table } = setup();
		await toggles()[1].trigger("click");
		await nextTick();
		toggles()[0].element.focus();
		showRowForm(0, table.rows[1]);
		await nextTick();
		expect(table.rows[1].__open).toBe(true); // a toggle would have closed it
		expect(active()).toBe(document.getElementById(rowFormId(0, table.rows[1].__key)));
	});
});

describe("the open row's card header", () => {
	it("has Remove, named by the row, and Done; each asks the panel", async () => {
		const t = reactive(
			buildDraftTable({
				fieldname: "items",
				spec,
				required: false,
				proposed: null,
				saved: saved(),
				verb: "update",
			})
		);
		const w = mount(DraftRowBlock, {
			props: {
				table: t,
				row: t.rows[1],
				tableIndex: 0,
				rowIndex: 1,
				draftLink: { key: "", items: [], open: false, anchor: null },
			},
			global: { stubs: { DraftLinkMenu } },
		});
		const remove = w.get(".jv-rowform-remove");
		expect(remove.attributes("aria-label")).toBe("Remove row 2");
		await remove.trigger("click");
		expect(w.emitted("remove")).toHaveLength(1);
		await w.get(".jv-rowform-close").trigger("click");
		expect(w.emitted("close")).toHaveLength(1);
		w.unmount();
	});
});
