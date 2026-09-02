import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * jarvis#1062 owner feedback: the code-key reference list (no inputs) was not
 * user friendly. ConfigForm.vue now renders a REAL form for the known
 * engagement settings (CONFIG_FIELD_SET, @/lib/agentConfigFields) - link
 * pickers for Company/Fiscal year, a date pair, number inputs, a select -
 * always rendered, with the merge semantics unchanged: known fields win on
 * matching keys, an unrecognised key survives untouched in Advanced (JSON),
 * and an empty field means the key is absent from the saved config.
 */

const api = vi.hoisted(() => ({ searchLink: vi.fn().mockResolvedValue([]) }));
vi.mock("@/api", () => api);

vi.mock("frappe-ui", () => ({
	Autocomplete: {
		name: "Autocomplete",
		props: ["options", "modelValue", "placeholder"],
		emits: ["update:query", "update:modelValue"],
		template: `<div>
			<input :placeholder="placeholder" @input="$emit('update:query', $event.target.value)" />
			<button
				v-for="o in options"
				:key="o.value"
				:data-option="o.value"
				type="button"
				@click="$emit('update:modelValue', o)"
			>{{ o.label }}</button>
		</div>`,
	},
	Button: {
		name: "Button",
		props: ["label", "loading"],
		emits: ["click"],
		template: `<button :data-label="label" @click="$emit('click')">{{ label }}</button>`,
	},
	ErrorMessage: {
		name: "ErrorMessage",
		props: ["message"],
		template: `<div class="error">{{ message }}</div>`,
	},
	FormControl: {
		name: "FormControl",
		props: ["modelValue", "type", "label", "options", "description"],
		emits: ["update:modelValue"],
		template: `<div>
			<span class="fc-label">{{ label }}</span>
			<select
				v-if="type === 'select'"
				:data-label="label"
				:value="modelValue"
				@change="$emit('update:modelValue', $event.target.value)"
			>
				<option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option>
			</select>
			<textarea
				v-else-if="type === 'textarea'"
				:data-label="label"
				:value="modelValue"
				@input="$emit('update:modelValue', $event.target.value)"
			/>
			<input
				v-else
				:data-label="label"
				:type="type"
				:value="modelValue"
				@input="$emit('update:modelValue', $event.target.value)"
			/>
			<slot name="suffix" />
			<p v-if="description">{{ description }}</p>
		</div>`,
	},
}));

import ConfigForm from "./ConfigForm.vue";

function mountForm(config = {}, extraProps = {}) {
	return mount(ConfigForm, { props: { config, saving: false, ...extraProps } });
}

function field(w, label) {
	return w.find(`[data-label="${label}"]`);
}

beforeEach(() => {
	vi.clearAllMocks();
	api.searchLink.mockResolvedValue([]);
});

describe("the known-settings form always renders, config empty or not", () => {
	it("renders every field with an empty config", () => {
		const w = mountForm({});
		expect(w.text()).toContain(
			"Settings this agent reads when it runs. Leave a field empty to use the default."
		);
		expect(w.text()).toContain("Company");
		expect(w.text()).toContain("Fiscal year");
		expect(w.text()).toContain("Period from");
		expect(w.text()).toContain("Period to");
		expect(w.text()).toContain("Materiality benchmark amount");
		expect(w.text()).toContain("Materiality percentage");
		expect(w.text()).toContain("Engagement risk level");
		expect(w.text()).toContain("Rounding step");
		// the old code-key reference list (a <code> chip per key) is gone
		expect(w.find("code").exists()).toBe(false);
	});

	it("shows each field's help text", () => {
		const w = mountForm({});
		expect(w.text()).toContain("Defaults to your default company");
		expect(w.text()).toContain("Defaults to the current fiscal year");
		expect(w.text()).toContain("Optional; overrides the fiscal year");
		expect(w.text()).toContain("Used to judge which differences matter");
		expect(w.text()).toContain(
			"Auditors report checks as not evaluable when materiality is unset"
		);
		expect(w.text()).toContain("Step used to detect round-number plugs");
	});
});

describe("existing values pre-fill every field", () => {
	it("seeds the form from the installation's current config", () => {
		const w = mountForm({
			company: "Acme Ltd",
			fiscal_year: "2026",
			from_date: "2026-04-01",
			to_date: "2027-03-31",
			benchmark_value: 1000000,
			percentage: 5,
			engagement_risk_level: "medium",
			rounding_step: 100,
		});
		expect(field(w, "Period from").element.value).toBe("2026-04-01");
		expect(field(w, "Period to").element.value).toBe("2027-03-31");
		expect(field(w, "Materiality benchmark amount").element.value).toBe("1000000");
		expect(field(w, "Materiality percentage").element.value).toBe("5");
		expect(field(w, "Engagement risk level").element.value).toBe("medium");
		expect(field(w, "Rounding step").element.value).toBe("100");
		// Company/Fiscal year (Autocomplete) show the current value as the picked option
		expect(w.text()).toContain("Acme Ltd");
	});
});

describe("saving produces the right JSON", () => {
	it("typing values into every field and saving merges them", async () => {
		const w = mountForm({});
		await field(w, "Period from").setValue("2026-04-01");
		await field(w, "Period to").setValue("2027-03-31");
		await field(w, "Materiality benchmark amount").setValue("1000000");
		await field(w, "Materiality percentage").setValue("5");
		await field(w, "Engagement risk level").setValue("high");
		await field(w, "Rounding step").setValue("100");
		await w.find('[data-label="Save configuration"]').trigger("click");

		expect(w.emitted("save")[0][0]).toEqual({
			from_date: "2026-04-01",
			to_date: "2027-03-31",
			benchmark_value: 1000000,
			percentage: 5,
			engagement_risk_level: "high",
			rounding_step: 100,
		});
	});

	it("picking a Company/Fiscal year option saves the picked value", async () => {
		api.searchLink.mockResolvedValue([{ value: "Acme Ltd" }]);
		const w = mountForm({});
		// primes on focus/click, per-field Autocomplete
		const companyBox = w.findAll("input[placeholder^='Search']")[0];
		await companyBox.trigger("focusin");
		await flushPromises();
		const pick = w.find('button[data-option="Acme Ltd"]');
		expect(pick.exists()).toBe(true);
		await pick.trigger("click");
		await w.find('[data-label="Save configuration"]').trigger("click");
		expect(w.emitted("save")[0][0]).toEqual({ company: "Acme Ltd" });
	});
});

describe("clearing a field drops its key", () => {
	it("clearing a populated field omits it from the saved config", async () => {
		const w = mountForm({ rounding_step: 100, percentage: 5 });
		await field(w, "Rounding step").setValue("");
		await w.find('[data-label="Save configuration"]').trigger("click");
		const saved = w.emitted("save")[0][0];
		expect(saved).not.toHaveProperty("rounding_step");
		expect(saved.percentage).toBe(5);
	});

	it("re-picking Company to a different value saves the new one, not a stale key", async () => {
		api.searchLink.mockResolvedValue([{ value: "Other Co" }]);
		const w = mountForm({ company: "Acme Ltd" });
		expect(w.text()).toContain("Acme Ltd");
		const companyBox = w.findAll("input[placeholder^='Search']")[0];
		await companyBox.trigger("focusin");
		await flushPromises();
		await w.find('button[data-option="Other Co"]').trigger("click");
		await w.find('[data-label="Save configuration"]').trigger("click");
		expect(w.emitted("save")[0][0]).toEqual({ company: "Other Co" });
	});
});

describe("an unknown config key survives untouched in Advanced (JSON)", () => {
	it("seeds an unrecognised key into Advanced, not a form field", () => {
		const w = mountForm({ company: "Acme Ltd", custom_flag: true, nested: { a: 1 } });
		const textarea = w.find("textarea");
		const advanced = JSON.parse(textarea.element.value);
		expect(advanced).toEqual({ custom_flag: true, nested: { a: 1 } });
	});

	it("keeps the unknown key on save alongside the known fields", async () => {
		const w = mountForm({ custom_flag: true });
		await field(w, "Rounding step").setValue("50");
		await w.find('[data-label="Save configuration"]').trigger("click");
		expect(w.emitted("save")[0][0]).toEqual({ custom_flag: true, rounding_step: 50 });
	});
});
