import { effectScope, nextTick, ref, reactive } from "vue";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useConversationActivity } from "./useConversationActivity";

let scope, state, store;
beforeEach(() => {
	state = Object.fromEntries(
		["currentId", "recovering", "queuedTurn", "stoppedRunId", "currentMsgId"].map((key) => [
			key,
			ref(null),
		])
	);
	Object.assign(state, {
		sending: ref(false),
		waiting: ref(false),
		stoppedMsgIds: ref(new Set()),
	});
	state.currentId.value = "conv-a";
	store = reactive({
		streamingConvId: null,
		activeConversationActivity: null,
		stopConversationRun: vi.fn(),
		scheduleConversationActivityRefresh: vi.fn(),
	});
	scope = effectScope();
	scope.run(() => useConversationActivity({ store, ...state }));
});
afterEach(() => scope.stop());
describe("ChatView activity bridge", () => {
	it("publishes idle, live recovery and queued states without overriding ChatView", async () => {
		expect(store.activeConversationActivity.state).toBeNull();
		state.sending.value = true;
		expect(store.activeConversationActivity.state).toBe("preparing");
		state.sending.value = false;
		state.recovering.value = { message_id: "msg" };
		expect(store.activeConversationActivity.state).toBe("recovering");
		state.recovering.value = null;
		state.sending.value = false;
		state.queuedTurn.value = { run_id: "q", state: "queued" };
		expect(store.activeConversationActivity.state).toBe("queued");
	});
	it("clears the active pulse on ChatView's reconnect or silent reconciliation", () => {
		store.streamingConvId = "conv-a";
		expect(store.activeConversationActivity.state).toBe("streaming");
		store.streamingConvId = null;
		expect(store.activeConversationActivity.state).toBeNull();
	});
	it("shows newer queued/preparing work ahead of an older recovery", async () => {
		state.recovering.value = { message_id: "old" };
		state.queuedTurn.value = { run_id: "new", state: "queued" };
		state.sending.value = true;
		expect(store.activeConversationActivity.state).toBe("queued");
		state.queuedTurn.value = null;
		expect(store.activeConversationActivity.state).toBe("preparing");
	});
	it("observes the canonical Stop mute immediately, including legacy message-only Stop", () => {
		state.currentMsgId.value = "msg-a";
		state.stoppedRunId.value = "run-a";
		expect(store.stopConversationRun).toHaveBeenCalledWith("conv-a", "run-a", "msg-a");
		state.stoppedMsgIds.value.add("legacy-msg");
		expect(store.stopConversationRun).toHaveBeenCalledWith("conv-a", null, "legacy-msg");
	});
	it("suppresses the confirmed cancelled queued run, not another live run", async () => {
		state.queuedTurn.value = { run_id: "q", state: "queued" };
		state.sending.value = true;
		await nextTick();
		state.queuedTurn.value = null;
		state.sending.value = false;
		state.waiting.value = false;
		await nextTick();
		expect(store.stopConversationRun).toHaveBeenCalledWith("conv-a", "q");
	});
	it("does not call queue cancellation when the chip becomes a live run or changes chat", async () => {
		state.queuedTurn.value = { run_id: "q" };
		state.sending.value = true;
		await nextTick();
		state.queuedTurn.value = null;
		await nextTick();
		expect(store.stopConversationRun).not.toHaveBeenCalled();
		state.queuedTurn.value = { run_id: "other-q" };
		await nextTick();
		state.currentId.value = "conv-b";
		state.queuedTurn.value = null;
		state.sending.value = false;
		await nextTick();
		expect(store.stopConversationRun).not.toHaveBeenCalled();
	});
	it("reconciles after navigation and releases the active override on teardown", async () => {
		state.currentId.value = "conv-b";
		await nextTick();
		expect(store.scheduleConversationActivityRefresh).toHaveBeenCalled();
		scope.stop();
		expect(store.activeConversationActivity).toBeNull();
	});
});
