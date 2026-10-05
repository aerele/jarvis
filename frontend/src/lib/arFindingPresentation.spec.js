import { describe, expect, it } from "vitest";
import { arFindingPresentation } from "./arFindingPresentation";

function finding(overrides = {}) {
	return {
		agent: "ar-collections-operator",
		rule_id: "ar-review-v1",
		ref_doctype: "Sales Invoice",
		ref_name: "SI-1",
		result_class: "derived_candidate",
		detail_md: [
			"Settled — SI-1",
			"Customer: Example Ltd.; account: Debtors - E; company: Example.",
			"Recorded balance at cutoff: 0.0 INR. No positive recorded balance to chase; no reminder prepared.",
			"Cutoff 2026-09-30; current records, not historical reconstruction. Confirm disputes, unrecorded payments and the recipient with the named reviewer. No interest, fees, sending, posting, ECL or credit-risk conclusion.",
		].join("\n"),
		...overrides,
	};
}

describe("AR recorded finding presentation", () => {
	it("separates the recorded result, account identity, balance and scope without mutating history", () => {
		const source = finding();
		const original = JSON.stringify(source);
		const result = arFindingPresentation(source);
		expect(result.outcome).toBe("No reminder needed");
		expect(result.summary).toBe(
			"No positive recorded balance to chase; no reminder prepared."
		);
		expect(result.balance).toBe("0.0 INR");
		expect(result.cutoff).toBe("2026-09-30");
		expect(result.facts).toEqual([
			{ label: "Customer", value: "Example Ltd." },
			{ label: "Receivable account", value: "Debtors - E" },
			{ label: "Company", value: "Example" },
		]);
		expect(result.limitations).toHaveLength(3);
		expect(result.limitations[0]).toContain("not historical reconstruction");
		expect(result.india).toBeNull();
		expect(result.reminderDraft).toBe("");
		expect(JSON.stringify(source)).toBe(original);
	});

	it.each(["-5 INR", "1.234 KWD", "9007199254740993.001 KWD", "1E-7 USD"])(
		"preserves the recorded balance %s exactly rather than rounding or converting it",
		(balance) => {
			const source = finding();
			source.detail_md = source.detail_md.replace("0.0 INR", balance);
			expect(arFindingPresentation(source).balance).toBe(balance);
		}
	);

	it("preserves opening-period context and a recorded historical hold", () => {
		const source = finding();
		source.detail_md = source.detail_md
			.replace("Settled —", "Hold —")
			.replace(
				"Recorded balance at cutoff:",
				"Opening/prior-period invoice included. Recorded balance at cutoff:"
			)
			.replace(
				"No positive recorded balance to chase; no reminder prepared.",
				"Historical cutoff: review only, never a current payment request."
			);
		const result = arFindingPresentation(source);
		expect(result.opening).toBe(true);
		expect(result.summary).toContain("Historical cutoff");
		expect(result.outcome).toBe("Review needed before contact");
		expect(result.reminderDraft).toBe("");
	});

	it("does not replace a cutoff balance with the finding amount or a current balance", () => {
		const source = finding({ amount: 0, outstanding_amount: 60 });
		source.detail_md = source.detail_md
			.replace("Settled —", "Hold —")
			.replace("0.0 INR", "100.00 INR")
			.replace(
				"No positive recorded balance to chase; no reminder prepared.",
				"Later-dated allocations exist; historical/current balances must not become a demand."
			);
		expect(arFindingPresentation(source).balance).toBe("100.00 INR");
	});

	it("keeps missing evidence as a hold without inventing a zero balance", () => {
		const source = finding();
		source.detail_md = source.detail_md
			.replace("Settled —", "Hold —")
			.replace(
				"Recorded balance at cutoff: 0.0 INR. No positive recorded balance to chase; no reminder prepared.",
				"Evidence cannot support a reminder: No active invoice ledger evidence."
			);
		expect(arFindingPresentation(source).balance).toBe("");
		expect(arFindingPresentation(source).summary).toContain(
			"No active invoice ledger evidence"
		);
	});

	it("labels the India fields without treating missing fields as a compliance failure", () => {
		const source = finding();
		source.detail_md +=
			"\nIndia review: Verify the recorded tax evidence. E-invoice applicability and tax compliance are not determined. billing_address_gstin: not recorded; company_gstin: not recorded; einvoice_status: Pending Cancellation; gst_category: Registered Regular; irn: not recorded";
		const result = arFindingPresentation(source);
		expect(result.india.guidance).toBe(
			"Verify the recorded tax evidence. E-invoice applicability and tax compliance are not determined."
		);
		expect(result.india.fields).toHaveLength(5);
		expect(result.india.fields[0]).toEqual({ label: "Customer GSTIN", value: "not recorded" });
		expect(result.india.fields[2]).toEqual({
			label: "E-invoice status",
			value: "Pending Cancellation",
		});
	});

	it("retains unexpected India metadata verbatim instead of dropping it", () => {
		const source = finding();
		const content = "Verify evidence. irn: not recorded; new_field: retained";
		source.detail_md += `\nIndia review: ${content}`;
		expect(arFindingPresentation(source).india).toEqual({ guidance: content, fields: [] });
	});

	it("preserves an unsent draft's paragraphs exactly, separately from the result", () => {
		const source = finding();
		const draft =
			"Subject: Balance confirmation for invoice SI-1\n\nDear Customer,\nPlease confirm the balance.\n\nThank you.";
		source.detail_md =
			source.detail_md.replace("Settled —", "Reminder Candidate —") +
			"\n\nUNSENT DRAFT — human verification required\n" +
			draft;
		const result = arFindingPresentation(source);
		expect(result.reminderDraft).toBe(draft);
		expect(result.outcome).toBe("Reminder draft ready for review");
		expect(result.summary).not.toContain("Subject:");
	});

	it("does not label a missing draft as ready or hide a draft on a held invoice", () => {
		const source = finding();
		expect(
			arFindingPresentation({
				...source,
				detail_md: source.detail_md.replace("Settled —", "Reminder Candidate —"),
			})
		).toBeNull();
		expect(
			arFindingPresentation({
				...source,
				detail_md:
					source.detail_md +
					"\n\nUNSENT DRAFT — human verification required\nUnexpected draft",
			})
		).toBeNull();
	});

	it.each([
		{ agent: "another-agent" },
		{ rule_id: "another-rule" },
		{ ref_doctype: "Purchase Invoice" },
		{ ref_name: "SI-2" },
		{ result_class: "confirmed_outcome" },
		{ detail_md: "A new, unrecognised explanation.\nIt must remain visible." },
		{ detail_md: null },
	])("leaves unrelated or unrecognised records on the original renderer: %j", (overrides) => {
		expect(arFindingPresentation(finding(overrides), "ar-collections-operator")).toBeNull();
	});

	it("falls back without dropping unexpected lines or guessing ambiguous account labels", () => {
		const source = finding();
		expect(
			arFindingPresentation({
				...source,
				detail_md: source.detail_md + "\nAdditional material evidence",
			})
		).toBeNull();
		expect(
			arFindingPresentation({
				...source,
				detail_md: source.detail_md.replace("Example Ltd.", "Example; account: Another"),
			})
		).toBeNull();
	});

	it("handles CRLF and the runtime's bracket-safe invoice reference", () => {
		const source = finding({ ref_name: "SI[1]" });
		source.detail_md = source.detail_md.replace("SI-1", "SI(1)").replace(/\n/g, "\r\n");
		expect(arFindingPresentation(source).outcome).toBe("No reminder needed");
	});
});
