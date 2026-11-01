// Execute Desktop applyDraft with a deferred RPC. This uses the real handler so
// a late old response cannot quietly regain authority after A → B → A.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(
	process.env.DESKTOP_CHAT_SOURCE || new URL("../views/ChatView.vue", import.meta.url),
	"utf8",
);
const start = source.indexOf("async function applyDraft(");
const end = source.indexOf("// SUX-3/SUXI-2:", start);
assert.ok(start > -1 && end > start, "could not extract Desktop applyDraft");
const body = source.slice(start, end);

function deferred() {
	let resolve;
	const promise = new Promise((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

function harness() {
	let generation = 0;
	const currentId = { value: "A" };
	const activeAction = { value: { key: "action-A" } };
	const actionFor = { value: "message-A" };
	const draftPanel = { value: null };
	const summaryState = { value: { model: null } };
	const pending = deferred();
	const calls = [];
	const effects = [];
	const model = {
		messageKey: "message-A",
		actionKey: "action-A",
		verb: "create",
		doctype: "ToDo",
		applying: false,
		error: null,
	};
	draftPanel.value = model;
	const deps = {
		draftPanel,
		summaryState,
		draftValues: () => ({}),
		actionFor,
		activeAction,
		draftActionKey: (action) => action?.key || "",
		captureDraftActionView: (_action, messageKey, actionKey) => ({
			generation,
			conversation: currentId.value,
			messageKey,
			actionKey,
		}),
		draftModelViewIsCurrent: (view, candidate) =>
			generation === view.generation &&
			currentId.value === view.conversation &&
			actionFor.value === view.messageKey &&
			activeAction.value?.key === view.actionKey &&
			candidate.actionKey === view.actionKey &&
			candidate.messageKey === view.messageKey,
		parkedNoteFor: () => null,
		parkedByCard: () => null,
		parkedDrafts: { value: new Map() },
		pendingActions: { value: [] },
		api: {
			applyAction: async (payload) => {
				calls.push(payload);
				return pending.promise;
			},
		},
		applyActionPayload: (_model, payload) => payload,
		draftSaveOutcome: () => ({ kind: "saved" }),
		brand: { agentName: "Jarvis" },
		closeDraftPanel: () => effects.push("close"),
		notify: () => effects.push("notify"),
		fixingDraft: { value: "" },
		loadConversation: async (id) => effects.push(`load:${id}`),
		chatViewIsCurrent: (view) =>
			generation === view.generation && currentId.value === view.conversation,
		store: { loadConversations: () => effects.push("store") },
		showQueuedContinuation: () => effects.push("queue"),
		markMissing: () => {},
		markMissingRows: () => {},
		_formMetaCache: {},
		markParked: (items) => items,
		nextTick: async () => {},
		draftParkedRegion: { value: null },
		errMessage: () => "save failed",
	};
	const { applyDraft } = new Function(...Object.keys(deps), `${body}\nreturn { applyDraft };`)(
		...Object.values(deps),
	);
	return {
		model,
		calls,
		effects,
		currentId,
		draftPanel,
		applyDraft,
		resolve: pending.resolve,
		switchView(id, actionKey, messageKey) {
			generation++;
			currentId.value = id;
			activeAction.value = { key: actionKey };
			actionFor.value = messageKey;
			const next = { messageKey, actionKey, applying: false, error: null };
			draftPanel.value = next;
			summaryState.value = { model: next };
			return next;
		},
	};
}

test("a late Desktop draft apply cannot close, reload, or queue a later A view", async () => {
	const h = harness();
	const applying = h.applyDraft(0, h.model);
	await Promise.resolve();
	assert.equal(h.calls[0].conversation, "A", "the request keeps its origin conversation");

	// The second A uses the same action/message identity. Only generation and the
	// new panel object can distinguish it from the old request.
	h.switchView("B", "action-B", "message-B");
	const laterA = h.switchView("A", "action-A", "message-A");
	h.resolve({ ok: true, queued: true });
	await applying;

	assert.equal(h.currentId.value, "A");
	assert.equal(h.draftPanel.value, laterA);
	assert.equal(laterA.applying, false);
	assert.deepEqual(h.effects, []);
});
