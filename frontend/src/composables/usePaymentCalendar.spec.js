import { vi, afterEach } from "vitest";
import { effectScope } from "vue";
import { flushPromises } from "@vue/test-utils";
vi.mock("@/api/paymentCalendar", () => ({}));
vi.mock("@/utils/datetime", () => ({ siteTimezone: () => "UTC" }));
import { usePaymentCalendar } from "./usePaymentCalendar";
import {
	fakeApi,
	complete,
	detail,
	ok,
	refusal,
	deferred,
	SID,
} from "../../tests/payment-calendar/fixtures";

describe("Payment Calendar state and async boundaries", () => {
	let scope, api, model;
	beforeEach(() => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date("2026-10-24T12:00:00Z"));
		api = fakeApi();
		scope = effectScope();
		model = scope.run(() => usePaymentCalendar({ api, timezone: () => "UTC" }));
	});
	afterEach(() => {
		scope.stop();
		vi.useRealTimers();
	});

	it("restores validated preferences and displays one server snapshot", async () => {
		await model.initialize();
		await flushPromises();
		expect(model.company.value).toBe("Manufacturing");
		expect(model.accounts.value).toEqual(["Bank A"]);
		expect(model.snapshot.value.snapshot_id).toBe(SID);
		expect(model.selectedDate.value).toBe("2026-10-24");
		expect(model.day.value.projected_closing).toBe("110.00");
	});
	it("does not guess a company when no preference exists", async () => {
		api.getPreferences.mockResolvedValue(ok(null));
		await model.initialize();
		expect(model.company.value).toBe("");
		expect(api.requestSnapshot).not.toHaveBeenCalled();
	});
	it("ignores old preferences after the user has chosen another company", async () => {
		const late = deferred();
		api.getPreferences.mockReturnValue(late.promise);
		const initializing = model.initialize();
		await flushPromises();
		await model.chooseCompany("Services");
		late.resolve(ok({ company: "Manufacturing", accounts: ["Bank A"] }));
		await initializing;
		await flushPromises();
		expect(model.company.value).toBe("Services");
	});
	it("clears all financial state synchronously on company change and drops the old job reply", async () => {
		await model.initialize();
		await flushPromises();
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		const old = model.refresh();
		await flushPromises();
		const next = model.chooseCompany("Services");
		expect(model.snapshot.value).toBeNull();
		expect(model.detail.value).toBeNull();
		expect(model.accounts.value).toEqual([]);
		await next;
		late.resolve(ok(complete()));
		await old;
		await flushPromises();
		expect(model.snapshot.value.scope.company).toBe("Services");
	});
	it("drops a slow older date response without rerunning accounting sources", async () => {
		await model.initialize();
		await flushPromises();
		const late = deferred();
		api.getDayDetail.mockReturnValueOnce(late.promise);
		const old = model.selectDate("2026-10-25");
		await flushPromises();
		await model.selectDate("2026-10-26");
		late.resolve(ok(detail("2026-10-25")));
		await old;
		expect(model.detail.value.rows[0].voucher_no).toContain("2026-10-26");
		expect(api.requestSnapshot).toHaveBeenCalledTimes(1);
	});
	it("preserves selection when browsing months and refuses lookahead date selection", async () => {
		await model.initialize();
		await flushPromises();
		model.browseMonth(1);
		expect(model.month.value).toBe("2026-11");
		expect(model.selectedDate.value).toBe("2026-10-24");
		const count = api.getDayDetail.mock.calls.length;
		await model.selectDate("2027-10-01");
		expect(api.getDayDetail).toHaveBeenCalledTimes(count);
	});
	it("expires financial values explicitly and preserves selection for retry", async () => {
		await model.initialize();
		await flushPromises();
		await vi.advanceTimersByTimeAsync(300001);
		expect(model.phase.value).toBe("expired");
		expect(model.snapshot.value).toBeNull();
		expect(model.company.value).toBe("Manufacturing");
	});
	it("clears every panel after a permission refusal, including a late successful upcoming reply", async () => {
		await model.initialize();
		await flushPromises();
		const late = deferred();
		api.getUpcoming.mockReturnValueOnce(late.promise);
		api.getDayDetail.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		api.getCapabilities.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		const pending = model.selectDate("2026-10-25");
		await flushPromises();
		expect(model.snapshot.value).toBeNull();
		late.resolve(ok({ rows: [{ remaining: "PRIVATE_OLD_VALUE" }] }));
		await pending;
		expect(model.phase.value).toBe("forbidden");
		expect(model.upcoming.value).toEqual([]);
		expect(model.caps.value).toBeNull();
		expect(model.accounts.value).toEqual([]);
	});
	it("polls beyond five seconds and exposes a lost job with retry", async () => {
		api.getSnapshotStatus.mockResolvedValue(ok({ snapshot_id: SID, state: "queued" }));
		await model.initialize();
		api.getSnapshotStatus.mockResolvedValue(
			ok({ snapshot_id: SID, state: "failed", code: "job_lost" })
		);
		await vi.advanceTimersByTimeAsync(6100);
		expect(model.phase.value).toBe("failed");
		expect(model.error.value.code).toBe("job_lost");
	});
	it("serializes preference writes so a late old save cannot become the saved latest selection", async () => {
		await model.initialize();
		await flushPromises();
		const late = deferred();
		api.setPreferences.mockReturnValueOnce(late.promise);
		await model.chooseCompany("Services");
		await model.chooseCompany("Manufacturing");
		expect(api.setPreferences).toHaveBeenCalledTimes(1);
		late.resolve(ok({ company: "Services", accounts: [] }));
		await flushPromises();
		expect(api.setPreferences.mock.calls.at(-1)).toEqual(["Manufacturing", []]);
		expect(model.company.value).toBe("Manufacturing");
	});
	it("reloads floor capabilities after a revision conflict without losing filters", async () => {
		await model.initialize();
		await flushPromises();
		api.configureFloor.mockResolvedValue(refusal("settings_changed", "scope_changed"));
		const before = api.getCapabilities.mock.calls.length;
		await model.saveFloor("25.00");
		expect(model.floorError.value.code).toBe("settings_changed");
		expect(api.getCapabilities.mock.calls.length).toBeGreaterThan(before);
		expect(model.accounts.value).toEqual(["Bank A"]);
	});
	it("stops polling and clears values when the page leaves", async () => {
		api.getSnapshotStatus.mockResolvedValue(ok({ snapshot_id: SID, state: "queued" }));
		await model.initialize();
		const count = api.getSnapshotStatus.mock.calls.length;
		scope.stop();
		await vi.advanceTimersByTimeAsync(20000);
		expect(api.getSnapshotStatus).toHaveBeenCalledTimes(count);
		expect(model.snapshot.value).toBeNull();
	});
	it("releases a floor save after the company changes while saving", async () => {
		await model.initialize();
		const late = deferred();
		api.configureFloor.mockReturnValueOnce(late.promise);
		const saving = model.saveFloor("30.00");
		await model.chooseCompany("Services");
		late.resolve(ok(complete().floor));
		await saving;
		expect(model.floorSaving.value).toBe(false);
		expect(model.company.value).toBe("Services");
	});
	it("keeps a conflicting floor save busy until the fresh revision arrives", async () => {
		await model.initialize();
		api.configureFloor.mockResolvedValueOnce(refusal("settings_changed", "scope_changed"));
		const late = deferred();
		api.getCapabilities.mockReturnValueOnce(late.promise);
		const saving = model.saveFloor("33.00");
		await flushPromises();
		expect(model.floorSaving.value).toBe(true);
		await model.saveFloor("33.00");
		expect(api.configureFloor).toHaveBeenCalledTimes(1);
		late.resolve(ok({ ...model.caps.value, settings: { ...complete().floor, revision: 2 } }));
		await saving;
		expect(model.floorSaving.value).toBe(false);
	});
	it("acknowledges a saved floor without holding the dialog for a background snapshot", async () => {
		await model.initialize();
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		const result = await model.saveFloor("33.00");
		expect(result.ok).toBe(true);
		expect(model.floorSaving.value).toBe(false);
		late.resolve(ok(complete()));
		await flushPromises();
	});
	it("refreshes once when the site date changes and avoids a browser-ahead loop", async () => {
		await model.initialize();
		vi.setSystemTime(new Date("2026-10-25T00:00:00Z"));
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(2);
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(2);
	});
	it("bounds a permanently queued job and allows explicit retry", async () => {
		const clock = vi.spyOn(performance, "now").mockImplementation(() => Date.now());
		api.getSnapshotStatus.mockResolvedValue(ok({ snapshot_id: SID, state: "queued" }));
		await model.initialize();
		await vi.advanceTimersByTimeAsync(916000);
		expect(model.phase.value).toBe("failed");
		expect(model.error.value.code).toBe("timeout");
		await model.refresh();
		expect(model.phase.value).toBe("queued");
		clock.mockRestore();
	});
	it("uses the refreshed snapshot floor in the displayed configuration", async () => {
		await model.initialize();
		const changed = complete();
		changed.floor.safety_floor = "90.00";
		changed.floor.revision = 2;
		api.getSnapshotStatus.mockResolvedValueOnce(ok(changed));
		await model.refresh();
		expect(model.caps.value.settings.safety_floor).toBe("90.00");
	});
	it("rechecks cleared capabilities when access is restored and Refresh is pressed", async () => {
		await model.initialize();
		api.getDayDetail.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		api.getCapabilities.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		await model.selectDate("2026-10-25");
		expect(model.caps.value).toBeNull();
		await model.refresh();
		expect(model.caps.value?.can_configure).toBe(true);
	});
	it("rechecks capabilities when a revocation purges the snapshot as expired", async () => {
		await model.initialize();
		api.getDayDetail.mockResolvedValueOnce(refusal("not_found_or_expired", "expired"));
		api.getCapabilities.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		await model.selectDate("2026-10-25");
		expect(model.phase.value).toBe("forbidden");
		expect(model.caps.value).toBeNull();
	});
	it("preserves still-permitted bank choices after a successful access recheck", async () => {
		await model.initialize();
		api.getDayDetail.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		await model.selectDate("2026-10-25");
		expect(model.accounts.value).toEqual(["Bank A"]);
		expect(api.setPreferences.mock.calls.at(-1)).toEqual(["Manufacturing", ["Bank A"]]);
	});
	it("persists the permitted intersection when an expired snapshot rechecks accounts", async () => {
		const normal = (await api.getCapabilities("Manufacturing")).data;
		api.getCapabilities.mockResolvedValue(
			ok({ ...normal, accounts: [...normal.accounts, { name: "Cash" }] })
		);
		api.getPreferences.mockResolvedValue(
			ok({ company: "Manufacturing", accounts: ["Bank A", "Cash"] })
		);
		await model.initialize();
		api.getDayDetail.mockResolvedValueOnce(refusal("not_found_or_expired", "expired"));
		api.getCapabilities.mockResolvedValueOnce(ok(normal));
		await model.selectDate("2026-10-25");
		expect(model.accounts.value).toEqual(["Bank A"]);
		expect(api.setPreferences).toHaveBeenLastCalledWith("Manufacturing", ["Bank A"]);
	});
	it("ignores a slow party-page response after switching to customers", async () => {
		await model.initialize();
		const late = deferred();
		api.getDayDetail.mockReturnValueOnce(late.promise);
		const previous = model.changeDetailPage("parties", 2);
		await flushPromises();
		await model.setDetailView("customers");
		late.resolve(ok(detail("2026-10-24", "Payable", 1, 2)));
		await previous;
		expect(model.detail.value.rows[0].voucher_type).toBe("Sales Invoice");
	});
	it("soft refreshes before expiry and swaps a fresh snapshot without losing the view", async () => {
		await model.initialize();
		await model.selectDate("2026-10-25");
		model.browseMonth(1);
		await model.setDetailView("customers");
		await model.changeDetailPage("rows", 2);
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		await vi.advanceTimersByTimeAsync(270000);
		expect(api.requestSnapshot).toHaveBeenCalledTimes(2);
		expect(model.refreshing.value).toBe(true);
		expect(model.snapshot.value.snapshot_id).toBe(SID);
		const fresh = complete();
		fresh.generated_at = new Date().toISOString();
		fresh.expires_at = new Date(Date.now() + 300000).toISOString();
		fresh.projection.book_cash = "150.00";
		late.resolve(ok(fresh));
		await flushPromises();
		expect(model.refreshing.value).toBe(false);
		expect(model.snapshot.value.projection.book_cash).toBe("150.00");
		expect(model.selectedDate.value).toBe("2026-10-25");
		expect(model.month.value).toBe("2026-11");
		expect(model.activeView.value).toBe("customers");
		expect(model.pages.value.Receivable.rows).toBe(2);
	});
	it("does not restart an automatic job on focus and clears expired figures while it finishes", async () => {
		await model.initialize();
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		await vi.advanceTimersByTimeAsync(270000);
		window.dispatchEvent(new Event("focus"));
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(2);
		await vi.advanceTimersByTimeAsync(30001);
		expect(model.snapshot.value).toBeNull();
		expect(model.phase.value).not.toBe("expired");
		const fresh = complete();
		fresh.expires_at = new Date(Date.now() + 300000).toISOString();
		late.resolve(ok(fresh));
		await flushPromises();
		expect(model.phase.value).toBe("ready");
		expect(api.requestSnapshot).toHaveBeenCalledTimes(2);
	});
	it("defers hidden-tab refresh and resumes once without requiring a manual retry", async () => {
		await model.initialize();
		const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
		await vi.advanceTimersByTimeAsync(270000);
		expect(api.requestSnapshot).toHaveBeenCalledTimes(1);
		const fresh = complete();
		fresh.expires_at = new Date(Date.now() + 300000).toISOString();
		api.getSnapshotStatus.mockResolvedValueOnce(ok(fresh));
		hidden.mockReturnValue(false);
		document.dispatchEvent(new Event("visibilitychange"));
		await flushPromises();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(2);
		expect(model.phase.value).toBe("ready");
		hidden.mockRestore();
	});
	it("does not let background work supersede a pending floor write", async () => {
		await model.initialize();
		const late = deferred();
		api.configureFloor.mockReturnValueOnce(late.promise);
		const saving = model.saveFloor("35.00");
		await vi.advanceTimersByTimeAsync(270000);
		expect(api.requestSnapshot).toHaveBeenCalledTimes(1);
		late.resolve(ok(complete().floor));
		await saving;
		await flushPromises();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(2);
	});

	it("keeps the displayed snapshot usable while polling a different replacement ID", async () => {
		await model.initialize();
		await model.openBucket("Payable", "overdue");
		const nextId = "b".repeat(32);
		api.requestSnapshot.mockResolvedValueOnce(ok({ snapshot_id: nextId, state: "queued" }));
		api.getSnapshotStatus.mockResolvedValueOnce(ok({ snapshot_id: nextId, state: "running" }));
		await vi.advanceTimersByTimeAsync(270000);
		expect(model.snapshotId.value).toBe(SID);
		await model.selectDate("2026-10-26");
		expect(api.getDayDetail.mock.calls.at(-1)[0]).toBe(SID);
		const fresh = complete("Manufacturing", ["Bank A"], nextId);
		fresh.expires_at = new Date(Date.now() + 300000).toISOString();
		api.getSnapshotStatus.mockResolvedValueOnce(ok(fresh));
		await vi.advanceTimersByTimeAsync(1000);
		expect(model.snapshotId.value).toBe(nextId);
		expect(api.getDayDetail.mock.calls.at(-1)[0]).toBe(nextId);
		expect(api.getBucketDetail.mock.calls.at(-1)[0]).toBe(nextId);
		expect(model.bucket.value.name).toBe("overdue");
	});
	it("resumes after expiry in a hidden tab and cancels all refresh work on disposal", async () => {
		await model.initialize();
		const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
		await vi.advanceTimersByTimeAsync(300001);
		expect(model.snapshot.value).toBeNull();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(1);
		const fresh = complete();
		fresh.expires_at = new Date(Date.now() + 300000).toISOString();
		api.getSnapshotStatus.mockResolvedValueOnce(ok(fresh));
		hidden.mockReturnValue(false);
		document.dispatchEvent(new Event("visibilitychange"));
		await flushPromises();
		expect(model.snapshot.value).not.toBeNull();
		scope.stop();
		await vi.advanceTimersByTimeAsync(600000);
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(2);
		hidden.mockRestore();
	});
	it("rejects a late background snapshot after the company changes", async () => {
		await model.initialize();
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		await vi.advanceTimersByTimeAsync(270000);
		const fresh = complete("Services", []);
		fresh.expires_at = new Date(Date.now() + 300000).toISOString();
		api.getSnapshotStatus.mockResolvedValueOnce(ok(fresh));
		await model.chooseCompany("Services");
		late.resolve(ok(complete()));
		await flushPromises();
		expect(model.snapshot.value.scope.company).toBe("Services");
	});
	it("clears values after a background network failure and does not retry in a loop", async () => {
		await model.initialize();
		api.requestSnapshot.mockResolvedValueOnce(refusal("network_error", "failed"));
		await vi.advanceTimersByTimeAsync(270000);
		expect(model.phase.value).toBe("failed");
		expect(model.snapshot.value).toBeNull();
		expect(model.refreshing.value).toBe(false);
		window.dispatchEvent(new Event("focus"));
		await vi.advanceTimersByTimeAsync(600000);
		expect(api.requestSnapshot).toHaveBeenCalledTimes(2);
	});
	it("clears financial data and controls when background access is revoked", async () => {
		await model.initialize();
		api.requestSnapshot.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		api.getCapabilities.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		await vi.advanceTimersByTimeAsync(270000);
		expect(model.phase.value).toBe("forbidden");
		expect(model.snapshot.value).toBeNull();
		expect(model.caps.value).toBeNull();
		expect(model.accounts.value).toEqual([]);
	});
	function monthlyApi() {
		api.requestSnapshot.mockImplementation(async (company, accounts, month = "2026-10") =>
			ok({
				snapshot_id: (month === "current"
					? new Date(Date.now()).toISOString().slice(0, 7)
					: month
				)
					.replace("-", "")
					.padStart(32, "0"),
				state: "queued",
			})
		);
		api.getSnapshotStatus.mockImplementation(async (id) => {
			const month = id.slice(-6, -2) + "-" + id.slice(-2);
			const value = complete();
			value.snapshot_id = id;
			value.expires_at = new Date(Date.now() + 300000).toISOString();
			value.scope.anchor = new Date(Date.now()).toISOString().slice(0, 10);
			value.scope = {
				...value.scope,
				month,
				window_start: month + "-01",
				window_end:
					month +
					"-" +
					new Date(Number(month.slice(0, 4)), Number(month.slice(5)), 0).getDate(),
			};
			value.projection.start = value.scope.window_start;
			value.projection.end = value.scope.window_end;
			value.projection.month = month;
			return ok(value);
		});
	}
	it("loads only the current month initially and requests a new month on navigation", async () => {
		monthlyApi();
		await model.initialize();
		expect(model.snapshot.value.scope.month).toBe("2026-10");
		await model.browseMonth(1);
		await flushPromises();
		expect(api.requestSnapshot.mock.calls.at(-1)[2]).toBe("2026-11");
		expect(model.snapshot.value.scope.month).toBe("2026-11");
		expect(model.selectedDate.value).toBe("2026-11-01");
	});
	it("fetches adjacent selected dates and Today in their own month", async () => {
		monthlyApi();
		await model.initialize();
		await model.selectDate("2026-11-02");
		await flushPromises();
		expect(model.snapshot.value.scope.month).toBe("2026-11");
		expect(api.getDayDetail.mock.calls.at(-1)[1]).toBe("2026-11-02");
		await model.goToday();
		await flushPromises();
		expect(model.snapshot.value.scope.month).toBe("2026-10");
		expect(model.selectedDate.value).toBe("2026-10-24");
	});
	it("does not request a second job for date changes inside the loaded month", async () => {
		monthlyApi();
		await model.initialize();
		await model.selectDate("2026-10-26");
		expect(api.requestSnapshot).toHaveBeenCalledTimes(1);
	});
	it("ignores late results when the user browses several months quickly", async () => {
		monthlyApi();
		await model.initialize();
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		const old = model.browseMonth(1);
		await flushPromises();
		await model.browseMonth(1);
		await flushPromises();
		late.resolve(ok(complete()));
		await old;
		expect(model.month.value).toBe("2026-12");
		expect(model.snapshot.value.scope.month).toBe("2026-12");
	});
	it("keeps tab changes local while another month is being fetched", async () => {
		monthlyApi();
		await model.initialize();
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		const pending = model.browseMonth(1);
		await flushPromises();
		const before = api.getDayDetail.mock.calls.length;
		await model.setDetailView("customers");
		expect(api.getDayDetail).toHaveBeenCalledTimes(before);
		const monthly = await api.getSnapshotStatus("202611".padStart(32, "0"));
		late.resolve(monthly);
		await pending;
		expect(api.getDayDetail.mock.calls.at(-1)[2]).toBe("Receivable");
	});
	it("honours an adjacent click back into the loaded month during a slow navigation", async () => {
		monthlyApi();
		await model.initialize();
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		const pending = model.browseMonth(1);
		await flushPromises();
		await model.selectDate("2026-10-31");
		late.resolve(await api.getSnapshotStatus("202611".padStart(32, "0")));
		await pending;
		expect(model.snapshot.value.scope.month).toBe("2026-10");
		expect(model.selectedDate.value).toBe("2026-10-31");
	});
	it("loads a valid deep-linked month directly rather than building today first", async () => {
		monthlyApi();
		scope.stop();
		scope = effectScope();
		model = scope.run(() =>
			usePaymentCalendar({ api, timezone: () => "UTC", initialDate: "2026-11-05" })
		);
		await model.initialize();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(1);
		expect(api.requestSnapshot.mock.calls[0][2]).toBe("2026-11");
		expect(model.selectedDate.value).toBe("2026-11-05");
	});
	it("coalesces rapid month choices behind the active server job", async () => {
		monthlyApi();
		await model.initialize();
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		const loading = model.browseMonth(1);
		await flushPromises();
		await model.browseMonth(1);
		await model.browseMonth(1);
		await model.browseMonth(1);
		expect(api.requestSnapshot).toHaveBeenCalledTimes(2);
		expect(model.month.value).toBe("2027-02");
		late.resolve(await api.getSnapshotStatus("202611".padStart(32, "0")));
		await loading;
		await flushPromises();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(3);
		expect(model.snapshot.value.scope.month).toBe("2027-02");
	});
	it("keeps the current view on worker contention and retries only the latest month", async () => {
		monthlyApi();
		await model.initialize();
		api.requestSnapshot.mockResolvedValueOnce(refusal("worker_busy"));
		await model.browseMonth(1);
		expect(model.snapshot.value).not.toBeNull();
		expect(model.phase.value).toBe("ready");
		expect(model.refreshing.value).toBe(true);
		await model.browseMonth(1);
		expect(api.requestSnapshot).toHaveBeenCalledTimes(2);
		await vi.advanceTimersByTimeAsync(1000);
		expect(model.snapshot.value.scope.month).toBe("2026-12");
		expect(model.error.value).toBeNull();
	});
	it("bounds worker-busy retries and keeps only nonfinancial layout after expiry", async () => {
		monthlyApi();
		await model.initialize();
		const clock = vi.spyOn(performance, "now").mockImplementation(() => Date.now());
		try {
			api.requestSnapshot.mockResolvedValue(refusal("worker_busy"));
			await model.browseMonth(1);
			await vi.advanceTimersByTimeAsync(65000);
			expect(model.refreshing.value).toBe(false);
			expect(model.refreshIssue.value.code).toBe("worker_busy");
			const count = api.requestSnapshot.mock.calls.length;
			await vi.advanceTimersByTimeAsync(300000);
			expect(api.requestSnapshot).toHaveBeenCalledTimes(count);
			expect(model.snapshot.value).toBeNull();
			expect(model.loadingSnapshot.value.projection.book_cash).toBeNull();
			expect(model.loadingSnapshot.value.projection.days).toEqual({});
		} finally {
			clock.mockRestore();
		}
	});
	it("stops panel loading when a month selection exhausts worker-busy retries", async () => {
		monthlyApi();
		await model.initialize();
		const clock = vi.spyOn(performance, "now").mockImplementation(() => Date.now());
		try {
			api.requestSnapshot.mockResolvedValue(refusal("worker_busy"));
			await model.browseMonth(1);
			expect(model.detailLoading.value).toBe(true);
			await vi.advanceTimersByTimeAsync(65000);
			expect(model.refreshIssue.value.code).toBe("worker_busy");
			expect(model.detailLoading.value).toBe(false);
			expect(model.upcomingLoading.value).toBe(false);
			monthlyApi();
			await model.refresh();
			expect(model.snapshot.value.scope.month).toBe("2026-11");
			expect(model.detailLoading.value).toBe(false);
			expect(model.upcomingLoading.value).toBe(false);
		} finally {
			clock.mockRestore();
		}
	});
	it("pauses idle report refreshes, clears expired figures and resumes once on interaction", async () => {
		monthlyApi();
		await model.initialize();
		await vi.advanceTimersByTimeAsync(10 * 60000);
		const requests = api.requestSnapshot.mock.calls.length;
		await vi.advanceTimersByTimeAsync(20 * 60000);
		expect(api.requestSnapshot).toHaveBeenCalledTimes(requests);
		expect(model.snapshot.value).toBeNull();
		expect(model.detail.value).toBeNull();
		expect(model.upcoming.value).toEqual([]);
		expect(model.phase.value).toBe("expired");
		document.dispatchEvent(new Event("click"));
		await flushPromises();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(requests + 1);
		expect(model.phase.value).toBe("ready");
		scope.stop();
		document.dispatchEvent(new Event("click"));
		document.dispatchEvent(new Event("keydown"));
		document.dispatchEvent(new Event("wheel"));
		await vi.advanceTimersByTimeAsync(20 * 60000);
		expect(api.requestSnapshot).toHaveBeenCalledTimes(requests + 1);
	});
	it("keeps refreshing while the user interacts without duplicating accepted jobs", async () => {
		monthlyApi();
		await model.initialize();
		for (let i = 0; i < 5; i++) {
			await vi.advanceTimersByTimeAsync(4 * 60000);
			document.dispatchEvent(new Event(i % 2 ? "wheel" : "keydown"));
			await flushPromises();
		}
		expect(api.requestSnapshot.mock.calls.length).toBeGreaterThan(4);
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		const pending = model.refresh({ automatic: true, background: true });
		await flushPromises();
		const count = api.requestSnapshot.mock.calls.length;
		document.dispatchEvent(new Event("click"));
		document.dispatchEvent(new Event("keydown"));
		await flushPromises();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(count);
		const fresh = complete();
		fresh.scope.month = model.month.value;
		fresh.expires_at = new Date(Date.now() + 300000).toISOString();
		late.resolve(ok(fresh));
		await pending;
	});
	it("cancels worker-busy retries when the page is disposed", async () => {
		monthlyApi();
		await model.initialize();
		api.requestSnapshot.mockResolvedValue(refusal("worker_busy"));
		await model.browseMonth(1);
		const count = api.requestSnapshot.mock.calls.length;
		scope.stop();
		await vi.advanceTimersByTimeAsync(20000);
		expect(api.requestSnapshot).toHaveBeenCalledTimes(count);
		expect(model.loadingSnapshot.value).toBeNull();
	});
	it("drops worker-busy retries and old layout when company changes", async () => {
		await model.initialize();
		api.requestSnapshot.mockResolvedValueOnce(refusal("worker_busy"));
		await model.refresh({ background: true });
		await model.chooseCompany("Services");
		const count = api.requestSnapshot.mock.calls.length;
		await vi.advanceTimersByTimeAsync(2000);
		expect(api.requestSnapshot).toHaveBeenCalledTimes(count);
		expect(model.snapshot.value.scope.company).toBe("Services");
		expect(model.loadingSnapshot.value).toBeNull();
	});
	it("removes the loading frame as soon as a busy retry reports revoked access", async () => {
		await model.initialize();
		api.requestSnapshot.mockResolvedValueOnce(refusal("worker_busy"));
		await model.refresh({ background: true });
		api.requestSnapshot.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		api.getCapabilities.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		await vi.advanceTimersByTimeAsync(1000);
		expect(model.phase.value).toBe("forbidden");
		expect(model.snapshot.value).toBeNull();
		expect(model.loadingSnapshot.value).toBeNull();
	});
	it("recovers to the server-current month when an old month leaves the allowed range", async () => {
		monthlyApi();
		await model.initialize();
		await model.selectDate("2025-10-30");
		vi.setSystemTime(new Date("2026-11-01T00:00:00Z"));
		api.requestSnapshot.mockResolvedValueOnce(refusal("invalid_request", "invalid"));
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		expect(api.requestSnapshot.mock.calls.at(-1)[2]).toBe("current");
		expect(model.month.value).toBe("2026-11");
		expect(model.selectedDate.value).toBe("2026-11-01");
		expect(model.phase.value).toBe("ready");
	});
	it("does not loop when current-month fallback still has invalid inputs", async () => {
		monthlyApi();
		await model.initialize();
		const count = api.requestSnapshot.mock.calls.length;
		api.requestSnapshot.mockResolvedValue(refusal("invalid_request", "invalid"));
		await model.refresh({ background: true });
		expect(api.requestSnapshot).toHaveBeenCalledTimes(count + 2);
		expect(model.phase.value).toBe("failed");
	});
	it("loads the latest requested month even if the superseded month's job fails", async () => {
		monthlyApi();
		await model.initialize();
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		const pending = model.browseMonth(1);
		await flushPromises();
		await model.browseMonth(1);
		late.resolve(ok({ state: "failed", code: "job_lost" }));
		await pending;
		expect(model.snapshot.value.scope.month).toBe("2026-12");
		expect(model.phase.value).toBe("ready");
	});
	it("rechecks permissions before recovering from a purged superseded snapshot", async () => {
		monthlyApi();
		await model.initialize();
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		const pending = model.browseMonth(1);
		await flushPromises();
		await model.browseMonth(1);
		const count = api.getCapabilities.mock.calls.length;
		late.resolve(refusal("not_found_or_expired", "expired"));
		await pending;
		expect(api.getCapabilities.mock.calls.length).toBeGreaterThan(count);
		expect(model.snapshot.value.scope.month).toBe("2026-12");
	});
	it("waits and retries worker contention on first open without an immediate error page", async () => {
		monthlyApi();
		api.requestSnapshot.mockResolvedValueOnce(refusal("worker_busy"));
		await model.initialize();
		expect(model.phase.value).toBe("requesting");
		expect(model.waitingForWorker.value).toBe(true);
		await vi.advanceTimersByTimeAsync(1000);
		expect(model.phase.value).toBe("ready");
		expect(model.error.value).toBeNull();
	});
	it("offers explicit recovery after bounded initial-load worker contention", async () => {
		const clock = vi.spyOn(performance, "now").mockImplementation(() => Date.now());
		try {
			api.requestSnapshot.mockResolvedValue(refusal("worker_busy"));
			await model.initialize();
			await vi.advanceTimersByTimeAsync(65000);
			expect(model.phase.value).toBe("failed");
			expect(model.error.value.code).toBe("worker_busy");
			expect(model.loadingSnapshot.value).toBeNull();
		} finally {
			clock.mockRestore();
		}
	});
	it("does not recover a purged superseded snapshot after permissions are revoked", async () => {
		monthlyApi();
		await model.initialize();
		const late = deferred();
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		const pending = model.browseMonth(1);
		await flushPromises();
		await model.browseMonth(1);
		api.getCapabilities.mockResolvedValueOnce(refusal("forbidden", "forbidden"));
		late.resolve(refusal("not_found_or_expired", "expired"));
		await pending;
		expect(model.phase.value).toBe("forbidden");
		expect(model.snapshot.value).toBeNull();
		expect(model.loadingSnapshot.value).toBeNull();
		expect(api.requestSnapshot).toHaveBeenCalledTimes(2);
	});
	it("keeps a newer selection while a server-current month fallback is running", async () => {
		monthlyApi();
		await model.initialize();
		await model.selectDate("2025-10-30");
		vi.setSystemTime(new Date("2026-11-01T00:00:00Z"));
		const late = deferred();
		api.requestSnapshot.mockResolvedValueOnce(refusal("invalid_request", "invalid"));
		api.getSnapshotStatus.mockReturnValueOnce(late.promise);
		window.dispatchEvent(new Event("focus"));
		await flushPromises();
		await model.browseMonth(1);
		late.resolve(await api.getSnapshotStatus("202611".padStart(32, "0")));
		await flushPromises();
		expect(model.month.value).toBe("2025-11");
		expect(model.snapshot.value.scope.month).toBe("2025-11");
	});
});
