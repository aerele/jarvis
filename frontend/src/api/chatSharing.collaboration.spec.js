import { it, expect, vi } from "vitest";
import { listAllShares } from "./chatSharing";
it("opts admin lists into mode metadata without changing paging or search", async () => {
	const fetcher = vi.fn(async () => ({
		ok: true,
		json: async () => ({ message: { rows: [], total: 0 } }),
	}));
	vi.stubGlobal("fetch", fetcher);
	try {
		await listAllShares({ start: 25, page_length: 25, search: "Purchasing" });
		expect(fetcher.mock.calls[0][0]).toBe("/api/method/jarvis.chat.sharing.list_all_shares");
		expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({
			start: 25,
			page_length: 25,
			search: "Purchasing",
			include_modes: 1,
		});
	} finally {
		vi.unstubAllGlobals();
	}
});
