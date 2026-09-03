import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

/**
 * jarvis#1062 owner feedback: the code-key reference list (no inputs) was not
 * user friendly. ConfigForm.vue now renders a REAL form for the known
 * engagement settings (CONFIG_FIELD_SET, @/lib/agentConfigFields) - link
 * pickers for Company/Fiscal year, a date pair, number inputs, a select -
 * with the merge semantics unchanged: known fields win on matching keys, an
 * unrecognised key survives untouched in Advanced (JSON), and an empty field
 * means the key is absent from the saved config.
 *
 * jarvis#1063 (jarvis-only half): the scope fields (Company/Fiscal
 * year/Period) always render; the agent-specific fields (materiality
 * amount/percentage, risk level, rounding step) render only when their
 * storage PATH (a dot path for a nested key - close-auditor's evaluate.py
 * reads them under a top-level `materiality` object) is in the `configKeys`
 * prop (the listing's config_keys, get_agent, itself dot paths).
 * mountForm()'s default configKeys covers all four so the pre-existing
 * "always renders" tests below still describe a close-auditor-shaped agent;
 * the dedicated "per-agent gating" describe block below covers the subset,
 * empty and flat-legacy-migration cases.
 */

const ALL_AGENT_SPECIFIC_PATHS = [
	"materiality.benchmark_value",
	"materiality.percentage",
	"materiality.engagement_risk_level",
	"materiality.rounding_step",
];

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
	return mount(ConfigForm, {
		props: { config, configKeys: ALL_AGENT_SPECIFIC_PATHS, saving: false, ...extraProps },
	});
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
	it("seeds the form from the installation's current config (materiality nested)", () => {
		const w = mountForm({
			company: "Acme Ltd",
			fiscal_year: "2026",
			from_date: "2026-04-01",
			to_date: "2027-03-31",
			materiality: {
				benchmark_value: 1000000,
				percentage: 5,
				engagement_risk_level: "medium",
				rounding_step: 100,
			},
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
	it("typing values into every field and saving merges them - materiality NESTED (jarvis#1063 CRITICAL)", async () => {
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
			materiality: {
				benchmark_value: 1000000,
				percentage: 5,
				engagement_risk_level: "high",
				rounding_step: 100,
			},
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
	it("clearing a populated field deletes its nested path, siblings under materiality survive", async () => {
		const w = mountForm({ materiality: { rounding_step: 100, percentage: 5 } });
		await field(w, "Rounding step").setValue("");
		await w.find('[data-label="Save configuration"]').trigger("click");
		const saved = w.emitted("save")[0][0];
		expect(saved.materiality).not.toHaveProperty("rounding_step");
		expect(saved.materiality.percentage).toBe(5);
	});

	it("clearing every materiality field drops the now-empty materiality object entirely", async () => {
		const w = mountForm({ materiality: { rounding_step: 100 } });
		await field(w, "Rounding step").setValue("");
		await w.find('[data-label="Save configuration"]').trigger("click");
		expect(w.emitted("save")[0][0]).toEqual({});
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
		expect(w.emitted("save")[0][0]).toEqual({
			custom_flag: true,
			materiality: { rounding_step: 50 },
		});
	});

	it("an UNKNOWN sibling under materiality (e.g. pl_balance) survives a rounding_step edit, not replaced", async () => {
		const w = mountForm({ materiality: { pl_balance: 50000, rounding_step: 100 } });
		const textarea = w.find("textarea");
		expect(JSON.parse(textarea.element.value)).toEqual({ materiality: { pl_balance: 50000 } });

		await field(w, "Rounding step").setValue("200");
		await w.find('[data-label="Save configuration"]').trigger("click");
		expect(w.emitted("save")[0][0]).toEqual({
			materiality: { pl_balance: 50000, rounding_step: 200 },
		});
	});
});

// jarvis#1063 (jarvis-only half): agent-specific fields are gated by the
// listing's config_keys, matched against each field's storage PATH (a dot
// path for a nested key) - the scope fields never are.
describe("per-agent gating via the configKeys prop", () => {
	it("configKeys=[] (e.g. bank-recon-operator) renders only the scope fields, plus the note", () => {
		const w = mountForm({}, { configKeys: [] });
		expect(w.text()).toContain("Company");
		expect(w.text()).toContain("Fiscal year");
		expect(w.text()).toContain("Period from");
		expect(w.text()).toContain("Period to");
		expect(w.text()).not.toContain("Materiality benchmark amount");
		expect(w.text()).not.toContain("Materiality percentage");
		expect(w.text()).not.toContain("Engagement risk level");
		expect(w.text()).not.toContain("Rounding step");
		expect(w.text()).toContain("This agent has no additional settings.");
	});

	it("a partial configKeys (dot paths) renders only the matching agent-specific fields, no note", () => {
		const w = mountForm(
			{},
			{ configKeys: ["materiality.percentage", "materiality.rounding_step"] }
		);
		expect(w.text()).toContain("Materiality percentage");
		expect(w.text()).toContain("Rounding step");
		expect(w.text()).not.toContain("Materiality benchmark amount");
		expect(w.text()).not.toContain("Engagement risk level");
		expect(w.text()).not.toContain("This agent has no additional settings.");
	});

	it("a bare flat key (no materiality. prefix) in configKeys matches nothing - paths are dot paths, not local keys", () => {
		const w = mountForm({}, { configKeys: ["percentage"] });
		expect(w.text()).not.toContain("Materiality percentage");
		expect(w.text()).toContain("This agent has no additional settings.");
	});

	it("the full configKeys set (close-auditor) renders every agent-specific field, no note", () => {
		const w = mountForm({});
		expect(w.text()).not.toContain("This agent has no additional settings.");
	});

	it("a stale agent-specific value this agent doesn't declare stays visible in Advanced (JSON), not lost", async () => {
		// e.g. a materiality.percentage saved on a non-close-auditor installation.
		// With configKeys=[] the field has no control here - it must NOT
		// silently vanish (unreadable, unclearable); it surfaces in Advanced
		// (JSON) instead, same as any other unrecognised key, and round-trips
		// unchanged on save.
		const w = mountForm({ materiality: { percentage: 5 } }, { configKeys: [] });
		const textarea = w.find("textarea");
		expect(JSON.parse(textarea.element.value)).toEqual({ materiality: { percentage: 5 } });
		expect(w.text()).not.toContain("Materiality percentage");

		await w.find('[data-label="Save configuration"]').trigger("click");
		expect(w.emitted("save")[0][0]).toEqual({ materiality: { percentage: 5 } });
	});

	it("saving with configKeys=[] and nothing seeded emits an empty object", async () => {
		const w = mountForm({}, { configKeys: [] });
		await w.find('[data-label="Save configuration"]').trigger("click");
		expect(w.emitted("save")[0][0]).toEqual({});
	});
});

// jarvis#1063 CRITICAL fix: close-auditor/evaluate.py reads materiality
// config NESTED, not flat. Installations saved before this fix have it flat
// at the top level - migrate transparently, one-directional (flat -> nested,
// never back).
describe("legacy flat-key migration (materiality.* used to be saved flat)", () => {
	it("seed: pre-fills the field from a flat legacy key when the nested one is absent", () => {
		const w = mountForm({ benchmark_value: 750000 });
		expect(field(w, "Materiality benchmark amount").element.value).toBe("750000");
	});

	it("seed: the nested value wins over a flat legacy value when both are present", () => {
		const w = mountForm({ benchmark_value: 1, materiality: { benchmark_value: 2 } });
		expect(field(w, "Materiality benchmark amount").element.value).toBe("2");
	});

	it("seed: a flat legacy value does not leak into Advanced (JSON) once migrated into the form", () => {
		const w = mountForm({ benchmark_value: 750000, custom_flag: true });
		const textarea = w.find("textarea");
		expect(JSON.parse(textarea.element.value)).toEqual({ custom_flag: true });
	});

	it("save: a migrated flat value is written nested, and the flat key is dropped", async () => {
		const w = mountForm({ benchmark_value: 750000 });
		await w.find('[data-label="Save configuration"]').trigger("click");
		const saved = w.emitted("save")[0][0];
		expect(saved).toEqual({ materiality: { benchmark_value: 750000 } });
		expect(saved).not.toHaveProperty("benchmark_value");
	});

	it("save: editing a migrated field and saving writes only the nested path", async () => {
		const w = mountForm({ percentage: 5 });
		await field(w, "Materiality percentage").setValue("7");
		await w.find('[data-label="Save configuration"]').trigger("click");
		expect(w.emitted("save")[0][0]).toEqual({ materiality: { percentage: 7 } });
	});
});
