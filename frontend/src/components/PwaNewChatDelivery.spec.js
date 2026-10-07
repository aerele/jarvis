import { beforeEach, afterEach, it, expect, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { ref } from "vue";

const router = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("vue-router", () => ({ useRouter: () => router }));
vi.mock("../../../pwa/src/maintenanceGate", () => ({ holdActive: ref(false) }));
vi.mock("@/branding", () => ({ agentName: ref("Jarvis") }));
vi.mock("../../../pwa/src/api", () => ({
	getChatUiSettings: vi.fn(async () => ({ llm_model: "model-a" })),
	getPromptSuggestions: vi.fn(async () => ({})),
	getMySettings: vi.fn(async () => ({})),
	sendRecoverableMessage: vi.fn(),
}));
vi.mock("../../../pwa/src/lib/notifications", () => ({ feed: ref([]) }));
import * as api from "../../../pwa/src/api";
import NewChat from "../../../pwa/src/views/NewChatView.vue";
import { holdActive } from "../../../pwa/src/maintenanceGate";
import { recoveryState } from "../../../pwa/src/sendRecoveryStore";
let wrapper;
beforeEach(() => {
	vi.clearAllMocks();
	recoveryState.requests = [];
	recoveryState.heroDraft = null;
	recoveryState.editingNewRequest = null;
	recoveryState.newChatPicks = {};
	holdActive.value = false;
});
afterEach(() => {
	wrapper?.unmount();
	vi.useRealTimers();
});
async function open() {
	wrapper = mount(NewChat, {
		global: {
			stubs: { BrandMark: true, Sheet: true, AutoModeToggle: true, VoiceSheet: true },
		},
	});
	await flushPromises();
	await wrapper.get("textarea").setValue("Review invoice");
}
it("preserves an uncertain new-chat request and routes to the existing recovery screen", async () => {
	api.sendRecoverableMessage.mockRejectedValue(new Error("lost response"));
	await open();
	await wrapper.get('[aria-label="Send"]').trigger("click");
	await flushPromises();
	expect(router.push).toHaveBeenCalledWith("/send-recovery");
	expect(recoveryState.requests[0]).toMatchObject({
		text: "Review invoice",
		conversation: "",
		state: "uncertain",
	});
	expect(recoveryState.requests[0].id).toMatch(/^[a-f0-9]{32}$/);
	await wrapper.get("textarea").setValue("Another click");
	await wrapper.get('[aria-label="Send"]').trigger("click");
	expect(api.sendRecoverableMessage).toHaveBeenCalledTimes(1);
	expect(recoveryState.requests).toHaveLength(1);
});
it("keeps accepted new-chat evidence for the recovery view to adopt without resending", async () => {
	api.sendRecoverableMessage.mockResolvedValue({
		delivery: "settled",
		result: { ok: true, conversation_id: "A", message_id: "M", run_id: "R" },
	});
	await open();
	await wrapper.get('[aria-label="Send"]').trigger("click");
	await flushPromises();
	expect(recoveryState.requests[0]).toMatchObject({
		state: "accepted",
		result: { conversation_id: "A" },
	});
	expect(api.sendRecoverableMessage).toHaveBeenCalledTimes(1);
});

it("keeps the existing Menu control working", async () => {
	await open();
	await wrapper.get('[aria-label="Menu"]').trigger("click");
	const { store } = await import("../../../pwa/src/store");
	expect(store.drawerOpen).toBe(true);
	store.drawerOpen = false;
});

it("preserves newer hero text and files when returning to recovery", async () => {
	recoveryState.requests = [
		{
			id: "a".repeat(32),
			conversation: "",
			state: "uncertain",
			text: "original",
			attachments: [],
		},
	];
	recoveryState.heroDraft = {
		text: "draft",
		attachments: [{ name: "new.pdf", file_url: "/private/files/new.pdf" }],
		model: "chosen",
		thinking: "high",
		autoMode: true,
	};
	await open();
	await wrapper.get("textarea").setValue("Newer text");
	await wrapper.get('[aria-label="Send"]').trigger("click");
	await flushPromises();
	expect(recoveryState.heroDraft).toMatchObject({
		text: "Newer text",
		model: "chosen",
		autoMode: true,
		attachments: [{ name: "new.pdf" }],
	});
	expect(api.sendRecoverableMessage).not.toHaveBeenCalled();
});
it("a rejected hero request can change model without losing its files or a newer draft", async () => {
	const request = {
		id: "b".repeat(32),
		conversation: "",
		state: "rejected",
		text: "original",
		attachments: [{ name: "invoice.pdf", file_url: "/private/files/invoice.pdf" }],
		approvalTokens: [],
		model: "removed-model",
		thinking: "high",
		autoMode: true,
	};
	recoveryState.requests = [request];
	recoveryState.editingNewRequest = request.id;
	recoveryState.heroDraft = { text: "newer draft", attachments: [] };
	api.getChatUiSettings.mockResolvedValue({
		llm_model: "model-a",
		pool_models: [{ model: "working-model" }],
		thinking_levels: ["high"],
	});
	api.sendRecoverableMessage.mockResolvedValue({
		delivery: "settled",
		result: { ok: false, reason: "busy" },
	});
	await open();
	// Use the actual model chooser (Sheet is stubbed, so trigger through the exposed setup binding).
	wrapper.vm.selectedModel = "working-model";
	await wrapper.get('[aria-label="Send"]').trigger("click");
	await flushPromises();
	expect(api.sendRecoverableMessage).toHaveBeenCalledWith(
		expect.objectContaining({
			model: "working-model",
			thinking: "high",
			autoMode: true,
			attachments: [{ name: "invoice.pdf", file_url: "/private/files/invoice.pdf" }],
		})
	);
	wrapper.unmount();
	expect(recoveryState.requests[0].text).toBe("Review invoice");
	expect(recoveryState.requests[0].attachments).toHaveLength(1);
	expect(recoveryState.requests[0].id).not.toBe("b".repeat(32));
	expect(recoveryState.heroDraft.text).toBe("newer draft");
});
it("a blocked dictated send remains in the hero draft", async () => {
	await open();
	holdActive.value = true;
	await wrapper.vm.send("Spoken addition");
	expect(wrapper.get("textarea").element.value).toContain("Spoken addition");
	expect(recoveryState.heroDraft.text).toContain("Spoken addition");
	expect(api.sendRecoverableMessage).not.toHaveBeenCalled();
});
it("RNG failure does not clear hero input or lock sending", async () => {
	await open();
	const rng = vi.spyOn(globalThis.crypto, "getRandomValues").mockImplementation(() => {
		throw Error("rng");
	});
	try {
		await wrapper.get('[aria-label="Send"]').trigger("click");
		await flushPromises();
		expect(wrapper.get("textarea").element.value).toBe("Review invoice");
		expect(recoveryState.requests).toHaveLength(0);
		expect(wrapper.vm.busy).toBe(false);
	} finally {
		rng.mockRestore();
	}
});
it("observes a late hero success after the recovery deadline", async () => {
	await open();
	vi.useFakeTimers();
	let finish;
	api.sendRecoverableMessage.mockImplementation(
		() =>
			new Promise((resolve) => {
				finish = resolve;
			})
	);
	await wrapper.get('[aria-label="Send"]').trigger("click");
	await vi.advanceTimersByTimeAsync(30001);
	expect(recoveryState.requests[0].state).toBe("uncertain");
	finish({
		delivery: "settled",
		result: { ok: true, conversation_id: "A", message_id: "M", run_id: "R" },
	});
	await flushPromises();
	expect(recoveryState.requests[0].state).toBe("accepted");
	vi.useRealTimers();
});
