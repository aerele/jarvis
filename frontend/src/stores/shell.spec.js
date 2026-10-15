import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

// The store is a module singleton that reads matchMedia at import (the 820px
// rail-collapse and 767px phone breakpoints). Stub it BEFORE import so both
// resolve to "wide, not phone" — the branch these tests exercise. vi.hoisted
// runs early enough; the ModelEffortPicker spec uses the same shim.
vi.hoisted(() => {
	const store = new Map();
	globalThis.localStorage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => store.set(k, String(v)),
		removeItem: (k) => store.delete(k),
		clear: () => store.clear(),
	};
	window.matchMedia = (q) => ({
		matches: false,
		media: q,
		onchange: null,
		addEventListener() {},
		removeEventListener() {},
		addListener() {},
		removeListener() {},
		dispatchEvent: () => false,
	});
});

// shell.js pulls in frappe-ui + @/api (a frappe-ui resource tree that does not
// resolve under vitest). None of it is exercised by these sidebar-state tests.
vi.mock("frappe-ui", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));
vi.mock("@/api", () => ({ approvalsBadge: vi.fn(), listConversations: vi.fn() }));
const getReviewAccess = vi.fn();
vi.mock("@/api/learning", () => ({ getReviewAccess }));
vi.mock("@/lib/errors", () => ({ errHtml: (e) => String(e) }));
vi.mock("@/onboarding/readiness.js", () => ({ needsOnboarding: () => false }));

const { useShellStore } = await import("./shell");
const api = await import("@/api");
const store = useShellStore();

describe("shell store: spacious-view auto-collapse (Dashboard Builder)", () => {
	beforeEach(() => {
		store.setSpaciousView(false);
		store.sidebarPref = "open";
	});

	it("collapses the rail while the view is active WITHOUT touching the saved preference", () => {
		expect(store.sidebarCollapsed).toBe(false);
		store.setSpaciousView(true);
		expect(store.sidebarCollapsed).toBe(true);
		// the persisted preference is never written by the request
		expect(store.sidebarPref).toBe("open");
	});

	it("restores the exact saved preference the moment the view is released", () => {
		store.setSpaciousView(true);
		expect(store.sidebarCollapsed).toBe(true);
		store.setSpaciousView(false);
		expect(store.sidebarCollapsed).toBe(false);
		expect(store.sidebarPref).toBe("open");
	});

	it("a saved 'collapsed' preference stays collapsed after the view releases", () => {
		store.sidebarPref = "collapsed";
		store.setSpaciousView(true);
		expect(store.sidebarCollapsed).toBe(true);
		store.setSpaciousView(false);
		expect(store.sidebarCollapsed).toBe(true);
		expect(store.sidebarPref).toBe("collapsed");
	});

	it("a manual peek while active is transient — the next visit re-collapses", () => {
		store.setSpaciousView(true);
		store.sidebarCollapsed = false; // user opens the rail to peek
		expect(store.sidebarCollapsed).toBe(false);
		expect(store.sidebarPref).toBe("open"); // peek does not persist
		store.setSpaciousView(false); // leave
		store.setSpaciousView(true); // come back
		expect(store.sidebarCollapsed).toBe(true); // auto-collapse re-applies
	});
});

describe("shell store: approvals badge", () => {
	beforeEach(() => {
		vi.useFakeTimers();
		api.approvalsBadge.mockReset();
	});
	afterEach(() => {
		store.stopBadgeTimer();
		vi.useRealTimers();
	});

	it("refreshes again the moment the next chat card badges", async () => {
		api.approvalsBadge
			.mockResolvedValueOnce({ count: 2, next_in: 30 })
			.mockResolvedValueOnce({ count: 3, next_in: null });
		await store.refreshApprovalsCount();
		expect(store.approvalsCount).toBe(2);
		await vi.advanceTimersByTimeAsync(29 * 1000);
		expect(api.approvalsBadge).toHaveBeenCalledTimes(1);
		await vi.advanceTimersByTimeAsync(2 * 1000);
		expect(api.approvalsBadge).toHaveBeenCalledTimes(2);
		expect(store.approvalsCount).toBe(3);
		await vi.advanceTimersByTimeAsync(60 * 60 * 1000);
		expect(api.approvalsBadge).toHaveBeenCalledTimes(2);
	});

	it("re-reads once when a refresh lands while one is in flight", async () => {
		let release;
		api.approvalsBadge
			.mockImplementationOnce(() => new Promise((r) => (release = r)))
			.mockResolvedValueOnce({ count: 0, next_in: null });
		const first = store.refreshApprovalsCount();
		store.refreshApprovalsCount(); // e.g. a board decision during a poll
		release({ count: 1, next_in: null }); // read before the decision landed
		await first;
		expect(api.approvalsBadge).toHaveBeenCalledTimes(2);
		expect(store.approvalsCount).toBe(0);
	});

	it("stops the pending refresh when the shell goes away", async () => {
		api.approvalsBadge.mockResolvedValue({ count: 1, next_in: 5 });
		await store.refreshApprovalsCount();
		store.stopBadgeTimer();
		await vi.advanceTimersByTimeAsync(60 * 1000);
		expect(api.approvalsBadge).toHaveBeenCalledTimes(1);
	});
});

describe("shell store: reviewer badge (sidebar Skills)", () => {
	beforeEach(() => {
		getReviewAccess.mockReset();
		store.reviewCount = 0;
	});

	it("never calls the reviewer-guarded probe for a non-reviewer", async () => {
		window.is_skill_reviewer = false;
		await store.refreshReviewCount();
		expect(getReviewAccess).not.toHaveBeenCalled();
		expect(store.reviewCount).toBe(0);
	});

	it("sums every pending review queue for a reviewer", async () => {
		window.is_skill_reviewer = true;
		getReviewAccess.mockResolvedValue({
			pending_patterns: 2,
			pending_promotions: 1,
			pending_skill_promotions: 3,
		});
		await store.refreshReviewCount();
		expect(store.reviewCount).toBe(6);
		delete window.is_skill_reviewer;
	});

	it("re-reads once when a refresh lands while one is in flight", async () => {
		window.is_skill_reviewer = true;
		let release;
		getReviewAccess
			.mockImplementationOnce(() => new Promise((r) => (release = r)))
			.mockResolvedValueOnce({ pending_skill_promotions: 2 });
		const first = store.refreshReviewCount();
		store.refreshReviewCount(); // e.g. review:pending during a route refresh
		release({ pending_skill_promotions: 1 });
		await first;
		expect(getReviewAccess).toHaveBeenCalledTimes(2);
		expect(store.reviewCount).toBe(2);
		delete window.is_skill_reviewer;
	});

	it("keeps the last count when the probe fails", async () => {
		window.is_skill_reviewer = true;
		store.reviewCount = 4;
		getReviewAccess.mockRejectedValue(new Error("boom"));
		await store.refreshReviewCount();
		expect(store.reviewCount).toBe(4);
		delete window.is_skill_reviewer;
	});
});

describe("shell store: server activity (#591)", () => {
	const rows = (activity = [{ run_id: "run-b", message_id: "msg-b", state: "streaming" }]) => [
		{ name: "conv-b", title: "Chat B", activity },
	];
	beforeEach(() => {
		vi.useFakeTimers();
		store.stopConversationActivity();
		store.startConversationActivity();
		store.currentConvId = null;
		store.activeConversationActivity = null;
		api.listConversations.mockReset().mockResolvedValue(rows());
	});
	afterEach(() => {
		store.stopConversationActivity();
		vi.useRealTimers();
	});

	it("seeds background activity from a full list load", async () => {
		await store.loadConversations();
		expect(store.conversationIndicator("conv-b")?.label).toBe("Replying");
		expect(api.listConversations).toHaveBeenCalledWith({ refresh: false });
	});
	it("reconciles without prewarm, badge refresh or full row replacement", async () => {
		await store.loadConversations();
		const conversations = store.conversations;
		await store.refreshConversationActivity();
		expect(api.listConversations).toHaveBeenLastCalledWith({ refresh: true });
		expect(store.conversations).toBe(conversations);
	});
	it("keeps a silent tool and recovery visible beyond three minutes", async () => {
		await store.refreshConversationActivity();
		await vi.advanceTimersByTimeAsync(4 * 60 * 1000);
		expect(store.conversationIndicator("conv-b")?.label).toBe("Replying");
		api.listConversations.mockResolvedValue(rows([{ run_id: "run-b", state: "recovering" }]));
		await store.refreshConversationActivity();
		expect(store.conversationIndicator("conv-b")?.label).toBe("Recovering");
	});
	it("retains last known truth on a failed refresh", async () => {
		await store.refreshConversationActivity();
		api.listConversations.mockRejectedValue(new Error("offline"));
		await store.refreshConversationActivity();
		expect(store.conversationIndicator("conv-b")?.label).toBe("Replying");
	});
	it("coalesces event bursts and follows an invalidation during a request", async () => {
		let release;
		api.listConversations.mockImplementationOnce(
			() =>
				new Promise((r) => {
					release = r;
				})
		);
		const first = store.refreshConversationActivity();
		for (let i = 0; i < 20; i++) store.scheduleConversationActivityRefresh();
		release(rows());
		await first;
		await vi.advanceTimersByTimeAsync(1000);
		expect(api.listConversations).toHaveBeenCalledTimes(2);
	});
	it("serializes a full list read and a later refresh so older data cannot win", async () => {
		let release;
		api.listConversations
			.mockImplementationOnce(
				() =>
					new Promise((r) => {
						release = r;
					})
			)
			.mockResolvedValueOnce(rows([]));
		const first = store.loadConversations();
		store.refreshConversationActivity();
		expect(api.listConversations).toHaveBeenCalledTimes(1);
		release(rows());
		await first;
		await vi.advanceTimersByTimeAsync(1000);
		expect(api.listConversations).toHaveBeenCalledTimes(2);
		expect(store.conversationIndicator("conv-b")).toBeNull();
	});
	it("does not swallow a full list reload behind a background refresh", async () => {
		let release;
		api.listConversations
			.mockImplementationOnce(
				() =>
					new Promise((r) => {
						release = r;
					})
			)
			.mockResolvedValueOnce(rows([]));
		const refresh = store.refreshConversationActivity();
		const full = store.loadConversations();
		release(rows());
		await refresh;
		await full;
		expect(api.listConversations).toHaveBeenCalledTimes(2);
		expect(api.listConversations).toHaveBeenLastCalledWith({ refresh: false });
		expect(store.conversations[0].activity).toEqual([]);
	});
	it("updates only a changed key and preserves equal activity references", async () => {
		await store.refreshConversationActivity();
		const original = store.conversationActivity.get("conv-b");
		api.listConversations.mockResolvedValue([
			...rows(),
			{ name: "conv-c", activity: [{ run_id: "run-c", state: "streaming" }] },
		]);
		await store.refreshConversationActivity();
		expect(store.conversationActivity.get("conv-b")).toBe(original);
	});
	it("suppresses Stop immediately, including a stale pre-stop read", async () => {
		await store.refreshConversationActivity();
		let release;
		api.listConversations.mockImplementationOnce(
			() =>
				new Promise((r) => {
					release = r;
				})
		);
		const pending = store.refreshConversationActivity();
		store.stopConversationRun("conv-b", "run-b", "msg-b");
		expect(store.conversationIndicator("conv-b")).toBeNull();
		release(rows([]));
		await pending;
		api.listConversations.mockResolvedValue(rows());
		await store.refreshConversationActivity();
		expect(store.conversationIndicator("conv-b")).toBeNull();
	});
	it("does not let stopping/cancelling one run hide another run", async () => {
		api.listConversations.mockResolvedValue(
			rows([
				{ run_id: "run-b", state: "streaming" },
				{ run_id: "queued-b", state: "queued" },
			])
		);
		await store.refreshConversationActivity();
		store.stopConversationRun("conv-b", "queued-b");
		expect(store.conversationIndicator("conv-b")?.label).toBe("Replying");
		store.stopConversationRun("conv-b", "run-b");
		api.listConversations.mockResolvedValue(rows([{ run_id: "new-run", state: "streaming" }]));
		await store.refreshConversationActivity();
		expect(store.conversationIndicator("conv-b")?.label).toBe("Replying");
	});
	it("suppresses pure legacy work by message identity", async () => {
		api.listConversations.mockResolvedValue(
			rows([{ run_id: null, message_id: "legacy-msg", state: "streaming" }])
		);
		await store.refreshConversationActivity();
		store.stopConversationRun("conv-b", "legacy-transport-id", "legacy-msg");
		expect(store.conversationIndicator("conv-b")).toBeNull();
	});
	it("ignores an in-flight response after detach and clears snapshots", async () => {
		await store.refreshConversationActivity();
		let release;
		api.listConversations.mockImplementationOnce(
			() =>
				new Promise((r) => {
					release = r;
				})
		);
		const pending = store.refreshConversationActivity();
		store.stopConversationActivity();
		release(rows());
		await pending;
		expect(store.conversationActivity.size).toBe(0);
		expect(store.conversationIndicator("conv-b")).toBeNull();
	});
	it("keeps open-chat idle/recovering authoritative over server running", async () => {
		await store.refreshConversationActivity();
		store.currentConvId = "conv-b";
		store.activeConversationActivity = { conversation_id: "conv-b", state: null };
		expect(store.conversationIndicator("conv-b")).toBeNull();
		store.activeConversationActivity = { conversation_id: "conv-b", state: "recovering" };
		expect(store.conversationIndicator("conv-b")?.label).toBe("Recovering");
	});
});
