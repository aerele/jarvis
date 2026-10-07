import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * "Duplicate to edit" on a built-in PDF template (jarvis-admin-v2#638).
 *
 * Built-ins are a read-only spec sheet. The button copies one into the custom
 * editor, prefilled from the built-in's whole spec, under a fresh `<key>-copy`
 * key, and Save goes through the same save API a hand-built template uses.
 */

vi.hoisted(() => {
	window.matchMedia = (q) => ({
		matches: false,
		media: q,
		onchange: null,
		addEventListener() {},
		removeEventListener() {},
		addListener() {},
		removeListener() {},
		dispatchEvent: () => false,
	});
});

// Stubs keep what the tests read observable: Button label + click, FormControl
// as a plain input bound to modelValue (so the editor's values are readable).
vi.mock("frappe-ui", () => {
	const passthrough = (name, tag) => ({
		name,
		props: ["label", "theme", "variant", "iconLeft", "loading", "disabled", "modelValue"],
		template: `<${tag} class="stub-${name.toLowerCase()}" :data-label="label" :data-variant="variant" :disabled="disabled"><slot /></${tag}>`,
	});
	return {
		Badge: passthrough("Badge", "span"),
		Button: {
			name: "Button",
			props: ["label", "theme", "variant", "iconLeft", "loading", "disabled"],
			emits: ["click"],
			template: `<button class="stub-button" :data-label="label" :data-variant="variant" :disabled="disabled" @click="$emit('click', $event)"><slot /></button>`,
		},
		Checkbox: passthrough("Checkbox", "span"),
		ErrorMessage: passthrough("ErrorMessage", "span"),
		FeatherIcon: passthrough("FeatherIcon", "span"),
		FormControl: {
			name: "FormControl",
			props: ["label", "modelValue", "type", "options"],
			emits: ["update:modelValue"],
			template: `<input class="stub-formcontrol" :data-label="label" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
		},
		Switch: passthrough("Switch", "span"),
		TabButtons: passthrough("TabButtons", "div"),
		Tooltip: passthrough("Tooltip", "span"),
		confirmDialog: vi.fn(),
		toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
	};
});

vi.mock("@/api", () => ({
	deletePdfTemplate: vi.fn(),
	getPdfTemplate: vi.fn(),
	listPdfTemplates: vi.fn(),
	pdfTemplateOptions: vi.fn(),
	previewPdfTemplate: vi.fn(() => Promise.resolve({ data: { html: "<p>x</p>" } })),
	savePdfTemplate: vi.fn(),
	setDefaultPdfTemplate: vi.fn(),
}));

import * as api from "@/api";
import PdfTemplatesPane from "./PdfTemplatesPane.vue";

const BRANDED = {
	key: "branded",
	label: "Branded",
	description: "Brand green with a bold accent bar.",
	accent: "#2e7d6b",
	body_font: "sans",
	display_font: "sans",
	masthead: "left",
	cover: "auto",
	dark: "#0f3d34",
	show_logo: true,
	accent_bar: true,
	watermark: "",
	page_size: "A4",
	orientation: "portrait",
	margins_mm: 15,
	custom: false,
	enabled: true,
};
const FORMAL = {
	...BRANDED,
	key: "formal",
	label: "Formal",
	accent: "#2c2c2c",
	dark: "#141414",
	accent_bar: false,
	watermark: "CONFIDENTIAL",
	margins_mm: 20,
	body_font: "serif",
	display_font: "serif",
};
const MINE = { ...BRANDED, key: "mine", label: "Mine", custom: true };

function listWith(templates, def = "branded") {
	api.listPdfTemplates.mockResolvedValue({ data: { templates, default: def } });
}

async function mountPane(templates, { def } = {}) {
	window.is_jarvis_admin = true;
	listWith(templates, def);
	const w = mount(PdfTemplatesPane, { global: { stubs: { SettingsPane: false } } });
	await flushPromises();
	return w;
}

const labels = (w) => w.findAll(".stub-button").map((b) => b.attributes("data-label"));
const duplicateBtn = (w) =>
	w.findAll(".stub-button").find((b) => b.attributes("data-label") === "Duplicate to edit");
const field = (w, label) =>
	w.findAll(".stub-formcontrol").find((i) => i.attributes("data-label") === label);

beforeEach(() => {
	vi.clearAllMocks();
	api.pdfTemplateOptions.mockResolvedValue({
		data: { companies: [], letter_heads: [], fonts: [{ key: "sans", label: "Sans" }] },
	});
	api.previewPdfTemplate.mockResolvedValue({ data: { html: "<p>x</p>" } });
	api.getPdfTemplate.mockResolvedValue({
		data: { template_key: "mine", label: "Mine", enabled: 1, accent_color: "#2e7d6b" },
	});
	api.savePdfTemplate.mockResolvedValue({ data: { key: "branded-copy" } });
});

afterEach(() => {
	delete window.is_jarvis_admin;
});

describe("PdfTemplatesPane Duplicate to edit", () => {
	it("shows the button on a built-in's sheet only", async () => {
		const w = await mountPane([BRANDED, MINE], { def: "branded" });
		expect(duplicateBtn(w)).toBeTruthy();
		expect(duplicateBtn(w).attributes("data-variant")).toBe("subtle");

		// Switch to the custom template: no button.
		await w
			.findAll("[role=button]")
			.find((n) => n.text().includes("Mine"))
			.trigger("click");
		await flushPromises();
		expect(labels(w)).not.toContain("Duplicate to edit");
	});

	it("opens the editor prefilled from the built-in with a copy key", async () => {
		const w = await mountPane([FORMAL, BRANDED], { def: "formal" });
		await duplicateBtn(w).trigger("click");
		await flushPromises();

		expect(field(w, "Template key").element.value).toBe("formal-copy");
		expect(field(w, "Label").element.value).toBe("Formal (copy)");
		expect(labels(w)).not.toContain("Duplicate to edit");
	});

	it("suffixes -copy-2 when -copy is taken", async () => {
		const taken = { ...MINE, key: "formal-copy", label: "Formal (copy)" };
		const w = await mountPane([FORMAL, taken], { def: "formal" });
		await duplicateBtn(w).trigger("click");
		await flushPromises();
		expect(field(w, "Template key").element.value).toBe("formal-copy-2");
	});

	it("saves the built-in's whole spec through savePdfTemplate", async () => {
		const w = await mountPane([FORMAL, BRANDED], { def: "formal" });
		await duplicateBtn(w).trigger("click");
		await flushPromises();
		const save = w.findAll(".stub-button").find((b) => b.attributes("data-label") === "Save");
		await save.trigger("click");
		await flushPromises();

		expect(api.savePdfTemplate).toHaveBeenCalledTimes(1);
		const payload = JSON.parse(api.savePdfTemplate.mock.calls[0][0]);
		expect(payload).toMatchObject({
			template_key: "formal-copy",
			label: "Formal (copy)",
			accent_color: "#2c2c2c",
			dark_color: "#141414",
			body_font: "serif",
			display_font: "serif",
			masthead_align: "left",
			cover: "auto",
			watermark: "CONFIDENTIAL",
			page_size: "A4",
			orientation: "portrait",
			margins_mm: 20,
			show_logo: true,
			accent_bar: false,
			enabled: true,
			// Faithful copy: the server layers these on the built-in's full spec,
			// and refuses (rather than overwrites) a key that already exists.
			based_on: "formal",
			is_new: 1,
		});
	});

	it("sends is_new for a blank New template too, and no based_on", async () => {
		const w = await mountPane([FORMAL], { def: "formal" });
		await w
			.findAll(".stub-button")
			.find((b) => b.attributes("data-label") === "New template")
			.trigger("click");
		await flushPromises();
		await field(w, "Template key").setValue("acme");
		await w
			.findAll(".stub-button")
			.find((b) => b.attributes("data-label") === "Save")
			.trigger("click");
		await flushPromises();
		const payload = JSON.parse(api.savePdfTemplate.mock.calls[0][0]);
		expect(payload.is_new).toBe(1);
		expect(payload.based_on).toBeUndefined();
	});
});
