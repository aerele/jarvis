import { describe, it, expect } from "vitest";
import { isApproveRejectPair } from "./approvalVerbs";

describe("approvalVerbs", () => {
	it("detects a lone approve-verb + reject-verb pair", () => {
		expect(isApproveRejectPair(["Approve", "Reject"])).toBe(true);
		expect(isApproveRejectPair(["Approved", "Rejected"])).toBe(true);
		expect(isApproveRejectPair(["Approve as drafted", "Reject this"])).toBe(true);
		expect(isApproveRejectPair([" approve ", " REJECT "])).toBe(true);
	});

	it("rejects sets that aren't an approve/reject pair", () => {
		expect(isApproveRejectPair(["Reject invoice"])).toBe(false);
		expect(isApproveRejectPair(["Yes", "No"])).toBe(false);
		expect(isApproveRejectPair(["Approve", "Approve"])).toBe(false);
		expect(isApproveRejectPair(["Approve", "Reject", "Escalate"])).toBe(false);
		expect(isApproveRejectPair([])).toBe(false);
		expect(isApproveRejectPair(null)).toBe(false);
	});
});
