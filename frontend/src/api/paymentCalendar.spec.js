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
});
