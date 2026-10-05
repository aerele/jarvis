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
const apiAgents = vi.hoisted(() => ({
	getAPReviewDefaults: vi.fn(),
	getARReviewDefaults: vi.fn(),
	getBankReconReviewDefaults: vi.fn(),
}));
vi.mock("@/api/agents", () => apiAgents);

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

function mountForm(config = {}, extraProps = {}, mountOptions = {}) {
	return mount(ConfigForm, {
		props: { config, configKeys: ALL_AGENT_SPECIFIC_PATHS, saving: false, ...extraProps },
		...mountOptions,
	});
}

function field(w, label) {
	return w.find(`[data-label="${label}"]`);
}

function fiscalYearPicker(wrapper) {
	return wrapper
		.findAllComponents({ name: "Autocomplete" })
		.find((component) => component.props("placeholder") === "Search Fiscal Year…");
}

function apDefaults(company = "Example") {
	return {
		defaults: {
			company,
			policy_version: "AP-MATCH-v1",
			price_tolerance_percent: 0,
			basis: "current_records",
			review_scope: "document_matching_only",
		},
		site_date: "2026-09-30",
		fiscal_years: [
			{ name: "2026-2027", from_date: "2026-04-01", to_date: "2027-03-31" },
			{ name: "2025-2026", from_date: "2025-04-01", to_date: "2026-03-31" },
		],
		message: "",
	};
}

describe("AR review configuration", () => {
	const configKeys = [
		"report_date",
		"policy_version",
		"settlement_hold_days",
		"basis",
		"review_scope",
	];
	function arDefaults(company = "Example") {
		const result = apDefaults(company);
		delete result.defaults.price_tolerance_percent;
		Object.assign(result.defaults, {
			policy_version: "AR-REVIEW-v1",
			settlement_hold_days: 3,
			review_scope: "review_and_drafts_only",
		});
		return result;
	}
	beforeEach(() => {
		apiAgents.getARReviewDefaults.mockReset().mockResolvedValue(arDefaults());
		apiAgents.getAPReviewDefaults.mockClear();
	});
	it("uses the shared fiscal-year picker and saves explicitly unsent settings", async () => {
		const wrapper = mountForm({}, { configKeys });
		await flushPromises();
		expect(apiAgents.getARReviewDefaults).toHaveBeenCalled();
		expect(apiAgents.getAPReviewDefaults).not.toHaveBeenCalled();
		expect(fiscalYearPicker(wrapper).props("modelValue").value).toBe("2026-2027");
		expect(wrapper.text()).toContain("older open receivables");
		expect(wrapper.text()).toContain("India Compliance");
		expect(field(wrapper, "Net-price tolerance").exists()).toBe(false);
		expect(field(wrapper, "Recent-settlement hold").element.value).toBe("3");
		await field(wrapper, "Save configuration").trigger("click");
		expect(wrapper.emitted("save")[0][0]).toMatchObject({
			...arDefaults().defaults,
			fiscal_year: "2026-2027",
			from_date: "2026-04-01",
			report_date: "2026-09-30",
		});
	});
	it("preserves saved review policy and zero-day holds", async () => {
		const policy = {
			...arDefaults().defaults,
			policy_version: "Finance-policy-9",
			settlement_hold_days: 0,
			from_date: "2026-04-01",
			to_date: "2026-08-31",
			report_date: "2026-08-31",
		};
		const wrapper = mountForm(policy, { configKeys });
		await flushPromises();
		await field(wrapper, "Fill empty settings").trigger("click");
		await flushPromises();
		await field(wrapper, "Save configuration").trigger("click");
		expect(wrapper.emitted("save")[0][0]).toEqual({ ...policy, fiscal_year: "2026-2027" });
	});
	it("does not overwrite edits with delayed defaults", async () => {
		let resolve;
		apiAgents.getARReviewDefaults.mockReturnValue(
			new Promise((done) => {
				resolve = done;
			})
		);
		const wrapper = mountForm({}, { configKeys });
		await field(wrapper, "AR review policy reference").setValue("Reviewed-v2");
		resolve(arDefaults());
		await flushPromises();
		expect(field(wrapper, "AR review policy reference").element.value).toBe("Reviewed-v2");
		expect(field(wrapper, "Recent-settlement hold").element.value).toBe("");
	});
});

describe("bank reconciliation review configuration", () => {
	const configKeys = [
		"company",
		"bank_account",
		"from_date",
		"to_date",
		"voucher_lookback_days",
		"policy_version",
		"review_scope",
	];
	function bankDefaults(company = "Example") {
		return {
			defaults: {
				company,
				policy_version: "BANK-RECON-v1",
				review_scope: "proposals_only",
				voucher_lookback_days: 30,
				from_date: "2026-08-01",
				to_date: "2026-08-31",
				bank_account: "Example Bank - EX",
			},
			site_date: "2026-09-30",
			fiscal_years: [],
			message: "",
			bank_accounts: [{ name: "Example Bank - EX", account: "Bank - EX" }],
		};
	}
	beforeEach(() => {
		apiAgents.getBankReconReviewDefaults.mockReset().mockResolvedValue(bankDefaults());
		apiAgents.getARReviewDefaults.mockClear();
		apiAgents.getAPReviewDefaults.mockClear();
	});
	it("renders bank fields and proposals-only wording", async () => {
		const wrapper = mountForm({ company: "Example" }, { configKeys });
		await flushPromises();
		expect(wrapper.text()).toContain("Bank account");
		expect(wrapper.text()).toContain("Voucher lookback");
		expect(wrapper.text()).toContain("Bank reconciliation policy reference");
		expect(wrapper.text()).toContain("Statement window. Lines dated inside it are reviewed.");
		const options = wrapper.find("select").exists()
			? wrapper.findAll("option").map((o) => o.text())
			: [];
		expect(options).toEqual(["Proposals only, no reconciling"]);
	});
	it("fills defaults via the bank endpoint and saves the lookback as a number", async () => {
		const wrapper = mountForm({ company: "Example" }, { configKeys });
		await flushPromises();
		expect(apiAgents.getBankReconReviewDefaults).toHaveBeenCalledWith("Example");
		expect(apiAgents.getARReviewDefaults).not.toHaveBeenCalled();
		expect(apiAgents.getAPReviewDefaults).not.toHaveBeenCalled();
		expect(field(wrapper, "Voucher lookback").element.value).toBe("30");
		await field(wrapper, "Save configuration").trigger("click");
		const saved = wrapper.emitted("save")[0][0];
		expect(saved).toMatchObject({
			company: "Example",
			bank_account: "Example Bank - EX",
			from_date: "2026-08-01",
			to_date: "2026-08-31",
			policy_version: "BANK-RECON-v1",
			review_scope: "proposals_only",
		});
		expect(saved.voucher_lookback_days).toBe(30);
	});
	it("hides saved-config issues for fields a default has filled, until saved", async () => {
		const wrapper = mountForm(
			{},
			{
				configKeys,
				validationErrors: {
					company: "Select a company.",
					bank_account: "Select an enabled company bank account.",
					config: "Configuration must be a JSON object.",
				},
			}
		);
		expect(wrapper.text()).toContain("Select a company.");
		await flushPromises();
		expect(field(wrapper, "Voucher lookback").element.value).toBe("30");
		expect(wrapper.text()).not.toContain("Select a company.");
		expect(wrapper.text()).not.toContain("Select an enabled company bank account.");
		expect(wrapper.text()).toContain("Configuration must be a JSON object.");
	});
	it("keeps an issue while the field still holds the saved value", async () => {
		apiAgents.getBankReconReviewDefaults.mockRejectedValue(new Error("Forbidden"));
		const wrapper = mountForm(
			{ company: "Example", voucher_lookback_days: 120 },
			{
				configKeys,
				validationErrors: {
					voucher_lookback_days: "Choose a voucher lookback from 0 to 90 calendar days.",
				},
			}
		);
		await flushPromises();
		expect(wrapper.text()).toContain("Choose a voucher lookback from 0 to 90 calendar days.");
	});
});

describe("AP explicit review configuration", () => {
	const configKeys = [
		"report_date",
		"policy_version",
		"price_tolerance_percent",
		"basis",
		"review_scope",
	];

	it("uses the same fiscal-year picker, year label and help as other agents", async () => {
		const config = { company: "Example", fiscal_year: "2026-2027" };
		const wrapper = mountForm(config, { configKeys });
		const other = mountForm(config);
		await flushPromises();
		expect(fiscalYearPicker(wrapper).props("modelValue")).toEqual(
			fiscalYearPicker(other).props("modelValue")
		);
		expect(fiscalYearPicker(wrapper).props("options")).toEqual([
			{ label: "2026-2027", value: "2026-2027" },
			{ label: "2025-2026", value: "2025-2026" },
		]);
		expect(wrapper.text()).toContain("Defaults to the current fiscal year");
		expect(wrapper.find('select[data-label="Fiscal year"]').exists()).toBe(false);
		wrapper.unmount();
		other.unmount();
	});

	it("searches only the company's readable fiscal years using the shared picker", async () => {
		vi.useFakeTimers();
		const wrapper = mountForm({ company: "Example" }, { configKeys });
		try {
			await flushPromises();
			await fiscalYearPicker(wrapper).find("input").setValue(" 2025 ");
			await vi.advanceTimersByTimeAsync(300);
			await flushPromises();
			expect(fiscalYearPicker(wrapper).props("options")).toEqual([
				{ label: "2025-2026", value: "2025-2026" },
			]);
			expect(api.searchLink).not.toHaveBeenCalled();
		} finally {
			wrapper.unmount();
			vi.useRealTimers();
		}
	});

	it("keeps manual entry available when defaults cannot be read", async () => {
		apiAgents.getAPReviewDefaults.mockRejectedValue(new Error("Forbidden"));
		const wrapper = mountForm(
			{ company: "Example" },
			{
				configKeys,
				validationErrors: {
					policy_version: "Enter an AP matching policy reference (1–128 characters).",
					price_tolerance_percent: "Enter a net-price tolerance from 0 to 100%.",
				},
			}
		);
		expect(wrapper.find('[role="status"]').text()).toContain(
			"Complete these settings before running"
		);
		await flushPromises();
		expect(wrapper.text()).toContain("Could not load suggested settings");
		expect(wrapper.find('[role="status"]').text()).toContain("net-price tolerance");
		expect(field(wrapper, "Net-price tolerance").element.value).toBe("");
		await field(wrapper, "Save configuration").trigger("click");
		expect(wrapper.emitted("save")[0][0]).toEqual({ company: "Example" });
	});

	it("reports unsaved edits and preserves them when field keys are merely reloaded", async () => {
		const policy = {
			...apDefaults().defaults,
			fiscal_year: "2026-2027",
			from_date: "2026-04-01",
			to_date: "2026-09-30",
			report_date: "2026-09-30",
			policy_version: "AP-v1",
		};
		const wrapper = mountForm(policy, { configKeys });
		await flushPromises();
		expect(wrapper.emitted("dirty").at(-1)).toEqual([false]);
		await field(wrapper, "AP matching policy reference").setValue("AP-v2");
		expect(wrapper.emitted("dirty").at(-1)).toEqual([true]);
		await wrapper.setProps({ configKeys: [...configKeys] });
		expect(field(wrapper, "AP matching policy reference").element.value).toBe("AP-v2");
		await wrapper.setProps({ config: { ...policy, policy_version: "AP-v2" } });
		await flushPromises();
		expect(wrapper.emitted("dirty").at(-1)).toEqual([false]);
	});

	it("prevents editing AP settings while their save is in flight", async () => {
		const wrapper = mountForm({}, { configKeys, saving: true });
		expect(wrapper.find("fieldset").attributes("disabled")).toBeDefined();
		await wrapper.setProps({ saving: false });
		expect(wrapper.find("fieldset").attributes("disabled")).toBeUndefined();
	});

	it("reloads fiscal-year choices after configuration is saved", async () => {
		const wrapper = mountForm({ company: "Example" }, { configKeys });
		await flushPromises();
		await wrapper.setProps({ saving: true });
		await wrapper.setProps({ config: { company: "Example", fiscal_year: "2026-2027" } });
		await wrapper.setProps({ saving: false });
		await flushPromises();
		expect(fiscalYearPicker(wrapper).props("options")).toContainEqual({
			label: "2025-2026",
			value: "2025-2026",
		});
	});

	it("preserves a custom cross-year window and saves zero tolerance", async () => {
		const configKeys = [
			"report_date",
			"policy_version",
			"price_tolerance_percent",
			"basis",
			"review_scope",
		];
		const wrapper = mountForm(
			{
				company: "Example",
				from_date: "2026-03-01",
				to_date: "2026-04-30",
				report_date: "2026-04-15",
				policy_version: "AP-v1",
				price_tolerance_percent: 0,
				basis: "current_records",
				review_scope: "document_matching_only",
			},
			{ configKeys }
		);
		await flushPromises();
		expect(wrapper.text()).toContain("company, fiscal period");
		expect(fiscalYearPicker(wrapper).props("modelValue")).toBeNull();
		expect(field(wrapper, "Evidence cutoff").element.value).toBe("2026-04-15");
		expect(field(wrapper, "AP matching policy reference").element.value).toBe("AP-v1");
		expect(field(wrapper, "Net-price tolerance").element.value).toBe("0");
		await wrapper.find('[data-label="Save configuration"]').trigger("click");
		expect(wrapper.emitted("save")[0][0]).toMatchObject({
			report_date: "2026-04-15",
			policy_version: "AP-v1",
			price_tolerance_percent: 0,
			basis: "current_records",
			review_scope: "document_matching_only",
		});
	});

	it("shows unsaved starter settings and company fiscal-year-to-date dates", async () => {
		const wrapper = mountForm({}, { configKeys });
		await flushPromises();
		expect(fiscalYearPicker(wrapper).props("modelValue")).toEqual({
			label: "2026-2027",
			value: "2026-2027",
		});
		expect(field(wrapper, "Period from").element.value).toBe("2026-04-01");
		expect(field(wrapper, "Period to").element.value).toBe("2026-09-30");
		expect(field(wrapper, "Evidence cutoff").element.value).toBe("2026-09-30");
		expect(field(wrapper, "Net-price tolerance").element.value).toBe("0");
		expect(wrapper.text()).toContain(
			"not accounting-standard certification or policy approval"
		);
		expect(wrapper.emitted("dirty").at(-1)).toEqual([true]);
		expect(wrapper.emitted("save")).toBeUndefined();
		await field(wrapper, "Save configuration").trigger("click");
		expect(wrapper.emitted("save")[0][0]).toEqual({
			...apDefaults().defaults,
			fiscal_year: "2026-2027",
			from_date: "2026-04-01",
			to_date: "2026-09-30",
			report_date: "2026-09-30",
		});
	});

	it("adds the matching fiscal year without replacing saved policy, tolerance or dates", async () => {
		const policy = {
			...apDefaults().defaults,
			policy_version: "helooo",
			price_tolerance_percent: 10,
			from_date: "2026-04-01",
			to_date: "2026-08-31",
			report_date: "2026-09-01",
		};
		const wrapper = mountForm(policy, { configKeys });
		await flushPromises();
		await field(wrapper, "Fill empty settings").trigger("click");
		await flushPromises();
		await fiscalYearPicker(wrapper).find('[data-option="2026-2027"]').trigger("click");
		await field(wrapper, "Save configuration").trigger("click");
		expect(wrapper.emitted("save")[0][0]).toEqual({ ...policy, fiscal_year: "2026-2027" });
	});

	it("uses historical year end rather than today when a fiscal year is explicitly chosen", async () => {
		const wrapper = mountForm({}, { configKeys });
		await flushPromises();
		await fiscalYearPicker(wrapper).find('[data-option="2025-2026"]').trigger("click");
		expect(field(wrapper, "Period from").element.value).toBe("2025-04-01");
		expect(field(wrapper, "Period to").element.value).toBe("2026-03-31");
		expect(field(wrapper, "Evidence cutoff").element.value).toBe("2026-03-31");
		wrapper.findComponent({ name: "Autocomplete" }).vm.$emit("update:modelValue", {
			value: "Example",
		});
		await flushPromises();
		expect(fiscalYearPicker(wrapper).props("modelValue")).toEqual({
			label: "2025-2026",
			value: "2025-2026",
		});
		fiscalYearPicker(wrapper).vm.$emit("update:modelValue", null);
		await flushPromises();
		expect(fiscalYearPicker(wrapper).props("modelValue")).toBeNull();
		expect(field(wrapper, "Period from").element.value).toBe("2025-04-01");
		await field(wrapper, "Save configuration").trigger("click");
		expect(wrapper.emitted("save")[0][0]).not.toHaveProperty("fiscal_year");
	});

	it("does not choose arbitrarily between overlapping or future fiscal years", async () => {
		for (const fiscal_years of [
			[
				...apDefaults().fiscal_years,
				{ name: "Calendar 2026", from_date: "2026-01-01", to_date: "2026-12-31" },
			],
			[{ name: "Future", from_date: "2027-04-01", to_date: "2028-03-31" }],
		]) {
			apiAgents.getAPReviewDefaults.mockResolvedValue({ ...apDefaults(), fiscal_years });
			const wrapper = mountForm({ company: "Example" }, { configKeys });
			await flushPromises();
			expect(fiscalYearPicker(wrapper).props("modelValue")).toBeNull();
			expect(field(wrapper, "Period from").element.value).toBe("");
			expect(field(wrapper, "Period to").element.value).toBe("");
			wrapper.unmount();
		}
	});

	it("does not apply late suggestions over an edited or submitted form", async () => {
		for (const action of ["edit", "save"]) {
			let resolveDefaults;
			apiAgents.getAPReviewDefaults.mockReturnValue(
				new Promise((resolve) => {
					resolveDefaults = resolve;
				})
			);
			const wrapper = mountForm({ company: "Example" }, { configKeys });
			if (action === "edit") await field(wrapper, "Net-price tolerance").setValue("7");
			else await field(wrapper, "Save configuration").trigger("click");
			resolveDefaults(apDefaults());
			await flushPromises();
			expect(field(wrapper, "Net-price tolerance").element.value).toBe(
				action === "edit" ? "7" : ""
			);
			expect(field(wrapper, "Period from").element.value).toBe("");
			wrapper.unmount();
		}
	});

	it("fences company lookups and never applies the previous company's fiscal calendar", async () => {
		let resolveOld;
		apiAgents.getAPReviewDefaults.mockImplementation((company) =>
			company === "Example"
				? new Promise((resolve) => {
						resolveOld = resolve;
				  })
				: Promise.resolve({
						...apDefaults("Second"),
						fiscal_years: [
							{ name: "Second FY", from_date: "2026-07-01", to_date: "2027-06-30" },
						],
				  })
		);
		const wrapper = mountForm({ company: "Example" }, { configKeys });
		wrapper
			.findComponent({ name: "Autocomplete" })
			.vm.$emit("update:modelValue", { value: "Second" });
		await flushPromises();
		resolveOld(apDefaults());
		await flushPromises();
		expect(fiscalYearPicker(wrapper).props("modelValue")).toEqual({
			label: "Second FY",
			value: "Second FY",
		});
		expect(field(wrapper, "Period from").element.value).toBe("2026-07-01");
		expect(fiscalYearPicker(wrapper).props("options")).toEqual([
			{ label: "Second FY", value: "Second FY" },
		]);
	});
});

beforeEach(() => {
	vi.clearAllMocks();
	api.searchLink.mockResolvedValue([]);
	apiAgents.getAPReviewDefaults.mockReset();
	apiAgents.getAPReviewDefaults.mockResolvedValue(apDefaults());
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
		expect(apiAgents.getAPReviewDefaults).not.toHaveBeenCalled();
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

// jarvis#1062 P0-1 (production-readiness audit): the Advanced (JSON) panel
// re-collapsed when clicked inside its own expanded textarea, with the
// chevron desynced from the content's real open/closed state. Not
// reproduced here - DocSection.vue's toggle was already scoped to the
// header only (header and content are siblings, never ancestor/descendant)
// - but the header is now a real, keyboard-reachable <button> and the
// content wrapper carries its own @click.stop as defensive hardening.
// These specs document and lock in the required behavior either way.
describe("Advanced (JSON) panel: a content click never re-collapses it (jarvis#1062 P0-1)", () => {
	function openAdvanced(w) {
		return w.find(".border-t button").trigger("click");
	}
	function chevron(w) {
		return w.find(".lucide-chevron-right");
	}

	it("starts collapsed (chevron not rotated, textarea not visible)", () => {
		const w = mountForm({});
		expect(chevron(w).classes()).not.toContain("rotate-90");
		expect(w.find("textarea").isVisible()).toBe(false);
	});

	it("opens on a header click - chevron rotates, textarea becomes visible", async () => {
		const w = mountForm({});
		await openAdvanced(w);
		expect(chevron(w).classes()).toContain("rotate-90");
		expect(w.find("textarea").isVisible()).toBe(true);
	});

	it("a click INSIDE the open textarea keeps it open and does not desync the chevron", async () => {
		const w = mountForm({}, {}, { attachTo: document.body });
		await openAdvanced(w);
		const textarea = w.find("textarea");
		await textarea.trigger("click");
		// jsdom does not perform default actions (focus-on-click) for a
		// dispatched synthetic click the way a real browser does - call the
		// native focus() a real click would trigger, so the assertion below
		// reflects what a user actually experiences.
		textarea.element.focus();
		await w.vm.$nextTick();

		expect(w.find("textarea").isVisible()).toBe(true);
		expect(chevron(w).classes()).toContain("rotate-90");
		expect(document.activeElement).toBe(textarea.element);
		w.unmount();
	});

	it("clicking the header again still closes it (toggle still works after a content click)", async () => {
		const w = mountForm({});
		await openAdvanced(w);
		await w.find("textarea").trigger("click");
		await openAdvanced(w);
		expect(w.find("textarea").isVisible()).toBe(false);
		expect(chevron(w).classes()).not.toContain("rotate-90");
	});
});

describe("Advanced (JSON) hint text", () => {
	it("its example uses the NESTED materiality shape, not a flat key the form already renders a field for", () => {
		// jarvis#1063 CRITICAL follow-up: the old example -
		// {"benchmark_value": 1000000} - was exactly the flat legacy shape
		// save() deletes (migration is one-directional, flat -> nested). A
		// user following the hint verbatim would type a value that vanishes
		// on save. The example must never again name a key the form itself
		// covers.
		const w = mountForm({});
		const hint = w.text();
		expect(hint).toContain('"materiality"');
		expect(hint).not.toContain('"benchmark_value": 1000000');
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
