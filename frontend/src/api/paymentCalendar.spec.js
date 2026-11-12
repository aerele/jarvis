import { vi, afterEach } from "vitest";
const { call } = vi.hoisted(() => ({ call: vi.fn() }));
vi.mock("frappe-ui", () => ({ call }));
import * as api from "./paymentCalendar";

describe("Payment Calendar transport", () => {
	afterEach(() => call.mockReset());
	it("keeps expected refusals as ordinary envelopes", async () => {
		const refusal = { ok: false, state: "unavailable", code: "worker_busy", retryable: true };
		call.mockResolvedValue(refusal);
		expect(await api.requestSnapshot("Manufacturing", ["Bank"])).toEqual(refusal);
		expect(call).toHaveBeenCalledWith("jarvis.payment_calendar.api.request_snapshot", {
			company: "Manufacturing",
			accounts: ["Bank"],
		});
	});
	it("forwards drilldown identity and pagination without supplying an actor", async () => {
		call.mockResolvedValue({ ok: true, data: {} });
		await api.getDayDetail("opaque", "2026-10-24", "Payable", 2, 3);
		expect(call).toHaveBeenCalledWith("jarvis.payment_calendar.api.get_day_detail", {
			snapshot_id: "opaque",
			date: "2026-10-24",
			side: "Payable",
			page: 2,
			party_page: 3,
			page_size: 25,
			parties: false,
		});
	});
	it("contains network failure text instead of showing it to the user", async () => {
		call.mockRejectedValue(new Error("PRIVATE_SUPPLIER_123"));
		const result = await api.getCapabilities();
		expect(result).toEqual({
			ok: false,
			state: "failed",
			code: "network_error",
			retryable: true,
		});
		expect(JSON.stringify(result)).not.toContain("PRIVATE_SUPPLIER_123");
	});
	it("treats an expired login as forbidden rather than a transport failure", async () => {
		call.mockRejectedValue({ status: 401 });
		expect((await api.getCapabilities()).state).toBe("forbidden");
	});
	it("forwards only the selected month in addition to company and account IDs", async () => {
		call.mockResolvedValue({ ok: true, data: {} });
		await api.requestSnapshot("Manufacturing", ["Bank"], "2026-11");
		expect(call).toHaveBeenCalledWith("jarvis.payment_calendar.api.request_snapshot", {
			company: "Manufacturing",
			accounts: ["Bank"],
			month: "2026-11",
		});
	});
	it("omits sort and order from detail calls when none is given", async () => {
		call.mockResolvedValue({ ok: true, data: {} });
		await api.getDayDetail("opaque", "2026-10-24", "Payable");
		await api.getBucketDetail("opaque", "window", "Payable");
		for (const [, args] of call.mock.calls) {
			expect(args).not.toHaveProperty("sort");
			expect(args).not.toHaveProperty("order");
		}
	});
	it("treats a null sorting argument as no sorting", async () => {
		call.mockResolvedValue({ ok: true, data: {} });
		await api.getDayDetail("opaque", "2026-10-24", "Payable", 1, 1, null);
		await api.getBucketDetail("opaque", "window", "Payable", 1, 1, null);
		for (const [, args] of call.mock.calls) {
			expect(args).not.toHaveProperty("sort");
			expect(args).not.toHaveProperty("order");
		}
	});
	it("forwards a whitelisted sort and order on day detail", async () => {
		call.mockResolvedValue({ ok: true, data: {} });
		await api.getDayDetail("opaque", "2026-10-24", "Payable", 1, 1, {
			sort: "party",
			order: "asc",
		});
		expect(call).toHaveBeenCalledWith("jarvis.payment_calendar.api.get_day_detail", {
			snapshot_id: "opaque",
			date: "2026-10-24",
			side: "Payable",
			page: 1,
			party_page: 1,
			page_size: 25,
			parties: false,
			sort: "party",
			order: "asc",
		});
	});
	it("forwards a whitelisted sort and order on bucket detail", async () => {
		call.mockResolvedValue({ ok: true, data: {} });
		await api.getBucketDetail("opaque", "now", "Payable", 2, 1, {
			sort: "due_date",
			order: "desc",
		});
		expect(call).toHaveBeenCalledWith("jarvis.payment_calendar.api.get_bucket_detail", {
			snapshot_id: "opaque",
			bucket: "now",
			side: "Payable",
			page: 2,
			party_page: 1,
			page_size: 25,
			parties: false,
			sort: "due_date",
			order: "desc",
		});
	});
	it("sends the requested page size on day and bucket detail", async () => {
		call.mockResolvedValue({ ok: true, data: {} });
		await api.getDayDetail("opaque", "2026-10-24", "Payable", 1, 1, undefined, 5);
		await api.getBucketDetail("opaque", "now", "Payable", 1, 1, undefined, 5);
		expect(call.mock.calls.slice(-2).map(([, args]) => args.page_size)).toEqual([5, 5]);
	});
	it("drops a sort or order outside the whitelist", async () => {
		call.mockResolvedValue({ ok: true, data: {} });
		await api.getDayDetail("opaque", "2026-10-24", "Payable", 1, 1, {
			sort: "id; drop",
			order: "sideways",
		});
		await api.getBucketDetail("opaque", "window", "Payable", 1, 1, {
			sort: "remaining",
			order: "up",
		});
		const [[, day], [, bucket]] = call.mock.calls;
		expect(day).not.toHaveProperty("sort");
		expect(day).not.toHaveProperty("order");
		expect(bucket.sort).toBe("remaining");
		expect(bucket).not.toHaveProperty("order");
	});
	it.each(["today", "immediate", "now", "window"])("accepts the %s bucket", async (bucket) => {
		call.mockResolvedValue({ ok: true, data: {} });
		await api.getBucketDetail("opaque", bucket, "Payable");
		expect(call.mock.calls[0][1].bucket).toBe(bucket);
	});
	it("requests all-account balances for a company", async () => {
		call.mockResolvedValue({ ok: true, data: { snapshot_id: "job1" } });
		await api.requestAccountBalances("Manufacturing");
		expect(call).toHaveBeenCalledWith("jarvis.payment_calendar.api.request_account_balances", {
			company: "Manufacturing",
		});
	});
	it("reads the all-account balances job status", async () => {
		call.mockResolvedValue({ ok: true, data: {} });
		await api.getAccountBalancesStatus("job1");
		expect(call).toHaveBeenCalledWith(
			"jarvis.payment_calendar.api.get_account_balances_status",
			{ job_id: "job1" }
		);
	});
	it("keeps balances refusals as ordinary envelopes and contains network failure", async () => {
		const refusal = { ok: false, state: "unavailable", code: "worker_busy", retryable: true };
		call.mockResolvedValueOnce(refusal);
		expect(await api.requestAccountBalances("Manufacturing")).toEqual(refusal);
		call.mockRejectedValueOnce({ status: 403 });
		expect((await api.getAccountBalancesStatus("job1")).state).toBe("forbidden");
	});
});
