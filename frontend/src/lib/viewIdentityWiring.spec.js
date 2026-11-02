import { describe, expect, it } from "vitest";
import fs from "fs";
import path from "path";

const desktop = fs.readFileSync(path.resolve(__dirname, "../views/ChatView.vue"), "utf8");
const dashboard = fs.readFileSync(
	path.resolve(__dirname, "../pages/dashboards/DashboardChatPane.vue"),
	"utf8"
);

function body(source, head, next) {
	const start = source.indexOf(head);
	expect(start, head).toBeGreaterThan(-1);
	const end = next ? source.indexOf(next, start + head.length) : source.length;
	return source.slice(start, end === -1 ? source.length : end);
}

describe("client view identity wiring", () => {
	it("fences a File Box approve or deny RPC to its original view and wait item", () => {
		// The composable owns refresh generations and binds each item to its chat.
		// Its runtime tests cover late responses and A -> B -> A navigation.
		const refresh = body(desktop, "} = useFileBoxWaits({", "const fileboxWaitBusy");
		expect(refresh).toContain("conversation: currentId,");
		expect(refresh).toContain("load: api.fileboxOpenWaits,");

		const decide = body(
			desktop,
			"async function decideFileboxWait(",
			"// Whether the last loadConversation"
		);
		expect(decide).toContain("const view = captureChatView(item.name);");
		expect(decide).toContain("item.conversation !== view.conversation");
		expect(decide).toContain("fileboxWaitIsCurrent(view)");
		expect(decide).toContain("await api.decideApproval(view.token, text, approve);");
		expect(decide).toContain("store.refreshApprovalsCount?.();");
		expect(decide).toContain("fileboxWaitBusy.value === view.token");
		expect(decide).toContain("loadConversation(view.conversation)");
	});

	it("does not let a post-render continuation affect a new chat view", () => {
		const transcript = body(
			dashboard,
			"async function loadTranscript(",
			"// debounced refetch"
		);
		expect(transcript).toContain("if (dashboardViewIsCurrent(view)) scrollBottom();");

		const older = body(desktop, "async function loadOlder(", "// Arrow click:");
		const tick = older.indexOf("await nextTick();");
		const guard = older.indexOf("if (!chatViewIsCurrent(view)) return;", tick);
		const scroll = older.indexOf("holdFromBottom(fromBottom)", tick);
		expect(guard).toBeGreaterThan(tick);
		expect(scroll).toBeGreaterThan(guard);
	});

	it("fences every remaining desktop closure continuation to its original view", () => {
		const context = body(
			desktop,
			"async function loadContext(",
			"function openCompactDialog("
		);
		expect(context).toContain("const view = captureChatView();");
		expect(context).toContain("if (!chatViewIsCurrent(view)) return;");

		const queued = body(
			desktop,
			"async function resyncQueuedTurn(",
			"// --- interactive clarifying"
		);
		expect(queued).toContain("const view = captureChatView();");
		expect(queued).toContain("api.getActiveTurn(view.conversation)");
		expect(queued).toContain("if (!chatViewIsCurrent(view)) return;");

		const next = body(desktop, "async function proposeNext(", "// Resync (R3 fix");
		expect(next).toContain('const view = captureChatView(m?.name || "");');
		expect(next).toContain("api.proposeNextAction(view.conversation, step)");
		expect(next).toContain("await resyncPendingConfirmations(view.conversation)");
		expect(next).toContain("nextBusyKey.value === view.token");
	});

	it("keeps deferred controls and render callbacks inside their original view", () => {
		const compact = body(desktop, "async function runCompact(", "// Compact token count");
		expect(compact).toContain("const view = captureChatView();");
		expect(compact).toContain("api.compactConversation(view.conversation, hint)");
		expect(compact).toContain("if (!chatViewIsCurrent(view)) return;");

		const retry = body(desktop, "async function retry(", "function resendFailed(");
		expect(retry).toContain("const view = captureChatView(messageId);");
		expect(retry).toContain("!messageActionIsCurrent(view)");

		const newChat = body(desktop, "async function newChat(", "// Welcome cards");
		expect(newChat).toContain("const view = captureChatView();");
		expect(newChat).toContain("const newView = captureChatView();");
		expect(newChat).toContain("if (!chatViewIsCurrent(view)) return;");

		const model = body(
			desktop,
			"async function selectModel(",
			"async function selectThinking("
		);
		expect(model).toContain("api.setConversationModel(view.conversation, m)");
		expect(model).toContain("chatViewIsCurrent(view)");

		const queued = body(desktop, "async function cancelQueued(", "// Poll-on-focus");
		expect(queued).toContain('const view = captureChatView(q?.run_id || "");');
		expect(queued).toContain("if (!queuedIsCurrent()) return;");

		const older = body(desktop, "function armOlderNextFrame(", "function holdFromBottom(");
		expect(older).toContain("if (chatViewIsCurrent(view)) _olderArmed = true;");

		const mermaid = body(desktop, "async function processMermaid(", "// Drop a hover");
		expect(mermaid).toContain("async function processMermaid(view = captureChatView())");
		expect(mermaid).toContain("if (chatViewIsCurrent(view)) scrollBottomIfPinned();");
	});

	it("binds direct draft actions to the loaded view, message, and action identity", () => {
		const action = body(
			desktop,
			"function captureDraftActionView(",
			"const confirmFor = computed"
		);
		expect(action).toContain("captureChatView(actionKey)");
		expect(action).toContain("actionFor.value === view.messageKey");
		expect(action).toContain("draftActionKey(action) === view.actionKey");
		expect(desktop).not.toContain("watch(_rawActionFor");

		const panel = body(desktop, "async function openDraftPanel(", "function addDraftRow");
		expect(panel).toContain("if (!draftActionViewIsCurrent(view)) return false;");

		const summary = body(desktop, "async function ensureActionSummary(", "// A create card");
		expect(summary).toContain("summaryState.value.messageKey !== messageKey");
		expect(summary).toContain("!draftActionViewIsCurrent(view)");

		const apply = body(desktop, "async function applyDraft(", "// SUX-3/SUXI-2:");
		expect(apply).toContain("const draftStillCurrent = () =>");
		expect(apply).toContain("conversation: view.conversation");
		expect(apply).toContain("if (!draftStillCurrent()) return;");
		expect(apply).toContain("await loadConversation(view.conversation)");

		const load = body(desktop, "async function loadConversation(", "// Render any ```mermaid");
		expect(load).toContain("directActionViewGeneration.value = view.generation;");
		expect(desktop).toContain("function resetDraftActionState()");
		expect(desktop).toContain("parkedDrafts.value = new Map();");
	});

	it("does not let a stale direct confirmation clear a later view's same token", () => {
		for (const [head, next] of [
			["async function confirmPending(", "// Approve & run"],
			["async function approveAndRunPending(", "// Resolution shared"],
			["async function discardPending(", "// Next step offered"],
		]) {
			const action = body(desktop, head, next);
			const finallyGuard = action.lastIndexOf("if (chatViewIsCurrent(view)) {");
			const clear = action.lastIndexOf("inflightTokens.delete(token);");
			expect(finallyGuard).toBeGreaterThan(-1);
			expect(clear).toBeGreaterThan(finallyGuard);
		}

		for (const [head, next] of [
			["async function approve(", "async function dismiss("],
			["async function dismiss(", "// ── run state"],
		]) {
			const action = body(dashboard, head, next);
			const finallyGuard = action.lastIndexOf("if (dashboardViewIsCurrent(view)) {");
			const clear = action.lastIndexOf("pendingCardInFlight.delete(view.token);");
			expect(finallyGuard).toBeGreaterThan(-1);
			expect(clear).toBeGreaterThan(finallyGuard);
		}
	});

	it("does not let a stale dashboard thumbnail route from a new chat view", () => {
		expect(desktop).toMatch(
			/v-else-if="\s*hasCurrentMessageSnapshot\s*&&\s*canPromoteDashCanvas\(cv\)\s*"/
		);
		const open = body(desktop, "async function openInDashboards(", "// Lazily fetch");
		expect(open).toContain('const view = captureChatView(m?.name || "");');
		expect(open).toContain("!messageActionIsCurrent(view)");
		expect(open).toContain("!canPromoteDashCanvas(cv)");
		expect(open).toContain("dashboardForConversation(view.conversation)");
		expect(open.match(/!messageActionIsCurrent\(view\)/g)).toHaveLength(2);
		expect(open).toContain("conversation: view.conversation");
		expect(open).toContain("artifactSourceIsCurrent(source)");

		const artifact = body(desktop, "async function openArtifact(", "// --- Per-document PDF");
		expect(artifact).toContain('const view = captureChatView(m?.name || "");');
		expect(artifact).toContain("if (!messageActionIsCurrent(view)) return;");
		expect(artifact).toContain("artifact.value !== loading");
		expect(artifact).toContain("{ m, cv, url, conv, view, kind: t }");
		expect(desktop).toContain("function artifactSourceIsCurrent(source)");
		expect(desktop).toContain('@click="openInDashboards(artifact.m, artifact.cv, artifact)"');
	});

	it("keeps feedback and tool-detail controls in their accepted message snapshot", () => {
		expect(desktop).toContain("feedbackFor.value = null;");
		expect(desktop).toContain("feedbackView.value = null;");
		expect(desktop).toContain('v-if="feedbackViewIsCurrent(m)"');
		expect(desktop).toContain("onFeedbackRate($event, feedbackViewFor(m))");
		expect(desktop).toContain("onFeedbackClose(feedbackViewFor(m))");

		const rate = body(desktop, "function onFeedbackRate(", "function onFeedbackClose(");
		expect(rate).toContain("feedbackView.value !== view");
		expect(rate).toContain("!messageActionIsCurrent(view)");
		expect(rate).toContain("api.submitFeedback(view.token");

		const tool = body(desktop, "function toggleTool(", "function toolShown(");
		expect(tool).toContain("messageActionIsCurrent(captureChatView(name))");
		expect(tool).toContain("const view = captureChatView(name);");
		expect(tool).toContain("!messageActionIsCurrent(view)");
		expect(tool).toContain("const detailViewIsCurrent = () => messageActionIsCurrent(view);");
		expect(desktop).toContain(':disabled="!hasCurrentMessageSnapshot"');
		expect(desktop).toMatch(
			/v-if="\s*hasCurrentMessageSnapshot\s*&&\s*toolOpen\[item\.row\.name\]\s*"/
		);
	});
});
