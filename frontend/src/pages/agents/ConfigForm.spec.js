import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";

/**
 * jarvis#1062 polish - ConfigForm.vue's empty-configuration state used to say
 * "No configuration set yet - add keys under Advanced (JSON)." with no hint
 * of what a key actually does. Replaced with an explanation plus a compact
 * reference of the settings the run path really reads (verified against
 * agent_scope.py's _resolve and the set_config docstring in agents_api.py) -
 * and the Advanced (JSON) helper now shows a worked example instead of a
 * vague "arrays/objects live here".
 */

vi.mock("frappe-ui", () => ({
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
		props: ["modelValue", "type", "label"],
		emits: ["update:modelValue"],
		template: `<textarea v-if="type === 'textarea'" :data-label="label" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" /><input v-else :data-label="label" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
	},
	Switch: {
		name: "Switch",
		props: ["modelValue", "label"],
		emits: ["update:modelValue"],
		template: `<button :data-label="label" @click="$emit('update:modelValue', !modelValue)">{{ label }}</button>`,
	},
}));

import ConfigForm from "./ConfigForm.vue";

function mountForm(config = {}, extraProps = {}) {
	return mount(ConfigForm, { props: { config, saving: false, ...extraProps } });
}

describe("empty state: explanation + reference list", () => {
	it("shows the explanation instead of the old vague sentence", () => {
		const w = mountForm({});
		expect(w.text()).toContain(
			"Engagement settings this agent reads when it runs. Leave a setting empty to use the default."
		);
		expect(w.text()).not.toContain("No configuration set yet");
	});

	it("lists every config key the run path actually reads, with its default/description", () => {
		const w = mountForm({});
		const text = w.text();
		// company / fiscal_year / from_date / to_date (agent_scope.py _resolve)
		expect(text).toContain("company");
		expect(text).toContain("Company to audit; default: your default company");
		expect(text).toContain("fiscal_year");
		expect(text).toContain("Fiscal year to cover; default: the current one");
		expect(text).toContain("from_date / to_date");
		expect(text).toContain("Explicit period; overrides fiscal_year");
		// benchmark_value / percentage / engagement_risk_level / rounding_step
		// (set_config docstring, agents_api.py)
		expect(text).toContain("benchmark_value / percentage");
		expect(text).toContain("Materiality");
		expect(text).toContain("engagement_risk_level");
		expect(text).toContain("low / medium / high");
		expect(text).toContain("rounding_step");
		expect(text).toContain("Round-number plug detection step");
	});

	it("does not show the reference list once a real config value exists", () => {
		const w = mountForm({ company: "Acme Ltd" });
		expect(w.text()).not.toContain("Engagement settings this agent reads when it runs");
	});
});

describe("Advanced (JSON) helper text", () => {
	it("shows a worked example instead of the old generic sentence", () => {
		const w = mountForm({});
		expect(w.text()).toContain(
			'Enter values as JSON, for example {"company": "Acme Ltd", "benchmark_value": 1000000, "percentage": 5}. Form fields above win on matching keys.'
		);
	});
});

describe("existing ConfigForm behaviour is unchanged", () => {
	it("seeds scalar fields as form controls and complex values into Advanced", () => {
		const w = mountForm({
			company: "Acme Ltd",
			enabled_flag: true,
			rounding_step: 100,
			nested: { a: 1 },
		});
		expect(w.find('input[data-label="Company"]').exists()).toBe(true);
		expect(w.find('[data-label="Enabled Flag"]').exists()).toBe(true);
		expect(w.find('input[data-label="Rounding Step"]').exists()).toBe(true);
		const textarea = w.find("textarea");
		expect(textarea.exists()).toBe(true);
		expect(JSON.parse(textarea.element.value)).toEqual({ nested: { a: 1 } });
	});

	it("merges form fields over the advanced JSON on save, form fields winning", async () => {
		const w = mountForm({ company: "Acme Ltd" });
		await w.find('input[data-label="Company"]').setValue("New Co");
		await w.find('[data-label="Save configuration"]').trigger("click");
		expect(w.emitted("save")[0][0]).toEqual({ company: "New Co" });
	});

	it("rejects invalid Advanced JSON without emitting save", async () => {
		const w = mountForm({});
		await w.find("textarea").setValue("{not json");
		await w.find('[data-label="Save configuration"]').trigger("click");
		expect(w.emitted("save")).toBeUndefined();
		expect(w.find(".error").text()).toContain("Advanced JSON is not valid");
	});
});
