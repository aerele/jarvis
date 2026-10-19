import fs from "node:fs";
import { nextTick, ref, watch } from "vue";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { conversationLoadFailure } from "@/lib/conversationLoadFailure";
import { heldNewestSeq, mergeNewestPage, refreshNames } from "@/lib/historyPaging";
import { createPendingSends, injectPendingBubbles } from "@/utils/voiceSendGlue";

// Execute the actual SFC functions at their API boundary, following the existing
// desktopSendRecovery harness. Browser checks cover the full mounted view/router.
const source = fs.readFileSync("src/views/ChatView.vue", "utf8");
function declaration(name) {
	const start = source.search(new RegExp(`(?:async )?function ${name}\\(`));
	if (start < 0) throw new Error("Missing SFC function " + name);
	return source.slice(start, source.indexOf("\n}", start) + 2);
}
const loading = source.slice(
	source.indexOf("// Whether the last loadConversation"),
	source.indexOf("// Render any ```mermaid blocks")
);
const selectionWatch = source.slice(
	source.indexOf("watch(currentId, (id) => {"),
	source.indexOf("\nconst messages = ref([]);")
);
const body = [
	loading,
	...[
		"resetRunState",
		"swapDraft",
		"selectConversation",
		"closeArtifact",
		"closeDraftPanel",
	].map(declaration),
	selectionWatch,
].join("\n");

function deferred() {
	let resolve, reject;
	const promise = new Promise((yes, no) => {
		resolve = yes;
		reject = no;
	});
	return { promise, resolve, reject };
}
const response = (id, content = "Reply " + id) => ({
	conversation: { name: id, title: "Title " + id },
	messages: [{ name: "message-" + id, role: "assistant", content }],
});
const refused = (exc_type) => Object.assign(new Error("PRIVATE-ERROR-SENTINEL"), { exc_type });

function harness(initial = "A") {
	const state = Object.fromEntries(
		[...body.matchAll(/\b(\w+)\.value\b/g)].map((m) => [m[1], ref(null)])
	);
	const noop = () => {};
	const getConversation = vi.fn(async (id) => response(id));
	const forbiddenCalls = vi.fn(() => {
		throw new Error("Fallback must not call another endpoint");
	});
	Object.assign(state, {
		watch,
		nextTick,
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
		__: (text) => "Translated: " + text,
		notify: vi.fn(),
		api: new Proxy(
			{ getConversation },
			{ get: (target, name) => target[name] || forbiddenCalls }
		),
		store: { currentConvId: initial, streamingConvId: "A", clearUnread: noop },
		route: { params: { id: initial } },
		router: {
			replace: vi.fn((target) => {
				state.route.params = typeof target === "string" ? { id: target.slice(3) } : {};
			}),
		},
		flushReveal: noop,
		dropGotoMorph: noop,
		resetAutoModeFor: vi.fn(),
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
	});
	Object.assign(state, {
		currentId: ref(initial),
		messages: ref(response("A").messages),
		input: ref("Draft A"),
		drafts: ref({ NEW: "Draft home" }),
		historyPaging: ref({ conv: "A", hasMore: false, before: null, newest: null }),
		pendingActions: ref([{ conversation: "A", token: "PRIVATE-CARD-SENTINEL" }]),
		macroRun: ref(null),
		activeTools: ref(["old tool"]),
		mention: ref({ open: true }),
		artifact: ref({ title: "Owned artifact", conv: "A" }),
		draftPanel: ref({ title: "Owned preview", fields: { customer: "Unsaved edit" } }),
		loadedConvTitle: ref("Title A"),
		originPage: ref("dashboards"),
		originOf: ref("A"),
		composerRef: ref({ focusInput: vi.fn() }),
	});
	const functions = new Function(
		...Object.keys(state),
		body +
			"\nreturn {loadConversation, selectConversation, leave: () => { _recoveryViewActive = false; }};"
	)(...Object.values(state));
	return { state, ...functions, getConversation, forbiddenCalls };
}

describe("unavailable chat recovery through the actual ChatView functions", () => {
	beforeEach(() => localStorage.clear());
	it.each(["PermissionError", "DoesNotExistError"])(
		"returns home after %s without retaining the previous transcript or mixing drafts",
		async (kind) => {
			const h = harness();
			h.getConversation.mockRejectedValue(refused(kind));
			await h.selectConversation("B");
			await nextTick();
			const s = h.state;
			expect(s.currentId.value).toBeNull();
			expect(s.store.currentConvId).toBeNull();
			expect(localStorage.getItem("jarvis-last-conv")).toBeNull();
			expect(s.messages.value).toEqual([]);
			expect(s.loadedConvTitle.value).toBe("");
			expect(s.originPage.value).toBe("");
			expect(s.artifact.value).toEqual({ title: "Owned artifact", conv: "A" });
			expect(s.draftPanel.value.fields.customer).toBe("Unsaved edit");
			expect(
				s.pendingActions.value.filter((p) => p.conversation === s.currentId.value)
			).toEqual([]);
			expect(s.sending.value).toBe(false);
			expect(s.input.value).toBe("Draft home");
			expect(s.drafts.value.A).toBe("Draft A");
			expect(s.router.replace).toHaveBeenLastCalledWith({ name: "Chat" });
			expect(s.notify).toHaveBeenCalledTimes(1);
			expect(s.notify).toHaveBeenCalledWith("Translated: That chat isn't available", {
				type: "error",
			});
			expect(s.composerRef.value.focusInput).not.toHaveBeenCalled();
			expect(h.forbiddenCalls).not.toHaveBeenCalled();
		}
	);
	it.each(["PermissionError", "DoesNotExistError"])("handles a cold %s link", async (kind) => {
		const h = harness("B");
		h.state.messages.value = [];
		h.state.input.value = "";
		h.getConversation.mockRejectedValue(refused(kind));
		await h.loadConversation("B");
		expect(h.state.currentId.value).toBeNull();
		expect(h.state.router.replace).toHaveBeenCalledWith({ name: "Chat" });
		expect(h.state.notify).toHaveBeenCalledTimes(1);
		expect(h.state.input.value).toBe("Draft home");
	});
	it("preserves newer typing in a different chat when an older refusal arrives", async () => {
		const h = harness(),
			old = deferred();
		h.getConversation.mockImplementation((id) =>
			id === "B" ? old.promise : Promise.resolve(response(id))
		);
		const pending = h.selectConversation("B");
		await h.selectConversation("C");
		h.state.input.value = "Newer draft C";
		h.state.composerRef.value.focusInput.mockClear();
		old.reject(refused("PermissionError"));
		await pending;
		expect(h.state.currentId.value).toBe("C");
		expect(h.state.messages.value[0].content).toBe("Reply C");
		expect(h.state.input.value).toBe("Newer draft C");
		expect(h.state.router.replace).toHaveBeenLastCalledWith("/c/C");
		expect(h.state.notify).not.toHaveBeenCalled();
		expect(h.state.composerRef.value.focusInput).not.toHaveBeenCalled();
	});
	it("ignores an old A refusal after navigating A to B to A", async () => {
		const h = harness(),
			old = deferred();
		h.getConversation.mockImplementationOnce(() => old.promise);
		const pending = h.loadConversation("A");
		await h.selectConversation("B");
		await h.selectConversation("A");
		h.state.input.value = "Newer draft A";
		old.reject(refused("DoesNotExistError"));
		await pending;
		expect(h.state.currentId.value).toBe("A");
		expect(h.state.messages.value[0].content).toBe("Reply A");
		expect(h.state.input.value).toBe("Newer draft A");
		expect(h.state.notify).not.toHaveBeenCalled();
	});
	it("does not redirect, toast or focus after leaving the view", async () => {
		const h = harness(),
			old = deferred();
		h.getConversation.mockReturnValue(old.promise);
		const pending = h.selectConversation("B");
		h.leave();
		old.reject(refused("PermissionError"));
		await pending;
		expect(h.state.router.replace).not.toHaveBeenCalledWith({ name: "Chat" });
		expect(h.state.notify).not.toHaveBeenCalled();
		expect(h.state.composerRef.value.focusInput).not.toHaveBeenCalled();
	});
	it("keeps the latest successful read when an older same-chat read completes", async () => {
		const h = harness(),
			old = deferred();
		h.getConversation.mockReturnValueOnce(old.promise);
		const pending = h.loadConversation("A");
		const fresh = response("A", "Latest reply");
		h.getConversation.mockResolvedValueOnce(fresh);
		await h.loadConversation("A");
		old.resolve(response("A"));
		await pending;
		expect(h.state.messages.value[0].content).toBe("Latest reply");
	});
	it.each([new TypeError("Failed to fetch"), { status: 500 }, { exc_type: "CSRFTokenError" }])(
		"preserves existing handling for other failures: %s",
		async (error) => {
			const h = harness();
			h.getConversation.mockRejectedValue(error);
			await expect(h.selectConversation("B")).rejects.toBe(error);
			expect(h.state.currentId.value).toBe("B");
			expect(h.state.notify).not.toHaveBeenCalled();
			expect(h.state.router.replace).not.toHaveBeenCalledWith({ name: "Chat" });
		}
	);
	it("keeps typing during the refused load scoped and restores each saved draft", async () => {
		const h = harness(),
			old = deferred();
		h.getConversation.mockReturnValueOnce(old.promise);
		const pending = h.selectConversation("B");
		h.state.input.value = "Typed while B loads";
		old.reject(refused("PermissionError"));
		await pending;
		expect(h.state.input.value).toBe("Draft home");
		expect(h.state.drafts.value.B).toBe("Typed while B loads");
		await h.selectConversation("A");
		expect(h.state.input.value).toBe("Draft A");
		expect(h.state.drafts.value.NEW).toBe("Draft home");
	});
	it("still loads an accessible chat and focuses its composer", async () => {
		const h = harness();
		await h.selectConversation("B");
		expect(h.state.messages.value[0].content).toBe("Reply B");
		expect(h.state.loadedConvTitle.value).toBe("Title B");
		expect(h.state.composerRef.value.focusInput).toHaveBeenCalledTimes(1);
		expect(h.state.notify).not.toHaveBeenCalled();
		expect(h.forbiddenCalls).not.toHaveBeenCalled();
	});
});
