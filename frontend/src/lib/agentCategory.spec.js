import { describe, it, expect } from "vitest";
import { categoryTitle } from "./agentCategory";

describe("categoryTitle", () => {
	it("passes a real label straight through unchanged", () => {
		expect(categoryTitle("Accounts Payable")).toBe("Accounts Payable");
		expect(categoryTitle("CRM")).toBe("CRM");
		expect(categoryTitle("Close and Reporting")).toBe("Close and Reporting");
	});

	it("falls back to Other for an empty/nullish category", () => {
		expect(categoryTitle("")).toBe("Other");
		expect(categoryTitle(null)).toBe("Other");
		expect(categoryTitle(undefined)).toBe("Other");
	});

	it("title-cases a raw hyphenated slug (pre-migrate row fallback)", () => {
		expect(categoryTitle("bank-recon")).toBe("Bank Recon");
		expect(categoryTitle("hrms-payroll")).toBe("Hrms Payroll");
	});
});
