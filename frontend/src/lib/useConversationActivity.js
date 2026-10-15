import { onScopeDispose, watch } from "vue";

// #591: reuse ChatView's canonical lifecycle decisions for its open row and
// local Stop mutes. Background rows are reconciled separately from the server.
export function useConversationActivity({
	store,
	currentId,
	recovering,
	queuedTurn,
	sending,
	waiting,
	stoppedRunId,
	stoppedMsgIds,
	currentMsgId,
}) {
	watch(
		[currentId, () => store.streamingConvId, recovering, queuedTurn, sending, waiting],
		([id, streaming, recovery, queued, isSending, isWaiting]) => {
			let state = null;
			if (streaming === id && id) state = "streaming";
			else if (queued)
				state = queued.state && queued.state !== "queued" ? "preparing" : "queued";
			else if (isSending || isWaiting) state = "preparing";
			else if (recovery) state = "recovering";
			store.activeConversationActivity = id ? { conversation_id: id, state } : null;
		},
		{ immediate: true, flush: "sync" }
	);
	watch(
		stoppedRunId,
		(run) => {
			if (run) store.stopConversationRun(currentId.value, run, currentMsgId.value);
		},
		{ flush: "sync" }
	);
	watch(
		() => [...stoppedMsgIds.value],
		(ids, previous) => {
			for (const id of ids) {
				if (!previous.includes(id)) store.stopConversationRun(currentId.value, null, id);
			}
		},
		{ flush: "sync" }
	);
	// The queued chip disappears with an idle composer only after cancellation
	// (or terminal reconciliation). Promotion keeps it busy; navigation changes id.
	watch(
		[currentId, queuedTurn, sending, waiting],
		([id, queued, busy, wait], [oldId, oldQueued]) => {
			if (id === oldId && oldQueued && !queued && !busy && !wait) {
				store.stopConversationRun(id, oldQueued.run_id);
			}
		},
		{ flush: "post" }
	);
	watch(currentId, () => store.scheduleConversationActivityRefresh());
	onScopeDispose(() => {
		store.activeConversationActivity = null;
		store.scheduleConversationActivityRefresh();
	});
}
