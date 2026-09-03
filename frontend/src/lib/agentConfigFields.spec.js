import { describe, it, expect } from "vitest";
import {
	CONFIG_FIELD_SET,
	SCOPE_CONFIG_FIELDS,
	AGENT_SPECIFIC_CONFIG_FIELDS,
	KNOWN_CONFIG_KEYS,
	CONFIG_FIELD_LABELS,
	NUMBER_CONFIG_KEYS,
	KEY_TO_PATH,
	getPath,
	setPath,
	deletePath,
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

// jarvis#1063 CRITICAL fix: close-auditor/evaluate.py reads its materiality
// inputs NESTED under a top-level "materiality" object, not as flat
// top-level keys - the scope keys stay flat (company, from_date, ...).
describe("KEY_TO_PATH (local form key -> storage dot path)", () => {
	it("scope keys are flat - path equals key", () => {
		expect(KEY_TO_PATH.company).toBe("company");
		expect(KEY_TO_PATH.fiscal_year).toBe("fiscal_year");
		expect(KEY_TO_PATH.from_date).toBe("from_date");
		expect(KEY_TO_PATH.to_date).toBe("to_date");
	});

	it("the materiality keys are nested dot paths", () => {
		expect(KEY_TO_PATH.benchmark_value).toBe("materiality.benchmark_value");
		expect(KEY_TO_PATH.percentage).toBe("materiality.percentage");
		expect(KEY_TO_PATH.engagement_risk_level).toBe("materiality.engagement_risk_level");
		expect(KEY_TO_PATH.rounding_step).toBe("materiality.rounding_step");
	});
});

describe("getPath / setPath / deletePath (dot-path helpers)", () => {
	it("getPath reads a flat and a nested path", () => {
		const obj = { company: "Acme", materiality: { benchmark_value: 100 } };
		expect(getPath(obj, "company")).toBe("Acme");
		expect(getPath(obj, "materiality.benchmark_value")).toBe(100);
	});

	it("getPath returns undefined for a missing segment at any depth", () => {
		expect(getPath({}, "materiality.benchmark_value")).toBeUndefined();
		expect(getPath({ materiality: {} }, "materiality.benchmark_value")).toBeUndefined();
		expect(getPath({ materiality: 5 }, "materiality.benchmark_value")).toBeUndefined();
	});

	it("setPath creates the intermediate object on a fresh target", () => {
		const obj = {};
		setPath(obj, "materiality.benchmark_value", 100);
		expect(obj).toEqual({ materiality: { benchmark_value: 100 } });
	});

	it("setPath MERGES into an existing intermediate object rather than replacing it", () => {
		const obj = { materiality: { pl_balance: 5000 } };
		setPath(obj, "materiality.benchmark_value", 100);
		expect(obj).toEqual({ materiality: { pl_balance: 5000, benchmark_value: 100 } });
	});

	it("deletePath removes just the leaf, siblings survive", () => {
		const obj = { materiality: { benchmark_value: 100, percentage: 5 } };
		deletePath(obj, "materiality.benchmark_value");
		expect(obj).toEqual({ materiality: { percentage: 5 } });
	});

	it("deletePath prunes the intermediate object once it is left empty", () => {
		const obj = { materiality: { benchmark_value: 100 } };
		deletePath(obj, "materiality.benchmark_value");
		expect(obj).toEqual({});
	});

	it("deletePath no-ops when the path does not resolve", () => {
		const obj = { company: "Acme" };
		deletePath(obj, "materiality.benchmark_value");
		expect(obj).toEqual({ company: "Acme" });
	});
});
