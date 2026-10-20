// typing on a big table stays cheap. ChatView is too big to mount, so a small
// harness renders the same reads ChatView's template makes (a status per row, the
// summary, the "Saving removes" line, a cell per row) through the REAL memo, and the
// specs count calls and renders on a 500-row x 70-field table. A keystroke in Qty or
// Rate still re-renders (draftTotals reads them; accepted), so the keystrokes here
// are in Description, a field no total reads.
import fs from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { defineComponent, h, nextTick, reactive, ref } from "vue";

vi.mock("@/lib/rowForm", async (orig) => {
	const m = await orig();
	return { ...m, rowStatus: vi.fn(m.rowStatus), rowFormLayout: vi.fn(m.rowFormLayout) };
});
import { rowFormLayout, rowStatus } from "@/lib/rowForm";
import { parseSavedRows, removedSavedRows } from "@/lib/draftApply";
import { buildDraftTable, markMissingRows } from "@/lib/draftTables";
import { createRowStatusMemo, ruleParentKeys, ruleReadsIdx } from "@/lib/rowStatusMemo";
import DraftRowBlock from "@/components/chat/DraftRowBlock.vue";

const ROWS = 500;
const TYPES = [
	"Data",
	"Link",
	"Float",
	"Currency",
	"Check",
	"Int",
	"Small Text",
	"Select",
	"Date",
];
const names = ["description", ...Array.from({ length: 69 }, (_, i) => "f" + (i + 1))];
const form_fields = [];
names.forEach((fieldname, i) => {
	if (i % 10 === 0) form_fields.push({ fieldtype: "Section Break", label: "S" + i });
	form_fields.push({
		fieldname,
		label: fieldname,
		fieldtype: i === 0 ? "Small Text" : TYPES[i % TYPES.length],
		options: "",
		reqd: 0,
		read_only: 0,
		// Most rules read the record's update_stock; one reads a sibling field only.
		depends_on: i % 3 ? "eval:doc.f2 && parent.update_stock===1" : "",
		mandatory_depends_on: i % 7 ? "" : "eval:doc.f4==1",
	});
});
const spec = {
	label: "Items",
	child_doctype: "Row",
	columns: form_fields.filter((f) => f.fieldname).slice(0, 5),
	extra_columns: [],
	form_fields,
};
const savedRows = () =>
	Array.from({ length: ROWS }, (_, r) => {
		const o = { name: "r" + r };
		for (const n of names) o[n] = String(r % 5);
		return o;
	});
const check = (fieldname, value) => ({ fieldname, control: "check", value, orig: value });
const data = (fieldname, value) => ({ fieldname, control: "text", value, orig: value });

function makeModel() {
	return {
		verb: "update",
		doctype: "Sales Invoice",
		docName: "SI-1",
		docstatus: 0,
		parentDefaults: {},
		linkContext: {},
		fields: [check("update_stock", "Yes"), data("remarks", ""), data("title", "")],
		tables: [
			buildDraftTable({
				fieldname: "items",
				spec: structuredClone(spec), // a spec edits its table
				verb: "update",
				saved: savedRows(),
			}),
		],
	};
}

let wrapper;
let renders;
let cellRenders;
let memos = [];
afterEach(() => {
	if (wrapper) wrapper.unmount();
	memos.forEach((m) => m.dispose());
	memos = [];
});
beforeEach(() => {
	rowStatus.mockClear();
	rowFormLayout.mockClear();
});

// What ChatView's template reads: per row a status and a cell, per table the summary
// and the removed-rows line.
function mountPanel() {
	const model = ref(makeModel());
	const memo = createRowStatusMemo(() => model.value);
	memos.push(memo);
	renders = 0;
	cellRenders = new Map();
	const Cell = defineComponent({
		props: { row: { type: Object, required: true } },
		setup: (p) => () => {
			cellRenders.set(p.row.__key, (cellRenders.get(p.row.__key) || 0) + 1);
			return h("span", String(p.row.description));
		},
	});
	const Panel = defineComponent({
		setup: () => () => {
			renders++;
			return h(
				"div",
				model.value.tables.map((t, ti) =>
					h("div", { key: t.fieldname }, [
						h("p", { class: "removed" }, removedSavedRows(t).length),
						h("p", { class: "summary" }, (memo.summaries.value[ti] || {}).text || ""),
						...t.rows.map((r, ri) => {
							const s = memo.rowStatusFor(t, r, ri);
							return h("div", { key: r.__key, class: "row" }, [
								h(Cell, { row: r }),
								h("b", `${s.missing}:${s.changed}${r.__open ? ":open" : ""}`),
							]);
						}),
					])
				)
			);
		},
	});
	wrapper = mount(Panel);
	const t = () => model.value.tables[0];
	return { model, memo, t, flush: () => nextTick() };
}

describe("a keystroke on a 500-row x 70-field table", () => {
	it("typing in row 3 works out row 3's status once", async () => {
		const { t, flush } = mountPanel();
		rowStatus.mockClear();
		t().rows[3].description = "x";
		await flush();
		expect(rowStatus).toHaveBeenCalledTimes(1);
		expect(rowStatus.mock.calls[0][1].__key).toBe(t().rows[3].__key);
	});

	it("a keystroke that leaves the status as it was re-renders no other row", async () => {
		const { t, flush } = mountPanel();
		// The first edit makes the row differ from its saved one (a changed set of
		// {description}); the next ones leave the status as it is.
		t().rows[3].description = "x";
		await flush();
		const before = renders;
		const cellsBefore = new Map(cellRenders);
		t().rows[3].description = "xy";
		await flush();
		t().rows[3].description = "xyz";
		await flush();
		expect(renders).toBe(before); // the panel itself did not render
		const touched = [...cellRenders].filter(([k, n]) => n !== (cellsBefore.get(k) || 0));
		expect(touched.map(([k]) => k)).toEqual([t().rows[3].__key]); // only row 3's cell
	});

	it("a keystroke that changes the status renders once, and only that row's status is new", async () => {
		const { t, memo, flush } = mountPanel();
		const other = memo.rowStatusFor(t(), t().rows[4], 4);
		const before = renders;
		t().rows[3].description = "x"; // now differs from the saved row
		await flush();
		expect(renders).toBe(before + 1);
		expect(memo.rowStatusFor(t(), t().rows[4], 4)).toBe(other);
	});

	it("keeps a row's status object while nothing in it changes", async () => {
		const { t, memo, flush } = mountPanel();
		t().rows[3].description = "x";
		await flush();
		const s = memo.rowStatusFor(t(), t().rows[3], 3);
		const set = s.changedFields;
		t().rows[3].description = "xx";
		await flush();
		expect(memo.rowStatusFor(t(), t().rows[3], 3)).toBe(s);
		expect(memo.rowStatusFor(t(), t().rows[3], 3).changedFields).toBe(set);
	});

	it("does not parse origJson per render", async () => {
		const { t, flush } = mountPanel();
		const json = t().origJson;
		const parse = vi.spyOn(JSON, "parse");
		try {
			t().rows[3].description = "x"; // a re-render (the status changed)
			await flush();
			t().rows[3].description = "xy";
			await flush();
			t().rows.push({ ...t().rows[0], __key: 9001, __name: undefined }); // another render
			await flush();
			t().rows[1].f1 = "9";
			await flush();
			expect(parse.mock.calls.filter(([s]) => s === json)).toHaveLength(0);
		} finally {
			parse.mockRestore();
		}
	});

	it("typing in a main field no rule reads recomputes no row status", async () => {
		const { model, flush } = mountPanel();
		rowStatus.mockClear();
		const before = renders;
		model.value.fields[1].value = "a remark"; // remarks
		await flush();
		model.value.fields[2].value = "a title";
		await flush();
		expect(rowStatus).not.toHaveBeenCalled();
		expect(renders).toBe(before);
	});

	it("a main field a rule reads does recompute the rows that read it", async () => {
		const { model, t, flush } = mountPanel();
		rowStatus.mockClear();
		model.value.fields[0].value = "No"; // update_stock
		await flush();
		expect(rowStatus).toHaveBeenCalledTimes(ROWS);
		// and the new value reached the rules
		expect(rowStatus.mock.calls[0][2].update_stock).toBe(0);
		expect(t().rows).toHaveLength(ROWS);
	});

	it("the record's docstatus reaches the rules too", async () => {
		const { model, flush } = mountPanel();
		rowStatus.mockClear();
		model.value.docstatus = 1;
		await flush();
		expect(rowStatus).toHaveBeenCalledTimes(ROWS);
		expect(rowStatus.mock.calls[0][2].docstatus).toBe(1);
	});
});

describe("'Saving removes N rows' follows the rows, not the parse", () => {
	it("updates when a row is removed", async () => {
		const { t, flush } = mountPanel();
		const text = () => wrapper.find(".removed").text();
		expect(text()).toBe("0");
		t().rows.splice(1, 1);
		await flush();
		expect(text()).toBe("1");
		t().rows.splice(0, 1);
		await flush();
		expect(text()).toBe("2");
		// and the labels name the right rows
		expect(removedSavedRows(t())).toEqual(["0", "1"].map((v) => v));
	});

	it("parses origJson once however often it is asked", () => {
		const model = makeModel();
		const t = model.tables[0];
		t.origJson = JSON.stringify(savedRows().slice(0, 3)); // a string never parsed yet
		const parse = vi.spyOn(JSON, "parse");
		try {
			for (let i = 0; i < 5; i++) removedSavedRows(t);
			parseSavedRows(t.origJson);
			expect(parse.mock.calls.filter(([s]) => s === t.origJson)).toHaveLength(1);
		} finally {
			parse.mockRestore();
		}
	});

	it("survives a bad origJson", () => {
		expect(removedSavedRows({ origJson: "{nope", rows: [], columns: [] })).toEqual([]);
		expect(removedSavedRows({ origJson: "null", rows: [], columns: [] })).toEqual([]);
	});
});

describe("the rules' view of the record", () => {
	it("lists the parent.* names a table's rules read", () => {
		const keys = ruleParentKeys({
			formFields: [
				{ depends_on: "eval:parent.update_stock===1 && doc.parent" },
				{ mandatory_depends_on: "eval:in_list(['A'], parent.purpose)" },
				{ read_only_depends_on: "warehouse" }, // a bare fieldname reads doc
			],
		});
		expect([...keys].sort()).toEqual(["purpose", "update_stock"]);
	});

	it("falls back to every parent value for a rule that reads parent any other way", () => {
		expect(ruleParentKeys({ formFields: [{ depends_on: "eval:parent['x']" }] })).toBeNull();
		expect(ruleParentKeys({ formFields: [{ depends_on: "eval:f(parent)" }] })).toBeNull();
	});

	it("a table with such a rule sees an edit to any main field", async () => {
		const { model, t, flush } = mountPanel();
		t().formFields[1].depends_on = "eval:parent['remarks']";
		await flush();
		rowStatus.mockClear();
		model.value.fields[1].value = "z";
		await flush();
		expect(rowStatus).toHaveBeenCalledTimes(ROWS);
	});

	it("hands a table the same parent object while what its rules read is unchanged", async () => {
		const { model, t, memo, flush } = mountPanel();
		const p = memo.parentFor(t());
		model.value.fields[1].value = "z";
		await flush();
		expect(memo.parentFor(t())).toBe(p);
		model.value.fields[0].value = "No";
		await flush();
		expect(memo.parentFor(t())).not.toBe(p);
	});
});

describe("the open row's form follows the same rule", () => {
	it("does not lay out again for a keystroke in another row or an unread main field", async () => {
		const { model, t, memo, flush } = mountPanel();
		t().rows[7].__open = true;
		const row = t().rows[7];
		const Host = defineComponent({
			setup: () => () =>
				h(DraftRowBlock, {
					table: t(),
					row,
					parent: memo.parentFor(t()),
					tableIndex: 0,
					rowIndex: 7,
					changedFields: memo.rowStatusFor(t(), row, 7).changedFields,
					savedRow: memo.rowStatusFor(t(), row, 7).savedRow,
					draftLink: { open: false },
				}),
		});
		const host = mount(Host);
		try {
			await flush();
			rowFormLayout.mockClear();
			t().rows[3].description = "x";
			await flush();
			model.value.fields[1].value = "z";
			await flush();
			expect(rowFormLayout).not.toHaveBeenCalled();
			row.f9 = "7"; // its own row does
			await flush();
			expect(rowFormLayout).toHaveBeenCalled();
		} finally {
			host.unmount();
		}
	});
});

// A small card: three rows over a few fields, for the cases that need no big table.
function smallModel(extra = {}, verb = "create") {
	const f = (fieldname, e = {}) => ({
		fieldname,
		label: fieldname,
		fieldtype: "Data",
		options: "",
		reqd: 0,
		read_only: 0,
		...e,
	});
	const cols = [f("item"), f("note")];
	const saved = ["a", "b", "c"].map((n, i) => ({ name: "r" + i, item: n, note: "n" + i }));
	return reactive({
		verb,
		doctype: "Order",
		docName: "O-1",
		docstatus: 0,
		parentDefaults: {},
		linkContext: {},
		fields: [data("remarks", "")],
		tables: [
			buildDraftTable({
				fieldname: "items",
				spec: {
					label: "Items",
					child_doctype: "Row",
					columns: cols,
					extra_columns: [],
					form_fields: [...cols, f("tag", extra)],
				},
				verb: "update",
				saved,
			}),
		],
	});
}

describe("a discarded panel stops costing", () => {
	it("a second panel leaves only its own tables' watchers running", async () => {
		const { model, flush } = mountPanel();
		model.value = makeModel(); // panel B replaces panel A
		await flush();
		const stringify = vi.spyOn(JSON, "stringify");
		try {
			model.value.fields[1].value = "edit";
			await flush();
			// each live table's watcher compares its parent object once; A's is stopped
			const mine = stringify.mock.calls.filter(
				([a]) => a && typeof a === "object" && "docstatus" in a && "update_stock" in a
			);
			expect(mine).toHaveLength(1);
		} finally {
			stringify.mockRestore();
		}
	});
});

describe("a row's place is a real dependency (doc.idx)", () => {
	it("a row's badge follows the row after a removal above it", () => {
		const model = ref(smallModel({ depends_on: "eval:doc.idx==2" }));
		const memo = createRowStatusMemo(() => model.value);
		memos.push(memo);
		const t = () => model.value.tables[0];
		Object.assign(t().rows[1], { tag: "", __missing: ["tag"] }); // a failed save named it
		const badges = () => t().rows.map((r, ri) => memo.rowStatusFor(t(), r, ri).missing);
		expect(badges()).toEqual([0, 1, 0]);
		t().rows.splice(0, 1);
		expect(badges()).toEqual([1, 0]);
	});

	it("knows which rules read the row's place", () => {
		const rules = (rule) => ({ formFields: [{ depends_on: rule }] });
		expect(ruleReadsIdx(rules("eval:doc.idx>1"))).toBe(true);
		expect(ruleReadsIdx(rules("idx"))).toBe(true);
		expect(ruleReadsIdx(rules("eval:doc['idx']"))).toBe(true);
		expect(ruleReadsIdx(rules("eval:f(doc)"))).toBe(true);
		expect(ruleReadsIdx(rules("eval:doc.qty>1 && parent.x"))).toBe(false);
		expect(ruleReadsIdx(rules("eval:doc.idx_x>1"))).toBe(false);
	});

	it("a table whose rules do not read doc.idx is not woken by a removal", () => {
		const model = ref(smallModel());
		const memo = createRowStatusMemo(() => model.value);
		memos.push(memo);
		const t = () => model.value.tables[0];
		t().rows.forEach((r, ri) => memo.rowStatusFor(t(), r, ri));
		rowStatus.mockClear();
		t().rows.splice(0, 1);
		t().rows.forEach((r, ri) => memo.rowStatusFor(t(), r, ri));
		expect(rowStatus).not.toHaveBeenCalled();
	});
});

describe("a row's status follows the row", () => {
	const setup = () => {
		const model = ref(smallModel());
		const memo = createRowStatusMemo(() => model.value);
		memos.push(memo);
		const t = () => model.value.tables[0];
		return { model, memo, t, status: (ri) => memo.rowStatusFor(t(), t().rows[ri], ri) };
	};

	it("a failed save's missing fields set and clear the badge", () => {
		const { model, t, status } = setup();
		t().rows[1].tag = "";
		expect(status(1).missing).toBe(0);
		markMissingRows(model.value, [{ parentfield: "items", idx: 2, fieldname: "tag" }]);
		expect(status(1).missing).toBe(1);
		markMissingRows(model.value, []);
		expect(status(1).missing).toBe(0);
	});

	it("a row replaced wholesale gets its own status", () => {
		const { t, status } = setup();
		expect(status(1).changed).toBe(0);
		const before = status(1);
		t().rows[1] = { ...t().rows[1], note: "other", __key: 777 };
		const after = status(1);
		expect(after).not.toBe(before);
		expect(after.changedFields.has("note")).toBe(true);
	});

	it("reopening a panel starts from the new panel's rows", async () => {
		const model = ref(smallModel());
		const memo = createRowStatusMemo(() => model.value);
		memos.push(memo);
		const first = model.value.tables[0];
		first.rows[0].note = "edited";
		expect(memo.rowStatusFor(first, first.rows[0], 0).changedFields.has("note")).toBe(true);
		model.value = null;
		expect(memo.summaries.value).toEqual([]);
		model.value = smallModel();
		const t = model.value.tables[0];
		expect(memo.rowStatusFor(t, t.rows[0], 0).changedFields.size).toBe(0);
		expect(memo.parentFor(t).doctype).toBe("Order");
		model.value.docstatus = 1;
		await nextTick();
		expect(memo.parentFor(t).docstatus).toBe(1);
	});
});

describe("ChatView reads rows through the memo", () => {
	const src = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
	it("has no panel-wide status computed or whole-record parent any more", () => {
		expect(src).toMatch(/createRowStatusMemo\(\(\) => draftPanel\.value\)/);
		expect(src).not.toMatch(/draftRowStatuses|draftParentValues/);
		expect(src).toMatch(/const rowStatusFor = rowMemo\.rowStatusFor/);
		expect(src).toMatch(/const draftChangeSummaries = rowMemo\.summaries/);
	});
	it("hands the open row's form its table's parent object", () => {
		expect(src).toMatch(/<DraftRowBlock[^>]*:parent="rowMemo\.parentFor\(t\)"/s);
	});
	it("still works out 'Saving removes' from the current rows on every render", () => {
		expect(src).toMatch(/removedSavedRows\(t\)\.length/);
		expect(src).not.toMatch(/computed\([^)]*removedSavedRows/);
	});
});
