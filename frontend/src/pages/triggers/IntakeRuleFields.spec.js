import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const apiTr = vi.hoisted(() => ({
	listIntakeFolders: vi.fn(),
	getIntakeFieldOptions: vi.fn(),
	searchIntakeDoctypes: vi.fn(() => Promise.resolve([])),
}));
vi.mock("@/api/triggers", () => apiTr);
vi.mock("@/api", () => ({ searchLink: vi.fn(() => Promise.resolve([])) }));

vi.mock("frappe-ui", () => ({
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "modelValue", "placeholder", "loading"],
		emits: ["update:modelValue", "update:query"],
		template: `<div class="autocomplete" :data-placeholder="placeholder" :data-loading="loading ? '1' : '0'"><button type="button">pick</button></div>`,
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
	Select: {
		name: "Select",
		props: ["modelValue", "options", "disabled", "placeholder"],
		emits: ["update:modelValue"],
		template: `<select :value="modelValue" :disabled="disabled" @change="$emit('update:modelValue', $event.target.value)"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select>`,
	},
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "label", "options", "disabled", "placeholder"],
		emits: ["update:modelValue"],
		template: `<input :type="type === 'checkbox' ? 'checkbox' : 'text'" :aria-label="label" :value="modelValue" :checked="type === 'checkbox' ? !!modelValue : undefined" :disabled="disabled" @input="type !== 'checkbox' && $emit('update:modelValue', $event.target.value)" @change="type === 'checkbox' && $emit('update:modelValue', $event.target.checked)" />`,
	},
}));

import { searchLink } from "@/api";
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
		const grid = w.find('[data-testid="match-grid"]');
		expect(grid.find('input[aria-label="Allocated To"]').exists()).toBe(true);
		expect(grid.find('input[aria-label="Status"]').exists()).toBe(false);
	});

	it("shows every spec default under Fixed values, not only the mandatory ones", async () => {
		const w = await mountIt(
			formOf({
				intake_spec: {
					fields: ["description"],
					table: null,
					match: [],
					defaults: { status: "Closed" },
				},
			})
		);
		expect(w.find('[data-testid="default-status"]').exists()).toBe(true);
		expect(w.text()).toContain("Fixed values");
		// a field the file already fills gets no fixed value
		const read = await mountIt(
			formOf({
				intake_spec: {
					fields: ["description", "status"],
					table: null,
					match: [],
					defaults: { status: "Closed" },
				},
			})
		);
		expect(read.find('[data-testid="default-status"]').exists()).toBe(false);
	});

	it("styles every intake sub-section heading alike and words the match help plainly", async () => {
		const w = await mountIt(
			formOf({
				intake_source: "Record",
				intake_record_doctype: "ToDo",
				intake_spec: {
					fields: ["description", "allocated_to"],
					table: null,
					match: [],
					defaults: {},
				},
			})
		);
		const heads = w.findAll("h3").map((h) => [h.text(), h.classes().join(" ")]);
		const names = heads.map((h) => h[0]);
		for (const n of [
			"Record source",
			"Fields to read",
			"Match to existing records",
			"Fixed values",
		])
			expect(names).toContain(n);
		expect(new Set(heads.map((h) => h[1])).size).toBe(1);
		expect(w.text()).toContain("Only use values that already exist in the site");
		expect(w.text()).toContain("Files attached to these records are read");
	});

	it("gives the Table picker the full width", async () => {
		apiTr.getIntakeFieldOptions.mockResolvedValue({
			...TODO,
			tables: [{ fieldname: "items", label: "Items", child_doctype: "Row", columns: [] }],
		});
		const w = await mountIt(formOf());
		const section = w.find('[data-testid="intake-table-section"]');
		expect(section.find("select").classes()).toContain("w-full");
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

	it("changing the record doctype clears the write-back picks", async () => {
		const w = await mountIt(
			formOf({
				intake_source: "Record",
				intake_record_doctype: "ToDo",
				intake_status_field: "status",
				intake_link_field: "ref",
			})
		);
		const picker = w
			.findAllComponents({ name: "Autocomplete" })
			.find((c) => c.props("placeholder") === "Files attached to records of…");
		await picker.vm.$emit("update:modelValue", { label: "Note", value: "Note" });
		expect(patches(w).at(-1)).toEqual({
			intake_record_doctype: "Note",
			intake_status_field: "",
			intake_link_field: "",
		});
		// Re-picking the same doctype keeps them.
		const before = patches(w).length;
		await picker.vm.$emit("update:modelValue", { label: "ToDo", value: "ToDo" });
		expect(patches(w).at(-1)).toEqual({ intake_record_doctype: "ToDo" });
		expect(patches(w).length).toBe(before + 1);
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

	it("typing in the DocType pickers replaces their options with the search result", async () => {
		vi.useFakeTimers();
		try {
			for (const source of ["Folder", "Record"]) {
				const w = mount(IntakeRuleFields, {
					props: { form: formOf({ intake_source: source }) },
				});
				await vi.advanceTimersByTimeAsync(400);
				apiTr.searchIntakeDoctypes.mockResolvedValue([{ value: "ToDo" }]);
				const picker = w
					.findAllComponents({ name: "Autocomplete" })
					.find((p) => p.attributes("data-placeholder") === "Pick a DocType…");
				picker.vm.$emit("update:query", "ToD");
				await vi.advanceTimersByTimeAsync(400);
				expect(picker.props("options")).toEqual([{ label: "ToDo", value: "ToDo" }]);
				// The server-side filter owns this list: never the generic DocType search.
				expect(apiTr.searchIntakeDoctypes).toHaveBeenCalledWith("ToD");
				expect(searchLink).not.toHaveBeenCalledWith("DocType", expect.anything());
			}
		} finally {
			vi.useRealTimers();
		}
	});

	it("a slow earlier DocType search never overwrites a newer one (latest wins)", async () => {
		vi.useFakeTimers();
		try {
			const w = mount(IntakeRuleFields, { props: { form: formOf() } });
			await vi.advanceTimersByTimeAsync(400);
			let release;
			apiTr.searchIntakeDoctypes.mockImplementationOnce(
				() => new Promise((res) => (release = () => res([{ value: "Old" }])))
			);
			apiTr.searchIntakeDoctypes.mockResolvedValueOnce([{ value: "New" }]);
			const picker = w
				.findAllComponents({ name: "Autocomplete" })
				.find((p) => p.attributes("data-placeholder") === "Pick a DocType…");
			picker.vm.$emit("update:query", "O");
			await vi.advanceTimersByTimeAsync(400);
			picker.vm.$emit("update:query", "Ne");
			await vi.advanceTimersByTimeAsync(400);
			release();
			await flushPromises();
			expect(picker.props("options")).toEqual([{ label: "New", value: "New" }]);
		} finally {
			vi.useRealTimers();
		}
	});

	it("says the fields are loading while the field list is on its way", async () => {
		let release;
		apiTr.getIntakeFieldOptions.mockImplementationOnce(
			() => new Promise((res) => (release = () => res(TODO)))
		);
		const w = mount(IntakeRuleFields, { props: { form: formOf() } });
		await flushPromises();
		expect(w.find("[role=status]").text()).toContain("Loading the fields of ToDo");
		release();
		await flushPromises();
		expect(w.find("[role=status]").exists()).toBe(false);
	});

	it("stops the instructions at 4000 characters and says so", async () => {
		const w = await mountIt(formOf({ intake_instructions: "x".repeat(4000) }));
		const box = w.find('input[aria-label="Instructions"]');
		expect(box.attributes("maxlength")).toBe("4000");
		expect(w.find('[data-testid="instructions-count"]').text()).toBe("4000 / 4000");
		expect(w.find('[data-testid="instructions-count"]').classes()).toContain("text-ink-red-4");
		// a paste past the limit is cut, not sent on to fail on save
		await box.setValue("y".repeat(4100));
		const sent = patches(w)
			.filter((p) => "intake_instructions" in p)
			.at(-1);
		expect(sent.intake_instructions).toHaveLength(4000);
	});

	it("keeps the form inside its column so a picker's list cannot be wider than the screen", async () => {
		const w = await mountIt(formOf());
		expect(w.find('[data-testid="intake-fields"]').classes()).toEqual(
			expect.arrayContaining(["min-w-0", "max-w-full"])
		);
		for (const pick of w.findAll("[data-pick]")) expect(pick.classes()).toContain("min-w-0");
	});

	it("puts focus back on the picker after a pick", async () => {
		const host = document.createElement("div");
		document.body.appendChild(host);
		const w = mount(IntakeRuleFields, { attachTo: host, props: { form: formOf() } });
		await flushPromises();
		const picker = w
			.findAllComponents({ name: "Autocomplete" })
			.find((p) => p.attributes("data-placeholder") === "Pick a DocType…");
		picker.vm.$emit("update:modelValue", { value: "Note" });
		await flushPromises();
		expect(document.activeElement).toBe(w.find('[data-pick="target"] button').element);
		w.unmount();
		host.remove();
	});

	it("lays the fields out as one aligned grid with a muted type hint per row", async () => {
		const w = await mountIt(formOf());
		const grid = w.find('[data-testid="intake-check-grid"]');
		expect(grid.classes()).toEqual(
			expect.arrayContaining(["grid", "grid-cols-1", "sm:grid-cols-2", "xl:grid-cols-3"])
		);
		const rows = grid.findAll('[data-testid="intake-check-row"]');
		expect(rows.length).toBeGreaterThan(0);
		expect(rows[0].find('[data-testid="type-hint"]').text()).not.toBe("");
	});

	it("shows the same match-by hint on Link fields", async () => {
		const w = await mountIt(formOf());
		expect(w.text()).toContain("Link, match by");
	});

	it("groups the table under a Table rows section with its columns in the same grid", async () => {
		apiTr.getIntakeFieldOptions.mockResolvedValue({
			...TODO,
			tables: [
				{
					fieldname: "items",
					label: "Items",
					columns: [
						{ fieldname: "item", label: "Item", fieldtype: "Link", options: "Item" },
						{ fieldname: "qty", label: "Qty", fieldtype: "Float", options: "" },
					],
				},
			],
		});
		const w = await mountIt(
			formOf({
				intake_spec: {
					fields: ["description"],
					table: { fieldname: "items", columns: ["qty"] },
					match: [],
					defaults: {},
				},
			})
		);
		const section = w.find('[data-testid="intake-table-section"]');
		expect(section.text()).toContain("Table rows");
		const grid = section.find('[data-testid="intake-check-grid"]');
		expect(grid.findAll('[data-testid="intake-check-row"]')).toHaveLength(2);
		expect(grid.text()).toContain("Link, match by Item");
		expect(section.find(".pl-3").exists()).toBe(true);
	});

	it("the Record source section is one grid: doctype, Run when, status, link; Optional, not None", async () => {
		const w = await mountIt(
			formOf({ intake_source: "Record", intake_record_doctype: "ToDo" })
		);
		const sec = w.find('[data-testid="writeback"]');
		expect(sec.text()).toContain("Record source");
		expect(sec.find(".grid").classes()).toEqual(
			expect.arrayContaining(["grid-cols-1", "sm:grid-cols-2"])
		);
		const labels = sec.findAll("label").map((l) => l.text());
		expect(labels).toEqual([
			"Record doctype",
			"Run when",
			"Status field on record",
			"Draft link field on record",
		]);
		expect(sec.text()).toContain("Jarvis writes the intake status here");
		expect(sec.text()).toContain("Jarvis writes the draft's name here");
		expect(sec.text()).not.toContain("None");
		const selects = w.findAllComponents({ name: "Select" });
		const optional = selects.filter(
			(c) => (c.props("options") || [])[0]?.label === "Optional"
		);
		expect(optional).toHaveLength(2);
		expect(optional.every((c) => c.classes().includes("w-full"))).toBe(true);
		expect(optional[0].props("options")[0].value).toBe("");
	});

	it("Run when emits intake_record_event with the stored value", async () => {
		const w = await mountIt(
			formOf({ intake_source: "Record", intake_record_doctype: "ToDo" })
		);
		const ctrl = w
			.findAllComponents({ name: "Select" })
			.find((c) => (c.props("options") || []).some((o) => o.value === "On Submit"));
		expect(ctrl.props("options").map((o) => o.label)).toEqual([
			"When a file is attached",
			"After Insert",
			"On Submit",
		]);
		expect(ctrl.props("modelValue")).toBe("File attached");
		await ctrl.vm.$emit("update:modelValue", "On Submit");
		expect(patches(w).at(-1)).toEqual({ intake_record_event: "On Submit" });
	});

	it("keeps a long field label and its hint on separate lines", async () => {
		const w = await mountIt(formOf());
		const row = w.find('[data-testid="intake-check-row"]');
		expect(row.classes()).toContain("flex-col");
		const hint = row.find('[data-testid="type-hint"]');
		expect(hint.classes()).not.toContain("truncate");
		expect(hint.classes()).toContain("break-words");
	});
});
