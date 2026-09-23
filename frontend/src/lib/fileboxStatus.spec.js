import { describe, it, expect } from "vitest";
import { STATUS_BADGE, STATUSES, STATUS_OPTIONS, statusBadge, resultLink } from "./fileboxStatus";

describe("fileboxStatus", () => {
	it("labels every ladder status", () => {
		expect(STATUSES.map((s) => STATUS_BADGE[s].label)).toEqual([
			"Processing",
			"Needs approval",
			"Draft created",
			"No draft",
			"Failed",
		]);
		expect(STATUS_BADGE.failed.theme).toBe("red");
		expect(STATUS_BADGE.draft_created.theme).toBe("green");
	});

	it("offers All + the five statuses as filter options", () => {
		expect(STATUS_OPTIONS.map((o) => o.value)).toEqual(["", ...STATUSES]);
	});

	it("still renders the legacy done / error values", () => {
		expect(statusBadge({ status: "done" }).label).toBe("Done");
		expect(statusBadge({ status: "error" })).toEqual({ label: "Failed", theme: "red" });
	});

	it("shows a queued-behind-chat processing row as waiting", () => {
		expect(statusBadge({ status: "processing", behind_chat: 1 }).label).toBe(
			"Waiting (behind chat)"
		);
		expect(statusBadge({ status: "failed", behind_chat: 1 }).label).toBe("Failed");
	});

	it("falls back to a gray raw label for an unknown status", () => {
		expect(statusBadge({ status: "weird" })).toEqual({ label: "weird", theme: "gray" });
		expect(statusBadge(null)).toEqual({ label: "", theme: "gray" });
	});

	it("links a draft to Desk and an approval in-app", () => {
		expect(resultLink({ result_link: "/app/purchase-invoice/PI-1" })).toEqual({
			kind: "desk",
			href: "/app/purchase-invoice/PI-1",
		});
		expect(resultLink({ result_link: "/approvals/ab12" })).toEqual({
			kind: "route",
			href: "/approvals/ab12",
		});
	});

	it("never links anything else", () => {
		for (const href of [
			null,
			undefined,
			"",
			"https://evil.example",
			"//evil.example/app/x",
			"javascript:alert(1)",
			"/c/abc",
			"/approvalsx",
		]) {
			expect(resultLink({ result_link: href })).toBeNull();
		}
		expect(resultLink(null)).toBeNull();
	});
});
