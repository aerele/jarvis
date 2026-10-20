// The draft grid's leading cell: the row number and the row-form toggle sit
// in the FIRST column, so they are on screen at scrollLeft 0 on a phone with no sticky
// CSS; Remove stays in the last one. ChatView is too big to mount, so this compiles the
// REAL <table class="jv-grid"> markup out of the view source and mounts it with stubbed
// cells and the same helpers ChatView passes in. Layout (the 96px cap, wrapping, tap
// targets, light/dark) is CSS and cannot be measured in jsdom: those are source-level
// assertions on the rules, and a real browser has not confirmed them.
import fs from "node:fs";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick, reactive } from "vue";
import { toggleLabel, toggleParts } from "@/lib/draftTables";
import { closeRowForm, rowFormId, rowToggleId, toggleRowForm } from "@/lib/rowFormFocus";

const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
const gridStart = src.indexOf('<table class="jv-grid">');
expect(gridStart).toBeGreaterThan(-1);
const gridMarkup = src.slice(gridStart, src.indexOf("</table>", gridStart) + "</table>".length);
// All declarations of every rule whose selector list names `selector`.
const css = src.slice(src.indexOf("<style")).replace(/\/\*[\s\S]*?\*\//g, "");
const cssRule = (selector) => {
	const out = [];
	for (const m of css.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
		const sels = m[1].split(",").map((x) => x.trim());
		if (sels.includes(selector)) out.push(m[2]);
	}
	expect(out.length, `${selector} rule`).toBeGreaterThan(0);
	return out.join("\n");
};

const col = (fieldname) => ({ fieldname, label: fieldname, fieldtype: "Data" });
const mkTable = (withForm, keys) => ({
	columns: [col("a"), col("b")],
	formFields: withForm ? [col("a")] : [],
	rows: keys.map((k) => ({ __key: k, __open: false, a: "x", b: "y" })),
});

let wrapper;
afterEach(() => wrapper?.unmount());

function mountGrid({ withForm = true, keys = ["k1", "k2", "k3"], status = () => ({}) } = {}) {
	const t = reactive(mkTable(withForm, keys));
	const removeDraftRow = vi.fn((ti, ri) => t.rows.splice(ri, 1));
	wrapper = mount(
		{
			setup: () => ({
				t,
				ti: 0,
				rowStatusFor: (tt, r, ri) => status(ri),
				rowToggleLabel: (tt, r, ri) => toggleLabel(ri + 1, status(ri)),
				toggleParts,
				rowToggleId,
				rowFormId,
				toggleRowForm,
				closeRowForm: (ti, r) => (r.__open = false),
				rowMemo: { parentFor: () => ({}) },
				removeDraftRow,
				draftLink: {},
				paletteVars: {},
				onDraftLink() {},
				closeDraftLink() {},
				pickDraftLink() {},
			}),
			template: `<div>${gridMarkup}</div>`,
		},
		{ attachTo: document.body, global: { stubs: { DraftCell: true, DraftRowBlock: true } } }
	);
	return { t, removeDraftRow };
}
const bodyRows = () => wrapper.findAll("tbody tr");

describe("the leading cell on every draft-grid row", () => {
	it("is the first cell of each row and carries the 1-based row number (the number the summary uses)", () => {
		mountGrid({ keys: ["k1", "k2", "k3"] });
		const rows = bodyRows();
		expect(rows).toHaveLength(3);
		rows.forEach((tr, i) => {
			const first = tr.element.children[0];
			expect(first.tagName).toBe("TD");
			expect(first.classList.contains("jv-grid-lead")).toBe(true);
			expect(first.querySelector(".jv-grid-num").textContent.trim()).toBe(String(i + 1));
		});
	});
	it("renumbers when a row above is removed", async () => {
		const { t } = mountGrid({ keys: ["k1", "k2", "k3"] });
		t.rows.splice(0, 1);
		await nextTick();
		const nums = bodyRows().map((tr) => tr.find(".jv-grid-num").text());
		expect(nums).toEqual(["1", "2"]);
	});
	it("holds the toggle, and Remove is not in it", () => {
		mountGrid();
		bodyRows().forEach((tr, i) => {
			const lead = tr.element.children[0];
			const toggle = lead.querySelector(".jv-grid-more");
			expect(toggle).not.toBeNull();
			expect(toggle.id).toBe(rowToggleId(0, `k${i + 1}`));
			expect(lead.querySelector(".jv-grid-del")).toBeNull();
			const last = tr.element.children[tr.element.children.length - 1];
			expect(last.classList.contains("jv-grid-x")).toBe(true);
			expect(last.querySelector(".jv-grid-del")).not.toBeNull();
			expect(last.querySelector(".jv-grid-more")).toBeNull();
		});
	});
	it("Remove still removes by position", async () => {
		const { removeDraftRow } = mountGrid();
		await bodyRows()[1].find(".jv-grid-del").trigger("click");
		expect(removeDraftRow).toHaveBeenCalledWith(0, 1);
	});
	it("keeps the short toggle text and the full-words aria-label", () => {
		mountGrid({ status: (ri) => (ri === 0 ? { missing: 2, changed: 3 } : {}) });
		const toggle = bodyRows()[0].find(".jv-grid-more");
		const badges = toggle.findAll(".jv-grid-badge").map((b) => b.text());
		expect(badges).toEqual(["2 required", "3 changed"]);
		expect(toggle.attributes("aria-label")).toBe(toggleLabel(1, { missing: 2, changed: 3 }));
		expect(toggle.attributes("title")).toBe(toggle.attributes("aria-label"));
	});
	it("a table with no form fields shows the number and no toggle", () => {
		mountGrid({ withForm: false });
		bodyRows().forEach((tr, i) => {
			expect(tr.find(".jv-grid-num").text()).toBe(String(i + 1));
			expect(tr.find(".jv-grid-more").exists()).toBe(false);
			expect(tr.find(".jv-grid-del").exists()).toBe(true);
		});
	});
});

describe("the header", () => {
	it("has a matching leading <th> with an accessible name", () => {
		mountGrid();
		const first = wrapper.find("thead tr").element.children[0];
		expect(first.tagName).toBe("TH");
		expect(first.classList.contains("jv-grid-lead")).toBe(true);
		const hidden = first.querySelector(".jv-sr");
		expect(hidden.textContent.trim()).toBe("Row");
		// one header cell per body cell, so columns line up
		const heads = wrapper.find("thead tr").element.children.length;
		expect(bodyRows()[0].element.children.length).toBe(heads);
	});
});

describe("focus return still lands on the toggle by key", () => {
	it("closing a row's form focuses that row's toggle in the leading cell", async () => {
		const { t } = mountGrid();
		t.rows.splice(0, 1); // a row above is gone: the key, not the position, finds it
		await nextTick();
		const row = t.rows[0];
		toggleRowForm(0, row);
		closeRowForm(0, row);
		await nextTick();
		await nextTick();
		expect(document.activeElement.id).toBe(rowToggleId(0, row.__key));
		expect(document.activeElement.closest("td").classList.contains("jv-grid-lead")).toBe(true);
	});
	it("clicking the toggle opens the row in place, as Desk does", async () => {
		const { t } = mountGrid();
		await bodyRows()[2].find(".jv-grid-more").trigger("click");
		expect(t.rows[2].__open).toBe(true);
		const rows = bodyRows();
		expect(rows).toHaveLength(3); // the row became its card; nothing was added below
		const cell = rows[2].element.children[0];
		expect(rows[2].classes()).toContain("jv-grid-openrow");
		expect(cell.getAttribute("colspan")).toBe(String(t.columns.length + 2));
		expect(rows[2].find("draft-row-block-stub").exists()).toBe(true);
		expect(rows[1].find(".jv-grid-more").exists()).toBe(true); // the others stay rows
	});
});

describe("the CSS (source-level: jsdom does no layout)", () => {
	it("caps the leading cell at 96px and overrides the 90px column minimum with higher specificity", () => {
		const base = cssRule(".jv-grid td");
		expect(base).toMatch(/min-width:\s*90px/);
		const lead = cssRule(".jv-grid td.jv-grid-lead");
		expect(lead).toMatch(/max-width:\s*96px/);
		expect(lead).toMatch(/width:\s*96px/);
		expect(lead).toMatch(/min-width:\s*0\b/);
		// `.jv-grid td` is (0,1,1); `.jv-grid td.jv-grid-lead` is (0,2,1), so it wins in any order
	});
	it("caps the header cell too, so the column cannot be wider than its cells", () => {
		expect(cssRule(".jv-grid th.jv-grid-lead")).toMatch(/max-width:\s*96px/);
	});
	it("the toggle's badges can wrap instead of widening the cell", () => {
		expect(cssRule(".jv-grid-lead .jv-grid-more")).toMatch(/flex-wrap:\s*wrap/);
		expect(cssRule(".jv-grid-lead .jv-grid-more")).toMatch(/max-width:\s*100%/);
	});
	it("uses theme tokens only: no hardcoded colour in the lead rules", () => {
		for (const sel of [
			".jv-grid td.jv-grid-lead",
			".jv-grid th.jv-grid-lead",
			".jv-grid-num",
			".jv-grid-lead .jv-grid-more",
		]) {
			expect(cssRule(sel), sel).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/);
		}
		expect(cssRule(".jv-grid-num")).toMatch(/color:\s*var\(--/);
	});
	it("adds no sticky positioning (the cell is first, so it needs none)", () => {
		expect(cssRule(".jv-grid td.jv-grid-lead")).not.toMatch(/sticky/);
	});
});
