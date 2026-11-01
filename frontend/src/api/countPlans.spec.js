import { beforeEach, describe, expect, it, vi } from "vitest";

const call = vi.hoisted(() => vi.fn());
vi.mock("frappe-ui", () => ({ call }));

import { getCountPlan, listCountPlans, recordCounts } from "./countPlans";

beforeEach(() => call.mockReset());

describe("count-plan API client", () => {
	it("uses the human count endpoints and preserves a typed zero observation", () => {
		listCountPlans({ start: 20, pageLength: 10 });
		getCountPlan("AR-COUNT-1", "ccp-key-1234567890");
		recordCounts(
			"AR-COUNT-1",
			"2026-11-01T10:30",
			[{ item_code: "ITEM-ONE", counted_qty: "0" }],
			"ccp-key-1234567890"
		);

		expect(call.mock.calls).toEqual([
			["jarvis.chat.cycle_count_entry.list_count_plans", { start: 20, page_length: 10 }],
			[
				"jarvis.chat.cycle_count_entry.get_count_plan",
				{ request: "AR-COUNT-1", entry_key: "ccp-key-1234567890" },
			],
			[
				"jarvis.chat.cycle_count_entry.record_counts",
				{
					request: "AR-COUNT-1",
					counted_at: "2026-11-01T10:30",
					entries: '[{"item_code":"ITEM-ONE","counted_qty":"0"}]',
					entry_key: "ccp-key-1234567890",
				},
			],
		]);
	});
});
