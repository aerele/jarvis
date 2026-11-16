// The draft edit form, mounted for real. The warn handler throws, so an identifier the
// template uses but the component does not define (a rename slip when the body moved out of
// ChatView) fails here instead of rendering blank in a browser.
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { computed, defineComponent, h, nextTick, reactive, ref } from "vue";
import DraftEditForm from "./DraftEditForm.vue";
import { panelField } from "../../lib/docFields";
import { buildDraftTable } from "../../lib/draftTables";

beforeAll(() => {
	// DraftLinkMenu places itself with these; jsdom has neither.
	document.elementFromPoint = () => null;
	globalThis.ResizeObserver = class {
		observe() {}
		disconnect() {}
	};
});

const meta = (fieldname, fieldtype, extra = {}) => ({
	fieldname,
	label: fieldname,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});
const linkState = (over = {}) =>
	reactive({ key: "", items: [], open: false, anchor: null, ...over });

// Main fields of every control kind and a saved Items table. `rows` saved rows; the agent's
// proposal keeps all but the last (so saving removes one) and changes a field outside the grid
// (tag) on each, so the table shows `rows - 1` rows.
function makeModel({ rows = 6, verb = "update", extra = {} } = {}) {
	const fields = [
		panelField(meta("title", "Data", { reqd: 1 }), "abc"),
		panelField(meta("customer", "Link", { options: "Customer" }), "CUST-1"),
		panelField(meta("kind", "Select", { options: "A\nB" }), "B"),
		panelField(meta("flag", "Check"), "1"),
		panelField(meta("day", "Date"), "2026-01-02"),
		panelField(meta("ro", "Data", { read_only: 1 }), "fixed"),
		panelField(meta("later", "Data", { reqd: 1 }), ""),
	];
	fields[0].changed = true;
	fields[1].serverInvalid = true;
	fields[6].serverMissing = true;
	const cols = [
		meta("item", "Link", { options: "Item" }),
		meta("qty", "Float"),
		meta("rate", "Float"),
	];
	const saved = Array.from({ length: rows }, (_, i) => ({
		name: "r" + i,
		item: "I" + i,
		qty: i + 1,
		rate: 10,
		tag: "t" + i,
	}));
	const items = buildDraftTable({
		fieldname: "items",
		spec: {
			label: "Items",
			child_doctype: "Row",
			columns: cols,
			extra_columns: [],
			form_fields: [...cols, meta("tag", "Data")],
		},
		verb: "update",
		saved,
		proposed: saved.slice(0, rows - 1).map((r) => ({ ...r, tag: "NEW" })),
	});
	return reactive({
		verb,
		doctype: "Order",
		docName: "O-1",
		docstatus: 0,
		parentDefaults: {},
		linkContext: {},
		fields,
		tables: [items],
		error: "",
		parked: "",
		updatedToast: false,
		...extra,
	});
}

let wrapper;
afterEach(() => wrapper?.unmount());
function mountForm(model, props = {}, slots = {}) {
	wrapper = mount(DraftEditForm, {
		props: { model, draftLink: linkState(), palette: { "--cta": "red" }, ...props },
		slots,
		attachTo: document.body,
		global: {
			stubs: { teleport: true },
			config: {
				warnHandler(msg) {
					throw new Error(msg);
				},
			},
		},
	});
	return wrapper;
}

describe("the form body renders the model", () => {
	it("marks changed, invalid and missing fields, and shows a read-only field as text", () => {
		mountForm(makeModel());
		const flds = wrapper.findAll(".jv-draft-fld");
		expect(flds).toHaveLength(7);
		expect(flds[0].classes()).toContain("changed");
		expect(flds[0].find(".jv-req").exists()).toBe(true); // required
		expect(flds[1].classes()).toContain("invalid");
		expect(flds[5].find(".jv-draft-ro").text()).toBe("fixed");
		expect(flds[5].find("input").exists()).toBe(false);
		expect(flds[6].classes()).toContain("missing"); // a blank required value
		expect(wrapper.find(".jv-draft-body select.jv-action-sel").exists()).toBe(true);
		expect(wrapper.find('.jv-draft-body input[type="date"]').element.value).toBe("2026-01-02");
	});

	it("is the single root the panel's flex column and the e2e selectors rely on", () => {
		mountForm(makeModel());
		expect(wrapper.element.tagName).toBe("DIV");
		expect(wrapper.element.className).toBe("jv-draft-body");
	});

	it("shows the link error and the 'updated from chat' toast only when set", async () => {
		mountForm(makeModel({ extra: { updatedToast: true } }), { linkError: "Could not search" });
		expect(wrapper.find('p.jv-draft-toast[role="alert"]').text()).toBe("Could not search");
		expect(wrapper.text()).toContain("Draft updated from chat");
		await wrapper.setProps({ linkError: "" });
		expect(wrapper.find('[role="alert"]').exists()).toBe(false);
	});

	it("summarises changed rows: named buttons open a row, the grouped button opens all of them", async () => {
		const model = makeModel({ rows: 4 }); // 3 rows shown, all changed: named
		mountForm(model);
		const named = wrapper.findAll(".jv-rows-changed-open");
		expect(named.map((b) => b.text())).toEqual(["1", "2", "3"]);
		await named[1].trigger("click");
		expect(model.tables[0].rows[1].__open).toBe(true);
		expect(model.tables[0].rows[0].__open).toBeFalsy();
		await wrapper.unmount();

		const big = makeModel({ rows: 7 }); // 6 rows shown, all changed: grouped, with a count button
		mountForm(big);
		const grouped = wrapper.find(".jv-rows-changed-open");
		expect(grouped.text()).toBe("Open 6 changed rows");
		await grouped.trigger("click");
		expect(big.tables[0].rows.filter((r) => r.__open)).toHaveLength(6);
		expect(wrapper.findAll(".jv-grid-openrow")).toHaveLength(6); // each open row is its own card in place
	});

	it("says which saved rows saving removes", () => {
		mountForm(makeModel());
		expect(wrapper.find(".jv-rows-removed").text()).toBe("Saving removes 1 saved row: I5");
	});

	it("follows a replaced model (the panel swaps the object when the draft is rebuilt)", async () => {
		mountForm(makeModel({ rows: 6 }));
		expect(wrapper.findAll("tbody tr")).toHaveLength(5);
		await wrapper.setProps({ model: makeModel({ rows: 3 }) });
		expect(wrapper.findAll("tbody tr")).toHaveLength(2);
		expect(wrapper.find(".jv-rows-removed").text()).toContain("1 saved row");
		expect(wrapper.findAll(".jv-rows-changed-open").map((b) => b.text())).toEqual(["1", "2"]);
	});
});

describe("totals, errors and the parked note", () => {
	it("totals Qty and Est. Total from the rows, and drops the amount when no rate column exists", async () => {
		const model = makeModel({ rows: 4 }); // 3 rows: qty 1 + 2 + 3, rate 10
		mountForm(model);
		const totals = wrapper.findAll(".jv-draft-total-input").map((i) => i.element.value);
		expect(totals).toEqual(["6", "60.00"]);
		model.tables[0].rows[0].qty = "10";
		await nextTick();
		expect(wrapper.findAll(".jv-draft-total-input")[0].element.value).toBe("15");
		const noRate = makeModel({ rows: 3 });
		noRate.tables[0].columns = noRate.tables[0].columns.filter((c) => c.fieldname !== "rate");
		noRate.tables[0].formFields = noRate.tables[0].formFields.filter(
			(c) => c.fieldname !== "rate"
		);
		await wrapper.setProps({ model: noRate });
		expect(wrapper.findAll(".jv-draft-total-input")).toHaveLength(1);
	});

	it("renders the host's error slot only while the model has an error", async () => {
		const model = makeModel();
		mountForm(model, {}, { error: () => h("div", { class: "host-error" }, "boom") });
		expect(wrapper.find(".host-error").exists()).toBe(false);
		model.error = { message: "nope" };
		await nextTick();
		expect(wrapper.find(".host-error").text()).toBe("boom");
	});

	it("always keeps the status region, empty until a note is parked, and exposes it for the host's focus", async () => {
		const model = makeModel();
		mountForm(model);
		const region = wrapper.find('.jv-draft-parked[role="status"]');
		expect(region.classes()).toContain("is-empty");
		expect(region.attributes("aria-live")).toBe("polite");
		model.parked = "Waiting for approval";
		await nextTick();
		expect(region.classes()).not.toContain("is-empty");
		expect(region.text()).toBe("Waiting for approval");
	});
});

describe("the host reaches the status region the way ChatView does", () => {
	it("exposes parkedRegion through a template ref, so the host can focus it after a parked save", () => {
		const model = makeModel();
		const Host = defineComponent({
			components: { DraftEditForm },
			setup() {
				const formEl = ref(null);
				// ChatView: const draftParkedRegion = computed(() => draftFormEl.value?.parkedRegion ?? null)
				return {
					model,
					link: linkState(),
					formEl,
					region: computed(() => formEl.value?.parkedRegion ?? null),
				};
			},
			template: '<DraftEditForm ref="formEl" :model="model" :draft-link="link" />',
		});
		wrapper = mount(Host, { attachTo: document.body });
		const region = wrapper.find('.jv-draft-parked[role="status"]').element;
		expect(wrapper.vm.region).toBe(region);
		wrapper.vm.region.focus();
		expect(document.activeElement).toBe(region);
	});
});

describe("editing the model in place", () => {
	it("typing in a field writes the model object the panel saves from", async () => {
		const model = makeModel();
		mountForm(model);
		await wrapper.find(".jv-draft-fld input").setValue("changed title");
		expect(model.fields[0].value).toBe("changed title");
	});

	it("Add row and Remove row edit the given model, with a fresh row key", async () => {
		const model = makeModel({ rows: 4 });
		mountForm(model);
		const before = model.tables[0].rows.map((r) => r.__key);
		await wrapper.find(".jv-draft-addrow").trigger("click");
		const rows = model.tables[0].rows;
		expect(rows).toHaveLength(4);
		const added = rows[3].__key;
		expect(before).not.toContain(added);
		await wrapper.findAll(".jv-grid-del")[0].trigger("click");
		expect(model.tables[0].rows.map((r) => r.__key)).toEqual([before[1], before[2], added]);
	});

	it("an open row's Insert below adds a blank row after it, opens that one and closes the first", async () => {
		const model = makeModel({ rows: 4 });
		model.tables[0].rows[0].__open = true;
		mountForm(model);
		const insert = wrapper
			.findAll("button")
			.find((b) => /insert/i.test(b.text() + b.attributes("title")));
		expect(insert, "the open row's Insert control").toBeTruthy();
		await insert.trigger("click");
		const rows = model.tables[0].rows;
		expect(rows).toHaveLength(4);
		expect(rows[0].__open).toBe(false);
		expect(rows[1].__open).toBe(true);
		expect(rows[1].__name).toBeFalsy(); // a blank row, not a saved one
	});
});

describe("link search is the host's: the form only forwards it", () => {
	it("a main link field emits link-search with its key, a value getter, the doctype and the event", async () => {
		const model = makeModel();
		mountForm(model);
		const input = wrapper.find('.jv-draft-fld input[placeholder="Search Customer…"]');
		await input.trigger("focus");
		const [key, target, doctype, ev] = wrapper.emitted("link-search")[0];
		expect(key).toBe("f:customer");
		expect(target()).toBe("CUST-1");
		expect(doctype).toBe("Customer");
		expect(ev.target).toBe(input.element); // the anchor the host measures and re-checks
		await input.trigger("blur");
		expect(wrapper.emitted("link-blur")[0][0].target).toBe(input.element);
	});

	it("a grid cell's link search carries the row's stable key through the form", async () => {
		const model = makeModel({ rows: 2 });
		mountForm(model);
		const cellInput = wrapper.find('tbody td input[aria-label="item"]');
		await cellInput.trigger("focus");
		const [key, target, doctype] = wrapper.emitted("link-search")[0];
		expect(key).toBe(`t:0:${model.tables[0].rows[0].__key}:item`);
		expect(target()).toBe("I0");
		expect(doctype).toBe("Item");
	});

	it("shows the menu for the open key only, forwards a pick with a setter, and never closes it itself", async () => {
		const model = makeModel();
		const draftLink = linkState({
			key: "f:customer",
			open: true,
			items: [{ value: "CUST-2", label: "Second" }],
			anchor: document.body,
		});
		mountForm(model, { draftLink });
		const menu = wrapper.find(".jv-action-linkmenu");
		expect(menu.exists()).toBe(true);
		expect(wrapper.findAll(".jv-action-linkmenu")).toHaveLength(1);
		await menu.find("button").trigger("click");
		const [setter, item] = wrapper.emitted("link-pick")[0];
		expect(item.value).toBe("CUST-2");
		setter("CUST-2"); // the host calls it after fetching the rule values
		expect(model.fields[1].value).toBe("CUST-2");
		await menu.find("button").trigger("keydown.esc");
		expect(wrapper.emitted("link-close")).toHaveLength(1);
		expect(draftLink.open).toBe(true); // the host owns the state and closes it on the event
	});
});
