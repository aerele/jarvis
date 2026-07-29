import { describe, it, expect } from "vitest";
import { planFeatures, planCycleLabel } from "@/account/format.js";

// planFeatures exists because the billing page renders plans from two sources
// in one grid and they disagree about the shape of `features`.
describe("planFeatures", () => {
	it("passes a real array through", () => {
		expect(planFeatures({ features: ["Unlimited chats", "Priority support"] })).toEqual([
			"Unlimited chats",
			"Priority support",
		]);
	});

	it("parses the stringified JSON array get_account sends", () => {
		expect(planFeatures({ features: '["Unlimited chats", "Priority support"]' })).toEqual([
			"Unlimited chats",
			"Priority support",
		]);
	});

	it("splits the newline text the plan rows send", () => {
		expect(planFeatures({ features: "Unlimited chats\nPriority support\n" })).toEqual([
			"Unlimited chats",
			"Priority support",
		]);
	});

	it("falls back to text when a bracketed string is not valid JSON", () => {
		// Would have thrown away the content under a JSON-only parser.
		expect(planFeatures({ features: "[beta] early access" })).toEqual(["[beta] early access"]);
	});

	it("returns an empty list for every empty shape", () => {
		expect(planFeatures({})).toEqual([]);
		expect(planFeatures(null)).toEqual([]);
		expect(planFeatures({ features: "" })).toEqual([]);
		expect(planFeatures({ features: "   " })).toEqual([]);
		expect(planFeatures({ features: "[]" })).toEqual([]);
	});

	it("drops blank entries from either shape", () => {
		expect(planFeatures({ features: ["a", "", null, "b"] })).toEqual(["a", "b"]);
		expect(planFeatures({ features: "a\n\n  \nb" })).toEqual(["a", "b"]);
	});
});

describe("planCycleLabel", () => {
	it("keys the billed line off the cycle", () => {
		expect(planCycleLabel({ billing_cycle: "Annual" })).toBe("Billed annually");
		expect(planCycleLabel({ billing_cycle: "Monthly" })).toBe("Billed monthly");
	});

	it("leads with the trial when the plan has one", () => {
		expect(planCycleLabel({ billing_cycle: "Monthly", trial_days: 15 })).toBe(
			"15-day free trial, then billed monthly"
		);
	});
});
