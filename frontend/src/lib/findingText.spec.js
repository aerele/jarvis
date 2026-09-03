import { describe, it, expect } from "vitest";
import { extractTechnicalDetails } from "./findingText";

// jarvis#1062 P0-2 (production-readiness audit): findings/coverage prose is
// bundle-generated - these fixtures are lifted verbatim (or near-verbatim)
// from the audit's own screenshots, not invented examples.
describe("extractTechnicalDetails", () => {
	it("lifts a boolean evaluation flag out of its parenthetical", () => {
		const { text, details } = extractTechnicalDetails(
			"data-trust grade for Goods In Transit - ATD: FLAGGED (clears reorder gate: False)."
		);
		expect(text).not.toContain("clears reorder gate");
		expect(text).not.toContain("False");
		expect(details).toContainEqual({ label: "Flag", value: "clears reorder gate: False" });
	});

	it("lifts a 'N class(es) not_evaluable' phrase out entirely", () => {
		const { text, details } = extractTechnicalDetails(
			"graded on the classes that evaluated, with 1 class(es) not_evaluable."
		);
		expect(text).not.toContain("not_evaluable");
		expect(text).not.toContain("class(es)");
		expect(details).toContainEqual({ label: "Not evaluable", value: "1 class" });
	});

	it("pluralizes a multi-class count correctly", () => {
		const { details } = extractTechnicalDetails("with 3 class(es) not_evaluable.");
		expect(details).toContainEqual({ label: "Not evaluable", value: "3 classes" });
	});

	it("lifts a DocType.field schema reference", () => {
		const { text, details } = extractTechnicalDetails(
			"Configure no account<->warehouse mapping is populated (Warehouse.account empty)."
		);
		expect(text).not.toContain("Warehouse.account");
		expect(details).toContainEqual({ label: "Field reference", value: "Warehouse.account" });
	});

	it("lifts a rule code (<prefix>-<name>-<hex4>, the real rules.ids.json shape)", () => {
		const { text, details } = extractTechnicalDetails(
			"nsv-tieout-7d92: Configure the mapping."
		);
		expect(text).not.toContain("nsv-tieout-7d92");
		expect(details).toContainEqual({ label: "Rule", value: "nsv-tieout-7d92" });
	});

	it("never mistakes an ordinary English hyphenated compound for a rule code", () => {
		const { text, details } = extractTechnicalDetails(
			"This is an up-to-date, state-of-the-art check."
		);
		expect(text).toContain("up-to-date");
		expect(text).toContain("state-of-the-art");
		expect(details).toEqual([]);
	});

	it("never mistakes ordinary digit-bearing hyphenated prose for a rule code (last segment isn't 4 hex chars)", () => {
		const { text, details } = extractTechnicalDetails(
			"the form-16-reconciliation for q4-2026-close is not evaluable"
		);
		expect(text).toBe("the form-16-reconciliation for q4-2026-close is not evaluable");
		expect(details).toEqual([]);
	});

	it("never mistakes an all-digit final segment (e.g. a year) for a rule-id hex suffix", () => {
		const { text, details } = extractTechnicalDetails("see the audit-run-2026 log for detail");
		expect(text).toBe("see the audit-run-2026 log for detail");
		expect(details).toEqual([]);
	});

	it("substitutes a bare not_evaluable token that survives outside the class-count phrase", () => {
		const { text } = extractTechnicalDetails("This check is not_evaluable right now.");
		expect(text).toBe("This check is not evaluable right now.");
	});

	it("never mistakes a markdown link's (url) for a boolean flag", () => {
		const { text, details } = extractTechnicalDetails(
			"See [the policy](https://example.com/policy) for details."
		);
		expect(text).toContain("[the policy](https://example.com/policy)");
		expect(details).toEqual([]);
	});

	it("never mangles a markdown link whose host happens to be capitalised", () => {
		const { text, details } = extractTechnicalDetails(
			"See [the policy](https://Example.com/policy) for details."
		);
		expect(text).toBe("See [the policy](https://Example.com/policy) for details.");
		expect(details).toEqual([]);
	});

	it("never mangles a capitalised host in a protocol-less markdown link target either", () => {
		const { text, details } = extractTechnicalDetails("See [the policy](Example.com/policy).");
		expect(text).toBe("See [the policy](Example.com/policy).");
		expect(details).toEqual([]);
	});

	it("still extracts a real DocType.field reference sitting next to a markdown link", () => {
		const { text, details } = extractTechnicalDetails(
			"See [the policy](https://example.com/policy) - Warehouse.account is empty."
		);
		expect(text).not.toContain("Warehouse.account");
		expect(details).toContainEqual({ label: "Field reference", value: "Warehouse.account" });
	});

	it("never pulls a preceding capitalised sentence-starter into a one-word DocType.field reference", () => {
		// A multiword allowance was tried and reverted: bundle prose is
		// imperative and sentence-initial-capitalised constantly ("Configure
		// Warehouse.account first."), and there is no syntactic way to tell a
		// real multiword DocType name from an ordinary capitalised word sitting
		// in front of a one-word reference.
		const { text, details } = extractTechnicalDetails("Configure Warehouse.account first.");
		expect(text).toBe("Configure first.");
		expect(details).toContainEqual({ label: "Field reference", value: "Warehouse.account" });
	});

	it("does not extract a DocType.field reference with no space before its closing paren (the URL-guard's own cost)", () => {
		const { text, details } = extractTechnicalDetails("(Warehouse.account)");
		expect(text).toBe("(Warehouse.account)");
		expect(details).toEqual([]);
	});

	it("handles the exact audit-screenshot coverage-note text without throwing, and pulls out both the rule code and the field reference", () => {
		const raw =
			"not evaluable: nsv-tieout-7d92: Configure no account<->warehouse mapping is populated for any scoped warehouse (Warehouse.account empty)";
		const { text, details } = extractTechnicalDetails(raw);
		expect(text).not.toContain("nsv-tieout-7d92");
		expect(text).not.toContain("Warehouse.account");
		expect(details).toContainEqual({ label: "Rule", value: "nsv-tieout-7d92" });
		expect(details).toContainEqual({ label: "Field reference", value: "Warehouse.account" });
	});

	it("de-duplicates a token that appears more than once", () => {
		const { details } = extractTechnicalDetails(
			"nsv-grad-7d92 flagged this; see nsv-grad-7d92 for the rule."
		);
		expect(details.filter((d) => d.value === "nsv-grad-7d92")).toHaveLength(1);
	});

	it("returns an empty details list and the original text unchanged for plain prose", () => {
		const { text, details } = extractTechnicalDetails("Everything looks fine.");
		expect(text).toBe("Everything looks fine.");
		expect(details).toEqual([]);
	});

	it("handles null/undefined/empty input without throwing", () => {
		expect(extractTechnicalDetails(null)).toEqual({ text: "", details: [] });
		expect(extractTechnicalDetails(undefined)).toEqual({ text: "", details: [] });
		expect(extractTechnicalDetails("")).toEqual({ text: "", details: [] });
	});
});
