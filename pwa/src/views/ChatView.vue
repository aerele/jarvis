<script setup>
import {
	computed,
	defineAsyncComponent,
	inject,
	nextTick,
	onMounted,
	onUnmounted,
	ref,
	watch,
} from "vue";
import BrandMark from "../components/BrandMark.vue";
import { agentName } from "@/branding";
import { useRouter } from "vue-router";
// The desktop SPA's renderer — dependency-free, and sharing it means an agent
// reply reads identically on both surfaces.
import { renderMarkdown } from "@shared/markdown.js";
import * as api from "../api";
import { store } from "../store";
import {
	parseAction,
	parseCards,
	parseCharts,
	parseSkillsUsed,
	stripAgentBlocks,
	toolStatus,
} from "../lib/blocks";
import { spanBetween } from "../lib/time";
import ActionCard from "../components/ActionCard.vue";
import ChartCard from "../components/ChartCard.vue";
import Composer from "../components/Composer.vue";
import DecisionCard from "../components/DecisionCard.vue";
import DecisionSheet from "../components/DecisionSheet.vue";
import FilePreviewSheet from "../components/FilePreviewSheet.vue";
import MessageMedia from "../components/MessageMedia.vue";
import RecordCards from "../components/RecordCards.vue";
import Sheet from "../components/Sheet.vue";
import SkillChips from "../components/SkillChips.vue";
import ThinkingIndicator from "../components/ThinkingIndicator.vue";
// Lazy: the voice sheet pulls in the shared audio recorder, and a user who never
// taps the mic should never pay for it.
const VoiceSheet = defineAsyncComponent(() => import("../components/VoiceSheet.vue"));

const props = defineProps({ id: { type: String, default: "" } });
const router = useRouter();
const socket = inject("$socket");

// "new" is a route-only placeholder: the conversation does not exist until the
// first send, which is when the backend creates (or focuses) it and tells us
// its real id.
const convId = ref(props.id === "new" ? "" : props.id);
const conversation = ref(null);
const messages = ref([]);
const input = ref("");
const loading = ref(false);
const sendBusy = ref(false);
const errorBanner = ref("");
const attachments = ref([]);
const pending = ref([]); // parked writes awaiting approval
const settings = ref(null);

// The turn in flight. Held separately from `messages` because it is not durable
// yet: the row exists server-side but its content arrives as a stream.
const live = ref(null); // { runId, messageId, text, tools[] }
// Stop is a UI-level cancel too — the backend may still finish the turn, so
// ignore what comes back rather than letting a "stopped" reply reappear.
const ignoredRuns = ref(new Set());

const decision = ref(null);
const preview = ref(null);
const voiceOpen = ref(false);
const menuOpen = ref(false);
const renaming = ref(false);
const renameText = ref("");
const menuError = ref("");
const starred = ref(false);
const autoApply = ref(false);

const scroller = ref(null);
const composer = ref(null);
// An action card is a live offer, not history: only the newest assistant turn
// may still be applied. Scrolling back to last week's proposal and tapping
// Create would write a record the user has long since moved on from.
const dismissedActions = ref(new Set());

const sending = computed(() => !!live.value || sendBusy.value);
const title = computed(
	() =>
		conversation.value?.title ||
		store.conversations.find((c) => c.name === convId.value)?.title ||
		"New chat"
);
const model = computed(
	() => conversation.value?.model_override || settings.value?.llm_model || ""
);
const micEnabled = computed(() => !!settings.value?.stt_enabled);

// Everything the agent's raw text carries, unpacked once per message. Done here
// rather than in the template: the template would re-run it on every render, and
// markdown + JSON parsing per message per frame is exactly how a chat thread
// starts dropping frames as it grows.
const view = (m) => {
	const content = m.content || "";
	const html = renderMarkdown(stripAgentBlocks(content));
	const cards = parseCards(content);
	const charts = parseCharts(content);
	return {
		html,
		cards,
		charts,
		action: parseAction(content),
		skills: parseSkillsUsed(content),
		took: spanBetween(m.creation, m.modified),
		// A turn can end with nothing to show: the runtime aborts a stalled model
		// call and writes an empty assistant row. With no prose, no cards, no
		// canvas and no error text, the thread would render a blank gap and the
		// user would be left wondering whether anything happened at all.
		// A stop is not a failure: excluded here so a stopped-before-stream turn
		// (content == "") never lands in the "Jarvis didn't return a reply" error
		// branch, telling the user it failed when they are the one who stopped it.
		// The marker itself renders from a SIBLING gated on `stopped`, outside
		// this chain - the exclusion alone would leave a blank gap.
		empty:
			!html &&
			!cards &&
			!charts.length &&
			!parseAction(content) &&
			!m.error &&
			!(m.canvas || []).length &&
			!m.stopped,
	};
};

// ── thread assembly ─────────────────────────────────────────────────────────
// Tool rows BELONG to the assistant turn that ran them. The worker creates the
// assistant placeholder first and appends each tool row after it, so in `seq`
// order the tools trail the answer — render them literally and the thread reads
// backwards ("16" … then "Ran 1 step"). Attach them to the assistant message
// instead (the same rule the desktop SPA's activityByAssistant uses) and show
// the card above the prose: it worked, then it answered.
//
// Each turn therefore renders as ONE card, not one bubble per call — a wall of
// "get_list → completed" is not an answer.
const items = computed(() => {
	const out = [];
	let current = null; // the assistant item tool rows attach to
	for (const m of messages.value) {
		// The streaming row renders from `live`, not from its (empty) stored copy.
		if (
			live.value &&
			(m.name === live.value.messageId || (m.role === "assistant" && m.streaming))
		)
			continue;

		if (m.role === "user") {
			current = null;
			out.push({ type: "user", key: m.name, msg: m });
		} else if (m.role === "tool") {
			// A tool row with no assistant turn to hang off (a recovered or
			// truncated thread) still has to appear — never silently drop it.
			if (!current) {
				current = { type: "assistant", key: m.name, msg: null, view: null, tools: [] };
				out.push(current);
			}
			current.tools.push(m);
		} else {
			current = { type: "assistant", key: m.name, msg: m, view: view(m), tools: [] };
			out.push(current);
		}
	}
	return out;
});

const lastAssistantKey = computed(() => {
	const assistants = items.value.filter((i) => i.type === "assistant" && i.msg);
	return assistants.length ? assistants[assistants.length - 1].key : "";
});

// The write leaves a receipt in the conversation, so reload rather than guess.
function onActionApplied() {
	load();
	store.loadConversations();
}

// ── scrolling ───────────────────────────────────────────────────────────────
function atBottom() {
	const el = scroller.value;
	if (!el) return true;
	return el.scrollHeight - el.scrollTop - el.clientHeight < 120;
}
// Follow the stream only if the user is already at the bottom; yanking them
// down while they scroll back through a long reply is the classic chat sin.
async function scrollToBottom(force = false) {
	const stick = force || atBottom();
	await nextTick();
	if (stick && scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
}

// ── loading ─────────────────────────────────────────────────────────────────
async function load(force = false) {
	if (!convId.value) return;
	loading.value = true;
	try {
		const d = await api.getConversation(convId.value);
		conversation.value = d?.conversation || null;
		messages.value = d?.messages || [];
		autoApply.value = !!d?.conversation?.auto_apply;
		const row = store.conversations.find((c) => c.name === convId.value);
		if (row) starred.value = !!row.starred;
		// A reply still streaming when we (re)opened the chat: restore the busy
		// state from the durable flag rather than assuming the turn is over.
		const last = messages.value[messages.value.length - 1];
		if (last?.role === "assistant" && last.streaming && !live.value) {
			live.value = { runId: "", messageId: last.name, text: last.content || "", tools: [] };
		}
		await scrollToBottom(force);
	} catch (e) {
		console.error("Jarvis PWA: failed to load conversation", e);
	} finally {
		loading.value = false;
	}
}

// Parked writes survive a reload: re-ask rather than leaving an approval the
// user can never reach.
async function loadPending() {
	if (!convId.value) return;
	try {
		const r = await api.listPendingConfirmations(convId.value);
		if (r?.ok && r.data)
			pending.value = r.data.pending.filter((p) => p.conversation === convId.value);
	} catch {
		/* the cards also arrive live; a failed resync is not worth a banner */
	}
}

// ── sending ─────────────────────────────────────────────────────────────────
async function send() {
	const text = input.value.trim();
	const ready = attachments.value.filter((a) => a.file_url);
	if ((!text && !ready.length) || sending.value) return;

	errorBanner.value = "";
	input.value = "";
	composer.value?.reset();
	attachments.value = [];
	sendBusy.value = true;
	// Optimistic user bubble; the server echoes it back on the next load.
	messages.value.push({
		name: `local-${Date.now()}`,
		role: "user",
		content: text,
		optimistic: true,
	});
	await scrollToBottom(true);

	try {
		// No model/effort override here: an existing chat keeps whatever it was
		// started with (the backend falls back to the workspace default). The
		// device's preference applies when a chat is CREATED, on the new-chat
		// screen — same rule as the native app.
		const res = await api.sendMessage(convId.value, text, {
			attachments: ready.map((a) => ({ file_url: a.file_url, file_name: a.name })),
		});
		if (res?.ok === false) {
			sendBusy.value = false;
			messages.value = messages.value.filter((m) => !m.optimistic);
			// A rollout started under this tab; the gate only latches at boot, so
			// reload onto it instead of leaving them retrying.
			if (res.reason === "release_update_required") {
				errorBanner.value = "Jarvis is being updated. Reloading…";
				setTimeout(() => window.location.reload(), 1500);
				return;
			}
			errorBanner.value = res.reason || "Couldn't send that message.";
			return;
		}
		// First send of a brand-new chat: adopt the id the backend just created,
		// and put the row in the list without a refetch.
		const id = res?.conversation_id;
		if (id && id !== convId.value) {
			convId.value = id;
			router.replace(`/c/${id}`);
			store.loadConversations();
		}
	} catch (e) {
		sendBusy.value = false;
		errorBanner.value = `That didn't reach ${agentName}. Check your connection and try again.`;
		messages.value = messages.value.filter((m) => !m.optimistic);
	}
}

async function stop() {
	const runId = live.value?.runId || "";
	// Ignore anything still arriving for this run: the backend may well finish
	// the turn anyway, and a reply the user stopped must not reappear.
	if (runId) ignoredRuns.value.add(runId);
	live.value = null;
	sendBusy.value = false;
	try {
		await api.stopRun(convId.value, runId);
	} catch {
		// Best-effort: the UI stop stands even if the turn finishes server-side.
	}
	load();
}

// ── attachments ─────────────────────────────────────────────────────────────
async function attach(files) {
	errorBanner.value = "";
	const staged = files.map((f, i) => ({
		key: `att-${Date.now()}-${i}`,
		name: f.name,
		file: f,
		// Local thumbnail while it uploads — a picked photo should appear instantly.
		preview: f.type.startsWith("image/") ? URL.createObjectURL(f) : "",
		uploading: true,
	}));
	attachments.value.push(...staged);

	await Promise.all(
		staged.map(async (a) => {
			try {
				const up = await api.uploadFile(a.file);
				const row = attachments.value.find((x) => x.key === a.key);
				if (row) {
					row.file_url = up.file_url;
					row.uploading = false;
				}
			} catch (e) {
				removeAttachment(a.key);
				errorBanner.value = e?.message || `Couldn't upload ${a.name}.`;
			}
		})
	);
}

function removeAttachment(key) {
	const row = attachments.value.find((a) => a.key === key);
	if (row?.preview) URL.revokeObjectURL(row.preview);
	attachments.value = attachments.value.filter((a) => a.key !== key);
}

function onTranscript(text) {
	input.value = input.value ? `${input.value} ${text}` : text;
}

// ── conversation menu ───────────────────────────────────────────────────────
async function toggleStar() {
	const next = !starred.value;
	starred.value = next;
	try {
		await api.setStar(convId.value, next);
		store.loadConversations();
	} catch {
		starred.value = !next;
	}
}

async function toggleAutoApply() {
	const next = !autoApply.value;
	menuError.value = "";
	try {
		await api.setAutoApply(convId.value, next);
		autoApply.value = next;
	} catch (e) {
		// Only a System Manager may turn this on — the server is the authority.
		menuError.value = e?.message || "Only a System Manager can enable auto-apply.";
	}
}

async function saveRename() {
	const t = renameText.value.trim();
	if (!t) return;
	menuError.value = "";
	try {
		await api.renameConversation(convId.value, t);
		if (conversation.value) conversation.value.title = t;
		store.applyRename(convId.value, t);
		renaming.value = false;
		menuOpen.value = false;
	} catch (e) {
		menuError.value = e?.message || "Couldn't rename this chat.";
	}
}

// ── realtime ────────────────────────────────────────────────────────────────
// `assistant:delta` carries the CUMULATIVE text, not an increment — assign it,
// never append, or the reply doubles up.
function onEvent(p) {
	const conv = p.conversation_id || p.conversation;
	if (conv !== convId.value) return;
	const ignored = p.run_id ? ignoredRuns.value.has(p.run_id) : false;

	switch (p.kind) {
		case "run:start":
			if (ignored) return;
			sendBusy.value = false;
			live.value = {
				runId: p.run_id || "",
				messageId: p.message_id || "",
				text: "",
				tools: [],
			};
			break;

		case "assistant:delta":
			if (ignored) return;
			if (!live.value)
				live.value = { runId: p.run_id || "", messageId: "", text: "", tools: [] };
			live.value.messageId = p.message_id || live.value.messageId;
			live.value.text = p.text || "";
			scrollToBottom();
			break;

		case "tool:start": {
			if (ignored || !live.value) return;
			const id = p.tool_call_id || `t-${live.value.tools.length}`;
			if (live.value.tools.some((t) => t.id === id)) return;
			live.value.tools.push({
				id,
				title: p.tool_title || p.tool_name || "Tool call",
				toolName: p.tool_name,
				status: "running",
			});
			scrollToBottom();
			break;
		}

		case "tool:end": {
			if (ignored || !live.value) return;
			const step = live.value.tools.find((t) => t.id === p.tool_call_id);
			if (step) step.status = toolStatus(p.status) === "error" ? "error" : "done";
			break;
		}

		case "run:end":
			sendBusy.value = false;
			live.value = null;
			// The reply is durable now; reconcile against it (canvas items, final
			// formatting) instead of trusting the streamed copy.
			load();
			break;

		case "run:error":
			sendBusy.value = false;
			live.value = null;
			if (!ignored) errorBanner.value = p.error || "That turn failed.";
			load();
			break;

		case "action:pending":
			if (!p.token || pending.value.some((x) => x.token === p.token)) return;
			pending.value.push({
				token: p.token,
				tool: p.tool || "",
				summary: p.summary || "",
				preview: p.preview ?? null,
				conversation: conv,
				run_id: p.run_id,
				expires_at: p.expires_at ?? null,
			});
			scrollToBottom();
			break;

		case "canvas":
		case "conversation:renamed":
			load();
			break;
	}
}

function onResolved(token) {
	pending.value = pending.value.filter((p) => p.token !== token);
	decision.value = null;
}

const onResync = () => {
	load();
	loadPending();
};

watch(
	() => props.id,
	(id) => {
		convId.value = id === "new" ? "" : id;
		store.clearUnread(convId.value);
		messages.value = [];
		conversation.value = null;
		pending.value = [];
		live.value = null;
		sendBusy.value = false;
		errorBanner.value = "";
		load(true);
		loadPending();
	}
);

onMounted(async () => {
	socket?.on("jarvis:event", onEvent);
	window.addEventListener("jv:resync", onResync);
	// Opening the chat IS reading it — drop the list's dot straight away.
	store.clearUnread(convId.value);
	if (!store.loaded) store.loadConversations();
	load(true);
	loadPending();
	try {
		settings.value = await api.getChatUiSettings();
	} catch {
		// The header subtitle and the mic are both optional; a failure here must
		// not stop the user from chatting.
	}
});
onUnmounted(() => {
	socket?.off("jarvis:event", onEvent);
	window.removeEventListener("jv:resync", onResync);
	attachments.value.forEach((a) => a.preview && URL.revokeObjectURL(a.preview));
});
</script>

<template>
	<div class="jv-bar">
		<button class="jv-icon-btn" aria-label="Back" @click="router.push('/')">
			<svg
				viewBox="0 0 24 24"
				width="20"
				height="20"
				fill="none"
				stroke="currentColor"
				stroke-width="1.9"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="m15 18-6-6 6-6" />
			</svg>
		</button>
		<div class="jv-head">
			<div class="jv-head-title">{{ title }}</div>
			<div class="jv-head-sub">{{ model ? `${agentName} · ${model}` : agentName }}</div>
		</div>
		<button
			v-if="convId"
			class="jv-icon-btn"
			aria-label="Chat options"
			@click="menuOpen = true"
		>
			<svg viewBox="0 0 24 24" width="19" height="19" fill="currentColor">
				<circle cx="12" cy="5" r="1.7" />
				<circle cx="12" cy="12" r="1.7" />
				<circle cx="12" cy="19" r="1.7" />
			</svg>
		</button>
	</div>

	<div ref="scroller" class="jv-scroll jv-thread">
		<div v-if="!items.length && !live && !loading" class="jv-empty">
			<BrandMark :size="52" />
			<div style="font-size: 16px; font-weight: 600; color: var(--ink9)">
				What can I do for you?
			</div>
			<div style="font-size: 14px; line-height: 1.5">
				Try “show me this month's overdue invoices”.
			</div>
		</div>

		<template v-for="it in items" :key="it.key">
			<div v-if="it.type === 'user'" class="jv-msg-user">
				<div v-if="it.msg.content" class="jv-bubble-user">{{ it.msg.content }}</div>
				<MessageMedia
					:items="it.msg.canvas"
					:message-name="it.msg.name"
					@open="preview = { item: $event, messageName: it.msg.name }"
				/>
			</div>

			<!-- The assistant does not get a bubble: a reply can be a page of
			     markdown, a table and a chart, and boxing all that in a chat
			     bubble is what made this screen look like a toy next to the app. -->
			<div v-else class="jv-msg-agent">
				<template v-if="it.msg">
					<div v-if="it.view.html" class="jv-md" v-html="it.view.html" />
					<div v-else-if="it.view.empty" class="jv-msg-error">
						{{ agentName }} didn't return a reply for this turn. Try asking again.
					</div>
					<!-- SIBLING of the chain above, not a v-else-if in it: a partial stop
					     has non-empty html, so chaining this would let the first branch
					     win and the marker would never render - the exact defect it
					     exists to fix. Gated only on `stopped`, it shows for both the
					     partial and the empty stop. -->
					<div v-if="it.msg.stopped" class="jv-stopped">You stopped this reply.</div>
					<ActionCard
						v-if="
							it.view.action &&
							it.key === lastAssistantKey &&
							!dismissedActions.has(it.key)
						"
						:action="it.view.action"
						:conversation="convId"
						@applied="onActionApplied"
						@dismissed="dismissedActions.add(it.key)"
					/>
					<RecordCards v-if="it.view.cards" :data="it.view.cards" />
					<ChartCard v-for="(c, ci) in it.view.charts" :key="ci" :spec="c" />
					<SkillChips :names="it.view.skills" />
					<div v-if="it.msg.error" class="jv-msg-error">{{ it.msg.error }}</div>
					<MessageMedia
						:items="it.msg.canvas"
						:message-name="it.msg.name"
						@open="preview = { item: $event, messageName: it.msg.name }"
					/>
					<div v-if="it.view.took" class="jv-took">
						<svg
							viewBox="0 0 24 24"
							width="11"
							height="11"
							fill="none"
							stroke="currentColor"
							stroke-width="2"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<circle cx="12" cy="12" r="9" />
							<path d="M12 7v5l3 2" />
						</svg>
						{{ it.view.took }}
					</div>
				</template>
			</div>
		</template>

		<!-- the turn in flight -->
		<template v-if="live">
			<div v-if="live.text" class="jv-msg-agent">
				<div class="jv-md" v-html="renderMarkdown(stripAgentBlocks(live.text))" />
			</div>
		</template>

		<ThinkingIndicator v-if="sending && !(live && live.text)" />

		<DecisionCard
			v-for="p in pending"
			:key="p.token"
			:summary="p.summary || p.tool || `${agentName} needs your approval`"
			@open="decision = p"
		/>
	</div>

	<div v-if="errorBanner" class="jv-banner">
		<svg
			viewBox="0 0 24 24"
			width="14"
			height="14"
			fill="none"
			stroke="currentColor"
			stroke-width="2"
			stroke-linecap="round"
		>
			<circle cx="12" cy="12" r="9" />
			<path d="M12 8v5M12 16h.01" />
		</svg>
		<span>{{ errorBanner }}</span>
		<button class="jv-banner-x" aria-label="Dismiss" @click="errorBanner = ''">
			<svg
				viewBox="0 0 24 24"
				width="14"
				height="14"
				fill="none"
				stroke="currentColor"
				stroke-width="2.2"
				stroke-linecap="round"
			>
				<path d="M18 6 6 18M6 6l12 12" />
			</svg>
		</button>
	</div>

	<Composer
		ref="composer"
		v-model="input"
		:sending="sending"
		:attachments="attachments"
		:mic-enabled="micEnabled"
		@send="send"
		@stop="stop"
		@attach="attach"
		@remove="removeAttachment"
		@mic="voiceOpen = true"
	/>

	<!-- chat options -->
	<Sheet :open="menuOpen" @close="(menuOpen = false), (renaming = false)">
		<div class="jv-menu">
			<template v-if="renaming">
				<div class="jv-menu-title">Rename chat</div>
				<input
					v-model="renameText"
					class="jv-input"
					placeholder="Chat title"
					@keydown.enter="saveRename"
				/>
				<div v-if="menuError" class="jv-menu-error">{{ menuError }}</div>
				<div class="jv-menu-actions">
					<button class="jv-btn is-ghost" @click="renaming = false">Cancel</button>
					<button
						class="jv-btn is-primary"
						:disabled="!renameText.trim()"
						@click="saveRename"
					>
						Save
					</button>
				</div>
			</template>

			<template v-else>
				<button class="jv-row-btn" @click="toggleStar">
					<svg
						viewBox="0 0 24 24"
						width="19"
						height="19"
						:fill="starred ? 'var(--amber-dot)' : 'none'"
						:stroke="starred ? 'var(--amber-dot)' : 'currentColor'"
						stroke-width="1.8"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path
							d="m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z"
						/>
					</svg>
					{{ starred ? "Starred" : "Star this chat" }}
				</button>

				<button class="jv-row-btn" @click="(renameText = title), (renaming = true)">
					<svg
						viewBox="0 0 24 24"
						width="19"
						height="19"
						fill="none"
						stroke="currentColor"
						stroke-width="1.8"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z" />
					</svg>
					Rename chat
				</button>

				<!-- Auto-apply removes the approval gate for this chat: the agent
				     commits ERP writes without asking. It is the single most
				     dangerous switch in the product, so it looks like one. -->
				<div class="jv-danger">
					<svg
						viewBox="0 0 24 24"
						width="18"
						height="18"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path
							d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"
						/>
						<path d="M12 9v4M12 17h.01" />
					</svg>
					<div class="jv-danger-main">
						<div class="jv-danger-title">Auto-apply changes</div>
						<div class="jv-danger-sub">
							{{ agentName }} commits ERP writes without asking. Turns off approval
							prompts.
						</div>
						<div v-if="menuError" class="jv-menu-error">{{ menuError }}</div>
					</div>
					<button
						class="jv-toggle"
						:class="{ 'is-on': autoApply }"
						role="switch"
						:aria-checked="autoApply"
						@click="toggleAutoApply"
					>
						<span />
					</button>
				</div>
			</template>
		</div>
	</Sheet>

	<DecisionSheet :action="decision" @close="decision = null" @resolved="onResolved" />
	<FilePreviewSheet
		:item="preview?.item"
		:message-name="preview?.messageName || ''"
		@close="preview = null"
	/>
	<VoiceSheet :open="voiceOpen" @close="voiceOpen = false" @transcript="onTranscript" />
</template>

<style scoped>
.jv-head {
	flex: 1;
	min-width: 0;
}
.jv-head-title {
	font-size: 14.5px;
	font-weight: 600;
	color: var(--ink9);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-head-sub {
	font-size: 11.5px;
	color: var(--ink5);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}

.jv-thread {
	display: flex;
	flex-direction: column;
	gap: 14px;
	padding: 16px 14px 12px;
}
.jv-msg-user {
	display: flex;
	flex-direction: column;
	align-items: flex-end;
}
/* A tinted bubble, not a solid violet slab with white text: the reply next to
   it is plain body copy, and a saturated block beside it fights for attention
   it doesn't need. Same treatment as the native app. */
.jv-bubble-user {
	max-width: 82%;
	padding: 10px 13px;
	border-radius: 16px 16px 5px 16px;
	background: var(--accent-bg);
	color: var(--ink8);
	font-size: 13.5px;
	line-height: 1.45;
	white-space: pre-wrap;
	overflow-wrap: anywhere;
}
.jv-msg-agent {
	min-width: 0;
}
/* The activity card sits above the prose it produced; give it room. (Scoped
   styles reach a child component's root element, which is what .jv-tools is.) */
.jv-msg-agent .jv-tools {
	margin-bottom: 10px;
}
.jv-msg-error {
	margin-top: 4px;
	font-size: 12px;
	line-height: 1.4;
	color: var(--red);
}
/* The stop marker is muted (--ink5), never the error tone above it: the user
   pressed Stop on purpose, so this states what happened, it doesn't warn. */
.jv-stopped {
	margin-top: 8px;
	padding-top: 7px;
	border-top: 1px solid var(--border);
	font-size: 11.5px;
	line-height: 1.4;
	color: var(--ink5);
}
.jv-took {
	display: flex;
	align-items: center;
	gap: 4px;
	margin-top: 5px;
	font-size: 11px;
	color: var(--ink4);
}

.jv-md {
	font-size: 14px;
	line-height: 1.6;
	color: var(--ink7);
	overflow-wrap: anywhere;
}
.jv-md :deep(p) {
	margin: 0 0 8px;
}
.jv-md :deep(p:last-child) {
	margin-bottom: 0;
}
.jv-md :deep(h1),
.jv-md :deep(h2),
.jv-md :deep(h3) {
	margin: 12px 0 6px;
	color: var(--ink9);
	font-weight: 600;
	line-height: 1.3;
}
.jv-md :deep(h1) {
	font-size: 19px;
}
.jv-md :deep(h2) {
	font-size: 16.5px;
}
.jv-md :deep(h3) {
	font-size: 15px;
}
.jv-md :deep(strong) {
	color: var(--ink8);
	font-weight: 600;
}
.jv-md :deep(a) {
	color: var(--accent);
}
.jv-md :deep(ul),
.jv-md :deep(ol) {
	margin: 0 0 8px;
	padding-left: 20px;
}
.jv-md :deep(li) {
	margin: 2px 0;
}
/* Wide content scrolls inside its own box; the thread never scrolls sideways. */
.jv-md :deep(pre),
.jv-md :deep(table) {
	display: block;
	max-width: 100%;
	overflow-x: auto;
}
.jv-md :deep(pre) {
	padding: 10px;
	border: 1px solid var(--border);
	border-radius: 8px;
	background: var(--card2);
	font-size: 12.5px;
}
.jv-md :deep(code) {
	font-size: 12.5px;
	background: var(--card2);
	padding: 1px 5px;
	border-radius: 5px;
}
.jv-md :deep(pre code) {
	background: transparent;
	padding: 0;
}
.jv-md :deep(table) {
	border-collapse: collapse;
	font-size: 12.5px;
	white-space: nowrap;
}
.jv-md :deep(th),
.jv-md :deep(td) {
	padding: 6px 10px;
	border: 1px solid var(--border);
	text-align: left;
}
.jv-md :deep(th) {
	background: var(--card2);
	color: var(--ink9);
}
.jv-md :deep(blockquote) {
	margin: 0 0 8px;
	padding: 2px 10px;
	border-left: 3px solid var(--border2);
	background: var(--card2);
	border-radius: 4px;
}

.jv-banner {
	display: flex;
	align-items: center;
	gap: 8px;
	flex: none;
	padding: 8px 14px;
	background: var(--red-bg);
	color: var(--red);
	font-size: 11.5px;
	font-weight: 500;
}
.jv-banner span {
	flex: 1;
	min-width: 0;
	line-height: 1.35;
}
.jv-banner-x {
	flex: none;
	border: 0;
	background: transparent;
	color: inherit;
	cursor: pointer;
	padding: 2px;
}

/* chat options sheet */
.jv-menu {
	padding: 4px 14px 18px;
}
.jv-menu-title {
	padding: 6px 2px 10px;
	font-size: 15px;
	font-weight: 600;
	color: var(--ink9);
}
.jv-row-btn {
	display: flex;
	align-items: center;
	gap: 12px;
	width: 100%;
	padding: 13px 4px;
	border: 0;
	border-bottom: 1px solid var(--border);
	background: transparent;
	color: var(--ink8);
	font: inherit;
	font-size: 14.5px;
	text-align: left;
	cursor: pointer;
}
.jv-row-btn svg {
	flex: none;
	color: var(--ink5);
}
.jv-input {
	width: 100%;
	height: 46px;
	padding: 0 14px;
	border: 1px solid var(--border2);
	border-radius: 12px;
	background: var(--card);
	color: var(--ink9);
	font: inherit;
	font-size: 14.5px;
	outline: none;
}
.jv-input:focus {
	border-color: var(--accent);
}
.jv-menu-error {
	margin-top: 6px;
	font-size: 12px;
	font-weight: 500;
	color: var(--red);
}
.jv-menu-actions {
	display: flex;
	gap: 10px;
	margin-top: 12px;
}
.jv-btn {
	flex: 1;
	height: 46px;
	border: 0;
	border-radius: 12px;
	font: inherit;
	font-size: 15px;
	font-weight: 600;
	cursor: pointer;
}
.jv-btn.is-primary {
	background: var(--accent-solid);
	color: #fff;
}
.jv-btn.is-ghost {
	border: 1px solid var(--border2);
	background: var(--card);
	color: var(--ink8);
}
.jv-btn:disabled {
	opacity: 0.55;
}

.jv-danger {
	display: flex;
	align-items: flex-start;
	gap: 11px;
	margin: 12px 0 0;
	padding: 13px;
	border: 1px solid var(--red);
	border-radius: 12px;
	background: var(--red-bg);
	color: var(--red);
}
.jv-danger-main {
	flex: 1;
	min-width: 0;
}
.jv-danger-title {
	font-size: 13.5px;
	font-weight: 600;
	color: var(--ink9);
}
.jv-danger-sub {
	margin-top: 2px;
	font-size: 12px;
	line-height: 1.4;
	color: var(--red);
}
.jv-toggle {
	flex: none;
	width: 44px;
	height: 26px;
	padding: 3px;
	border: 0;
	border-radius: 999px;
	background: var(--card3);
	cursor: pointer;
	transition: background 0.15s ease;
}
.jv-toggle span {
	display: block;
	width: 20px;
	height: 20px;
	border-radius: 999px;
	background: #fff;
	transition: transform 0.15s ease;
}
.jv-toggle.is-on {
	background: var(--red);
}
.jv-toggle.is-on span {
	transform: translateX(18px);
}
</style>
