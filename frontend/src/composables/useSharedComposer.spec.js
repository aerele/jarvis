import { ref } from "vue";
import { describe, it, expect, vi, beforeEach } from "vitest";
const api = vi.hoisted(() => ({ sendSharedMessage: vi.fn(), uploadSharedFile: vi.fn() }));
vi.mock("@/api/chatCollaboration", () => api);
import { useSharedComposer } from "./useSharedComposer";
beforeEach(() => {
	vi.clearAllMocks();
	document.cookie = "user_id=a%40example.com; path=/";
});
const setup = () => {
	const id = ref("C1"),
		canChat = ref(true),
		refresh = vi.fn();
	const composer = useSharedComposer({ id, canChat, refresh });
	composer.draft.value = "Current request";
	return { ...composer, id, canChat, refresh };
};
describe("shared composer actor-safe recovery", () => {
	it("keeps a later lost-ack ID while an earlier stopped request is retried", async () => {
		api.sendSharedMessage
			.mockResolvedValueOnce({ ok: true, run_id: "run1" })
			.mockRejectedValueOnce(new Error("offline"))
			.mockResolvedValue({ ok: true, run_id: "run2" });
		const c = setup();
		await c.send();
		c.reconcile([{ run_id: "run1", state: "cancelled", can_retry: true }]);
		c.draft.value = "Later request B";
		await c.send();
		const nonce = api.sendSharedMessage.mock.calls[1][2];
		c.adopt("run1");
		c.reconcile([{ run_id: "run1", state: "streaming" }]);
		await c.send();
		expect(api.sendSharedMessage.mock.calls[2][2]).toBe(nonce);
	});
	it("keeps a stale stopped read from enabling an identical send during retry", async () => {
		api.sendSharedMessage.mockResolvedValue({ ok: true, run_id: "run1" });
		const c = setup();
		await c.send();
		c.reconcile([{ run_id: "run1", state: "cancelled", can_retry: true }]);
		c.adopt("run1");
		c.reconcile([{ run_id: "run1", state: "cancelled", can_retry: true }]);
		await c.send();
		expect(api.sendSharedMessage).toHaveBeenCalledTimes(1);
		c.reconcile([{ run_id: "run1", state: "queued" }]);
		expect(c.canSend.value).toBe(false);
		c.reconcile([{ run_id: "run1", state: "streaming" }]);
		expect(c.draft.value).toBe("");
	});
	it("tracks a retry from another tab and requires an edit for a new request", async () => {
		api.sendSharedMessage.mockResolvedValue({ ok: true, run_id: "run1" });
		const c = setup();
		await c.send();
		c.reconcile([{ run_id: "run1", state: "cancelled", can_retry: true }]);
		await c.send();
		expect(api.sendSharedMessage).toHaveBeenCalledTimes(1);
		c.draft.value = "A different deliberate request";
		expect(c.canSend.value).toBe(true);
		c.reconcile([{ run_id: "run1", state: "queued" }]);
		expect(c.canSend.value).toBe(false);
		c.reconcile([{ run_id: "run1", state: "done" }]);
		expect(c.draft.value).toBe("A different deliberate request");
	});
	it("sends on HTTP sites without secure-context randomUUID", async () => {
		const rng = globalThis.crypto;
		vi.stubGlobal("crypto", { getRandomValues: rng.getRandomValues.bind(rng) });
		api.sendSharedMessage.mockResolvedValue({ ok: true, run_id: "run1" });
		try {
			const c = setup();
			await c.send();
			expect(api.sendSharedMessage.mock.calls[0][2]).toMatch(/^[a-f0-9]{32}$/);
		} finally {
			vi.unstubAllGlobals();
		}
	});
	it("never sends while access is view-only", async () => {
		const c = setup();
		c.canChat.value = false;
		await c.send();
		expect(api.sendSharedMessage).not.toHaveBeenCalled();
		expect(c.draft.value).toBe("Current request");
	});
	it("keeps the draft after a refusal", async () => {
		api.sendSharedMessage.mockResolvedValue({ ok: false, reason: "usage_limit" });
		const c = setup();
		await c.send();
		expect(c.draft.value).toBe("Current request");
		expect(c.error.value).toBeTruthy();
	});
	it("shows the actual refusal reason", async () => {
		api.sendSharedMessage.mockResolvedValue({
			ok: false,
			reason: "The site queue is full. Try shortly.",
		});
		const c = setup();
		await c.send();
		expect(c.error.value).toContain("The site queue is full");
	});
	it("does not describe a definite permission refusal as a lost acknowledgement", async () => {
		api.sendSharedMessage.mockRejectedValue(
			Object.assign(new Error("Permission denied for this chat"), { status: 403 })
		);
		const c = setup();
		await c.send();
		expect(c.error.value).toContain("Permission denied");
		expect(c.error.value).not.toContain("not confirmed");
		expect(c.failed.value).toBe(false);
	});
	it("reattaches a stopped request before retry so Enter cannot duplicate it", async () => {
		api.sendSharedMessage.mockResolvedValue({ ok: true, run_id: "run1" });
		const c = setup();
		await c.send();
		c.reconcile([{ run_id: "run1", state: "cancelled", can_retry: true }]);
		c.adopt("run1");
		await c.send();
		expect(api.sendSharedMessage).toHaveBeenCalledTimes(1);
		c.reconcile([{ run_id: "run1", state: "queued" }]);
		expect(c.canSend.value).toBe(false);
		c.reconcile([{ run_id: "run1", state: "streaming" }]);
		expect(c.draft.value).toBe("");
		await c.send();
		expect(api.sendSharedMessage).toHaveBeenCalledTimes(1);
	});
	it("reuses the same nonce after a lost acknowledgement", async () => {
		api.sendSharedMessage
			.mockRejectedValueOnce(new Error("offline"))
			.mockResolvedValue({ ok: true, run_id: "run1" });
		const c = setup();
		await c.send();
		await c.send();
		expect(api.sendSharedMessage).toHaveBeenCalledTimes(2);
		expect(api.sendSharedMessage.mock.calls[1]).toEqual(api.sendSharedMessage.mock.calls[0]);
		expect(api.sendSharedMessage.mock.calls[0]).toEqual([
			"C1",
			"Current request",
			expect.any(String),
			[],
		]);
	});
	it("keeps a queued draft and clears it only after the same request starts", async () => {
		api.sendSharedMessage.mockResolvedValue({ ok: true, run_id: "run1", queued: true });
		const c = setup();
		await c.send();
		c.reconcile([{ run_id: "run1", state: "queued" }]);
		expect(c.draft.value).toBe("Current request");
		c.reconcile([{ run_id: "run1", state: "streaming" }]);
		expect(c.draft.value).toBe("");
	});
	it("preserves newer text when an older accepted request starts", async () => {
		api.sendSharedMessage.mockResolvedValue({ ok: true, run_id: "run1" });
		const c = setup();
		await c.send();
		c.draft.value = "Newer unsent request";
		c.reconcile([{ run_id: "run1", state: "done" }]);
		expect(c.draft.value).toBe("Newer unsent request");
	});
	it("does not clear another chat's draft when a send finishes after navigation", async () => {
		let finish;
		api.sendSharedMessage.mockImplementation(
			() =>
				new Promise((resolve) => {
					finish = resolve;
				})
		);
		const c = setup();
		const sending = c.send();
		c.id.value = "C2";
		c.draft.value = "C2 draft";
		finish({ ok: true, run_id: "run1" });
		await sending;
		c.reconcile([{ run_id: "run1", state: "streaming" }]);
		expect(c.draft.value).toBe("C2 draft");
		c.id.value = "C1";
		c.reconcile([{ run_id: "run1", state: "streaming" }]);
		expect(c.draft.value).toBe("");
	});
	it("keeps a request that fails before model dispatch", async () => {
		api.sendSharedMessage.mockResolvedValue({ ok: true, run_id: "run1" });
		const c = setup();
		await c.send();
		c.reconcile([{ run_id: "run1", state: "errored", error_headline: "Request too long" }]);
		expect(c.draft.value).toBe("Current request");
		expect(c.error.value).toContain("Request too long");
	});
});
