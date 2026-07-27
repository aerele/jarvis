<template>
	<div class="flex h-full flex-col overflow-hidden bg-surface-white">
		<!-- header: title + New chat / Open in chat -->
		<div class="flex shrink-0 items-start justify-between gap-2 border-b px-4 py-3">
			<div class="flex min-w-0 flex-col gap-0.5">
				<span class="text-base font-semibold text-ink-gray-9">Create with chat</span>
				<span class="text-p-sm text-ink-gray-6"
					>Describe the automation; voice works too</span
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

		<!-- non-admin note: the composer below is disabled for this reason (the
		     agent would refuse server-side anyway - don't burn the round trip) -->
		<div v-if="!caps.can_manage" class="shrink-0 px-4 pt-3">
			<div class="flex items-start gap-2 rounded-md p-2 ring-1 ring-outline-gray-modals">
				<FeatherIcon name="info" class="mt-0.5 size-4 shrink-0 text-ink-gray-5" />
				<span class="text-xs text-ink-gray-7">
					Creating triggers requires the Jarvis Admin role, so this chat is disabled.
				</span>
			</div>
		</div>

		<!-- transcript -->
		<div ref="scroller" class="min-h-0 flex-1 overflow-y-auto px-4 py-4">
			<div v-if="loadingTranscript && !bubbles.length" class="flex justify-center py-8">
				<LoadingIndicator class="size-5 text-ink-gray-5" />
			</div>

			<!-- empty state: nudge toward the natural-language flow -->
			<div
				v-else-if="!bubbles.length && !runActive"
				class="flex h-full flex-col items-center justify-center gap-3 px-2 text-center"
			>
				<FeatherIcon name="git-branch" class="size-7.5 text-ink-gray-5" />
				<div class="flex flex-col items-center gap-1">
					<span class="text-base font-medium text-ink-gray-8"
						>Describe an automation</span
					>
					<span class="text-p-sm text-ink-gray-6">
						e.g. "Warn me when a Sales Invoice over 1 lakh is submitted"
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
						<!-- clarifying questions: the SAME option cards the main chat
						     renders. `jarvis-ask` is a standing capability, so a model
						     that asks here must not leave an empty bubble nobody can
						     answer. paletteVars is bound because the jv-* tokens AskCard
						     styles with are not global (SkillDetail precedent). -->
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

		<!-- parked ERP-write confirmations (create/update Jarvis Trigger…):
		     rendered in-pane so a chat-driven creation never dead-ends into
		     the full chat view just to click Approve -->
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

		<!-- composer: autosizing textarea + voice + send. Fully disabled for
		     non-admins (the note above explains why): the agent refuses the
		     request server-side anyway, so submitting would only burn a slow
		     LLM round trip. That permission lock is the ONLY `disabled` here:
		     a RAW textarea with v-model (Composer.vue's documented pattern -
		     Vue's vModelText keeps handling IME composition), and `sending`
		     gates the send button and send() alone. Disabling a focused, dirty
		     textarea mid-send blurs it, fires `change`, and the frappe-ui
		     Textarea this used to be re-emitted the PRE-CLEAR value, putting
		     the just-sent message back in the box. -->
		<div class="shrink-0 border-t px-4 py-3">
			<textarea
				ref="box"
				v-model="draft"
				rows="2"
				:placeholder="
					caps.can_manage ? 'Describe the trigger…' : 'Requires the Jarvis Admin role'
				"
				:disabled="!caps.can_manage"
				class="block w-full resize-none rounded border border-transparent bg-surface-gray-2 px-2 py-1.5 text-base text-ink-gray-8 transition-colors placeholder-ink-gray-4 hover:border-outline-gray-modals hover:bg-surface-gray-3 focus:border-outline-gray-4 focus:bg-surface-white focus:shadow-sm focus:outline-none focus:ring-0 focus-visible:ring-2 focus-visible:ring-outline-gray-3 disabled:bg-surface-gray-1 disabled:text-ink-gray-5 disabled:placeholder-ink-gray-3"
				@keydown="onKeydown"
				@input="autoGrow"
			/>
			<div class="mt-2 flex items-center justify-between gap-2">
				<div class="flex items-center gap-1.5">
					<VoiceRecorder
						v-if="caps.stt_enabled && caps.can_manage"
						compact
						@transcript="onTranscript"
					/>
				</div>
				<Button
					variant="solid"
					label="Send"
					:disabled="!draft.trim() || sending || !caps.can_manage"
					:loading="sending"
					@click="send"
				/>
			</div>
		</div>
	</div>
</template>

<script setup>
// TriggerChatPane - the embedded assistant chat on the Triggers tab (left
// pane). State machine:
//   conversation id  → useStorage("jarvis-triggers-conv-<user>") - persisted
//                      per user; "" means no conversation yet.
//   first send       → send_message(conversation:"", context {"page":"triggers"})
//                      creates/focuses one server-side; the returned
//                      conversation_id is stored.
//   mount w/ stored  → load its transcript; a 404 (deleted chat) silently
//                      clears storage and starts fresh.
//   realtime         → "jarvis:event" frames for OUR conversation schedule a
//                      debounced (300ms) get_conversation refetch; run:start
//                      raises run-active, run:end / run:error clear it.
//                      run:end and "trigger:changed" also tell the parent to
//                      refresh the triggers list (a trigger may have been
//                      created/edited by the agent).
//   no socket        → (?nosocket / headless QA) a bounded refetch ladder after
//                      each send stands in for the realtime frames.
import { ref, computed, watch, nextTick, inject, onMounted, onBeforeUnmount } from "vue";
import { useRouter } from "vue-router";
import { useStorage } from "@vueuse/core";
import { Button, FeatherIcon, LoadingIndicator, toast } from "frappe-ui";
import VoiceRecorder from "@/components/VoiceRecorder.vue";
import AskCard from "@/components/chat/AskCard.vue";
import { renderMarkdown } from "@/markdown";
import { parseAsk } from "@/lib/chatAsk";
import { useJarvisTheme } from "@/theme";
import { session } from "@/data/session";
import { sendTriggerChat, getTriggerConversation } from "@/api/triggers";
import { listPendingConfirmations, confirmTool, dismissTool } from "@/api";

// get_triggers_caps payload (can_manage gates the note + composer,
// stt_enabled the mic)
const props = defineProps({
	caps: { type: Object, default: () => ({}) },
});

const emit = defineEmits(["activity"]); // parent refreshes the triggers list

const router = useRouter();
const socket = inject("$socket", null);
// AskCard styles with the jv-* tokens, which this frappe-ui page does not bind.
const { paletteVars } = useJarvisTheme();

function errMsg(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}

// ── conversation persistence (per user, ChatComposer's namespacing idiom) ────
const conversation = useStorage(`jarvis-triggers-conv-${session.user || "anon"}`, "");

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
// <AskCard> renders it below the bubble (see activeAsk).
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
		const d = (await getTriggerConversation(conversation.value)) || {};
		if (id !== loadReq) return;
		messages.value = d.messages || [];
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

// ── pending-card copy ─────────────────────────────────────────────────────────
// A card only carries what list_pending_confirmations returns (token, tool,
// preview, summary, conversation, run_id). Rich path: a sandboxed dry-run
// preview (preview.would = the doc exactly as it would save) names the
// trigger and its shape. Fallback: clean the _describe_call line
// ("create_doc doctype=Jarvis Trigger" → "Create a Jarvis Trigger") so the
// raw tool string never shows.
const CARD_VERBS = {
	create_doc: "Create",
	update_doc: "Update",
	delete_doc: "Delete",
	submit_doc: "Submit",
	cancel_doc: "Cancel",
};
function eventLabel(value) {
	const hit = (props.caps.events || []).find((e) => e.value === value);
	return (hit && hit.label) || value || "";
}
function previewDoc(pa) {
	const w = pa && pa.preview && pa.preview.would;
	return w && typeof w === "object" && !Array.isArray(w) ? w : null;
}
// _describe_call joins "key=value" pairs with spaces and values may hold
// spaces ("Jarvis Trigger") - a value runs until the next key or the end.
// Anchored on a word boundary so "name=" never matches inside "docname=".
function summaryArg(s, key) {
	const m = new RegExp("(?:^|\\s)" + key + "=(.*?)(?=\\s+[a-z_]+=|$)").exec(s || "");
	return m ? m[1].trim() : "";
}
function cardTitle(pa) {
	const verb = CARD_VERBS[pa.tool];
	const doc = previewDoc(pa);
	if (doc && doc.doctype === "Jarvis Trigger") {
		return `${verb || "Save"} trigger: ${doc.trigger_name || doc.name || "(unnamed)"}`;
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
		if (dt === "Jarvis Trigger") return `${v} a Jarvis Trigger${nm ? ": " + nm : ""}`;
		if (dt) return `${v} ${dt}${nm ? " · " + nm : ""}`;
	}
	return raw || "Confirm this action";
}
function cardMeta(pa) {
	const doc = previewDoc(pa);
	if (!doc || doc.doctype !== "Jarvis Trigger") return "";
	return [
		doc.target_doctype,
		eventLabel(doc.doc_event),
		doc.action_type ? `${doc.action_type} action` : "",
	]
		.filter(Boolean)
		.join(" · ");
}

async function approve(pa) {
	pa.busy = true;
	try {
		const r = await confirmTool(pa.token, conversation.value);
		if (r && r.ok === false) {
			toast.error("Couldn't confirm — it may have expired. Ask again in the chat.");
		}
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		removeCard(pa.token);
		// the parked run resumes server-side after a confirm: refresh both the
		// transcript (receipt chip) and the triggers list (a row may now exist)
		scheduleRefetch();
		emit("activity");
	}
}

async function dismiss(pa) {
	pa.busy = true;
	try {
		await dismissTool(pa.token, conversation.value);
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		removeCard(pa.token);
		scheduleRefetch();
	}
}

// ── run state + composer ──────────────────────────────────────────────────────
const runActive = ref(false);
const sending = ref(false);
const draft = ref("");
const box = ref(null);

function autoGrow() {
	const ta = box.value;
	if (!ta) return;
	ta.style.height = "auto";
	ta.style.height = Math.min(ta.scrollHeight, 180) + "px";
}
// Programmatic changes (the send that clears the box, dictation) only reach the
// DOM on the next flush - flush:"post" so the textarea's value is already
// written when we measure scrollHeight. Typed input grows synchronously in the
// @input handler so the box never lags the caret.
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

async function send() {
	// non-admins can't reach here through the UI (composer disabled), but the
	// Enter handler still routes through send() - guard it too
	if (!props.caps.can_manage) return;
	const text = draft.value.trim();
	if (!text || sending.value) return;
	sending.value = true;
	draft.value = "";
	// optimistic user bubble - reconciled by the next transcript refetch
	const tmpName = `tmp-${Date.now()}`;
	messages.value = [...messages.value, { name: tmpName, role: "user", content: text }];
	nextTick(scrollBottom);
	try {
		const r = (await sendTriggerChat(conversation.value, text)) || {};
		if (r.ok === false) {
			// rejected (single-flight guard / usage cap) - nothing persisted
			messages.value = messages.value.filter((m) => m.name !== tmpName);
			if (!draft.value) draft.value = text;
			toast.error(r.reason || "Couldn't send your message.");
			return;
		}
		if (r.conversation_id && r.conversation_id !== conversation.value) {
			conversation.value = r.conversation_id;
		}
		runActive.value = true;
		nextTick(scrollBottom);
		if (!socket) startNoSocketLadder();
	} catch (e) {
		messages.value = messages.value.filter((m) => m.name !== tmpName);
		if (!draft.value) draft.value = text;
		toast.error(errMsg(e));
	} finally {
		sending.value = false;
	}
}

// Post a message into this pane programmatically (the AskCard's submitted
// answers). Ignored while a send is already in flight - the card keeps its
// picks, so the user can submit again once the pane is free.
function sendText(text) {
	const t = String(text || "").trim();
	if (!t || sending.value) return;
	draft.value = t;
	send();
}

function newChat() {
	loadReq++; // drop any in-flight transcript load
	conversation.value = "";
	messages.value = [];
	pendingCards.value = [];
	runActive.value = false;
	draft.value = "";
}

// ── realtime ──────────────────────────────────────────────────────────────────
function onEvent(p) {
	if (!p || !p.kind) return;
	// a trigger changed anywhere (agent-created, another admin) → refresh list
	if (p.kind === "trigger:changed") {
		emit("activity");
		return;
	}
	// turn frames carry conversation_id; action:pending frames carry conversation
	const frameConv = p.conversation_id || p.conversation;
	if (!conversation.value || frameConv !== conversation.value) return;
	// any frame for OUR conversation refreshes the transcript (debounced)
	scheduleRefetch();
	switch (p.kind) {
		case "run:start":
			runActive.value = true;
			break;
		case "run:end":
			runActive.value = false;
			// the run may have created/edited a trigger - refresh the list
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
