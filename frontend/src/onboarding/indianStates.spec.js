import { describe, it, expect } from "vitest";
import {
	COUNTRIES,
	INDIAN_STATES,
	isIndia,
	isValidIndianState,
	canonicalCountry,
	canonicalIndianState,
	isValidCountry,
} from "./indianStates.js";

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

// Review P1: restored / ERP-defaulted country values never pass through the <select>, so they
// must be canonicalised (legacy alias) and validated (a real Country) before submission.
describe("canonicalCountry / isValidCountry", () => {
	it("maps a legacy alias to the current canonical Country name", () => {
		expect(canonicalCountry("Turkey")).toBe("Türkiye");
		expect(isValidCountry("Turkey")).toBe(true);
	});

	it("resolves case-insensitively to the canonical casing", () => {
		expect(canonicalCountry("india")).toBe("India");
		expect(canonicalCountry("UNITED STATES")).toBe("United States");
		expect(isValidCountry("türkiye")).toBe(true);
	});

	it("rejects a value not in the list, including the old literal 'Other'", () => {
		expect(canonicalCountry("Other")).toBe("Other"); // returned as-is, not silently mapped
		expect(isValidCountry("Other")).toBe(false);
		expect(isValidCountry("")).toBe(false);
		expect(isValidCountry("Nowhereland")).toBe(false);
	});

	it("accepts a real Country name", () => {
		expect(isValidCountry("Türkiye")).toBe(true);
		expect(isValidCountry("India")).toBe(true);
		expect(isValidCountry(COUNTRIES[COUNTRIES.length - 1])).toBe(true);
	});
});

// canonicalIndianState: a GSTIN autofill must land a value the State <select> can show, or blank.
// India Compliance's state (titlecase of the registry stcd) may differ in spelling from our list.
describe("canonicalIndianState", () => {
	it("returns the exact option for a case-insensitive match", () => {
		expect(canonicalIndianState("KARNATAKA")).toBe("Karnataka");
		expect(canonicalIndianState("tamil nadu")).toBe("Tamil Nadu");
		// every canonical option resolves to itself
		for (const s of INDIAN_STATES) expect(canonicalIndianState(s)).toBe(s);
	});

	it("resolves known IC/registry aliases to the list's spelling", () => {
		expect(canonicalIndianState("Lakshadweep Islands")).toBe("Lakshadweep");
		expect(canonicalIndianState("Daman and Diu")).toBe(
			"Dadra and Nagar Haveli and Daman and Diu"
		);
		expect(canonicalIndianState("Pondicherry")).toBe("Puducherry");
	});

	it("returns '' on no confident match or empty (never an unshowable value)", () => {
		expect(canonicalIndianState("Freedonia")).toBe("");
		expect(canonicalIndianState("")).toBe("");
		expect(canonicalIndianState(null)).toBe("");
		// the resolved value, when non-empty, is always a real select option
		const r = canonicalIndianState("karnataka");
		expect(r === "" || isValidIndianState(r)).toBe(true);
	});
});
