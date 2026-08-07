<template>
	<div class="flex h-full flex-col overflow-hidden bg-surface-white">
		<!-- header: title + New chat / Open in chat -->
		<div class="flex min-h-[56px] shrink-0 items-center justify-between gap-2 border-b px-4">
			<div class="flex min-w-0 flex-col gap-0.5">
				<span class="text-base font-semibold text-ink-gray-9">Describe a dashboard</span>
				<span class="text-p-sm text-ink-gray-6"
					>{{ agentName }} draws it on the canvas</span
				>
			</div>
			<div class="flex shrink-0 items-center gap-1">
				<Button
					v-if="conversation"
					variant="ghost"
					label="Open in chat"
					iconLeft="message-circle"
					@click="router.push('/c/' + conversation)"
				/>
				<Button variant="ghost" label="New chat" iconLeft="plus" @click="newChat" />
			</div>
		</div>

		<!-- transcript -->
		<div ref="scroller" class="min-h-0 flex-1 overflow-y-auto px-4 py-4">
			<div v-if="loadingTranscript && !bubbles.length" class="flex justify-center py-8">
				<JvSpinner />
			</div>

			<!-- empty state: nudge toward the natural-language flow -->
			<div
				v-else-if="!bubbles.length && !runActive"
				class="flex h-full flex-col items-center justify-center gap-3 px-2 text-center"
			>
				<FeatherIcon name="bar-chart-2" class="size-7.5 text-ink-gray-5" />
				<div class="flex flex-col items-center gap-1">
					<span class="text-base font-medium text-ink-gray-8">Describe a dashboard</span>
					<span class="text-p-sm text-ink-gray-6">
						e.g. "Monthly sales by territory with a top-customers table"
					</span>
				</div>
			</div>

			<div v-else class="flex flex-col gap-3">
				<template v-for="m in bubbles" :key="m.name">
					<!-- user: right-aligned surface-gray bubble -->
					<div v-if="m.role === 'user'" class="flex justify-end">
						<div
							class="max-w-[85%] whitespace-pre-wrap rounded-lg bg-surface-gray-2 px-3 py-2 text-base text-ink-gray-8"
						>
							{{ m.content }}
						</div>
					</div>
					<!-- assistant error: inline red note (ChatView semantics, compact) -->
					<div v-else-if="m.error" class="flex">
						<div class="max-w-[95%] text-sm text-ink-red-4">
							{{ m.content || "That didn't go through. Try again." }}
						</div>
					</div>
					<!-- assistant: markdown, same renderer + prose classes as the
					     Approvals/Agents surfaces (renderMarkdown escapes HTML first) -->
					<div v-else class="flex flex-col">
						<div
							class="prose prose-sm min-w-0 max-w-none text-ink-gray-8"
							v-html="renderBubble(m.content)"
						/>
						<!-- clarifying questions the agent asked before building: the
						     SAME option cards the main chat renders. paletteVars is
						     bound here because the jv-* tokens AskCard styles with are
						     not global (SkillDetail precedent). -->
						<AskCard
							v-if="askFor === m.name && activeAsk"
							:key="m.name"
							:spec="activeAsk"
							:style="paletteVars"
							@submit="sendText"
						/>
					</div>
				</template>

				<!-- thinking indicator: subtle three-dot pulse while a run is active -->
				<div
					v-if="runActive"
					role="status"
					aria-live="polite"
					class="flex items-center gap-2 pt-1"
				>
					<span class="flex gap-1" aria-hidden="true">
						<span
							v-for="i in 3"
							:key="i"
							class="size-1.5 rounded-full bg-surface-gray-5 motion-safe:animate-pulse"
							:style="{ animationDelay: (i - 1) * 0.18 + 's' }"
						/>
					</span>
					<span class="text-xs text-ink-gray-5">Thinking…</span>
				</div>
			</div>
		</div>

		<!-- parked ERP-write confirmations (create/update Jarvis Dashboard…):
		     rendered in-pane so a chat-driven save never dead-ends into the
		     full chat view just to click Approve -->
		<div v-if="pendingCards.length" class="flex shrink-0 flex-col gap-2 border-t px-4 py-3">
			<div
				v-for="pa in pendingCards"
				:key="pa.token"
				class="flex flex-col gap-2 rounded-md p-3 ring-1 ring-outline-gray-modals"
			>
				<!-- human headline (+ detail line from the dry-run preview), never
				     the raw "create_doc doctype=…" tool string -->
				<div class="flex min-w-0 flex-col gap-0.5">
					<span class="text-sm font-medium text-ink-gray-8">{{ cardTitle(pa) }}</span>
					<span v-if="cardMeta(pa)" class="text-xs text-ink-gray-6">{{
						cardMeta(pa)
					}}</span>
				</div>
				<div class="flex items-center gap-2">
					<Button
						variant="solid"
						label="Approve"
						:loading="pa.busy"
						@click="approve(pa)"
					/>
					<Button
						variant="ghost"
						label="Dismiss"
						:disabled="pa.busy"
						@click="dismiss(pa)"
					/>
				</div>
			</div>
		</div>

		<!-- composer: autosizing textarea + voice + send.
		     A RAW textarea with v-model, per Composer.vue's documented pattern:
		     v-model (not :value + a manual emit) so Vue's own vModelText
		     directive keeps handling IME composition. And the box is NEVER
		     disabled: a focused, dirty textarea that is programmatically
		     disabled gets blurred by the browser, which fires `change` — the
		     frappe-ui Textarea this used to be listens on `change` too and
		     re-emitted the PRE-CLEAR value, restoring the message we had just
		     sent. Only the send button and send() are gated on `sending`. -->
		<div class="shrink-0 border-t px-4 py-3">
			<textarea
				ref="box"
				v-model="draft"
				rows="2"
				placeholder="Describe the dashboard…"
				class="block w-full resize-none rounded border border-transparent bg-surface-gray-2 px-2 py-1.5 text-base text-ink-gray-8 transition-colors placeholder-ink-gray-4 hover:border-outline-gray-modals hover:bg-surface-gray-3 focus:border-outline-gray-4 focus:bg-surface-white focus:shadow-sm focus:outline-none focus:ring-0 focus-visible:ring-2 focus-visible:ring-outline-gray-3"
				@keydown="onKeydown"
				@input="autoGrow"
			/>
			<div class="mt-2 flex items-center justify-between gap-2">
				<div class="flex items-center gap-1.5">
					<VoiceRecorder v-if="caps.stt_enabled" compact @transcript="onTranscript" />
					<!-- explicit data-mode: Auto leaves it to the opening interview
					     (the agent asks before the first build); Static bakes the
					     numbers in (one-time report); Live declares view-time sources.
					     TabButtons = real radio-group semantics + the raised-chip look,
					     so the active option isn't a second solid next to Send. -->
					<span
						class="ml-1 text-xs text-ink-gray-5"
						title="How this dashboard gets its data. Auto: I'll ask you before the first build."
					>
						Data
					</span>
					<TabButtons
						:buttons="DATA_MODES"
						:model-value="dataMode"
						@update:model-value="(v) => (dataMode = v || 'auto')"
					/>
				</div>
				<Button
					variant="solid"
					label="Send"
					:disabled="!draft.trim() || sending"
					:loading="sending"
					@click="send"
				/>
			</div>
		</div>
	</div>
</template>

<script setup>
// DashboardChatPane - the embedded assistant chat on the Dashboards builder
// tab (bottom pane). Dashboards is the only feature that keeps an embedded chat
// pane; the Triggers one it was originally forked from has been removed in
// favour of managing triggers from the list UI. State machine:
//   conversation id  → useStorage("jarvis-dash-conv-<user>") - persisted per
//                      user; "" means no conversation yet.
//   first send       → send_message(conversation:"", context {"page":"dashboards"})
//                      creates/focuses one server-side; the returned
//                      conversation_id is stored.
//   mount w/ stored  → load its transcript; a 404 (deleted chat) silently
//                      clears storage and starts fresh.
//   realtime         → "jarvis:event" frames for OUR conversation schedule a
//                      debounced (300ms) get_conversation refetch; run:start
//                      raises run-active, run:end / run:error clear it.
//                      kind==="canvas" frames for our conversation bubble up
//                      as emit("canvas", {message_id, items}) - the page pulls
//                      the html artifact onto the canvas pane.
//   no socket        → (?nosocket / headless QA) a bounded refetch ladder after
//                      each send stands in for the realtime frames.
import { ref, computed, watch, nextTick, inject, onMounted, onBeforeUnmount } from "vue";
import { useRouter } from "vue-router";
import { useStorage } from "@vueuse/core";
import { Button, FeatherIcon, TabButtons, toast } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import VoiceRecorder from "@/components/VoiceRecorder.vue";
import AskCard from "@/components/chat/AskCard.vue";
import { renderMarkdown } from "@/markdown";
import { parseAsk } from "@/lib/chatAsk";
import { builderCanvasFrame } from "@/lib/dashboardRestore";
import { useJarvisTheme } from "@/theme";
import { session } from "@/data/session";
import { sendDashboardChat, getDashboardConversation } from "@/api/dashboards";
import { listPendingConfirmations, confirmTool, dismissTool } from "@/api";
import { agentName } from "@/branding";
import { errHtml } from "@/lib/errors";

// get_dashboards_caps payload (creatable_scopes/manageable_roles feed the save
// dialog; stt_enabled - when the backend sends it - gates the mic)
const props = defineProps({
	caps: { type: Object, default: () => ({}) },
	// the selected theme key (lowercase) — forwarded in the send context so the
	// agent designs FOR that theme (the skill injects its token+recipe cheatsheet).
	theme: { type: String, default: "jarvis" },
	// when revising a saved dashboard, its name — forwarded in the send context
	// so the agent knows which dashboard it is iterating on ("" = a new one).
	editingName: { type: String, default: "" },
});

// canvas: {message_id, items[, restore]} for a canvas frame on our conversation
// (restore = replayed from the loaded transcript, not a live socket frame);
// activity: a run ended (the page may refresh lists); reset: New chat clicked.
const emit = defineEmits(["canvas", "activity", "reset"]);

const router = useRouter();
const socket = inject("$socket", null);
// AskCard styles with the jv-* tokens, which this frappe-ui page does not bind.
const { paletteVars } = useJarvisTheme();

// ── conversation persistence (per user, ChatComposer's namespacing idiom) ────
const conversation = useStorage(`jarvis-dash-conv-${session.user || "anon"}`, "");
// The message whose canvas the BUILDER last rendered - the page stamps it, this
// pane replays exactly that one. Scanning the transcript for "the newest html"
// instead would hand the builder an artifact main chat produced in the same
// thread ("Open in chat" shares it) and arm Save over it.
const canvasMsg = useStorage(`jarvis-dash-canvasmsg-${session.user || "anon"}`, "");

// ── transcript ────────────────────────────────────────────────────────────────
const messages = ref([]);
const loadingTranscript = ref(false);
const scroller = ref(null);

// user/assistant rows with visible content only (tool rows and internal
// chatter stay out of this compact pane)
const bubbles = computed(() =>
	messages.value.filter(
		(m) => (m.role === "user" || m.role === "assistant") && String(m.content || "").trim()
	)
);

// ChatView's stripBlocks, minimal subset: internal fenced blocks (actions,
// confirms, cards…) never render as raw fences in the pane. `jarvis-ask` is
// stripped from the PROSE here too, but unlike the others it is not discarded:
// <AskCard> renders it below the bubble (see activeAsk), so the clarifying
// questions the agent asks before building are answerable in this pane.
function stripBlocks(text) {
	return (text || "")
		.replace(/```jarvis-action[ \t]*\n[\s\S]*?```/g, "")
		.replace(/```confirm[ \t]*\n[\s\S]*?```/g, "")
		.replace(/```jarvis-ask[ \t]*\n[\s\S]*?```/g, "")
		.replace(/```jarvis-cards[ \t]*\n[\s\S]*?```/g, "")
		.replace(/```jarvis-skill[ \t]*\n[\s\S]*?```/g, "")
		.replace(/```jarvis-macro[ \t]*\n[\s\S]*?```/g, "")
		.replace(/```jarvis-chart[ \t]*\n[\s\S]*?```/g, "")
		.replace(/\n{3,}/g, "\n\n")
		.trim();
}
function renderBubble(text) {
	return renderMarkdown(stripBlocks(text));
}

// ── clarifying questions (ChatView's rule: the card lives on the LAST
// assistant message only, so a stale ask from three turns ago isn't answerable)
const lastAssistant = computed(() => {
	const rows = bubbles.value;
	for (let i = rows.length - 1; i >= 0; i--) {
		if (rows[i].role === "assistant" && !rows[i].error) return rows[i];
	}
	return null;
});
const activeAsk = computed(() =>
	lastAssistant.value ? parseAsk(lastAssistant.value.content) : null
);
const askFor = computed(() => (activeAsk.value ? lastAssistant.value.name : null));

function scrollBottom() {
	const el = scroller.value;
	if (el) el.scrollTop = el.scrollHeight;
}

function isGone(e) {
	return !!(
		e &&
		(e.status === 404 ||
			e.exc_type === "DoesNotExistError" ||
			e.status === 403 ||
			e.exc_type === "PermissionError")
	);
}

// monotonic request id - stale refetches dropped (useListPage idiom)
let loadReq = 0;
async function loadTranscript({ initial = false } = {}) {
	const id = ++loadReq;
	if (!conversation.value) return;
	if (initial) loadingTranscript.value = true;
	try {
		const d = (await getDashboardConversation(conversation.value)) || {};
		if (id !== loadReq) return;
		messages.value = d.messages || [];
		// Navigating away unmounts the builder and drops its canvas html, but the
		// transcript we just reloaded still carries every artifact the agent drew
		// (get_conversation returns each message's parsed `canvas` list). Replay
		// the one the BUILDER last rendered so coming back is lossless — the
		// page ignores it when a canvas is already on screen.
		const frame = builderCanvasFrame(messages.value, canvasMsg.value);
		if (frame) emit("canvas", { ...frame, restore: true });
		nextTick(scrollBottom);
	} catch (e) {
		if (id !== loadReq) return;
		if (isGone(e)) {
			// the stored conversation was deleted (or reassigned) - start fresh
			conversation.value = "";
			messages.value = [];
			runActive.value = false;
		}
		// transient errors keep the last-good transcript; the next frame retries
	} finally {
		if (id === loadReq) loadingTranscript.value = false;
	}
}

// debounced refetch driven by realtime frames
let refetchTimer = null;
function scheduleRefetch() {
	clearTimeout(refetchTimer);
	refetchTimer = setTimeout(() => {
		loadTranscript();
		refreshPending();
	}, 300);
}

// ── parked confirmations (gated ERP writes park for human approval) ──────────
const pendingCards = ref([]);

async function refreshPending() {
	if (!conversation.value) {
		pendingCards.value = [];
		return;
	}
	try {
		const env = await listPendingConfirmations(conversation.value);
		if (env && env.ok === false) return;
		const items = (env && env.data && env.data.pending) || [];
		// keep in-flight busy flags across refreshes
		const busy = new Set(pendingCards.value.filter((c) => c.busy).map((c) => c.token));
		pendingCards.value = items.map((it) => ({ ...it, busy: busy.has(it.token) }));
	} catch {
		// transient - the next frame retries
	}
}

function removeCard(token) {
	pendingCards.value = pendingCards.value.filter((c) => c.token !== token);
}

function confirmationStorageUnavailable(response) {
	const type = response && response.error && response.error.type;
	return type === "ConfirmationUnavailableError" || type === "ConfirmationOutcomeUnknownError";
}

// ── pending-card copy ─────────────────────────────────────────────────────────
// A card only carries what list_pending_confirmations returns (token, tool,
// preview, summary, conversation, run_id). Rich path: a sandboxed dry-run
// preview (preview.would = the doc exactly as it would save) names the
// dashboard and its scope. Fallback: clean the _describe_call line
// ("create_doc doctype=…" → "Create a …") so the raw tool string never shows.
const CARD_VERBS = {
	create_doc: "Create",
	update_doc: "Update",
	delete_doc: "Delete",
	submit_doc: "Submit",
	cancel_doc: "Cancel",
};
const SCOPE_LABELS = { User: "Private", Role: "Shared with a role", Org: "Everyone" };
function previewDoc(pa) {
	const w = pa && pa.preview && pa.preview.would;
	return w && typeof w === "object" && !Array.isArray(w) ? w : null;
}
// _describe_call joins "key=value" pairs with spaces and values may hold
// spaces ("Jarvis Dashboard") - a value runs until the next key or the end.
// Anchored on a word boundary so "name=" never matches inside "docname=".
function summaryArg(s, key) {
	const m = new RegExp("(?:^|\\s)" + key + "=(.*?)(?=\\s+[a-z_]+=|$)").exec(s || "");
	return m ? m[1].trim() : "";
}
function cardTitle(pa) {
	const verb = CARD_VERBS[pa.tool];
	const doc = previewDoc(pa);
	if (doc && doc.doctype === "Jarvis Dashboard") {
		return `${verb || "Save"} dashboard: ${doc.dashboard_title || doc.name || "(unnamed)"}`;
	}
	if (doc && doc.doctype && verb) {
		const name = verb === "Create" ? "" : doc.name || "";
		return `${verb} ${doc.doctype}${name ? " · " + name : ""}`;
	}
	const raw = pa.summary || (pa.preview && pa.preview.summary) || "";
	const m = /^(create|update|delete|submit|cancel)_doc\b/.exec(raw);
	if (m) {
		const v = CARD_VERBS[m[1] + "_doc"];
		const dt = summaryArg(raw, "doctype");
		const nm = summaryArg(raw, "name") || summaryArg(raw, "docname");
		if (dt === "Jarvis Dashboard") return `${v} a dashboard${nm ? ": " + nm : ""}`;
		if (dt) return `${v} ${dt}${nm ? " · " + nm : ""}`;
	}
	return raw || "Confirm this action";
}
function cardMeta(pa) {
	const doc = previewDoc(pa);
	if (!doc || doc.doctype !== "Jarvis Dashboard") return "";
	const scope =
		doc.scope === "Role" && doc.target_role
			? `Shared with ${doc.target_role}`
			: SCOPE_LABELS[doc.scope] || "";
	return [doc.dashboard_type, scope].filter(Boolean).join(" · ");
}

async function approve(pa) {
	pa.busy = true;
	let keepCard = false;
	try {
		const r = await confirmTool(pa.token, conversation.value);
		if (r && r.ok === false) {
			keepCard = confirmationStorageUnavailable(r);
			toast.error(
				keepCard
					? r.error.message
					: "Couldn't confirm — it may have expired. Ask again in the chat."
			);
		}
	} catch (e) {
		keepCard = true;
		toast.error(errHtml(e));
	} finally {
		if (keepCard) pa.busy = false;
		else removeCard(pa.token);
		// the parked run resumes server-side after a confirm: refresh both the
		// transcript (receipt chip) and anything list-shaped upstream
		scheduleRefetch();
		emit("activity");
	}
}

async function dismiss(pa) {
	pa.busy = true;
	let keepCard = false;
	try {
		const r = await dismissTool(pa.token, conversation.value);
		if (r && r.ok === false) {
			keepCard = confirmationStorageUnavailable(r);
			toast.error((r.error && r.error.message) || "Could not discard this confirmation.");
		}
	} catch (e) {
		keepCard = true;
		toast.error(errHtml(e));
	} finally {
		if (keepCard) pa.busy = false;
		else removeCard(pa.token);
		scheduleRefetch();
	}
}

// ── run state + composer ──────────────────────────────────────────────────────
const runActive = ref(false);
const sending = ref(false);
const draft = ref("");
const box = ref(null);

// Explicit data-mode toggle (goal requirement): "auto" = one of the questions
// the agent asks before the first build (it no longer guesses from wording),
// "static" = baked one-time report, "live" = declared view-time sources.
// Persisted per user so the choice survives page hops. ("auto" rather than ""
// so reka-ui's RadioGroup has a real value to select.) The API wrapper only
// forwards static/live; auto sends no data_mode.
const DATA_MODES = [
	{ label: "Auto", value: "auto" },
	{ label: "Static", value: "static" },
	{ label: "Live", value: "live" },
];
const dataMode = useStorage(`jarvis-dash-datamode-${session.user || "anon"}`, "auto");

function autoGrow() {
	const ta = box.value;
	if (!ta) return;
	ta.style.height = "auto";
	ta.style.height = Math.min(ta.scrollHeight, 180) + "px";
}
// Programmatic changes (the send that clears the box, dictation, the save
// dialog's hand-off) only reach the DOM on the next flush — flush:"post" so the
// textarea's value is already written when we measure scrollHeight. Typed input
// grows synchronously in the @input handler so the box never lags the caret.
watch(draft, autoGrow, { flush: "post" });

function onKeydown(e) {
	// An Enter that CONFIRMS an IME candidate (Japanese/Chinese/Korean) belongs
	// to the composition, not to us - sending on it posts the half-converted
	// reading. keyCode 229 is the pre-`isComposing` browsers' version of the
	// same signal.
	if (e.isComposing || e.keyCode === 229) return;
	// Enter sends, Shift+Enter keeps the newline
	if (e.key === "Enter" && !e.shiftKey) {
		e.preventDefault();
		send();
	}
}

function onTranscript(text) {
	// dictation appends to any typed draft (ChatComposer precedent)
	const cur = draft.value;
	draft.value = cur.trim() ? cur.replace(/\s+$/, "") + " " + text : text;
}

// send() can be handed a DIFFERENT conversation than the one it posted to (the
// stored one was deleted or reassigned server-side, so send_message opened a
// fresh one). That repoint is OURS: the run it just started is the one on
// screen, so the follow watcher below must not tear its Thinking indicator down.
let ownRepoint = false;

async function send() {
	const text = draft.value.trim();
	if (!text || sending.value) return;
	sending.value = true;
	draft.value = "";
	// optimistic user bubble - reconciled by the next transcript refetch
	const tmpName = `tmp-${Date.now()}`;
	messages.value = [...messages.value, { name: tmpName, role: "user", content: text }];
	nextTick(scrollBottom);
	try {
		const r =
			(await sendDashboardChat(
				conversation.value,
				text,
				dataMode.value,
				props.editingName,
				props.theme
			)) || {};
		if (r.ok === false) {
			// rejected (single-flight guard / usage cap) - nothing persisted
			messages.value = messages.value.filter((m) => m.name !== tmpName);
			if (!draft.value) draft.value = text;
			toast.error(r.reason || "Couldn't send your message.");
			return;
		}
		if (r.conversation_id && r.conversation_id !== conversation.value) {
			ownRepoint = true;
			conversation.value = r.conversation_id;
		}
		runActive.value = true;
		nextTick(scrollBottom);
		if (!socket) startNoSocketLadder();
	} catch (e) {
		messages.value = messages.value.filter((m) => m.name !== tmpName);
		if (!draft.value) draft.value = text;
		toast.error(errHtml(e));
	} finally {
		sending.value = false;
	}
}

// "New chat" ASKS the page rather than acting: the page owns the canvas and
// puts up the discard confirm, then calls resetChat() back through the exposed
// handle if the user goes ahead. Resetting here first would leave a cancelled
// confirm with an emptied chat next to a canvas that survived.
function newChat() {
	emit("reset");
}

// Drop everything that belongs to the thread on screen, leaving the pointer
// alone (the conversation watcher below re-points it). `keepRun` is for the
// one case where the thread moved but the run did not: our own send.
function clearThread({ keepRun = false } = {}) {
	loadReq++; // drop any in-flight transcript load
	messages.value = [];
	pendingCards.value = [];
	if (!keepRun) runActive.value = false;
	draft.value = "";
}

function resetChat() {
	clearThread();
	conversation.value = "";
}

// The page re-points the sticky conversation slot (opening a saved dashboard
// for editing resumes the thread that built it). Follow it, or the transcript
// on screen belongs to a different dashboard than the one the next send goes
// to. Guarded on `prev`: "" -> id is our own first send, already reconciled.
watch(conversation, (id, prev) => {
	if (!prev || id === prev) return;
	const own = ownRepoint;
	ownRepoint = false;
	clearThread({ keepRun: own });
	if (id) {
		loadTranscript({ initial: true });
		refreshPending();
	}
});

// Post a message into this pane programmatically (the save dialog's "Ask the
// assistant to fix these" hand-off). Ignored while a send is already in flight.
function sendText(text) {
	const t = String(text || "").trim();
	if (!t || sending.value) return;
	draft.value = t;
	send();
}
// Re-issue the transcript's restore frame. The page holds restores off while a
// ?chat=&canvas= promotion is in flight (an explicit deep-link owns the canvas);
// if that promotion is then declined or fails, the frame this pane already
// emitted has been dropped, and the builder would sit empty until some later
// transcript load. Declining must leave the builder as it was, so ask again.
function restoreCanvas() {
	const frame = builderCanvasFrame(messages.value, canvasMsg.value);
	if (frame) emit("canvas", { ...frame, restore: true });
}
defineExpose({ resetChat, sendText, restoreCanvas });

// ── realtime ──────────────────────────────────────────────────────────────────
function onEvent(p) {
	if (!p || !p.kind) return;
	// turn frames carry conversation_id; action:pending frames carry conversation
	const frameConv = p.conversation_id || p.conversation;
	if (!conversation.value || frameConv !== conversation.value) return;
	// the agent drew/updated an artifact this turn - the page pulls the html
	// item onto the canvas pane
	if (p.kind === "canvas") {
		emit("canvas", { message_id: p.message_id, items: p.items });
		return;
	}
	// any frame for OUR conversation refreshes the transcript (debounced)
	scheduleRefetch();
	switch (p.kind) {
		case "run:start":
			runActive.value = true;
			break;
		case "run:end":
			runActive.value = false;
			emit("activity");
			break;
		case "run:error":
			runActive.value = false;
			break;
	}
}

// no-socket fallback (?nosocket / headless QA): a bounded refetch ladder after
// each send; run-active clears when the reply settles (or at the ladder's end).
let ladderTimers = [];
function startNoSocketLadder() {
	ladderTimers.forEach(clearTimeout);
	ladderTimers = [3000, 8000, 15000, 30000, 60000].map((ms, i, arr) =>
		setTimeout(async () => {
			await loadTranscript();
			await refreshPending();
			const last = bubbles.value[bubbles.value.length - 1];
			const settled = last && last.role === "assistant" && !last.streaming;
			if (settled || i === arr.length - 1) {
				runActive.value = false;
				emit("activity");
			}
		}, ms)
	);
}

onMounted(() => {
	if (conversation.value) {
		loadTranscript({ initial: true });
		refreshPending();
	}
	socket && socket.on && socket.on("jarvis:event", onEvent);
});
onBeforeUnmount(() => {
	socket && socket.off && socket.off("jarvis:event", onEvent);
	clearTimeout(refetchTimer);
	ladderTimers.forEach(clearTimeout);
});
</script>
