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
import { recoveryState } from "../../../pwa/src/sendRecoveryStore";
let wrapper;
beforeEach(() => {
	vi.clearAllMocks();
	recoveryState.requests = [];
});
afterEach(() => wrapper?.unmount());
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
