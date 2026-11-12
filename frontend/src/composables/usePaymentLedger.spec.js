import { vi, afterEach } from "vitest";
import { effectScope, ref } from "vue";
import { flushPromises } from "@vue/test-utils";
vi.mock("@/api/paymentCalendar", () => ({}));
vi.mock("@/utils/datetime", () => ({ siteTimezone: () => "UTC" }));
import { usePaymentCalendar } from "./usePaymentCalendar";
import { usePaymentLedger } from "./usePaymentLedger";
import { ok, refusal, deferred } from "../../tests/payment-calendar/fixtures";
import { scenario } from "../../tests/payment-calendar/templateFixtures";

const SID = "a".repeat(32);
// Reads wait 150 ms (debounce): run the clock past it, then wait for the reading itself.
async function settle(reading) {
	await vi.advanceTimersByTimeAsync(200);
	return reading;
}
const base = scenario("default");
const COMPANY = base.scope.company;
const ACCOUNTS = base.scope.accounts;
const monthId = (month) => (month === "current" ? SID : month.replace("-", "").padStart(32, "0"));

// The golden default snapshot (month 2026-11), re-dated for the fake clock.
function snapshotFor(id) {
	const value = scenario("default");
	value.snapshot_id = id;
	value.expires_at = new Date(Date.now() + 300000).toISOString();
	if (id === SID) return value;
	const month = id.slice(-6, -2) + "-" + id.slice(-2);
	value.scope = {
		...value.scope,
		month,
		window_start: month + "-01",
		window_end: month + "-28",
	};
	return value;
}
const records = (tag, count = 8) =>
	Array.from({ length: count }, (_, i) => ({ id: `${tag}-${i}`, voucher_no: `${tag}-${i}` }));
const reply = (tag, page = 1) => ({
	rows: records(`${tag}-p${page}`),
	total: 8,
	page,
	has_more: page < 2,
});
const tagOf = (side, scope, date, page, sorting) =>
	[side, scope === "date" ? date : scope, sorting.sort, sorting.order].join("|") + `|p${page}`;

function fakeApi() {
	return {
		getCapabilities: vi.fn(async () =>
			ok({
				companies: [{ name: COMPANY, default_currency: "INR" }],
				accounts: ACCOUNTS.map((name) => ({
					name,
					account_name: name,
					account_currency: "INR",
				})),
				settings: base.floor,
				can_configure: true,
			})
		),
		getPreferences: vi.fn(async () => ok({ company: COMPANY, accounts: ACCOUNTS })),
		setPreferences: vi.fn(async (company, accounts) => ok({ company, accounts })),
		requestSnapshot: vi.fn(async (company, accounts, month = "current") =>
			ok({ snapshot_id: monthId(month), state: "queued" })
		),
		getSnapshotStatus: vi.fn(async (id) => ok(snapshotFor(id))),
		getDayDetail: vi.fn(async (id, date, side, page, partyPage, sorting) =>
			ok(reply(tagOf(side, "date", date, page, sorting || {}), page))
		),
		getBucketDetail: vi.fn(async (id, bucket, side, page, partyPage, sorting) =>
			ok(reply(tagOf(side, bucket, "", page, sorting || {}), page))
		),
		getUpcoming: vi.fn(async () => ok({ rows: [], total: 0 })),
		configureFloor: vi.fn(async () => ok(base.floor)),
	};
}

describe("Payment ledger composable", () => {
	let scope, api, core, ledger;
	beforeEach(async () => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date("2026-11-11T05:00:00Z"));
		api = fakeApi();
		scope = effectScope();
		scope.run(() => {
			core = usePaymentCalendar({ api, timezone: () => "UTC" });
			ledger = usePaymentLedger(core, { api });
		});
		await core.initialize();
		await settle();
	});
	afterEach(() => {
		scope.stop();
		vi.useRealTimers();
	});
	// The old core detail reads pass no sorting; only the ledger passes it.
	const ledgerCalls = (mock) => mock.mock.calls.filter((args) => args[5] !== undefined);
	const lastLedgerCall = (mock) => ledgerCalls(mock).at(-1);
	const hold = (mock) => {
		const late = deferred();
		mock.mockReturnValueOnce(late.promise);
		return late;
	};
	// A soft refresh that swaps in a fresh snapshot of `month`.
	async function softRefresh(month = "current") {
		api.requestSnapshot.mockImplementation(async () =>
			ok({ snapshot_id: monthId(month), state: "queued" })
		);
		await vi.advanceTimersByTimeAsync(270000);
		await settle();
	}

	describe("defaults and sorting", () => {
		it("starts on the date scope, Payable side, remaining descending", () => {
			expect(ledger.panelScope.value).toBe("date");
			expect(ledger.panelSide.value).toBe("Payable");
			expect(ledger.sortKey.value).toBe("remaining");
			expect(ledger.sortDirection.value).toBe("desc");
		});
		it("gives a new key its default direction and toggles the same key", async () => {
			await settle(ledger.setSort("party"));
			expect([ledger.sortKey.value, ledger.sortDirection.value]).toEqual(["party", "asc"]);
			await settle(ledger.setSort("party"));
			expect(ledger.sortDirection.value).toBe("desc");
			await settle(ledger.setSort("remaining"));
			expect([ledger.sortKey.value, ledger.sortDirection.value]).toEqual([
				"remaining",
				"desc",
			]);
			await settle(ledger.setSort("due_date"));
			expect([ledger.sortKey.value, ledger.sortDirection.value]).toEqual([
				"due_date",
				"asc",
			]);
		});
		it("ignores a sort key outside the whitelist", async () => {
			await ledger.setSort("id; drop");
			expect(ledger.sortKey.value).toBe("remaining");
		});
	});

	describe("panel reads", () => {
		it("reads the selected date with the server sort and keeps the first five rows", () => {
			expect(lastLedgerCall(api.getDayDetail)).toEqual([
				SID,
				"2026-11-11",
				"Payable",
				1,
				1,
				{ sort: "remaining", order: "desc" },
				5,
			]);
			expect(ledger.panel.rows).toHaveLength(5);
			expect(ledger.panel.rows[0].id).toBe("Payable|2026-11-11|remaining|desc|p1-p1-0");
			expect(ledger.panel.total).toBe(8);
			expect(ledger.panel.loading).toBe(false);
		});
		it("reads the other side for a side change", async () => {
			await settle(ledger.setSide("Receivable"));
			expect(lastLedgerCall(api.getDayDetail)[2]).toBe("Receivable");
			expect(ledger.panel.rows[0].id).toContain("Receivable");
		});
		it("reads the month scope from the window bucket", async () => {
			await settle(ledger.setScope("month"));
			expect(lastLedgerCall(api.getBucketDetail)).toEqual([
				SID,
				"window",
				"Payable",
				1,
				1,
				{ sort: "remaining", order: "desc" },
				5,
			]);
		});
		it.each(["immediate", "now", "today", "overdue", "undated"])(
			"reads the %s scope from its bucket, Payable only",
			async (name) => {
				await settle(ledger.setSide("Receivable"));
				await settle(ledger.setScope(name));
				expect(ledger.panelSide.value).toBe("Payable");
				expect(lastLedgerCall(api.getBucketDetail).slice(1, 3)).toEqual([name, "Payable"]);
				await settle(ledger.setSide("Receivable"));
				expect(ledger.panelSide.value).toBe("Payable");
			}
		);
		it("returns to the date scope on the Payable side", async () => {
			await settle(ledger.setSide("Receivable"));
			await settle(ledger.setScope("overdue"));
			await settle(ledger.backToDate());
			expect([ledger.panelScope.value, ledger.panelSide.value]).toEqual(["date", "Payable"]);
			expect(lastLedgerCall(api.getDayDetail)[1]).toBe("2026-11-11");
		});
		it("shows Unavailable, never rows, for an old-shape reply", async () => {
			api.getDayDetail.mockResolvedValueOnce(ok({}));
			await settle(ledger.setSide("Receivable"));
			expect(ledger.panel.unavailable).toBe(true);
			expect(ledger.panel.rows).toEqual([]);
			expect(ledger.panel.loading).toBe(false);
		});
		it("copies the server total_remaining string through", async () => {
			api.getDayDetail.mockResolvedValueOnce(
				ok({ ...reply("T"), total_remaining: "1234.50" })
			);
			await settle(ledger.setSide("Receivable"));
			expect(ledger.panel.total_remaining).toBe("1234.50");
			api.getDayDetail.mockResolvedValueOnce(ok({ ...reply("T"), total_remaining: "9.00" }));
			await settle(ledger.openModal());
			expect(ledger.modal.total_remaining).toBe("9.00");
		});
		it("sets total_remaining to null when the reply has none", async () => {
			expect(ledger.panel.total_remaining).toBeNull();
			await settle(ledger.openModal());
			expect(ledger.modal.total_remaining).toBeNull();
		});
		it("clears total_remaining while a new read is loading", async () => {
			api.getDayDetail.mockResolvedValueOnce(ok({ ...reply("T"), total_remaining: "5.00" }));
			await settle(ledger.setSide("Receivable"));
			const late = hold(api.getDayDetail);
			const pending = ledger.setSide("Payable");
			expect(ledger.panel.total_remaining).toBeNull();
			await vi.advanceTimersByTimeAsync(200);
			late.resolve(ok(reply("T")));
			await pending;
		});
		it("clears old rows while a new read is loading", async () => {
			const late = hold(api.getDayDetail);
			const pending = ledger.setSide("Receivable");
			expect(ledger.panel.rows).toEqual([]);
			expect(ledger.panel.loading).toBe(true);
			await vi.advanceTimersByTimeAsync(200);
			late.resolve(ok(reply("late")));
			await pending;
			expect(ledger.panel.loading).toBe(false);
		});
	});

	describe("stale replies", () => {
		it("drops a slow older date reply (copy of core test 74)", async () => {
			const late = hold(api.getDayDetail);
			const old = core.selectDate("2026-11-12");
			await settle();
			await core.selectDate("2026-11-13");
			late.resolve(ok(reply("OLD")));
			await old;
			await settle();
			expect(ledger.panel.rows[0].id).toContain("2026-11-13");
			expect(api.requestSnapshot).toHaveBeenCalledTimes(1);
		});
		it("drops an older read of the same date when a newer sort is requested", async () => {
			const late = hold(api.getDayDetail);
			const old = ledger.setSort("party");
			await vi.advanceTimersByTimeAsync(200);
			await settle(ledger.setSort("due_date"));
			late.resolve(ok(reply("OLD")));
			await old;
			expect(ledger.panel.rows[0].id).toContain("due_date");
		});
		it("drops a reply that returns after the core began a new generation", async () => {
			const late = hold(api.getDayDetail);
			const pending = ledger.setSide("Receivable");
			// A soft refresh begins a new generation but has not swapped yet.
			api.getSnapshotStatus.mockReturnValueOnce(new Promise(() => {}));
			await vi.advanceTimersByTimeAsync(270000);
			late.resolve(ok(reply("OLD")));
			await pending;
			expect(ledger.panel.rows).toEqual([]);
		});
		it("drops a reply for the old snapshot after the swap", async () => {
			const late = hold(api.getDayDetail);
			const pending = ledger.setSide("Receivable");
			await softRefresh();
			late.resolve(ok(reply("OLD")));
			await pending;
			expect(ledger.panel.rows[0].id).not.toContain("OLD");
		});
		it("ignores a refusal that belongs to a stale read", async () => {
			const late = hold(api.getDayDetail);
			const old = ledger.setSort("party");
			await vi.advanceTimersByTimeAsync(200);
			await settle(ledger.setSort("due_date"));
			late.resolve(refusal("forbidden", "forbidden"));
			await old;
			expect(core.phase.value).toBe("ready");
			expect(ledger.panel.error).toBeNull();
		});
	});

	describe("core events", () => {
		it("skips reads for a date outside the snapshot month, then reads after the swap", async () => {
			const count = ledgerCalls(api.getDayDetail).length;
			api.getSnapshotStatus.mockReturnValueOnce(new Promise(() => {}));
			void core.selectDate("2026-12-02");
			await settle();
			expect(ledgerCalls(api.getDayDetail)).toHaveLength(count);
			expect(ledger.panel.loading).toBe(true);
			expect(ledger.panel.rows).toEqual([]);
			api.getSnapshotStatus.mockImplementation(async (id) => ok(snapshotFor(id)));
			core.refresh({ background: true, month: "2026-12" });
			await settle();
			expect(lastLedgerCall(api.getDayDetail)[1]).toBe("2026-12-02");
			expect(ledger.panel.loading).toBe(false);
			expect(ledger.panel.rows).toHaveLength(5);
		});
		it("returns to the date scope when a date is selected", async () => {
			await settle(ledger.setScope("immediate"));
			await core.selectDate("2026-11-12");
			await settle();
			expect(ledger.panelScope.value).toBe("date");
			expect(lastLedgerCall(api.getDayDetail)[1]).toBe("2026-11-12");
		});
		it("keeps scope, side and sort through a soft refresh (copy of core test 279)", async () => {
			await settle(ledger.setSide("Receivable"));
			await settle(ledger.setScope("month"));
			await settle(ledger.setSort("party"));
			const swaps = api.getSnapshotStatus.mock.calls.length;
			await softRefresh();
			expect(api.getSnapshotStatus.mock.calls.length).toBeGreaterThan(swaps);
			expect([ledger.panelScope.value, ledger.panelSide.value]).toEqual([
				"month",
				"Receivable",
			]);
			expect([ledger.sortKey.value, ledger.sortDirection.value]).toEqual(["party", "asc"]);
			expect(lastLedgerCall(api.getBucketDetail)).toEqual([
				SID,
				"window",
				"Receivable",
				1,
				1,
				{ sort: "party", order: "asc" },
				5,
			]);
			expect(ledger.panel.rows).toHaveLength(5);
		});
		it("resets to the date scope and clears rows when the accounts change", async () => {
			await settle(ledger.setScope("overdue"));
			const next = core.setAccounts(ACCOUNTS);
			expect(ledger.panelScope.value).toBe("date");
			expect(ledger.panel.rows).toEqual([]);
			await next;
			await settle();
			expect(ledger.panel.rows).toHaveLength(5);
		});
		it("clears the panel and closes the modal when the snapshot expires", async () => {
			await settle(ledger.openModal());
			api.getSnapshotStatus.mockReturnValue(new Promise(() => {}));
			await vi.advanceTimersByTimeAsync(300001);
			expect(core.snapshot.value).toBeNull();
			expect(ledger.panel.rows).toEqual([]);
			expect(ledger.modal.open).toBe(false);
		});
		it("stops loading when the core gives up on a busy worker (copy of core test 655)", async () => {
			const clock = vi.spyOn(performance, "now").mockImplementation(() => Date.now());
			try {
				api.requestSnapshot.mockResolvedValue(refusal("worker_busy"));
				await core.browseMonth(1);
				expect(ledger.panel.loading).toBe(true);
				await vi.advanceTimersByTimeAsync(65000);
				expect(core.refreshIssue.value.code).toBe("worker_busy");
				expect(ledger.panel.loading).toBe(false);
			} finally {
				clock.mockRestore();
			}
		});
		it("routes a refusal through the core access recovery", async () => {
			api.getDayDetail.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
			api.getCapabilities.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
			await settle(ledger.setSide("Receivable"));
			await settle();
			expect(core.phase.value).toBe("forbidden");
			expect(ledger.panel.rows).toEqual([]);
		});
		it("recovers once from a scope change refusal", async () => {
			api.getDayDetail.mockResolvedValueOnce(refusal("scope_changed"));
			await settle(ledger.setSide("Receivable"));
			await settle();
			expect(core.phase.value).toBe("ready");
			expect(ledger.panel.rows).toHaveLength(5);
		});
	});

	describe("read failures that are not refusals", () => {
		it("keeps the page, the date and the side, and shows the error on the panel", async () => {
			api.getDayDetail.mockResolvedValueOnce(refusal("network_error", "failed"));
			await settle(ledger.setSide("Receivable"));
			await settle();
			expect(ledger.panel.error).toEqual({
				code: "network_error",
				state: "failed",
				retryable: true,
			});
			expect(ledger.panel.rows).toEqual([]);
			expect(ledger.panel.loading).toBe(false);
			expect(core.phase.value).toBe("ready");
			expect(core.snapshot.value).not.toBeNull();
			expect([ledger.panelSide.value, core.selectedDate.value]).toEqual([
				"Receivable",
				"2026-11-11",
			]);
		});
		it("re-reads the same date and side on retry and clears the error", async () => {
			api.getDayDetail.mockResolvedValueOnce(refusal("network_error", "failed"));
			await settle(ledger.setSide("Receivable"));
			await settle(ledger.retry());
			expect(ledger.panel.error).toBeNull();
			expect(ledger.panel.rows).toHaveLength(5);
			expect(lastLedgerCall(api.getDayDetail).slice(1, 3)).toEqual([
				"2026-11-11",
				"Receivable",
			]);
		});
		it("shows the error on the modal without closing it", async () => {
			await settle(ledger.openModal());
			api.getDayDetail.mockResolvedValueOnce(refusal("network_error", "failed"));
			await settle(ledger.changeModalPage(2));
			expect(ledger.modal).toMatchObject({ open: true, page: 2 });
			expect(ledger.modal.error.code).toBe("network_error");
			expect(core.phase.value).toBe("ready");
			await settle(ledger.retry());
			expect(ledger.modal.error).toBeNull();
			expect(ledger.modal.rows).toHaveLength(8);
		});
		it("does not call the core refusal path for an ordinary failure", async () => {
			const spy = vi.spyOn(core, "handleRefusal");
			api.getDayDetail.mockResolvedValueOnce(refusal("network_error", "failed"));
			await settle(ledger.setSide("Receivable"));
			expect(spy).not.toHaveBeenCalled();
		});
		it("sends a refusal to the core path", async () => {
			const spy = vi.spyOn(core, "handleRefusal");
			api.getDayDetail.mockResolvedValueOnce(refusal("scope_changed"));
			await settle(ledger.setSide("Receivable"));
			expect(spy).toHaveBeenCalledTimes(1);
		});
	});

	describe("modal", () => {
		it("opens on the panel scope, side and date at page 1", async () => {
			await settle(ledger.setSide("Receivable"));
			await settle(ledger.openModal());
			expect(ledger.modal).toMatchObject({
				open: true,
				scope: "date",
				side: "Receivable",
				date: "2026-11-11",
				page: 1,
				total: 8,
				has_more: true,
				loading: false,
			});
			expect(ledger.modal.rows).toHaveLength(8);
		});
		it("pages and switches side inside the modal only", async () => {
			await settle(ledger.openModal());
			await settle(ledger.changeModalPage(2));
			expect(lastLedgerCall(api.getDayDetail)[3]).toBe(2);
			await settle(ledger.setModalSide("Receivable"));
			expect(ledger.modal.page).toBe(1);
			expect(ledger.panelSide.value).toBe("Payable");
		});
		it("keeps the modal page across a soft refresh when the scope is unchanged", async () => {
			await settle(ledger.setScope("overdue"));
			await settle(ledger.openModal());
			await settle(ledger.changeModalPage(2));
			await softRefresh();
			expect(ledger.modal).toMatchObject({ open: true, scope: "overdue", page: 2 });
			expect(lastLedgerCall(api.getBucketDetail)[3]).toBe(2);
			expect(ledger.modal.rows).toHaveLength(8);
		});
		it("returns to page 1 when the page no longer exists after the swap", async () => {
			await settle(ledger.setScope("overdue"));
			await settle(ledger.openModal());
			await settle(ledger.changeModalPage(2));
			api.getBucketDetail.mockImplementation(async (id, bucket, side, page) =>
				ok(page > 1 ? { rows: [], total: 8, page, has_more: false } : reply("P1", page))
			);
			await softRefresh();
			await settle();
			expect(ledger.modal.page).toBe(1);
			expect(lastLedgerCall(api.getBucketDetail)[3]).toBe(1);
			expect(ledger.modal.rows).toHaveLength(8);
		});
		it("closes when the new snapshot no longer holds the modal date", async () => {
			await settle(ledger.openModal());
			await core.browseMonth(1);
			await settle();
			expect(core.snapshot.value.scope.month).toBe("2026-12");
			expect(ledger.modal.open).toBe(false);
		});
		it("re-reads the panel and the modal under one generation on a sort change", async () => {
			await settle(ledger.openModal());
			await settle(ledger.changeModalPage(2));
			const generation = core.generation;
			const days = ledgerCalls(api.getDayDetail).length;
			const pending = settle(ledger.setSort("party"));
			// The panel and the modal show the same scope, side, date and page: one shared read.
			await pending;
			expect(ledgerCalls(api.getDayDetail)).toHaveLength(days + 1);
			expect(core.generation).toBe(generation);
			expect(ledger.modal.page).toBe(1);
			expect(ledger.panel.rows[0].id).toContain("party|asc");
			expect(ledger.modal.rows[0].id).toContain("party|asc|p1");
		});
		it("closes and drops its late reply", async () => {
			const late = hold(api.getDayDetail);
			const opening = ledger.openModal();
			ledger.closeModal();
			late.resolve(ok(reply("LATE")));
			await opening;
			expect(ledger.modal.open).toBe(false);
			expect(ledger.modal.rows).toEqual([]);
		});
	});

	describe("read scheduling", () => {
		const dayCalls = () => ledgerCalls(api.getDayDetail).length;
		it("sends only the last read of a burst", async () => {
			const before = dayCalls();
			void ledger.setSide("Receivable");
			void ledger.setSide("Payable");
			await settle(ledger.setSide("Receivable"));
			expect(dayCalls()).toBe(before + 1);
			expect(lastLedgerCall(api.getDayDetail)[2]).toBe("Receivable");
		});
		it("waits 150 ms before it sends", async () => {
			const before = dayCalls();
			const reading = ledger.setSide("Receivable");
			await vi.advanceTimersByTimeAsync(149);
			expect(dayCalls()).toBe(before);
			await vi.advanceTimersByTimeAsync(1);
			expect(dayCalls()).toBe(before + 1);
			await reading;
		});
		it("reads the upcoming payments at once, in parallel with the delayed day read", async () => {
			const upcoming = hold(api.getUpcoming);
			const before = dayCalls();
			const selecting = core.selectDate("2026-11-12");
			await vi.advanceTimersByTimeAsync(0);
			// The upcoming read has left; the day read still waits for the debounce.
			expect(api.getUpcoming.mock.calls.at(-1)[1]).toBe("2026-11-12");
			expect(dayCalls()).toBe(before);
			// A slow upcoming reply does not hold the day read back.
			await vi.advanceTimersByTimeAsync(150);
			expect(dayCalls()).toBe(before + 1);
			expect(ledger.panel.loading).toBe(false);
			expect(core.upcomingLoading.value).toBe(true);
			upcoming.resolve(ok({ rows: [], total: 0 }));
			await selecting;
			expect(core.upcomingLoading.value).toBe(false);
		});
		it("never sends a read whose generation was superseded while it waited", async () => {
			const before = dayCalls();
			const reading = ledger.setSide("Receivable");
			api.getSnapshotStatus.mockReturnValueOnce(new Promise(() => {}));
			void core.refresh({ background: true });
			await vi.advanceTimersByTimeAsync(200);
			await reading;
			expect(dayCalls()).toBe(before);
		});
		it("asks the panel alone for five rows", () => {
			expect(lastLedgerCall(api.getDayDetail)[6]).toBe(5);
		});
		it("serves the panel and the modal of the same scope and page from one read", async () => {
			await settle(ledger.openModal());
			const before = dayCalls();
			await settle(ledger.setSide("Receivable"));
			await settle(ledger.setModalSide("Receivable"));
			// Two sides were read: panel alone (size 5), then the modal's own side change.
			expect(dayCalls()).toBe(before + 2);
			const shared = dayCalls();
			await settle(ledger.setSort("party"));
			expect(dayCalls()).toBe(shared + 1);
			expect(lastLedgerCall(api.getDayDetail)[6]).toBe(25);
			expect(ledger.panel.rows).toHaveLength(5);
			expect(ledger.modal.rows).toHaveLength(8);
			expect(ledger.panel.rows[0].id).toBe(ledger.modal.rows[0].id);
		});
		it("reads the panel and the modal separately when their pages differ", async () => {
			await settle(ledger.openModal());
			await settle(ledger.changeModalPage(2));
			const before = dayCalls();
			await settle(ledger.setSort("party"));
			// The sort change resets the modal to page 1: same page as the panel, one read.
			expect(dayCalls()).toBe(before + 1);
			await settle(ledger.changeModalPage(2));
			expect(dayCalls()).toBe(before + 2);
			expect(ledger.panel.rows[0].id).toContain("p1");
		});
	});

	describe("while the core has no snapshot", () => {
		const loadingOnly = (area) => {
			expect(area.rows).toEqual([]);
			expect(area.loading).toBe(true);
			expect(area.error).toBeNull();
			expect(area.unavailable).toBe(false);
		};
		it.each(["date", "month", "immediate", "now", "today", "overdue", "undated"])(
			"shows loading, never an empty list, after the snapshot expires on the %s scope",
			async (name) => {
				await settle(ledger.setScope(name));
				api.getSnapshotStatus.mockReturnValue(new Promise(() => {}));
				await vi.advanceTimersByTimeAsync(300001);
				expect(core.snapshot.value).toBeNull();
				loadingOnly(ledger.panel);
			}
		);
		it("keeps loading when a date is picked before the replacement snapshot arrives", async () => {
			api.getSnapshotStatus.mockReturnValue(new Promise(() => {}));
			await vi.advanceTimersByTimeAsync(300001);
			expect(core.snapshot.value).toBeNull();
			void core.selectDate("2026-11-14");
			await vi.advanceTimersByTimeAsync(300);
			loadingOnly(ledger.panel);
		});
		it("shows loading when the snapshot is cleared by an account change", async () => {
			await settle(ledger.setScope("overdue"));
			api.getSnapshotStatus.mockReturnValue(new Promise(() => {}));
			void core.setAccounts(ACCOUNTS.slice(0, 1));
			await vi.advanceTimersByTimeAsync(10);
			expect(core.snapshot.value).toBeNull();
			loadingOnly(ledger.panel);
		});
		it.each(["overdue", "date"])(
			"shows the busy-worker error with Retry on the %s scope when the core abandons the wait",
			async (name) => {
				const clock = vi.spyOn(performance, "now").mockImplementation(() => Date.now());
				try {
					await settle(ledger.setScope(name));
					api.requestSnapshot.mockResolvedValue(refusal("worker_busy"));
					await vi.advanceTimersByTimeAsync(300001);
					expect(core.snapshot.value).toBeNull();
					await vi.advanceTimersByTimeAsync(65000);
					expect(core.refreshIssue.value.code).toBe("worker_busy");
					expect(ledger.panel.loading).toBe(false);
					expect(ledger.panel.error.code).toBe("worker_busy");
					expect(ledger.panel.rows).toEqual([]);
				} finally {
					clock.mockRestore();
				}
			}
		);
		it("asks the core to refresh on Retry while it has no snapshot", async () => {
			api.getSnapshotStatus.mockReturnValue(new Promise(() => {}));
			await vi.advanceTimersByTimeAsync(300001);
			const requests = api.requestSnapshot.mock.calls.length;
			void ledger.retry();
			await vi.advanceTimersByTimeAsync(10);
			expect(api.requestSnapshot.mock.calls.length).toBeGreaterThan(requests);
		});
	});

	// Tests moved out of usePaymentCalendar.spec.js in F7, when the core lost its detail reads.
	// Same invariants; every refusal reaches the core through handleRefusal.
	describe("moved from the core spec", () => {
		const nextId = "b".repeat(32);
		const refuseNextDayRead = (...refusals) => {
			api.getDayDetail.mockResolvedValueOnce(refusal(...refusals));
		};
		async function selectAndSettle(date) {
			await core.selectDate(date);
			await settle();
		}

		it("makes no read for a lookahead date and keeps the selection (core test 87-95)", async () => {
			const count = ledgerCalls(api.getDayDetail).length;
			await core.selectDate("2028-11-01");
			await settle();
			expect(core.selectedDate.value).toBe("2026-11-11");
			expect(ledgerCalls(api.getDayDetail)).toHaveLength(count);
			expect(ledger.panel.rows).toHaveLength(5);
		});

		describe("access recovery after a detail refusal (core tests 229/238/246/253)", () => {
			it("rechecks cleared capabilities when access is restored and Refresh is pressed", async () => {
				refuseNextDayRead("forbidden", "forbidden");
				api.getCapabilities.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
				await selectAndSettle("2026-11-12");
				expect(core.caps.value).toBeNull();
				await core.refresh();
				expect(core.caps.value?.can_configure).toBe(true);
			});
			it("rechecks capabilities when a revocation purges the snapshot as expired", async () => {
				refuseNextDayRead("not_found_or_expired", "expired");
				api.getCapabilities.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
				await selectAndSettle("2026-11-12");
				expect(core.phase.value).toBe("forbidden");
				expect(core.caps.value).toBeNull();
				expect(ledger.panel.rows).toEqual([]);
			});
			it("preserves still-permitted bank choices after a successful access recheck", async () => {
				refuseNextDayRead("forbidden", "forbidden");
				await selectAndSettle("2026-11-12");
				expect(core.accounts.value).toEqual(ACCOUNTS);
				expect(api.setPreferences.mock.calls.at(-1)).toEqual([COMPANY, ACCOUNTS]);
			});
			it("persists the permitted intersection when an expired snapshot rechecks accounts", async () => {
				const normal = (await api.getCapabilities(COMPANY)).data;
				const extra = [...ACCOUNTS, "Cash"];
				api.getCapabilities.mockResolvedValue(
					ok({ ...normal, accounts: [...normal.accounts, { name: "Cash" }] })
				);
				api.requestSnapshot.mockImplementation(async (company, accounts) => {
					api.getSnapshotStatus.mockImplementation(async () => {
						const value = snapshotFor(SID);
						value.scope.accounts = [...accounts].sort();
						return ok(value);
					});
					return ok({ snapshot_id: SID, state: "queued" });
				});
				await core.reloadCapabilities();
				await core.setAccounts(extra);
				await settle();
				expect(core.accounts.value).toEqual([...extra].sort());
				refuseNextDayRead("not_found_or_expired", "expired");
				api.getCapabilities.mockResolvedValueOnce(ok(normal));
				await selectAndSettle("2026-11-12");
				expect(core.accounts.value).toEqual(ACCOUNTS);
				expect(api.setPreferences).toHaveBeenLastCalledWith(COMPANY, ACCOUNTS);
			});
			it("goes through handleRefusal with the token of the read", async () => {
				const spy = vi.spyOn(core, "handleRefusal");
				refuseNextDayRead("forbidden", "forbidden");
				await selectAndSettle("2026-11-12");
				expect(spy).toHaveBeenCalledTimes(1);
				expect(spy.mock.calls[0][0].state).toBe("forbidden");
				expect(typeof spy.mock.calls[0][1]).toBe("number");
			});
		});

		describe("reads while a replacement snapshot is polled (core tests 351-367)", () => {
			const pollReplacement = async () => {
				api.requestSnapshot.mockResolvedValueOnce(
					ok({ snapshot_id: nextId, state: "queued" })
				);
				api.getSnapshotStatus.mockResolvedValueOnce(
					ok({ snapshot_id: nextId, state: "running" })
				);
				await vi.advanceTimersByTimeAsync(270000);
			};
			const swapIn = async () => {
				const fresh = snapshotFor(SID);
				fresh.snapshot_id = nextId;
				api.getSnapshotStatus.mockResolvedValueOnce(ok(fresh));
				await vi.advanceTimersByTimeAsync(1000);
			};
			it("reads stay on the old snapshot id until the swap, then use the new one", async () => {
				await pollReplacement();
				expect(core.snapshotId.value).toBe(SID);
				await selectAndSettle("2026-11-12");
				expect(lastLedgerCall(api.getDayDetail)[0]).toBe(SID);
				await swapIn();
				expect(core.snapshotId.value).toBe(nextId);
				expect(lastLedgerCall(api.getDayDetail)[0]).toBe(nextId);
			});
			it("keeps an obligation scope across the swap and reads it from the new id", async () => {
				await settle(ledger.setScope("overdue"));
				await pollReplacement();
				expect(lastLedgerCall(api.getBucketDetail)[0]).toBe(SID);
				await swapIn();
				expect(core.snapshotId.value).toBe(nextId);
				expect(lastLedgerCall(api.getBucketDetail)[0]).toBe(nextId);
				expect(ledger.panelScope.value).toBe("overdue");
			});
		});

		it("fetches an adjacent month's date in its own snapshot (core test 467-473)", async () => {
			await selectAndSettle("2026-12-02");
			expect(core.snapshot.value.scope.month).toBe("2026-12");
			expect(lastLedgerCall(api.getDayDetail)[1]).toBe("2026-12-02");
			expect(ledger.panel.rows).toHaveLength(5);
		});
		it("keeps a side change local while another month is being fetched (core test 499/507)", async () => {
			const late = deferred();
			api.getSnapshotStatus.mockReturnValueOnce(late.promise);
			const pending = core.browseMonth(1);
			await settle();
			const before = ledgerCalls(api.getDayDetail).length;
			await settle(ledger.setSide("Receivable"));
			expect(ledgerCalls(api.getDayDetail)).toHaveLength(before);
			expect(ledger.panelSide.value).toBe("Receivable");
			late.resolve(ok(snapshotFor(monthId("2026-12"))));
			await pending;
			await settle();
			expect(lastLedgerCall(api.getDayDetail)[2]).toBe("Receivable");
			expect(ledger.panel.rows).toHaveLength(5);
		});
		it("keeps the panel filled for a wall display that refreshes without input (core test 685)", async () => {
			await vi.advanceTimersByTimeAsync(30 * 60000);
			expect(core.snapshot.value).not.toBeNull();
			expect(ledger.panel.rows).toHaveLength(5);
			expect(ledger.panel.loading).toBe(false);
		});
	});
});

// The core events normally bump the ledger sequence too. A stub core that changes one
// value silently proves each of the four guard conditions on its own.
describe("Payment ledger stale-reply guard, one condition at a time", () => {
	let core, ledger, scope, late;
	beforeEach(async () => {
		vi.useFakeTimers();
		scope = effectScope();
		const data = snapshotFor(SID);
		core = {
			snapshot: ref(data),
			snapshotId: ref(SID),
			selectedDate: ref("2026-11-11"),
			generation: 1,
			isCurrent: (token) => token === core.generation,
			onPanelsInvalidated: () => () => {},
			onPanelsAbandoned: () => () => {},
			handleRefusal: vi.fn(),
		};
		late = deferred();
		const api = {
			getDayDetail: vi
				.fn()
				.mockReturnValueOnce(late.promise)
				.mockReturnValue(new Promise(() => {})),
			getBucketDetail: vi.fn(),
		};
		ledger = scope.run(() => usePaymentLedger(core, { api }));
		ledger.retry();
		// The first read leaves after the debounce and waits on `late`.
		await vi.advanceTimersByTimeAsync(200);
	});
	afterEach(() => {
		scope.stop();
		vi.useRealTimers();
	});
	const lateReply = async () => {
		late.resolve(ok(reply("LATE")));
		await settle();
	};

	it("applies the reply when all four conditions hold", async () => {
		await lateReply();
		expect(ledger.panel.rows).toHaveLength(5);
	});
	it("drops it when the core generation moved", async () => {
		core.generation = 2;
		await lateReply();
		expect(ledger.panel.rows).toEqual([]);
	});
	it("drops it when a newer read of the ledger started", async () => {
		ledger.retry();
		await lateReply();
		expect(ledger.panel.rows).toEqual([]);
	});
	it("drops it when the snapshot id changed", async () => {
		core.snapshotId.value = "b".repeat(32);
		await lateReply();
		expect(ledger.panel.rows).toEqual([]);
	});
	it("drops it when the requested date is no longer the selected date", async () => {
		core.selectedDate.value = "2026-11-12";
		await lateReply();
		expect(ledger.panel.rows).toEqual([]);
	});
});

describe("Payment ledger lifetime", () => {
	it("unsubscribes both core listeners when its effect scope stops", () => {
		const stopInvalidated = vi.fn(),
			stopAbandoned = vi.fn();
		const core = {
			snapshot: ref(null),
			snapshotId: ref(null),
			selectedDate: ref(""),
			onPanelsInvalidated: vi.fn(() => stopInvalidated),
			onPanelsAbandoned: vi.fn(() => stopAbandoned),
		};
		const scope = effectScope();
		scope.run(() => usePaymentLedger(core, { api: {} }));
		expect(core.onPanelsInvalidated).toHaveBeenCalledTimes(1);
		expect(core.onPanelsAbandoned).toHaveBeenCalledTimes(1);
		expect(stopInvalidated).not.toHaveBeenCalled();
		scope.stop();
		expect(stopInvalidated).toHaveBeenCalledTimes(1);
		expect(stopAbandoned).toHaveBeenCalledTimes(1);
	});
});
