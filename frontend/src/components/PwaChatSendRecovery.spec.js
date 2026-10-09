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
	activeQueuedTurn: vi.fn(),
	queuePosition: vi.fn(),
	cancelQueuedTurn: vi.fn(),
	stopRun: vi.fn(),
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
let event;
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
		global: {
			provide: {
				$socket: {
					on: (_name, fn) => {
						event = fn;
					},
					off: vi.fn(),
				},
			},
		},
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
	recoveryState.heroDraft = null;
	recoveryState.newChatPicks = {};
	recoveryState.editingNewRequest = null;
	recoveryState.drafts = {};
	recoveryState.parkedDrafts = [];
	recoveryState.queued = {};
	recoveryState.starting = {};
	recoveryState.observedRuns = {};
	api.activeQueuedTurn.mockResolvedValue({ ok: true, active: null });
	api.queuePosition.mockResolvedValue({ ok: true, state: "queued", position: 2 });
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

const acceptedQueue = {
	delivery: "settled",
	result: {
		ok: true,
		conversation_id: "A",
		message_id: "M",
		run_id: "queue-R",
		queued: true,
		queued_position: 3,
	},
};
function action(label) {
	return wrapper.findAll("button").find((b) => b.text() === label);
}
function savedSeed() {
	api.getConversation.mockResolvedValue({
		conversation: { name: "A" },
		messages: [{ name: "M", role: "user", content: "Review invoice" }],
	});
}
it("retargeting preserves both drafts and their files with reversible draft selection", async () => {
	const pending = defer();
	api.sendRecoverableMessage.mockReturnValue(pending.promise);
	recoveryState.drafts.B = { text: "B draft", attachments: [{ ...file }] };
	await open();
	await send();
	wrapper.findComponent(Composer).vm.$emit("update:modelValue", "A newer draft");
	pending.resolve({
		delivery: "settled",
		result: { ok: true, conversation_id: "B", message_id: "M", run_id: "R" },
	});
	await flushPromises();
	await wrapper.setProps({ id: "B" });
	await flushPromises();
	expect(wrapper.findComponent(Composer).props("modelValue")).toBe("B draft");
	expect(wrapper.findComponent(Composer).props("attachments")).toHaveLength(1);
	await action("Use this draft").trigger("click");
	expect(wrapper.findComponent(Composer).props("modelValue")).toBe("A newer draft");
	expect(wrapper.text()).toContain("invoice.pdf");
	await action("Use this draft").trigger("click");
	expect(wrapper.findComponent(Composer).props("modelValue")).toBe("B draft");
	expect(wrapper.findComponent(Composer).props("attachments")[0].file_url).toBe(file.file_url);
	expect(api.sendRecoverableMessage).toHaveBeenCalledTimes(1);
});
it("empty source does not overwrite a destination attachment-only draft", async () => {
	recoveryState.drafts.B = { text: "", attachments: [{ ...file }] };
	api.sendRecoverableMessage.mockResolvedValue({
		delivery: "settled",
		result: { ok: true, conversation_id: "B", message_id: "M", run_id: "R" },
	});
	await open();
	await send();
	await wrapper.setProps({ id: "B" });
	await flushPromises();
	expect(wrapper.findComponent(Composer).props("attachments")).toHaveLength(1);
	expect(recoveryState.parkedDrafts).toHaveLength(0);
});
it("timed-out checks unlock across remount and old responses cannot settle a newer check", async () => {
	api.sendRecoverableMessage.mockRejectedValue(new Error("lost"));
	const old = defer(),
		newer = defer();
	api.checkDelivery.mockReturnValueOnce(old.promise).mockReturnValueOnce(newer.promise);
	await open();
	await send();
	vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
	await button("Check delivery").trigger("click");
	await vi.advanceTimersByTimeAsync(15000);
	expect(recoveryState.requests[0].checking).toBe(false);
	wrapper.unmount();
	await open();
	await button("Check delivery").trigger("click");
	old.resolve(rejected);
	await flushPromises();
	expect(recoveryState.requests[0].state).toBe("uncertain");
	expect(recoveryState.requests[0].checking).toBe(true);
	newer.resolve(rejected);
	await flushPromises();
	expect(recoveryState.requests[0].state).toBe("rejected");
	expect(recoveryState.requests[0].checking).toBe(false);
	expect(api.sendRecoverableMessage).toHaveBeenCalledTimes(1);
});
it("keeps queue feedback after seed reconciliation and remount without duplicate bubbles", async () => {
	api.sendRecoverableMessage.mockResolvedValue(acceptedQueue);
	await open();
	savedSeed();
	await send();
	expect(recoveryState.requests).toHaveLength(0);
	expect(wrapper.text()).toContain("Message queued");
	expect(wrapper.findComponent(Composer).props("sending")).toBe(true);
	expect(wrapper.findComponent(SendRecoveryCard).exists()).toBe(false);
	wrapper.unmount();
	await open();
	expect(wrapper.text()).toContain("Message queued");
	event({ kind: "run:start", conversation_id: "A", run_id: "queue-R", message_id: "reply" });
	await flushPromises();
	expect(wrapper.text()).not.toContain("Message queued");
	expect(wrapper.findComponent(Composer).props("sending")).toBe(true);
	event({ kind: "run:end", conversation_id: "A", run_id: "queue-R" });
	await flushPromises();
	expect(wrapper.findComponent(Composer).props("sending")).toBe(false);
});
it("terminal before acknowledgement cannot resurrect the queue", async () => {
	const pending = defer();
	api.sendRecoverableMessage.mockReturnValue(pending.promise);
	await open();
	await send();
	event({ kind: "run:end", conversation_id: "A", run_id: "queue-R" });
	savedSeed();
	pending.resolve(acceptedQueue);
	await flushPromises();
	expect(wrapper.text()).not.toContain("Message queued");
	expect(wrapper.findComponent(Composer).props("sending")).toBe(false);
});
it("discovers server queue after reload and only clears a confirmed cancellation", async () => {
	api.activeQueuedTurn.mockResolvedValue({
		ok: true,
		active: { run_id: "queue-R", state: "preparing" },
	});
	api.cancelQueuedTurn
		.mockRejectedValueOnce(new Error("lost"))
		.mockResolvedValueOnce({ ok: true });
	await open();
	expect(wrapper.text()).toContain("Starting…");
	await action("Cancel request").trigger("click");
	await flushPromises();
	expect(wrapper.text()).toContain("Cancellation is not confirmed");
	expect(wrapper.findComponent(Composer).props("sending")).toBe(true);
	await action("Cancel request").trigger("click");
	await flushPromises();
	expect(api.cancelQueuedTurn).toHaveBeenCalledWith("queue-R");
	expect(wrapper.findComponent(Composer).props("sending")).toBe(false);
});
it("late queue read cannot restore a terminal run", async () => {
	api.sendRecoverableMessage.mockResolvedValue(acceptedQueue);
	await open();
	savedSeed();
	await send();
	const pending = defer();
	api.queuePosition.mockReturnValue(pending.promise);
	await action("Check status").trigger("click");
	event({ kind: "run:end", conversation_id: "A", run_id: "queue-R" });
	pending.resolve({ ok: true, state: "queued", position: 1 });
	await flushPromises();
	expect(wrapper.text()).not.toContain("Message queued");
	expect(wrapper.findComponent(Composer).props("sending")).toBe(false);
});
it("resync clears a missed terminal and failed status reads retain the accepted request", async () => {
	api.sendRecoverableMessage.mockResolvedValue(acceptedQueue);
	await open();
	savedSeed();
	await send();
	api.queuePosition
		.mockRejectedValueOnce(new Error("offline"))
		.mockResolvedValueOnce({ ok: true, state: "done" });
	await action("Check status").trigger("click");
	await flushPromises();
	expect(wrapper.text()).toContain("Could not refresh status");
	window.dispatchEvent(new Event("jv:resync"));
	await flushPromises();
	expect(wrapper.text()).not.toContain("Message queued");
	expect(wrapper.findComponent(Composer).props("sending")).toBe(false);
});

it("polling a terminal before acknowledgement does not resurrect the queued receipt", async () => {
	api.activeQueuedTurn.mockResolvedValue({
		ok: true,
		active: { run_id: "queue-R", state: "queued" },
	});
	// A request started before discovery; seed its in-flight snapshot as on remount.
	recoveryState.requests.push({
		id: "pending",
		conversation: "A",
		text: "Review invoice",
		attachments: [],
		state: "sending",
		checking: false,
		result: null,
	});
	await open();
	api.queuePosition.mockResolvedValue({ ok: true, state: "done" });
	await action("Check status").trigger("click");
	await flushPromises();
	recoveryState.requests[0].state = "accepted";
	recoveryState.requests[0].result = acceptedQueue.result;
	savedSeed();
	await flushPromises();
	expect(wrapper.text()).not.toContain("Message queued");
	expect(wrapper.findComponent(Composer).props("sending")).toBe(false);
});
it("queue status from a previous conversation cannot affect the selected chat", async () => {
	api.sendRecoverableMessage.mockResolvedValue(acceptedQueue);
	await open();
	savedSeed();
	await send();
	const pending = defer();
	api.queuePosition.mockReturnValue(pending.promise);
	await action("Check status").trigger("click");
	await wrapper.setProps({ id: "B" });
	await flushPromises();
	pending.resolve({ ok: true, state: "queued", position: 1 });
	await flushPromises();
	expect(wrapper.text()).not.toContain("Message queued");
	expect(wrapper.findComponent(Composer).props("sending")).toBe(false);
	expect(recoveryState.queued.A.position).toBe(3);
});
it("stalled queue reads release the status control and keep waiting feedback", async () => {
	api.sendRecoverableMessage.mockResolvedValue(acceptedQueue);
	await open();
	savedSeed();
	await send();
	api.queuePosition
		.mockReturnValueOnce(new Promise(() => {}))
		.mockResolvedValueOnce({ ok: true, state: "ready" });
	vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
	await action("Check status").trigger("click");
	await vi.advanceTimersByTimeAsync(15000);
	expect(wrapper.text()).toContain("Could not refresh status");
	await action("Check status").trigger("click");
	await flushPromises();
	expect(wrapper.text()).toContain("Starting…");
	expect(wrapper.findComponent(Composer).props("sending")).toBe(true);
});

it("normal acceptance stays busy after seed reconciliation until the matching run starts and ends", async () => {
	const pending = defer();
	api.sendRecoverableMessage.mockReturnValue(pending.promise);
	await open();
	await send();
	expect(wrapper.findComponent(Composer).props("sending")).toBe(true);
	savedSeed();
	pending.resolve({
		delivery: "settled",
		result: { ok: true, conversation_id: "A", message_id: "M", run_id: "normal" },
	});
	await flushPromises();
	expect(recoveryState.requests).toHaveLength(0);
	expect(wrapper.findComponent(Composer).props("sending")).toBe(true);
	await send("Second message");
	expect(api.sendRecoverableMessage).toHaveBeenCalledTimes(1);
	api.queuePosition.mockResolvedValue({ ok: true, state: "dispatching" });
	wrapper.unmount();
	await open();
	expect(wrapper.findComponent(Composer).props("sending")).toBe(true);
	event({ kind: "run:start", conversation_id: "A", run_id: "normal" });
	await flushPromises();
	expect(recoveryState.starting.A).toBeUndefined();
	expect(wrapper.findComponent(Composer).props("sending")).toBe(true);
	event({ kind: "run:end", conversation_id: "A", run_id: "normal" });
	await flushPromises();
	expect(wrapper.findComponent(Composer).props("sending")).toBe(false);
});
it.each(["run:end", "run:error"])(
	"normal acceptance after %s does not restore busy state",
	async (kind) => {
		const pending = defer();
		api.sendRecoverableMessage.mockReturnValue(pending.promise);
		await open();
		await send();
		event({ kind, conversation_id: "A", run_id: "normal-late", error: "failed" });
		savedSeed();
		pending.resolve({
			delivery: "settled",
			result: { ok: true, conversation_id: "A", message_id: "M", run_id: "normal-late" },
		});
		await flushPromises();
		expect(wrapper.findComponent(Composer).props("sending")).toBe(false);
	}
);
it("edit as new preserves a newer draft and files without sending or importing approval selections", async () => {
	recoveryState.drafts.A = { text: "", attachments: [{ ...file }] };
	api.sendRecoverableMessage.mockRejectedValue(new Error("offline"));
	await open();
	await send();
	recoveryState.requests[0].approvalTokens = ["old-approval"];
	wrapper.findComponent(Composer).vm.$emit("update:modelValue", "Newer draft");
	await button("Edit as new message").trigger("click");
	await flushPromises();
	await button("Move to composer").trigger("click");
	await flushPromises();
	expect(recoveryState.requests).toHaveLength(0);
	expect(wrapper.findComponent(Composer).props("modelValue")).toBe("Review invoice");
	expect(wrapper.findComponent(Composer).props("attachments")[0].file_url).toBe(file.file_url);
	expect(recoveryState.parkedDrafts[0].text).toBe("Newer draft");
	expect(wrapper.text()).toContain("could duplicate work");
	expect(api.sendRecoverableMessage).toHaveBeenCalledTimes(1);
	api.sendRecoverableMessage.mockResolvedValue(rejected);
	await send();
	expect(api.sendRecoverableMessage.mock.calls[1][0].approvalTokens).toEqual([]);
});
it("discarding unknown delivery leaves newer composer text unchanged", async () => {
	api.sendRecoverableMessage.mockRejectedValue(new Error("offline"));
	await open();
	await send();
	wrapper.findComponent(Composer).vm.$emit("update:modelValue", "Keep me");
	await button("Discard").trigger("click");
	await flushPromises();
	await button("Discard request").trigger("click");
	await flushPromises();
	expect(recoveryState.requests).toHaveLength(0);
	expect(wrapper.findComponent(Composer).props("modelValue")).toBe("Keep me");
	expect(api.sendRecoverableMessage).toHaveBeenCalledTimes(1);
});

it("normal waiting state heals a missed terminal through server resync", async () => {
	api.sendRecoverableMessage.mockResolvedValue({
		delivery: "settled",
		result: { ok: true, conversation_id: "A", message_id: "M", run_id: "normal-ended" },
	});
	await open();
	savedSeed();
	await send();
	expect(wrapper.findComponent(Composer).props("sending")).toBe(true);
	api.queuePosition.mockResolvedValue({ ok: true, state: "done" });
	window.dispatchEvent(new Event("jv:resync"));
	await flushPromises();
	expect(wrapper.findComponent(Composer).props("sending")).toBe(false);
});

it.each([
	[
		"finished",
		{ name: "reply", role: "assistant", content: "Invoice reviewed", streaming: false },
		false,
	],
	[
		"streaming",
		{ name: "reply", role: "assistant", content: "Reviewing invoice", streaming: true },
		true,
	],
	["not started", null, true],
])(
	"legacy reply %s reconciles busy state after A → B → A without a Turn row",
	async (_label, reply, busy) => {
		api.queuePosition.mockResolvedValue({ ok: false });
		api.sendRecoverableMessage.mockResolvedValue({
			delivery: "settled",
			result: {
				ok: true,
				conversation_id: "A",
				message_id: "M",
				run_id: "legacy-run",
			},
		});
		await open();
		savedSeed();
		await send();
		expect(wrapper.findComponent(Composer).props("sending")).toBe(true);
		await wrapper.setProps({ id: "B" });
		await flushPromises();
		// Socket events for A are missed while B is selected.
		event({ kind: "run:start", conversation_id: "A", run_id: "legacy-run" });
		if (reply && !reply.streaming)
			event({ kind: "run:end", conversation_id: "A", run_id: "legacy-run" });
		api.getConversation.mockResolvedValue({
			conversation: { name: "A" },
			messages: [
				{ name: "M", role: "user", content: "Review invoice" },
				...(reply ? [reply] : []),
			],
		});
		await wrapper.setProps({ id: "A" });
		await flushPromises();
		expect(api.queuePosition).toHaveBeenCalledWith("legacy-run");
		expect(wrapper.findComponent(Composer).props("sending")).toBe(busy);
		if (reply) {
			expect(recoveryState.starting.A).toBeUndefined();
			expect(recoveryState.observedRuns["legacy-run"]).toBe(true);
		} else expect(recoveryState.starting.A).toBe("legacy-run");
	}
);

it("a late status response cannot restore waiting after the transcript shows a completed reply", async () => {
	api.sendRecoverableMessage.mockResolvedValue({
		delivery: "settled",
		result: {
			ok: true,
			conversation_id: "A",
			message_id: "M",
			run_id: "finished-run",
		},
	});
	await open();
	savedSeed();
	await send();
	const status = defer();
	api.queuePosition.mockReturnValue(status.promise);
	api.getConversation.mockResolvedValue({
		conversation: { name: "A" },
		messages: [
			{ name: "M", role: "user", content: "Review invoice" },
			{ name: "reply", role: "assistant", content: "Done", streaming: false },
		],
	});
	window.dispatchEvent(new Event("jv:resync"));
	await flushPromises();
	expect(wrapper.findComponent(Composer).props("sending")).toBe(false);
	status.resolve({ ok: true, state: "queued", position: 1 });
	await flushPromises();
	expect(wrapper.findComponent(Composer).props("sending")).toBe(false);
	expect(recoveryState.queued.A).toBeUndefined();
});

it("retries an uncertain request with its unchanged id, files and payload without restoring an old hold", async () => {
	api.sendRecoverableMessage.mockRejectedValueOnce(new Error("offline"));
	recoveryState.drafts.A = { text: "", attachments: [{ ...file }] };
	await open();
	await send();
	const original = JSON.parse(JSON.stringify(recoveryState.requests[0]));
	api.sendRecoverableMessage.mockResolvedValue({
		delivery: "settled",
		result: { ok: false, reason: "maintenance", message: "old hold" },
	});
	await button("Retry same request").trigger("click");
	await flushPromises();
	expect(api.sendRecoverableMessage).toHaveBeenCalledTimes(2);
	expect(recoveryState.requests[0]).toMatchObject({
		id: original.id,
		text: original.text,
		attachments: original.attachments,
		state: "rejected",
	});
	const { raiseHold } = await import("../../../pwa/src/maintenanceGate");
	expect(raiseHold).not.toHaveBeenCalled();
});
it("recovery composer retains the first-send preferences and files", async () => {
	recoveryState.newChatPicks = { model: "chosen-model", thinking: "high", autoMode: true };
	recoveryState.drafts[""] = {
		text: "draft",
		attachments: [{ ...file }],
		picks: { ...recoveryState.newChatPicks },
	};
	await open("");
	await send("Follow up");
	expect(api.sendRecoverableMessage.mock.calls[0][0]).toMatchObject({
		model: "chosen-model",
		thinking: "high",
		autoMode: true,
		attachments: [{ name: file.name, file_url: file.file_url }],
	});
	await button("Edit message and model").trigger("click");
	expect(recoveryState.editingNewRequest).toBe(recoveryState.requests[0].id);
});
it("Edit as new retains original first-send picks while parking a newer draft", async () => {
	api.sendRecoverableMessage.mockRejectedValue(new Error("offline"));
	recoveryState.newChatPicks = { model: "chosen-model", thinking: "high", autoMode: true };
	await open("");
	await send("Original");
	wrapper.findComponent(Composer).vm.$emit("update:modelValue", "Newer draft");
	await flushPromises();
	await button("Edit as new message").trigger("click");
	await flushPromises();
	await button("Move to composer").trigger("click");
	await flushPromises();
	expect(recoveryState.heroDraft).toMatchObject({
		text: "Original",
		model: "chosen-model",
		thinking: "high",
		autoMode: true,
	});
	expect(
		recoveryState.parkedDrafts.some(
			(d) => d.text === "Newer draft" && d.picks.model === "chosen-model"
		)
	).toBe(true);
});
