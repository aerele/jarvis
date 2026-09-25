import { describe, it, expect } from "vitest";
import {
	filesWaiting,
	skipLabel,
	refusalMessage,
	outcomeMessage,
	isSettled,
	missingSummary,
	statusLine,
} from "./heldActions";

describe("heldActions copy", () => {
	it("counts waiting files and labels Skip with them", () => {
		expect(filesWaiting(1)).toBe("1 file waiting");
		expect(filesWaiting(3)).toBe("3 files waiting");
		expect(skipLabel(1)).toBe("Don't create — skip 1 file");
		expect(skipLabel(4)).toBe("Don't create — skip 4 files");
		expect(skipLabel(0)).toBe("Don't create — skip 1 file");
	});

	it("words every refusal code, keeping the server's record-naming messages", () => {
		for (const code of [
			"not_found",
			"busy",
			"executing",
			"already_handled",
			"identity_refused",
			"stale",
			"target_missing",
			"tampered",
			"unverifiable",
			"record_required",
			"use_existing_unavailable",
			"interrupted",
		]) {
			const msg = refusalMessage({
				reason_code: code,
				error: { message: "raw server text" },
			});
			expect(msg, code).not.toBe("raw server text");
			expect(msg.length, code).toBeGreaterThan(10);
		}
		const exists = {
			reason_code: "exists",
			error: { message: "Supplier Acme already exists now." },
		};
		expect(refusalMessage(exists)).toBe("Supplier Acme already exists now.");
		const failed = { reason_code: "failed", error: { message: "Supplier Group is required" } };
		expect(refusalMessage(failed)).toBe("Couldn't create: Supplier Group is required");
		expect(refusalMessage({})).toBe("This approval could not be completed.");
		// Edit & create: the server names the field / record
		for (const code of [
			"needs_input",
			"edit_refused",
			"invalid",
			"unreadable",
			"pending_elsewhere",
		]) {
			expect(refusalMessage({ reason_code: code, error: { message: "server words" } })).toBe(
				"server words"
			);
		}
		expect(refusalMessage({ reason_code: "edit_unavailable" })).toContain("new records only");
	});

	it("tells the approver how many files continue", () => {
		expect(outcomeMessage({ reason_code: "created", waiters_count: 2 })).toBe(
			"Created. The 2 waiting files continue."
		);
		expect(
			outcomeMessage({
				reason_code: "use_existing",
				waiters_count: 1,
				data: { name: "SUP-9" },
			})
		).toBe("Using SUP-9. The waiting file continues.");
		expect(outcomeMessage({ reason_code: "discarded", waiters_count: 0 })).toBe(
			"Skipped. Nothing was created."
		);
		// A Skip that lost the race answers ok:true + already_handled: no "continue" claim.
		expect(
			outcomeMessage({ ok: true, reason_code: "already_handled", waiters_count: 3 })
		).toBe("This approval was already handled.");
	});

	it("drops a row from the lane only once it left Pending", () => {
		expect(isSettled({ ok: true })).toBe(true);
		expect(isSettled({ ok: false, reason_code: "failed", pa_status: "Failed" })).toBe(true);
		expect(isSettled({ ok: false, reason_code: "already_handled" })).toBe(true);
		for (const code of ["busy", "exists", "identity_refused", "record_not_found"]) {
			expect(isSettled({ ok: false, reason_code: code }), code).toBe(false);
		}
	});

	it("describes rows that can no longer be acted on", () => {
		expect(statusLine({ status: "Executing" })).toBe("Creating this record now…");
		expect(
			statusLine({
				status: "Executed",
				reason_code: "use_existing",
				result_doctype: "Supplier",
				result_name: "SUP-1",
			})
		).toBe("Resolved with the existing Supplier SUP-1.");
		expect(statusLine({ status: "Failed", reason: "The action was interrupted." })).toBe(
			"The action was interrupted."
		);
	});

	it("summarises missing fields as the ladder does", () => {
		expect(missingSummary(["A", "B", "C", "D", "E"])).toBe("A, B, C (+2 more)");
		expect(missingSummary(["A", "A", "B"])).toBe("A, B");
		expect(missingSummary([])).toBe("");
	});
});
