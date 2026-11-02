import fs from "node:fs";
import { computed, nextTick, ref, watch } from "vue";
import { describe, expect, it, vi } from "vitest";
import { toolFailureCopy } from "@/lib/actionSummary";
import { conversationLoadFailure } from "@/lib/conversationLoadFailure";
import { heldNewestSeq, mergeNewestPage, refreshNames } from "@/lib/historyPaging";
import { streamingRowIsLive } from "@/lib/silentRun";
import { createPendingSends, injectPendingBubbles } from "@/utils/voiceSendGlue";

// #773: a chat.send whose write started but could not be confirmed is parked for
// background recovery (run:recovering) instead of a false "No changes" error. The
// parked reply row is still blank and streaming, and its "Finishing in the
// background / Reconnecting..." copy renders INSIDE that row (overrideFor), so
// the thread must keep showing it, live and after a reload, while still hiding
// unrelated stale blank rows.
//
// These run the ACTUAL ChatView source (the visibleMessages computed, the live
// event handler and loadConversation), extracted from the SFC as in
// ChatView.unavailable.spec.js. The browser review covers the mounted view.
const source = fs.readFileSync("src/views/ChatView.vue", "utf8");
function between(from, to) {
	const start = source.indexOf(from);
	const end = source.indexOf(to, start);
	if (start < 0 || end < 0) throw new Error("Missing SFC section " + from);
	return source.slice(start, end);
}
function declaration(name) {
	const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
	if (start < 0) throw new Error("Missing SFC function " + name);
	return source.slice(start, source.indexOf("\n}", start) + 2);
}
const body = [
	between(
		"const orphanToolFailures = computed(",
		"// Group role=tool messages under the assistant turn"
	),
	declaration("onEvent"),
	between("// Whether the last loadConversation", "// Render any ```mermaid blocks"),
].join("\n");

const PARKED = "msg-parked";
const conversation = (rows) => ({
	conversation: { name: "A", title: "BOE" },
	messages: rows,
});
const user = { name: "msg-user", role: "user", content: "Read this BOE" };
const parkedRow = (extra = {}) => ({
	name: PARKED,
	role: "assistant",
	content: "",
	streaming: 1,
	recovering: 1,
	...extra,
});

function harness(rows) {
	// Every other `x.value` the extracted source reads is a ref; names it declares
	// itself (the computeds, local consts) are left to it.
	const declared = new Set([...body.matchAll(/\b(?:const|let|var) (\w+)\b/g)].map((m) => m[1]));
	const state = Object.fromEntries(
		[...body.matchAll(/\b(\w+)\.value\b/g)]
			.filter((m) => !declared.has(m[1]))
			.map((m) => [m[1], ref(null)])
	);
	const noop = () => {};
	const getConversation = vi.fn(async () => conversation(rows()));
	Object.assign(state, {
		computed,
		watch,
		nextTick,
		toolFailureCopy,
		streamingRowIsLive,
		conversationLoadFailure,
		heldNewestSeq,
		mergeNewestPage,
		refreshNames,
		injectPendingBubbles,
		_pendingSends: createPendingSends(),
		_NEW_CHAT_SCOPE: "NEW",
		_currentScope: () => state.currentId.value || "NEW",
		_recoveryRefreshEpoch: 0,
		_recoveryViewActive: true,
		_shownConvId: "A",
		_olderArmed: true,
		_restoreTop: null,
		_restoreUntil: 0,
		draftLinkGeneration: 0,
		recentPrompts: [],
		__: (text) => text,
		notify: vi.fn(),
		api: { getConversation },
		store: { currentConvId: "A", streamingConvId: "A", clearUnread: noop },
		route: { params: { id: "A" } },
		router: { replace: vi.fn() },
		pumpFenceReject: () => false,
		pumpFenceAccept: noop,
		flushReveal: noop,
		dropGotoMorph: noop,
		resetAutoModeFor: noop,
		loadContext: noop,
		_loadConnectorFocusFor: () => null,
		seedPendingFromRows: noop,
		pendingSeedRows: () => [],
		resyncPendingConfirmations: noop,
		resyncQueuedTurn: noop,
		refreshFileboxWaits: noop,
		refreshReportRuns: noop,
		promptsOf: () => [],
		promptRows: (_recent, messages) => messages,
		ensureCanvas: noop,
		tearDownActivityIfSettled: noop,
		_checkPulseOnce: noop,
		scrollBottom: noop,
		armOlderNextFrame: noop,
		processMermaid: noop,
		arrowNeeded: () => false,
	});
	Object.assign(state, {
		currentId: ref("A"),
		messages: ref([]),
		currentRunId: ref(null),
		currentMsgId: ref(null),
		recovering: ref(null),
		stoppedRunId: ref(null),
		stoppedMsgIds: ref(new Set()),
		activeTools: ref([]),
		historyPaging: ref({ conv: "A", hasMore: false, before: null, newest: null }),
		pendingActions: ref([]),
		drafts: ref({}),
		input: ref(""),
		threadEl: ref(null),
	});
	for (const name of declared) delete state[name];
	const fns = new Function(
		...Object.keys(state),
		body + "\nreturn { visibleMessages, onEvent, loadConversation };"
	)(...Object.values(state));
	return { state, ...fns };
}

const shown = (h) => h.visibleMessages.value.map((m) => m.name);

describe("a reply parked for background recovery stays on screen (#773)", () => {
	it("live: run:recovering keeps the blank parked row and its run is no longer current", () => {
		const h = harness(() => []);
		h.state.messages.value = [user, parkedRow({ recovering: 0 })];
		h.state.currentRunId.value = "run-1";
		h.state.currentMsgId.value = PARKED;
		expect(shown(h)).toContain(PARKED); // premise: the live turn's own row
		h.onEvent({
			kind: "run:recovering",
			conversation_id: "A",
			run_id: "run-1",
			message_id: PARKED,
			reason: "ack-timeout",
		});
		expect(h.state.currentRunId.value).toBeNull();
		expect(h.state.recovering.value).toEqual({ message_id: PARKED, reason: "ack-timeout" });
		expect(shown(h)).toEqual(["msg-user", PARKED]);
	});

	it("reload: the server's live recovering turn rebuilds the banner on its row", async () => {
		const h = harness(() => [
			user,
			parkedRow({ activity_state: "recovering", modified: "2001-01-01 00:00:00" }),
		]);
		await h.loadConversation("A");
		expect(h.state.recovering.value).toEqual({ message_id: PARKED, reason: "interrupted" });
		expect(h.state.currentRunId.value).toBeNull();
		expect(h.state.sending.value).toBe(false);
		expect(shown(h)).toEqual(["msg-user", PARKED]);
	});

	it("hides blank streaming rows that are neither the recovering nor the live one", async () => {
		const h = harness(() => [
			user,
			{
				name: "msg-stale",
				role: "assistant",
				content: "",
				streaming: 1,
				activity_state: null,
			},
			{ name: "msg-user-2", role: "user", content: "next" },
			parkedRow({ activity_state: "recovering" }),
		]);
		await h.loadConversation("A");
		expect(shown(h)).toEqual(["msg-user", "msg-user-2", PARKED]);
		// A recovering banner for some other message keeps a blank stale row hidden.
		h.state.recovering.value = { message_id: "msg-elsewhere", reason: "interrupted" };
		expect(shown(h)).toEqual(["msg-user", "msg-user-2"]);
	});

	it("keeps the live run, error and canvas rows exactly as before", () => {
		const h = harness(() => []);
		h.state.messages.value = [
			user,
			{ name: "msg-error", role: "assistant", content: "", streaming: 1, error: "Failed" },
			{
				name: "msg-canvas",
				role: "assistant",
				content: "",
				streaming: 1,
				canvas: [{ id: 1 }],
			},
			{ name: "msg-live", role: "assistant", content: "", streaming: 1 },
		];
		h.state.currentRunId.value = "run-2";
		h.state.currentMsgId.value = "msg-live";
		expect(shown(h)).toEqual(["msg-user", "msg-error", "msg-canvas", "msg-live"]);
		h.state.currentRunId.value = null;
		expect(shown(h)).toEqual(["msg-user", "msg-error", "msg-canvas"]);
	});

	it("the recovered answer replaces the parked state after its reload", async () => {
		let rows = [user, parkedRow({ activity_state: "recovering" })];
		const h = harness(() => rows);
		await h.loadConversation("A");
		expect(h.state.recovering.value?.message_id).toBe(PARKED);
		rows = [
			user,
			{ name: PARKED, role: "assistant", content: "Duty total 1,250.00", streaming: 0 },
		];
		await h.loadConversation("A");
		expect(h.state.recovering.value).toBeNull();
		expect(h.visibleMessages.value.map((m) => [m.name, m.content])).toEqual([
			["msg-user", "Read this BOE"],
			[PARKED, "Duty total 1,250.00"],
		]);
	});
});
