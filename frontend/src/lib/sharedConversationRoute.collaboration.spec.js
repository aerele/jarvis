import { describe, it, expect, vi } from "vitest";
import { sharedConversationRoute, collaborationNoticeRoute } from "./sharedConversationRoute";
describe("collaborative owner route recovery", () => {
	it("routes only this chat's inert collaboration notice, ignoring raw content", () => {
		expect(
			collaborationNoticeRoute(
				{ kind: "collaboration:changed", conversation_id: "C/1", content: "RAW_SECRET" },
				"C/1"
			)
		).toBe("/shared/C%2F1");
		expect(
			collaborationNoticeRoute(
				{ kind: "collaboration:changed", conversation_id: "C2" },
				"C1"
			)
		).toBeNull();
		expect(
			collaborationNoticeRoute({ kind: "message:delta", conversation_id: "C1" }, "C1")
		).toBeNull();
	});
	it("routes an owner through the common projected reader only after activation", async () => {
		const common = vi.fn(async () => ({ conversation: { activated: true } }));
		expect(
			await sharedConversationRoute(
				"C/1",
				async () => false,
				() => true,
				common
			)
		).toBe("/shared/C%2F1");
		expect(common).toHaveBeenCalledWith("C/1");
	});
	it("does not route a private chat or a failed common read", async () => {
		expect(
			await sharedConversationRoute(
				"C1",
				async () => false,
				() => true,
				async () => ({ conversation: { activated: false } })
			)
		).toBeNull();
		expect(
			await sharedConversationRoute(
				"C1",
				async () => false,
				() => true,
				async () => {
					throw new Error("denied");
				}
			)
		).toBeNull();
	});
	it("discards an activation result after navigation", async () => {
		let finish,
			current = true;
		const task = sharedConversationRoute(
			"C1",
			async () => false,
			() => current,
			() =>
				new Promise((resolve) => {
					finish = resolve;
				})
		);
		await Promise.resolve();
		current = false;
		finish({ conversation: { activated: true } });
		expect(await task).toBeNull();
	});
});
