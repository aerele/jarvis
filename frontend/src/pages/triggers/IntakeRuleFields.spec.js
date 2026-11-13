import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const apiTr = vi.hoisted(() => ({
	listIntakeFolders: vi.fn(),
	getIntakeFieldOptions: vi.fn(),
}));
vi.mock("@/api/triggers", () => apiTr);
vi.mock("@/api", () => ({ searchLink: vi.fn(() => Promise.resolve([])) }));

vi.mock("frappe-ui", () => ({
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "modelValue", "placeholder"],
		emits: ["update:modelValue", "update:query"],
		template: `<div class="autocomplete" :data-placeholder="placeholder" />`,
	},
	Button: {
		name: "Button",
		props: ["label", "variant", "disabled"],
		emits: ["click"],
		template: `<button :data-label="label" :data-variant="variant" @click="$emit('click')">{{ label }}</button>`,
	},
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: `<div v-if="message" class="error-message">{{ message }}</div>`,
	},
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "label", "options", "disabled"],
		emits: ["update:modelValue"],
		template: `<input :type="type === 'checkbox' ? 'checkbox' : 'text'" :aria-label="label" :value="modelValue" :checked="type === 'checkbox' ? !!modelValue : undefined" :disabled="disabled" @input="type !== 'checkbox' && $emit('update:modelValue', $event.target.value)" @change="type === 'checkbox' && $emit('update:modelValue', $event.target.checked)" />`,
	},
}));

import IntakeRuleFields from "./IntakeRuleFields.vue";

const TODO = {
	fields: [
		{
			fieldname: "description",
			label: "Description",
			fieldtype: "Text Editor",
			options: "",
			mandatory: true,
		},
		{
			fieldname: "status",
			label: "Status",
			fieldtype: "Select",
			options: "Open\nClosed",
			mandatory: false,
		},
		{
			fieldname: "allocated_to",
			label: "Allocated To",
			fieldtype: "Link",
			options: "User",
			mandatory: false,
		},
		{
			fieldname: "reference_type",
			label: "Reference Type",
			fieldtype: "Link",
			options: "DocType",
			mandatory: true,
		},
	],
	tables: [],
};

function formOf(over = {}) {
	return {
		intake_source: "Folder",
		intake_folder: "",
		intake_record_doctype: "",
		intake_target_doctype: "ToDo",
		intake_spec: { fields: ["description"], table: null, match: [], defaults: {} },
		intake_instructions: "",
		allow_incomplete_drafts: 0,
		intake_status_field: "",
		intake_link_field: "",
		llm_daily_cap: 100,
		...over,
	};
}

async function mountIt(form) {
	const w = mount(IntakeRuleFields, { props: { form } });
	await flushPromises();
	return w;
}
const patches = (w) => w.emitted("update").map((e) => e[0]);
const box = (w, label) => w.find(`input[aria-label="${label}"]`);

beforeEach(() => {
	apiTr.listIntakeFolders.mockReset().mockResolvedValue(["Home/Production Sheets"]);
	apiTr.getIntakeFieldOptions.mockReset().mockResolvedValue(TODO);
});

describe("IntakeRuleFields", () => {
	it("marks mandatory fields and checks the picked ones", async () => {
		const w = await mountIt(formOf());
		expect(box(w, "Description (required)").exists()).toBe(true);
		expect(box(w, "Description (required)").element.checked).toBe(true);
		expect(box(w, "Status").element.checked).toBe(false);
	});

	it("pre-checks the mandatory fields of a fresh spec", async () => {
		const w = await mountIt(
			formOf({ intake_spec: { fields: [], table: null, match: [], defaults: {} } })
		);
		const spec = patches(w).find((p) => p.intake_spec).intake_spec;
		expect(spec.fields).toEqual(["description", "reference_type"]);
	});

	it("lists mandatory fields that are neither picked nor defaulted in strict mode", async () => {
		const w = await mountIt(formOf());
		expect(w.text()).toContain("Needs a value or a default: Reference Type");
		const lenient = await mountIt(formOf({ allow_incomplete_drafts: 1 }));
		expect(lenient.text()).not.toContain("Needs a value or a default");
	});

	it("offers a default row only for mandatory fields that are not picked", async () => {
		const w = await mountIt(formOf());
		expect(w.find('[data-testid="default-reference_type"]').exists()).toBe(true);
		expect(w.find('[data-testid="default-description"]').exists()).toBe(false);
		expect(w.find('[data-testid="default-status"]').exists()).toBe(false);
	});

	it("a default covers a mandatory field", async () => {
		const w = await mountIt(
			formOf({
				intake_spec: {
					fields: ["description"],
					table: null,
					match: [],
					defaults: { reference_type: "Event" },
				},
			})
		);
		expect(w.text()).not.toContain("Needs a value or a default");
	});

	it("emits the new field list when a box is ticked", async () => {
		const w = await mountIt(formOf());
		await box(w, "Status").setValue(true);
		const last = patches(w).at(-1);
		expect(last.intake_spec.fields).toEqual(["description", "status"]);
	});

	it("offers match only for picked Link fields", async () => {
		const w = await mountIt(
			formOf({
				intake_spec: {
					fields: ["description", "allocated_to"],
					table: null,
					match: [],
					defaults: {},
				},
			})
		);
		expect(box(w, "Match: Allocated To").exists()).toBe(true);
		expect(box(w, "Match: Status").exists()).toBe(false);
	});

	it("shows write-back fields for the Record source only", async () => {
		const folder = await mountIt(formOf());
		expect(folder.find('[data-testid="writeback"]').exists()).toBe(false);
		const record = await mountIt(
			formOf({ intake_source: "Record", intake_record_doctype: "ToDo" })
		);
		expect(record.find('[data-testid="writeback"]').exists()).toBe(true);
	});

	it("has the allow incomplete drafts checkbox, off by default, with its description", async () => {
		const w = await mountIt(formOf());
		expect(box(w, "Allow incomplete drafts").element.checked).toBe(false);
		expect(w.text()).toContain(
			"Create the draft even when a mandatory field could not be read. Missing fields are listed on the draft. Submit still needs every mandatory field."
		);
	});

	it("shows the load error of the field list", async () => {
		apiTr.getIntakeFieldOptions.mockRejectedValue(new Error("Intake on 'X' is not allowed."));
		const w = await mountIt(formOf());
		expect(w.find(".error-message").exists()).toBe(true);
	});

	it("flipping the source clears the other source's picks", async () => {
		const toFolder = await mountIt(
			formOf({
				intake_source: "Record",
				intake_record_doctype: "ToDo",
				intake_status_field: "status",
				intake_link_field: "ref",
			})
		);
		await toFolder.find('button[data-label="Folder"]').trigger("click");
		expect(patches(toFolder).at(-1)).toEqual({
			intake_source: "Folder",
			intake_record_doctype: "",
			intake_status_field: "",
			intake_link_field: "",
		});
		const toRecord = await mountIt(formOf({ intake_folder: "Home/Production Sheets" }));
		await toRecord.find('button[data-label="Record"]').trigger("click");
		expect(patches(toRecord).at(-1)).toEqual({ intake_source: "Record", intake_folder: "" });
	});

	it("read-only with a redacted spec renders without loading or crashing", async () => {
		const w = mount(IntakeRuleFields, {
			props: {
				form: formOf({ intake_spec: null, intake_instructions: null }),
				readOnly: true,
				disabled: true,
			},
		});
		await flushPromises();
		expect(apiTr.getIntakeFieldOptions).not.toHaveBeenCalled();
		expect(apiTr.listIntakeFolders).not.toHaveBeenCalled();
		expect(w.find(".error-message").exists()).toBe(false);
		expect(w.text()).not.toContain("Needs a value or a default");
	});
});
