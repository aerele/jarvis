import { describe, it, expect } from "vitest";
import {
	CONFIG_FIELD_SET,
	SCOPE_CONFIG_FIELDS,
	AGENT_SPECIFIC_CONFIG_FIELDS,
	KNOWN_CONFIG_KEYS,
	CONFIG_FIELD_LABELS,
	NUMBER_CONFIG_KEYS,
} from "./agentConfigFields";

describe("CONFIG_FIELD_SET", () => {
	it("covers exactly the 8 keys the run path reads (agent_scope.py + set_config docstring)", () => {
		expect([...KNOWN_CONFIG_KEYS].sort()).toEqual(
			[
				"company",
				"fiscal_year",
				"from_date",
				"to_date",
				"benchmark_value",
				"percentage",
				"engagement_risk_level",
				"rounding_step",
			].sort()
		);
	});

	it("orders Company, Fiscal year, the date pair, benchmark, percentage, risk, rounding", () => {
		const order = CONFIG_FIELD_SET.map((f) => f.key || f.keys.join("/"));
		expect(order).toEqual([
			"company",
			"fiscal_year",
			"from_date/to_date",
			"benchmark_value",
			"percentage",
			"engagement_risk_level",
			"rounding_step",
		]);
	});

	it("company and fiscal_year are link fields over the right DocType", () => {
		const byKey = Object.fromEntries(
			CONFIG_FIELD_SET.filter((f) => f.key).map((f) => [f.key, f])
		);
		expect(byKey.company).toMatchObject({ type: "link", linkDoctype: "Company" });
		expect(byKey.fiscal_year).toMatchObject({ type: "link", linkDoctype: "Fiscal Year" });
	});

	it("engagement_risk_level offers exactly Low/Medium/High plus an unset option", () => {
		const field = CONFIG_FIELD_SET.find((f) => f.key === "engagement_risk_level");
		expect(field.options.map((o) => o.value)).toEqual(["", "low", "medium", "high"]);
	});

	it("only benchmark_value/percentage/rounding_step are numeric", () => {
		expect(NUMBER_CONFIG_KEYS.sort()).toEqual(
			["benchmark_value", "percentage", "rounding_step"].sort()
		);
	});

	it("every field has a human label reachable from CONFIG_FIELD_LABELS", () => {
		for (const key of KNOWN_CONFIG_KEYS) {
			expect(CONFIG_FIELD_LABELS[key]).toBeTruthy();
		}
	});
});

// jarvis#1063 (jarvis-only half): ConfigForm.vue always renders
// SCOPE_CONFIG_FIELDS and gates AGENT_SPECIFIC_CONFIG_FIELDS by the agent's
// own config_keys - these two subsets must partition CONFIG_FIELD_SET.
describe("SCOPE_CONFIG_FIELDS / AGENT_SPECIFIC_CONFIG_FIELDS", () => {
	it("scope is exactly company, fiscal_year, the date pair - the universal dispatch scope", () => {
		expect(SCOPE_CONFIG_FIELDS.map((f) => f.key || f.keys.join("/"))).toEqual([
			"company",
			"fiscal_year",
			"from_date/to_date",
		]);
	});

	it("agent-specific is exactly the close-auditor materiality/risk/rounding set", () => {
		expect(AGENT_SPECIFIC_CONFIG_FIELDS.map((f) => f.key)).toEqual([
			"benchmark_value",
			"percentage",
			"engagement_risk_level",
			"rounding_step",
		]);
	});

	it("the two subsets partition CONFIG_FIELD_SET with no overlap and no gap", () => {
		expect(SCOPE_CONFIG_FIELDS.length + AGENT_SPECIFIC_CONFIG_FIELDS.length).toBe(
			CONFIG_FIELD_SET.length
		);
		const scopeSet = new Set(SCOPE_CONFIG_FIELDS);
		for (const f of AGENT_SPECIFIC_CONFIG_FIELDS) {
			expect(scopeSet.has(f)).toBe(false);
		}
	});
});
