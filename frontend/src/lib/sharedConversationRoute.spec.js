import { describe, it, expect, vi } from "vitest";
import { sharedConversationRoute } from "./sharedConversationRoute";
describe("foreign chat share probe", () => {
	it("redirects a recipient using only the read probe", async () => {
		const probe = vi.fn(async () => true);
		expect(await sharedConversationRoute("C/1", probe, () => true)).toBe("/shared/C%2F1");
		expect(probe).toHaveBeenCalledWith("C/1");
	});
	it("keeps the unavailable recovery for an outsider or failed probe", async () => {
		expect(
			await sharedConversationRoute(
				"C1",
				async () => false,
				() => true
			)
		).toBeNull();
		expect(
			await sharedConversationRoute(
				"C1",
				async () => {
					throw new Error("offline");
				},
				() => true
			)
		).toBeNull();
	});
	it("ignores a recipient result after navigating away", async () => {
		let resolve;
		let active = true;
		const task = sharedConversationRoute(
			"C1",
			() => new Promise((r) => (resolve = r)),
			() => active
		);
		active = false;
		resolve(true);
		expect(await task).toBeNull();
	});
});
