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
	getMySettings: vi.fn(),
	updateMySettings: vi.fn(),
}));
vi.mock("frappe-ui", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import * as api from "@/api";
import { toast } from "frappe-ui";

describe("badgeFor", () => {
	// The AWAITING set mirrors jarvis_helpdesk/setup/install.py:39 and
	// jarvis_admin_v2/support/awaiting.py:10 — both ("Replied", "Resolved").
	it("treats Replied and Resolved as awaiting the customer", () => {
		expect(badgeFor("Replied")).toMatchObject({
			label: "Awaiting you",
			theme: "orange",
			variant: "subtle",
		});
		expect(badgeFor("Resolved")).toMatchObject({
			label: "Awaiting you",
			theme: "orange",
			variant: "subtle",
		});
	});

	it("treats Closed as closed", () => {
		expect(badgeFor("Closed")).toMatchObject({
			label: "Closed",
			theme: "gray",
			variant: "subtle",
		});
	});

	it("falls back to Open for every other status, including unknown ones", () => {
		// The catch-all is load-bearing: Paused exists today and Helpdesk can add
		// statuses without a frontend deploy. An unknown status must never render
		// blank or crash a row. No `blue` theme (frappe-ui's Badge themes don't
		// include the brand's purple, and a generic blue read as off-theme next to
		// it) - `solid` gray instead, distinct from Closed's `subtle` gray.
		for (const s of ["Open", "Paused", "Escalated", "", null, undefined]) {
			expect(badgeFor(s)).toMatchObject({ label: "Open", theme: "gray", variant: "solid" });
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

	it("uploads every file even when one fails, and returns the succeeded FILE REFERENCES (not a count)", async () => {
		// Fix 2: the caller (useStagedFiles's settleUpload) needs to know exactly
		// which files landed so a retry only re-sends the failures — Helpdesk's
		// media.upload has no un-attach endpoint, so re-uploading a File that
		// already succeeded would create a permanent duplicate attachment.
		api.supportUpload.mockRejectedValueOnce(new Error("too big")).mockResolvedValueOnce({});
		const fileA = { name: "a.png" };
		const fileB = { name: "b.png" };
		const s = useSupportStore();
		const done = await s.uploadTo("T1", [fileA, fileB]);
		expect(api.supportUpload).toHaveBeenCalledTimes(2);
		expect(done).toEqual([fileB]);
		expect(toast.error).toHaveBeenCalledTimes(1);
	});

	it("fingerprints the whole row, so it detects change without assuming `modified` exists", () => {
		const s = useSupportStore();
		s.tickets = [{ name: "T1", status: "Open" }];
		const before = s.fingerprintOf("T1");
		s.tickets = [{ name: "T1", status: "Replied" }];
		expect(s.fingerprintOf("T1")).not.toBe(before);
	});

	describe("loadThread", () => {
		beforeEach(() => {
			const s = useSupportStore();
			s.tickets = [{ name: "T1", subject: "Help", status: "Open" }];
		});

		it("unwraps the {ok,data} envelope AND sets thread.ticket to the ticket NAME", async () => {
			api.supportGetThread.mockResolvedValue({
				ok: true,
				data: {
					messages: [{ id: 1 }],
					ticket_attachments: [{ file_url: "/files/a.png" }],
					ticket_meta: { name: "T1", status: "Open", priority: "Medium" },
				},
			});
			const s = useSupportStore();
			await s.loadThread("T1");
			expect(s.thread.messages).toEqual([{ id: 1 }]);
			expect(s.thread.attachments).toEqual([{ file_url: "/files/a.png" }]);
			// The right-panel metadata rides get_thread (not the list row) — see the
			// store comment; without this the panel is blank for deep-linked tickets.
			expect(s.thread.meta).toEqual({ name: "T1", status: "Open", priority: "Medium" });
			// M6: thread.ticket is the "which ticket is loaded" marker the page
			// compares against to detect a switch (C1) — it must always be the
			// NAME, never the row (ticketRow()/fingerprintOf() already cover row
			// data, and a row object here broke that comparison for the next
			// open() call).
			expect(s.thread.ticket).toBe("T1");
		});

		it("defaults thread.meta to null when get_thread omits ticket_meta", async () => {
			// A pre-panel backend (or a transient response) may not carry ticket_meta;
			// the panel must read null (-> renders "—"), never a stale previous ticket's.
			api.supportGetThread.mockResolvedValue({
				ok: true,
				data: { messages: [], ticket_attachments: [] },
			});
			const s = useSupportStore();
			await s.loadThread("T1");
			expect(s.thread.meta).toBeNull();
		});

		it("toasts on failure and records the error", async () => {
			api.supportGetThread.mockRejectedValue(new Error("boom"));
			const s = useSupportStore();
			await s.loadThread("T1");
			expect(toast.error).toHaveBeenCalled();
			expect(s.thread.error).toBe("boom");
		});

		it("keeps the newest ticket's data when an earlier fetch resolves out of order", async () => {
			// The failure this pins: open T1 (a multi-second bench→CP→Helpdesk
			// round-trip), back out and open T2 while T1 is still in flight, T2
			// resolves first, then T1 resolves LAST. Without the ticket-name
			// guard, T1's stale response unconditionally overwrites `thread` and
			// T2's conversation would render under T1's stale data.
			let resolveT1;
			let resolveT2;
			const p1 = new Promise((r) => (resolveT1 = r));
			const p2 = new Promise((r) => (resolveT2 = r));
			api.supportGetThread.mockImplementationOnce(() => p1).mockImplementationOnce(() => p2);
			const s = useSupportStore();

			const load1 = s.loadThread("T1");
			const load2 = s.loadThread("T2");

			// T2 — the LATER call — resolves FIRST.
			resolveT2({
				ok: true,
				data: { messages: [{ id: "t2-msg" }], ticket_attachments: [] },
			});
			await load2;
			expect(s.thread.ticket).toBe("T2");
			expect(s.thread.messages).toEqual([{ id: "t2-msg" }]);

			// T1 — the EARLIER call — resolves LAST and must be dropped.
			resolveT1({
				ok: true,
				data: { messages: [{ id: "t1-msg" }], ticket_attachments: [] },
			});
			await load1;
			expect(s.thread.ticket).toBe("T2");
			expect(s.thread.messages).toEqual([{ id: "t2-msg" }]);
		});
	});

	describe("createTicket", () => {
		it("unwraps the ticket name AND the opening comm from {ok,data}", async () => {
			// openingComm lets the caller thread ticket-creation-time attachments onto
			// the auto-mirrored opening message so they render inline, same as a
			// reply's - see reply()'s `comm` for the established pattern.
			api.supportCreateTicket.mockResolvedValue({
				ok: true,
				data: { ticket: "T9", opening_comm: "COMM-1" },
			});
			const s = useSupportStore();
			const result = await s.createTicket("Subject", "Body");
			expect(result).toEqual({ name: "T9", openingComm: "COMM-1" });
		});

		it("falls back openingComm to null when the CP doesn't echo one (older Helpdesk)", async () => {
			api.supportCreateTicket.mockResolvedValue({ ok: true, data: { ticket: "T9" } });
			const s = useSupportStore();
			const result = await s.createTicket("Subject", "Body");
			expect(result).toEqual({ name: "T9", openingComm: null });
		});

		it("toasts and returns null on failure", async () => {
			api.supportCreateTicket.mockRejectedValue(new Error("nope"));
			const s = useSupportStore();
			const name = await s.createTicket("Subject", "Body");
			expect(name).toBeNull();
			expect(toast.error).toHaveBeenCalled();
		});

		it("toasts and returns null on a malformed 200 with no ticket name (I7)", async () => {
			// The caller relies on "the store toasted" — a silent null leaves the
			// spinner stopping with no message.
			api.supportCreateTicket.mockResolvedValue({ ok: true, data: {} });
			const s = useSupportStore();
			const name = await s.createTicket("Subject", "Body");
			expect(name).toBeNull();
			expect(toast.error).toHaveBeenCalled();
		});
	});

	describe("reply", () => {
		it("returns true on success", async () => {
			api.supportReply.mockResolvedValue({ ok: true });
			const s = useSupportStore();
			expect(await s.reply("T1", "hello")).toBe(true);
		});

		it("toasts and returns false on failure", async () => {
			api.supportReply.mockRejectedValue(new Error("fail"));
			const s = useSupportStore();
			expect(await s.reply("T1", "hello")).toBe(false);
			expect(toast.error).toHaveBeenCalled();
		});
	});

	describe("closeTicket", () => {
		it("returns true on success", async () => {
			api.supportCloseTicket.mockResolvedValue({ ok: true });
			const s = useSupportStore();
			expect(await s.closeTicket("T1")).toBe(true);
		});

		it("toasts and returns false on failure", async () => {
			api.supportCloseTicket.mockRejectedValue(new Error("fail"));
			const s = useSupportStore();
			expect(await s.closeTicket("T1")).toBe(false);
			expect(toast.error).toHaveBeenCalled();
		});
	});

	// copyPref is module-scope singleton state (not a `tickets`-style array the
	// outer beforeEach can reset), and it starts null exactly once per test
	// FILE. So unlike every describe above, this one is ONE deliberately
	// sequential narrative, not independent cases - splitting it into
	// order-agnostic `it()`s would either need a way to reset the cache back to
	// null (there isn't one outside a fresh module load) or fake that reset by
	// calling setCopyPref(null), which isn't a real call site makes. Each step
	// is still a distinct assertion; only their ORDER is load-bearing, same
	// idea as "toasts on a failed list AND keeps the last-good rows" above.
	describe("getCopyPref / setCopyPref (sequential: the cache starts null once)", () => {
		it("a failed first fetch resolves '' but leaves the cache unset, so the very next call retries", async () => {
			const s = useSupportStore();
			api.getMySettings.mockRejectedValueOnce(new Error("network"));
			expect(await s.getCopyPref()).toBe("");
			expect(api.getMySettings).toHaveBeenCalledTimes(1);
		});

		it("concurrent callers before a fetch resolves share ONE request, not one each", async () => {
			const s = useSupportStore();
			let resolve;
			api.getMySettings.mockReturnValue(new Promise((r) => (resolve = r)));
			const a = s.getCopyPref();
			const b = s.getCopyPref();
			resolve({ data: { support_context_copy_pref: "Yes" } });
			expect(await a).toBe("Yes");
			expect(await b).toBe("Yes");
			expect(api.getMySettings).toHaveBeenCalledTimes(1);
		});

		it("a later call reads the now-populated cache - no further round-trip", async () => {
			// Review finding: this path used to be an uncached fetch on every
			// click, with no loading indicator, before this caching was added.
			const s = useSupportStore();
			expect(await s.getCopyPref()).toBe("Yes");
			expect(api.getMySettings).not.toHaveBeenCalled();
		});

		it("setCopyPref overrides the cache locally AND writes through via updateMySettings", async () => {
			const s = useSupportStore();
			api.updateMySettings.mockResolvedValue({});
			s.setCopyPref("No");
			expect(api.updateMySettings).toHaveBeenCalledWith({ support_context_copy_pref: "No" });
			expect(await s.getCopyPref()).toBe("No");
			expect(api.getMySettings).not.toHaveBeenCalled();
		});

		it("a failed write-through does not throw and does not roll back the local cache", async () => {
			const s = useSupportStore();
			api.updateMySettings.mockRejectedValue(new Error("offline"));
			expect(() => s.setCopyPref("Yes")).not.toThrow();
			// Local-first: the user already made the choice that drove this call;
			// nothing in the UI awaits the write-through, so a failed save stays
			// silent rather than rolling back what the user just saw take effect
			// (review finding, deferred as a MINOR - not retried, not surfaced).
			expect(await s.getCopyPref()).toBe("Yes");
		});
	});
});
