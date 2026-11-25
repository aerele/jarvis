// Zero desktop change for the shared draft components that the phone now draws (rule P10).
//
// DraftEditForm, DraftPreview and DraftLinkMenu (shared) and the phone's Sheet each gained an
// additive `surface` / `full` option. With the option absent, or "desktop", what the desktop draws
// must be exactly what it drew before. The baseline file was recorded from the components at the
// commit BEFORE the option existed (`DRAFT_SURFACE_BASELINE=write npx vitest run ...` only when
// the desktop look is changed on purpose, and say so in the commit):
//   html   the markup of each case
//   style  the whole `<style>` text of each component; the current text must START WITH it, so a
//          phone rule can only be appended after the old ones, never edit or reorder them
// A markup comparison between "prop absent" and "prop desktop" alone could not see a change that
// hits both (a new v-if leaves a comment in the desktop markup), hence the stored baseline.
//
// A browser capture of the desktop panel is still owed (jsdom draws no layout).
import fs from "node:fs";
import path from "node:path";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { h, reactive } from "vue";
import DraftEditForm from "./chat/DraftEditForm.vue";
import DraftLinkMenu from "./chat/DraftLinkMenu.vue";
import DraftPreview from "./doc/DraftPreview.vue";
import Sheet from "../../../pwa/src/components/Sheet.vue";
import { panelField } from "../lib/docFields.js";
import { buildDraftTable } from "../lib/draftTables.js";
import { timesheetModel } from "../lib/timesheetDraftFixture.js";

const FILE = path.resolve(__dirname, "draftSurfaceBaseline.json");
const SOURCES = {
	DraftEditForm: "chat/DraftEditForm.vue",
	DraftPreview: "doc/DraftPreview.vue",
	DraftLinkMenu: "chat/DraftLinkMenu.vue",
	Sheet: "../../../pwa/src/components/Sheet.vue",
};
const styleOf = (name) => {
	const src = fs.readFileSync(path.resolve(__dirname, SOURCES[name]), "utf8");
	const start = src.indexOf("<style scoped>");
	return src.slice(start + "<style scoped>".length, src.indexOf("</style>", start));
};

const meta = (fieldname, fieldtype, extra = {}) => ({
	fieldname,
	label: fieldname,
	fieldtype,
	options: "",
	reqd: 0,
	read_only: 0,
	...extra,
});
// An update draft with every control kind, a changed field, a server-marked field and a saved
// table whose proposal drops one row.
function updateModel() {
	const fields = [
		panelField(meta("title", "Data", { reqd: 1 }), "abc"),
		panelField(meta("customer", "Link", { options: "Customer" }), "CUST-1"),
		panelField(meta("kind", "Select", { options: "A\nB" }), "B"),
		panelField(meta("flag", "Check"), "1"),
		panelField(meta("day", "Date"), "2026-01-02"),
		panelField(meta("note", "Small Text"), "hello"),
		panelField(meta("ro", "Data", { read_only: 1 }), "fixed"),
	];
	fields[0].changed = true;
	fields[1].serverInvalid = true;
	const cols = [
		meta("item", "Link", { options: "Item" }),
		meta("qty", "Float"),
		meta("rate", "Float"),
	];
	const saved = [0, 1, 2].map((i) => ({
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
		proposed: saved.slice(0, 2).map((r) => ({ ...r, tag: "NEW" })),
	});
	return {
		verb: "update",
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
	};
}
const link = (over = {}) => reactive({ key: "", items: [], open: false, anchor: null, ...over });

// A grid row's id carries its key from a counter that runs on across renders: compare without it.
const norm = (html) => html.replace(/(jv-row[a-z]+-\d+)-\d+/g, "$1-K");

// Each case renders one component and returns its markup. `surface` is added to the props only
// when given: the baseline was recorded with the prop absent.
const withSurface = (props, surface) => (surface === undefined ? props : { ...props, surface });
const CASES = {
	"form: timesheet create, plain": (surface) =>
		mount(DraftEditForm, {
			props: withSurface(
				{ model: reactive(timesheetModel()), draftLink: link(), palette: {} },
				surface
			),
		}).html(),
	"form: update with marks, error slot, parked note, link error": (surface) => {
		const model = reactive({
			...updateModel(),
			error: { message: "Fix it" },
			parked: "Parked.",
			updatedToast: true,
		});
		return mount(DraftEditForm, {
			props: withSurface(
				{ model, draftLink: link(), palette: {}, linkError: "Could not load." },
				surface
			),
			slots: { error: () => h("p", { class: "slot-error" }, "slot error") },
		}).html();
	},
	"preview: timesheet create": (surface) =>
		mount(DraftPreview, {
			props: withSurface({ model: timesheetModel() }, surface),
			global: { stubs: { transition: false } },
		}).html(),
	"preview: update with a headline, a label and disabled": (surface) =>
		mount(DraftPreview, {
			props: withSurface(
				{
					model: updateModel(),
					headline: "Change an order",
					confirmLabel: "Save changes",
					disabled: true,
				},
				surface
			),
			global: { stubs: { transition: false } },
		}).html(),
	"link menu": (surface) => {
		const w = mount(DraftLinkMenu, {
			props: {
				anchor: document.createElement("input"),
				items: [
					{ value: "CUST-1", label: "Acme" },
					{ value: "CUST-2", label: "" },
				],
				palette: { "--surface": "#fff" },
			},
			attachTo: document.body,
		});
		const html = document.body.innerHTML;
		w.unmount();
		return html;
	},
	"phone sheet, open": (surface) => {
		const w = mount(Sheet, {
			props: { open: true, label: "Actions" },
			slots: { default: "<button>One</button>" },
			attachTo: document.body,
		});
		const html = document.body.innerHTML;
		w.unmount();
		return html;
	},
};

beforeAll(() => {
	// DraftLinkMenu places itself with these; jsdom has neither.
	document.elementFromPoint = () => null;
	globalThis.ResizeObserver = class {
		observe() {}
		disconnect() {}
	};
});
afterEach(() => {
	document.body.innerHTML = "";
});

if (process.env.DRAFT_SURFACE_BASELINE === "write") {
	it("writes the baseline", () => {
		const html = Object.fromEntries(Object.entries(CASES).map(([k, run]) => [k, run()]));
		const style = Object.fromEntries(Object.keys(SOURCES).map((k) => [k, styleOf(k)]));
		fs.writeFileSync(FILE, JSON.stringify({ html, style }, null, "\t") + "\n");
	});
} else {
	const baseline = JSON.parse(fs.readFileSync(FILE, "utf8"));
	describe("the desktop draws what it drew before the phone surface existed", () => {
		// Break: a new element, class, attribute or a v-if comment appears in the desktop markup of a
		// shared component, with the surface prop absent or "desktop". No other test holds the whole
		// markup of these components.
		for (const name of Object.keys(CASES)) {
			it(`${name}: prop absent`, () =>
				expect(norm(CASES[name]())).toBe(norm(baseline.html[name])));
		}
		for (const name of Object.keys(CASES).filter((n) => /^(form|preview):/.test(n))) {
			it(`${name}: surface "desktop"`, () =>
				expect(norm(CASES[name]("desktop"))).toBe(norm(baseline.html[name])));
		}
	});

	describe("the style blocks only grew at the end", () => {
		// Break: a desktop rule of a shared component is edited, reordered or deleted (a phone rule
		// belongs after the old ones, behind its own `surface` class).
		it.each(Object.keys(SOURCES))("%s", (name) => {
			expect(styleOf(name).startsWith(baseline.style[name])).toBe(true);
		});
	});
}
