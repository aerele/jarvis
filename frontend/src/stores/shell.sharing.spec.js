import { describe, it, expect, vi, beforeEach } from "vitest";
vi.hoisted(() => {
	window.matchMedia = (q) => ({
		matches: false,
		media: q,
		addEventListener() {},
		removeEventListener() {},
	});
});
vi.mock("@/api", () => ({}));
vi.mock("@/api/learning", () => ({ getReviewAccess: vi.fn() }));
vi.mock("frappe-ui", () => ({ toast: { error: vi.fn() } }));
vi.mock("@/onboarding/readiness.js", () => ({ needsOnboarding: () => false }));
const mocks = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock("@/api/chatSharing", () => ({ listSharedWithMe: mocks.list }));
const { useShellStore } = await import("./shell");
const store = useShellStore();
beforeEach(() => vi.clearAllMocks());
describe("separate shared-chat index", () => {
	it("never inserts shared rows into the owned conversation list", async () => {
		store.conversations = [{ name: "owned" }];
		mocks.list.mockResolvedValue([{ name: "shared", title: "Shared", unseen: true }]);
		await store.loadSharedConversations();
		expect(store.sharedConversations.map((x) => x.name)).toEqual(["shared"]);
		expect(store.conversations.map((x) => x.name)).toEqual(["owned"]);
		expect(store.hasUnseenShares).toBe(true);
		store.markSharedSeen("shared");
		expect(store.hasUnseenShares).toBe(false);
	});
	it("does not let a stale in-flight index undo opening a shared chat", async () => {
		let resolve;
		mocks.list.mockImplementation(() => new Promise((r) => (resolve = r)));
		const pending = store.loadSharedConversations();
		store.markSharedSeen("shared");
		resolve([{ name: "shared", unseen: true }]);
		await pending;
		expect(store.sharedConversations[0].unseen).toBe(false);
	});
	it("coalesces repeated index reads and queues one refresh after invalidation", async () => {
		let resolve;
		mocks.list
			.mockImplementationOnce(() => new Promise((r) => (resolve = r)))
			.mockResolvedValueOnce([]);
		const a = store.loadSharedConversations();
		const b = store.loadSharedConversations();
		expect(mocks.list).toHaveBeenCalledTimes(1);
		store.loadSharedConversations({ refresh: true });
		resolve([{ name: "gone", unseen: true }]);
		await Promise.all([a, b]);
		await Promise.resolve();
		await Promise.resolve();
		expect(mocks.list).toHaveBeenCalledTimes(2);
		expect(store.sharedConversations).toEqual([]);
	});
});
