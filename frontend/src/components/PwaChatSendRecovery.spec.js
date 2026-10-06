import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { ref } from "vue";
vi.mock("vue-router", () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) }));
vi.mock("../../../pwa/src/maintenanceGate", () => ({
	holdActive: ref(false),
	raiseHold: vi.fn(),
	clearHold: vi.fn(),
	recheck: vi.fn(),
}));
vi.mock("../../../pwa/src/api", () => ({
	getConversation: vi.fn(),
	getChatUiSettings: vi.fn(),
	listPendingConfirmations: vi.fn(),
	sendRecoverableMessage: vi.fn(),
	checkDelivery: vi.fn(),
	uploadFile: vi.fn(),
	listConversations: vi.fn(),
}));
vi.mock("../../../pwa/src/components/BrandMark.vue", () => ({ default: { template: "<div />" } }));
vi.mock("../../../pwa/src/components/ActionCard.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/ChartCard.vue", () => ({ default: { template: "<div />" } }));
vi.mock("../../../pwa/src/components/DecisionCard.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/DecisionSheet.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/FilePreviewSheet.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/MessageMedia.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/RecordCards.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/Sheet.vue", () => ({ default: { template: "<div />" } }));
vi.mock("../../../pwa/src/components/SkillChips.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/ThinkingIndicator.vue", () => ({
	default: { template: "<div />" },
}));
vi.mock("../../../pwa/src/components/VersionPill.vue", () => ({
	default: { template: "<div />" },
}));
import ChatView from "../../../pwa/src/views/ChatView.vue";
import Composer from "../../../pwa/src/components/Composer.vue";
import SendRecoveryCard from "../../../pwa/src/components/SendRecoveryCard.vue";
import * as api from "../../../pwa/src/api";
import { recoveryState } from "../../../pwa/src/sendRecoveryStore";
import { store } from "../../../pwa/src/store";
let wrapper;
const rejected = { delivery: "settled", result: { ok: false, reason: "site busy" } };
const file = { name: "invoice.pdf", file_url: "/private/files/invoice.pdf", key: "file" };
const defer = () => {
	let resolve;
	const promise = new Promise((r) => {
		resolve = r;
	});
	return { promise, resolve };
};
async function open(id = "A") {
	wrapper = mount(ChatView, {
		props: { id },
		global: { provide: { $socket: { on: vi.fn(), off: vi.fn() } } },
	});
	await flushPromises();
}
async function send(text = "Review invoice") {
	const c = wrapper.findComponent(Composer);
	c.vm.$emit("update:modelValue", text);
	await flushPromises();
	c.vm.$emit("send");
	await flushPromises();
}
function button(label) {
	return wrapper
		.findComponent(SendRecoveryCard)
		.findAll("button")
		.find((b) => b.text() === label);
}
beforeEach(() => {
	vi.resetAllMocks();
	recoveryState.requests = [];
	recoveryState.drafts = {};
	store.loaded = true;
	store.conversations = [];
	api.getConversation.mockImplementation(async (id) => ({
		conversation: { name: id, title: `Chat ${id}` },
		messages: [],
	}));
	api.getChatUiSettings.mockResolvedValue({});
	api.listPendingConfirmations.mockResolvedValue({ ok: true, data: { pending: [] } });
	api.listConversations.mockResolvedValue([]);
	api.sendRecoverableMessage.mockResolvedValue(rejected);
	HTMLDialogElement.prototype.showModal = function () {
		this.setAttribute("open", "");
	};
	HTMLDialogElement.prototype.close = function () {
		this.removeAttribute("open");
	};
});
afterEach(() => {
	wrapper?.unmount();
	vi.useRealTimers();
});
describe("actual PWA chat recovery integration", () => {
	it("keeps rejected text and files through resync and route remount", async () => {
		recoveryState.drafts.A = { text: "", attachments: [{ ...file }] };
		await open();
		await send();
		window.dispatchEvent(new Event("jv:resync"));
		await flushPromises();
		expect(wrapper.findComponent(SendRecoveryCard).text()).toContain("invoice.pdf");
		wrapper.unmount();
		await open("B");
		expect(wrapper.findComponent(SendRecoveryCard).exists()).toBe(false);
		await wrapper.setProps({ id: "A" });
		await flushPromises();
		expect(wrapper.findComponent(SendRecoveryCard).text()).toContain("Review invoice");
	});
	it("retries attachment-only requests with the same files", async () => {
		recoveryState.drafts.A = { text: "", attachments: [{ ...file }] };
		await open();
		await send("");
		const before = recoveryState.requests[0].id;
		await button("Retry").trigger("click");
		await flushPromises();
		expect(api.sendRecoverableMessage).toHaveBeenCalledTimes(2);
		expect(recoveryState.requests[0].id).not.toBe(before);
		expect(recoveryState.requests[0].attachments).toEqual([
			{ name: file.name, file_url: file.file_url },
		]);
	});
	it("edits the failed request without replacing a newer composer draft", async () => {
		await open();
		await send();
		wrapper.findComponent(Composer).vm.$emit("update:modelValue", "New question");
		await flushPromises();
		await button("Edit").trigger("click");
		await flushPromises();
		const card = wrapper.findComponent(SendRecoveryCard);
		await card.get("textarea").setValue("Revised request");
		await card.get("form").trigger("submit");
		await flushPromises();
		expect(recoveryState.requests[0].text).toBe("Revised request");
		expect(wrapper.findComponent(Composer).props("modelValue")).toBe("New question");
	});
	it("checks a lost acknowledgement without sending twice", async () => {
		api.sendRecoverableMessage.mockRejectedValue(new Error("lost"));
		await open();
		await send();
		expect(recoveryState.requests[0].state).toBe("uncertain");
		api.checkDelivery.mockResolvedValue({
			delivery: "settled",
			result: { ok: true, conversation_id: "A", message_id: "M", run_id: "R" },
		});
		api.getConversation.mockResolvedValue({
			conversation: { name: "A" },
			messages: [{ name: "M", role: "user", content: "Review invoice" }],
		});
		await button("Check delivery").trigger("click");
		await flushPromises();
		expect(api.sendRecoverableMessage).toHaveBeenCalledTimes(1);
		expect(recoveryState.requests).toHaveLength(0);
		expect(wrapper.text()).toContain("Review invoice");
	});
	it("late send completion stays in A after switching to B", async () => {
		const pending = defer();
		api.sendRecoverableMessage.mockReturnValue(pending.promise);
		await open();
		await send();
		await wrapper.setProps({ id: "B" });
		await flushPromises();
		wrapper.findComponent(Composer).vm.$emit("update:modelValue", "B draft");
		pending.resolve(rejected);
		await flushPromises();
		expect(wrapper.findComponent(SendRecoveryCard).exists()).toBe(false);
		expect(wrapper.findComponent(Composer).props("modelValue")).toBe("B draft");
		await wrapper.setProps({ id: "A" });
		await flushPromises();
		expect(wrapper.findComponent(SendRecoveryCard).text()).toContain("Review invoice");
	});
	it("out-of-order loads cannot overwrite the selected chat", async () => {
		const pending = defer();
		api.getConversation.mockImplementation((id) =>
			id === "A"
				? pending.promise
				: Promise.resolve({
						conversation: { title: "Chat B" },
						messages: [{ name: "B1", role: "user", content: "Only B" }],
				  })
		);
		await open();
		await wrapper.setProps({ id: "B" });
		await flushPromises();
		pending.resolve({
			conversation: { title: "Chat A" },
			messages: [{ name: "A1", role: "user", content: "Only A" }],
		});
		await flushPromises();
		expect(wrapper.text()).toContain("Only B");
		expect(wrapper.text()).not.toContain("Only A");
	});
	it("does not send or clear an unfinished attachment", async () => {
		recoveryState.drafts.A = {
			text: "Keep this",
			attachments: [{ ...file, uploading: true }],
		};
		await open();
		await send("Keep this");
		expect(api.sendRecoverableMessage).not.toHaveBeenCalled();
		expect(wrapper.findComponent(Composer).props("modelValue")).toBe("Keep this");
		expect(wrapper.findComponent(Composer).props("attachments")).toHaveLength(1);
	});
	it.each(["maintenance", "release_update_required"])(
		"preserves text and files after %s rejection",
		async (reason) => {
			recoveryState.drafts.A = { text: "", attachments: [{ ...file }] };
			api.sendRecoverableMessage.mockResolvedValue({
				delivery: "settled",
				result: { ok: false, reason },
			});
			await open();
			await send();
			expect(recoveryState.requests[0].attachments[0].file_url).toBe(file.file_url);
			expect(wrapper.text()).toContain("Review invoice");
		}
	);
});

it("stalled sends become checkable and an old response cannot overwrite a retried attempt", async () => {
	const original = defer();
	api.sendRecoverableMessage.mockReturnValueOnce(original.promise).mockResolvedValue(rejected);
	await open();
	vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
	await send();
	await vi.advanceTimersByTimeAsync(30000);
	expect(recoveryState.requests[0].state).toBe("uncertain");
	api.checkDelivery.mockResolvedValue(rejected);
	await button("Check delivery").trigger("click");
	await flushPromises();
	const oldId = recoveryState.requests[0].id;
	await button("Retry").trigger("click");
	await flushPromises();
	expect(recoveryState.requests[0].id).not.toBe(oldId);
	original.resolve({
		delivery: "settled",
		result: { ok: true, conversation_id: "A", message_id: "old", run_id: "old" },
	});
	await flushPromises();
	expect(recoveryState.requests[0].state).toBe("rejected");
	expect(recoveryState.requests[0].result.message_id).toBeUndefined();
});

it("typed partial approval reconciles receipts without exposing Retry", async () => {
	api.sendRecoverableMessage.mockResolvedValue({
		delivery: "settled",
		result: { ok: false, confirmed: true, conversation_id: "A" },
	});
	await open();
	await send("confirm 1");
	expect(recoveryState.requests).toHaveLength(0);
	expect(wrapper.text()).toContain("one or more actions failed");
	expect(wrapper.findComponent(Composer).props("sending")).toBe(false);
});
