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
});
