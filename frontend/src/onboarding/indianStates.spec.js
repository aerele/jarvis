import { describe, it, expect } from "vitest";
import { COUNTRIES, isIndia, isValidIndianState } from "./indianStates.js";

// Review P1: the old curated list ended in the literal "Other". Address.country is a Link
// to a Frappe Country, and no Country named "Other" exists, so submitting it failed invoice
// creation after payment. The dropdown must offer only real Country names.
describe("COUNTRIES", () => {
	it("never offers the literal 'Other' (it is not a real Frappe Country)", () => {
		expect(COUNTRIES).not.toContain("Other");
	});

	it("lists India first, the curated markets next, then the full standard set", () => {
		expect(COUNTRIES[0]).toBe("India");
		expect(COUNTRIES.slice(0, 8)).toEqual([
			"India",
			"United States",
			"United Kingdom",
			"United Arab Emirates",
			"Singapore",
			"Australia",
			"Canada",
			"Germany",
		]);
		expect(COUNTRIES.length).toBeGreaterThan(200);
	});

	it("reaches the long tail that was previously only selectable as 'Other'", () => {
		expect(COUNTRIES).toContain("Japan");
		expect(COUNTRIES).toContain("Brazil");
		expect(COUNTRIES).toContain("Nigeria");
	});

	it("has no duplicates and no blank entries", () => {
		expect(new Set(COUNTRIES).size).toBe(COUNTRIES.length);
		expect(COUNTRIES.every((c) => typeof c === "string" && c.trim())).toBe(true);
	});

	it("still recognises India (domestic) vs a foreign country and valid Indian states", () => {
		expect(isIndia("India")).toBe(true);
		expect(isIndia("United States")).toBe(false);
		expect(isValidIndianState("Tamil Nadu")).toBe(true);
	});
});
