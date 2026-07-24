import { describe, it, expect, vi, beforeEach } from "vitest";
import { badgeFor, useSupportStore } from "@/stores/support";

vi.mock("@/api", () => ({
	supportListTickets: vi.fn(),
	supportGetThread: vi.fn(),
	supportCreateTicket: vi.fn(),
	supportReply: vi.fn(),
	supportCloseTicket: vi.fn(),
	supportAwaitingCount: vi.fn(),
	supportUpload: vi.fn(),
	supportDownloadUrl: (t, f) => `/proxy?ticket=${t}&file_url=${f}`,
}));
vi.mock("frappe-ui", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import * as api from "@/api";
import { toast } from "frappe-ui";

describe("badgeFor", () => {
	// The AWAITING set mirrors jarvis_helpdesk/setup/install.py:39 and
	// jarvis_admin_v2/support/awaiting.py:10 — both ("Replied", "Resolved").
	it("treats Replied and Resolved as awaiting the customer", () => {
		expect(badgeFor("Replied")).toMatchObject({ label: "Awaiting you", theme: "orange" });
		expect(badgeFor("Resolved")).toMatchObject({ label: "Awaiting you", theme: "orange" });
	});

	it("treats Closed as closed", () => {
		expect(badgeFor("Closed")).toMatchObject({ label: "Closed", theme: "gray" });
	});

	it("falls back to Open for every other status, including unknown ones", () => {
		// The catch-all is load-bearing: Paused exists today and Helpdesk can add
		// statuses without a frontend deploy. An unknown status must never render
		// blank or crash a row.
		for (const s of ["Open", "Paused", "Escalated", "", null, undefined]) {
			expect(badgeFor(s)).toMatchObject({ label: "Open", theme: "blue" });
		}
	});
});

describe("support store", () => {
	beforeEach(() => {
		vi.clearAllMocks();
		const s = useSupportStore();
		s.tickets = [];
		s.ticketsError = "";
	});

	it("unwraps the {ok,data} envelope for the ticket list", async () => {
		api.supportListTickets.mockResolvedValue({
			ok: true,
			data: { tickets: [{ name: "T1" }] },
		});
		const s = useSupportStore();
		await s.loadTickets();
		expect(s.tickets).toEqual([{ name: "T1" }]);
	});

	it("toasts on a failed list AND keeps the last-good rows", async () => {
		const s = useSupportStore();
		api.supportListTickets.mockResolvedValue({
			ok: true,
			data: { tickets: [{ name: "T1" }] },
		});
		await s.loadTickets();
		api.supportListTickets.mockRejectedValue(new Error("boom"));
		await s.loadTickets();
		expect(toast.error).toHaveBeenCalled();
		// A blank list would read as "you have no tickets" — a lie.
		expect(s.tickets).toEqual([{ name: "T1" }]);
		expect(s.ticketsError).toBe("boom");
	});

	it("never toasts for the ambient awaiting count", async () => {
		api.supportAwaitingCount.mockRejectedValue(new Error("nope"));
		const s = useSupportStore();
		await s.refreshAwaiting();
		expect(toast.error).not.toHaveBeenCalled();
		expect(s.awaitingCount).toBe(0);
	});

	it("uploads every file even when one fails", async () => {
		api.supportUpload.mockRejectedValueOnce(new Error("too big")).mockResolvedValueOnce({});
		const s = useSupportStore();
		const done = await s.uploadTo("T1", [{ name: "a.png" }, { name: "b.png" }]);
		expect(api.supportUpload).toHaveBeenCalledTimes(2);
		expect(done).toBe(1);
		expect(toast.error).toHaveBeenCalledTimes(1);
	});

	it("fingerprints the whole row, so it detects change without assuming `modified` exists", () => {
		const s = useSupportStore();
		s.tickets = [{ name: "T1", status: "Open" }];
		const before = s.fingerprintOf("T1");
		s.tickets = [{ name: "T1", status: "Replied" }];
		expect(s.fingerprintOf("T1")).not.toBe(before);
	});
});
