<script setup>
import { parkedByCard } from "@shared/lib/draftParked.js";
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
import {
	holdActive,
	raiseHold,
	clearHold,
	recheck as recheckMaintenance,
} from "../maintenanceGate";
import { agentName } from "@/branding";
import { useRouter } from "vue-router";
// The desktop SPA's renderer — dependency-free, and sharing it means an agent
// reply reads identically on both surfaces.
import { renderMarkdown } from "@shared/markdown.js";
import { admitEvent } from "@jsshared/pump_fence.mjs";
import { eventFence } from "../lib/pump_fence_state.js";
import { isShowCardRequest } from "../lib/showCardRequest.js";
import * as api from "../api";
import { store } from "../store";
import {
	countDiagrams,
	parseAction,
	parseCards,
	parseCharts,
	parseSkillsUsed,
	stripAgentBlocks,
	toolStatus,
} from "../lib/blocks";
import { spanBetween } from "../lib/time";
import { proposedLabel } from "../lib/cardAge.js";
import { sortPendingCards } from "../lib/sortPendingCards.js";
import { mergePendingSources, withExcluded } from "../lib/pendingResync.js";
import {
	discardedTokens,
	isRecentCard,
	markCardsEarlier,
	typedApprovalHint as hintFor,
} from "../lib/typedCardReply.js";
import ActionCard from "../components/ActionCard.vue";
import ChartCard from "../components/ChartCard.vue";
import Composer from "../components/Composer.vue";
import SendRecoveryCard from "../components/SendRecoveryCard.vue";
import { recoveryState, sendRecovery } from "../sendRecoveryStore";
import DecisionCard from "../components/DecisionCard.vue";
import DecisionSheet from "../components/DecisionSheet.vue";
import FilePreviewSheet from "../components/FilePreviewSheet.vue";
import { turnErrorInfo } from "../../../jarvis/public/js/turn_errors.mjs";
import MessageMedia from "../components/MessageMedia.vue";
import RecordCards from "../components/RecordCards.vue";
import Sheet from "../components/Sheet.vue";
import SkillChips from "../components/SkillChips.vue";
import ThinkingIndicator from "../components/ThinkingIndicator.vue";
import VersionPill from "../components/VersionPill.vue";
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
// Deep link to this thread in the desktop web chat (outside the PWA's
// /jarvis-mobile scope), where the SPA draws diagrams the PWA can't.
const webChatUrl = computed(() =>
	convId.value ? `/jarvis/c/${encodeURIComponent(convId.value)}` : "/jarvis/"
);
const messages = ref([]);
const input = ref("");
const loading = ref(false);
const sendBusy = ref(false);
const errorBanner = ref("");
// { [message_id]: code } from a live run:error event; not persisted, so a
// reload (messages re-fetched via load()) falls back to classifying the
// persisted error string alone. Without this, the banner above (which does
// get the live code) and this same message's inline error card would name
// the failure differently for the SAME event - the exact #702 defect this
// feature exists to fix, reproduced across two elements on one screen.
const errorMeta = ref({});
// Failed messages whose raw error text is expanded ("Details" in the card).
const rawOpen = ref(new Set());
function toggleRaw(key) {
	const next = new Set(rawOpen.value);
	next.has(key) ? next.delete(key) : next.add(key);
	rawOpen.value = next;
}
const attachments = ref([]);
const pending = ref([]); // parked writes awaiting approval
// A typed "confirm 2" binds to the token shown as number 2 here (approval_tokens),
// so the numbering must be stable: the shared, unit-tested comparator orders by
// (created_at, token by code unit), whatever order the store listed them in.
const orderedPending = computed(() => sortPendingCards(pending.value));
// Typed approval works here as on the desktop. An "Earlier" card (parked before the
// user's latest message) never gets the bare-phrase hint: only its number binds it.
const typedApprovalHint = computed(() => hintFor(orderedPending.value));
// Set when a typed yes/no bound no card (it went to Jarvis as a message); shown while
// an older card is still here, cleared with the last one (decision 13).
const olderCardsNote = ref(false);
const showOlderCardsNote = computed(
	() => olderCardsNote.value && orderedPending.value.some((p) => !isRecentCard(p))
);
// A coarse clock for the "Proposed 3h ago" label on a card left waiting.
const cardClock = ref(Date.now());
let _cardClockTick = null;
const settings = ref(null);

// The turn in flight. Held separately from `messages` because it is not durable
// yet: the row exists server-side but its content arrives as a stream.
const live = ref(null); // { runId, messageId, text, tools[] }
// Stop is a UI-level cancel too — the backend may still finish the turn, so
// ignore what comes back rather than letting a "stopped" reply reappear.
const ignoredRuns = ref(new Set());
// JF-018: the Relay-Pump epoch/seq watermark lives in a MODULE-scope singleton
// (see ../lib/pump_fence_state.js for why): this component is route-mounted and
// unmounts on /business, /files or /, so a component-scope fence would be wiped
// on every route away and readmit the stale frames it exists to drop. The
// singleton is what actually delivers "the terminal marker outlives the view".

const decision = ref(null);
// The token whose Confirm/Discard RPC is currently in flight in the open
// DecisionSheet (P0c). While set, loadPending's fresh reads must not re-show
// its card: the RPC's own response already removed it, but a resync request
// issued just before the RPC committed can still return the token as live.
const inflightToken = ref(null);
function onDecisionBusy(busy) {
	inflightToken.value = busy ? decision.value && decision.value.token : null;
}
// D1: tokens a typed "no" discarded (send()'s discardedTokens, below). The
// discard only filters pending.value; messages.value keeps tool_status
// "pending" for that row until the next load(), so a resync racing that
// window (the run-scoped poll, wake, or the menu re-check) must not rebuild
// the card from it. Persists past the single RPC inflightToken covers -
// reset only on a conversation switch (below).
const settledTokens = ref(new Set());
const preview = ref(null);
const voiceOpen = ref(false);
const menuOpen = ref(false);
const renaming = ref(false);
const renameText = ref("");
const menuError = ref("");
const starred = ref(false);

const scroller = ref(null);
const composer = ref(null);
// An action card is a live offer, not history: only the newest assistant turn
// may still be applied. Scrolling back to last week's proposal and tapping
// Create would write a record the user has long since moved on from.
const dismissedActions = ref(new Set());
// Drafts the server turned into a gated confirmation card: key -> the note.
const parkedActions = ref(new Map());

const localRequests = computed(() =>
	recoveryState.requests.filter((r) => r.conversation === convId.value)
);
const sending = computed(
	() =>
		!!live.value ||
		!!queuedTurn.value ||
		!!recoveryState.starting[convId.value] ||
		sendBusy.value ||
		localRequests.value.some((r) => r.state === "sending")
);
const queuedTurn = computed(() => recoveryState.queued[convId.value]);
const savedDrafts = computed(() =>
	recoveryState.parkedDrafts.filter((d) => d.conversation === convId.value)
);
const hasDraft = (draft) => !!(draft?.text || draft?.attachments?.length);
function useSavedDraft(draft) {
	const current = { text: input.value, attachments: attachments.value };
	input.value = draft.text;
	attachments.value = draft.attachments;
	if (hasDraft(current)) Object.assign(draft, current);
	else recoveryState.parkedDrafts.splice(recoveryState.parkedDrafts.indexOf(draft), 1);
	saveDraft();
}
// Bounded reads settle exactly once: late network results cannot mutate a new check.
async function boundedRead(promise) {
	let timer;
	try {
		return await Promise.race([
			promise,
			new Promise((_, reject) => {
				timer = setTimeout(() => reject(new Error("Read timed out")), 15000);
			}),
		]);
	} finally {
		clearTimeout(timer);
	}
}
let queueGeneration = 0;
let queueRead = null;
let queuePoll;
async function refreshQueue() {
	const cid = convId.value;
	const generation = viewGeneration;
	const epoch = queueGeneration;
	if (!cid || queueRead?.cid === cid) return;
	const ticket = { cid };
	queueRead = ticket;
	const q = recoveryState.queued[cid];
	const starting = recoveryState.starting[cid];
	const current = () => isCurrent(cid, generation) && epoch === queueGeneration;
	try {
		const result = await boundedRead(
			q || starting ? api.queuePosition(q?.run_id || starting) : api.activeQueuedTurn(cid)
		);
		if (!current()) return;
		if (q) {
			if (!result?.ok) throw new Error("Status unavailable");
			if (["queued", "preparing", "ready"].includes(result.state)) {
				Object.assign(q, { state: result.state, position: result.position, note: "" });
			} else if (
				[
					"dispatching",
					"streaming",
					"terminal_observed",
					"finalizing",
					"recovering",
					"done",
					"errored",
					"cancelled",
				].includes(result.state)
			) {
				recoveryState.observedRuns[q.run_id] = true;
				delete recoveryState.queued[cid];
				load();
			} else throw new Error("Unknown turn state");
		} else if (starting) {
			if (!result?.ok) return; // Legacy turns may have no admission row.
			if (["done", "errored", "cancelled"].includes(result.state)) {
				recoveryState.observedRuns[starting] = true;
				delete recoveryState.starting[cid];
				load();
			} else if (["queued", "preparing", "ready"].includes(result.state)) {
				delete recoveryState.starting[cid];
				recoveryState.queued[cid] = {
					run_id: starting,
					state: result.state,
					position: result.position,
				};
			}
		} else if (
			result?.ok &&
			result.active &&
			!recoveryState.observedRuns[result.active.run_id]
		) {
			recoveryState.queued[cid] = result.active;
		}
	} catch {
		if (current() && q)
			q.note = "Could not refresh status. Your request is still accepted. Check again.";
	} finally {
		if (queueRead === ticket) queueRead = null;
	}
}
async function cancelWaiting() {
	const cid = convId.value;
	const q = queuedTurn.value;
	if (!q || q.cancelling) return;
	q.cancelling = true;
	try {
		const result = await boundedRead(api.cancelQueuedTurn(q.run_id));
		if (recoveryState.queued[cid] !== q) return;
		if (result?.ok) {
			queueGeneration++;
			recoveryState.observedRuns[q.run_id] = true;
			delete recoveryState.queued[cid];
			if (cid === convId.value) load();
		} else q.note = "This request may have started. Check its status before trying again.";
	} catch {
		q.note = "Cancellation is not confirmed. Check status before trying again.";
	} finally {
		q.cancelling = false;
	}
}
let viewActive = true;
let viewGeneration = 0;
let loadGeneration = 0;
const isCurrent = (cid, generation) =>
	viewActive && cid === convId.value && generation === viewGeneration;

function saveDraft() {
	recoveryState.drafts[convId.value] = { text: input.value, attachments: attachments.value };
}
function restoreDraft() {
	const draft = recoveryState.drafts[convId.value];
	input.value = draft?.text || "";
	attachments.value = draft?.attachments || [];
}
restoreDraft();
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
		// Mermaid diagrams can't be drawn here; surface a chip to the web chat.
		diagrams: countDiagrams(content),
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

// Mirrors ChatView.vue's (desktop) errorInfo(): a live run:error's code, when
// this session saw it, always wins over reclassifying the persisted string
// (errorMeta above). Called once per assistant item from `items` below (as
// `err`), never from the template, for the same reason view() is precomputed.
function errorNote(m) {
	return turnErrorInfo(m.error, errorMeta.value[m.name] || "", { provider: m.provider });
}

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
			current = {
				type: "assistant",
				key: m.name,
				msg: m,
				view: view(m),
				err: m.error ? errorNote(m) : null,
				tools: [],
			};
			out.push(current);
		}
	}
	return out;
});

const lastAssistantKey = computed(() => {
	const assistants = items.value.filter((i) => i.type === "assistant" && i.msg);
	return assistants.length ? assistants[assistants.length - 1].key : "";
});

// The write leaves a receipt in the conversation (a closed failure, the assistant's
// explanation), so reload rather than guess.
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
// Whether new content should keep the view pinned to the newest text. Sending a
// message drops this (see send): the reply then grows DOWNWARD from where the
// turn landed instead of sliding its own opening up past the top of the viewport
// on every streamed chunk — the "large paragraph slides upward" bug. A real
// scroll back to the bottom resumes following; a fresh load restores it.
const follow = ref(true);
function onScroll() {
	follow.value = atBottom();
}
// Follow the newest text only while `follow` holds (parked at the bottom) AND no
// reply is streaming (`live`): chasing the bottom mid-stream drags the line you
// are reading up and off the top, so a long reply never settles enough to read.
// While streaming the reply grows downward and you scroll at your own pace; a
// forced scroll (open / send / stop) always lands, and once the turn ends normal
// follow resumes so late content still keeps a bottom-parked reader pinned.
async function scrollToBottom(force = false) {
	const stick = force || (follow.value && !live.value);
	await nextTick();
	if (stick && scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
}

// ── loading ─────────────────────────────────────────────────────────────────
async function load(force = false) {
	const cid = convId.value;
	const generation = viewGeneration;
	const ticket = ++loadGeneration;
	if (!cid) return;
	const current = () => isCurrent(cid, generation) && ticket === loadGeneration;
	// A fresh open / refresh follows the newest content again (streaming replies
	// arrive via socket events, never load(), so this never re-pins mid-reply).
	follow.value = true;
	loading.value = true;
	try {
		const d = await api.getConversation(cid);
		if (!current()) return;
		conversation.value = d?.conversation || null;
		messages.value = d?.messages || [];
		sendRecovery.reconcile(cid, messages.value);
		const row = store.conversations.find((c) => c.name === convId.value);
		if (row) starred.value = !!row.starred;
		// A reply still streaming when we (re)opened the chat: restore the busy
		// state from the durable flag rather than assuming the turn is over.
		const last = messages.value[messages.value.length - 1];
		if (last?.role === "assistant" && last.streaming && !live.value) {
			live.value = {
				runId: last.run_id || recoveryState.starting[cid] || "",
				messageId: last.name,
				text: last.content || "",
				tools: [],
			};
		}
		// Legacy turns have no admission row. On return to this chat, the
		// assistant row is the evidence that the accepted turn started or ended.
		// Streaming stays busy through `live`; a finished reply needs no Stop.
		if (last?.role === "assistant" && recoveryState.starting[cid]) {
			recoveryState.observedRuns[recoveryState.starting[cid]] = true;
			delete recoveryState.starting[cid];
			// A status read started before this transcript must not restore a queue.
			queueGeneration++;
		}
		await scrollToBottom(force);
	} catch (e) {
		console.error("Jarvis PWA: failed to load conversation", e);
	} finally {
		if (current()) loading.value = false;
	}
}

// PR-1: build a confirm-card item from a durable pending action-row (role="tool",
// tool_status="pending"), mirroring the SPA. pendingCardOf reads preview.card.
function _rowExpiresEpoch(s) {
	if (!s) return null;
	// The row's expires_at is a site-timezone datetime string; parse to epoch seconds
	// (best-effort - a live push carries the authoritative epoch and the server
	// enforces the real TTL, so null just omits the client countdown).
	const t = Date.parse(String(s).replace(" ", "T"));
	return Number.isFinite(t) ? Math.round(t / 1000) : null;
}
function pendingActionFromRow(m, cid) {
	return {
		conversation: cid,
		token: m.tool_call_id,
		tool: m.tool_name,
		summary: "",
		preview: { card: m.pending_card },
		run_id: null,
		// Server epoch on every pending row (a legacy one: its own creation); never a
		// local-time parse (sortPendingCards falls back to expires_at when null).
		created_at: m.created_at ?? null,
		expires_at: _rowExpiresEpoch(m.expires_at),
		seq: m.seq ?? null,
		recent: m.recent !== false,
	};
}

// Parked writes survive a reload: re-ask rather than leaving an approval the user
// can never reach. PR-1 reliability: the durable pending action-rows
// (get_conversation) are the PRIMARY source - a reload shows a confirmable card even
// if the best-effort action:pending push was missed. list_pending (Redis) is the
// backstop. Merge rows-first, dedup by token, REPLACE so a confirmed/expired card
// (no longer a pending row and gone from Redis) drops.
// Layered re-check (phase 1): the on-demand control's handler (the header-menu entry —
// the PWA has no jump affordance to ride). loadPending merges durable rows (primary) +
// the Redis backstop, so a manual pull surfaces a parked card the auto-heal missed. The
// card appearing is the feedback.
async function recheckPending() {
	// "menu" tags this as the human-driven on-demand control so the server can
	// attribute how often it surfaces a card the silent auto-heal missed.
	await loadPending("menu");
}

async function loadPending(source) {
	const cid = convId.value;
	if (!cid) return;
	const fromRows = (messages.value || [])
		.filter(
			(m) =>
				m.role === "tool" &&
				m.tool_status === "pending" &&
				m.pending_card &&
				m.tool_call_id
		)
		.map((m) => pendingActionFromRow(m, cid));
	let fromBackstop = null; // null = backstop read failed/unavailable (a transient blip)
	try {
		const r = await api.listPendingConfirmations(cid, source);
		// Only a CLEAN ok:true result is authoritative. ok:false (a store blip) or a
		// missing/malformed body leaves fromBackstop null so we KEEP the on-screen queue
		// rather than wipe a live-only card (the fail-closed hole the SPA guards). This
		// helper is also the open-seed path, so durable rows below always apply.
		if (r?.ok && r.data) fromBackstop = r.data.pending.filter((p) => p.conversation === cid);
	} catch {
		/* leave null: durable rows still apply, live cards kept */
	}
	// A resync in flight across a conversation switch must not write the previous
	// conversation's cards onto the new one (freshness guard, mirrors the SPA).
	if (convId.value !== cid) return;
	// On a blip keep the current cards; on a clean read the server list is authoritative
	// for this conversation (resolved cards drop). Durable rows always apply. Either
	// fresh source is excluded from re-adding a token whose Confirm/Discard RPC is
	// still in flight (P0c in-flight suppression) - see mergePendingSources. Only one
	// DecisionSheet is ever open, so at most one token is ever in flight.
	const base = fromBackstop === null ? pending.value : [];
	const inflight = withExcluded(
		settledTokens.value,
		inflightToken.value ? [inflightToken.value] : []
	);
	pending.value = mergePendingSources(base, [fromRows, fromBackstop || []], inflight);
}

// Auto-heal (layered design, phase 1): recover a card the live push dropped with NO user
// action. Both triggers are card-agnostic (never gated on a known pending card) and route
// through loadPending, inheriting its rows-first / failed-backstop-keeps-queue merge.
//   (a) a run-scoped, self-stopping poll: while a turn is in flight, reconcile every
//       PENDING_POLL_MS, then a couple of trailing reconciles after it settles (the
//       terminal frame can be the dropped one), then stop. Mirrors the desktop SPA.
//   (b) window focus + tab-visibility: a card minted while the app was backgrounded
//       surfaces on return. Debounced so focus + visibility co-firing is one resync.
const PENDING_POLL_MS = 2500;
const PENDING_POLL_TRAILING = 2;
let _pendingPoll = null;
let _pendingPollIdle = 0;
function startPendingPoll() {
	if (_pendingPoll) {
		_pendingPollIdle = 0;
		return;
	}
	_pendingPollIdle = 0;
	_pendingPoll = setInterval(() => {
		if (!convId.value) {
			stopPendingPoll();
			return;
		}
		loadPending("auto");
		if (live.value) _pendingPollIdle = 0;
		else if (++_pendingPollIdle > PENDING_POLL_TRAILING) stopPendingPoll();
	}, PENDING_POLL_MS);
}
function stopPendingPoll() {
	if (_pendingPoll) clearInterval(_pendingPoll);
	_pendingPoll = null;
	_pendingPollIdle = 0;
}
let _lastWake = 0;
function wakePending() {
	if (!convId.value) return;
	const now = Date.now();
	if (now - _lastWake < 2000) return; // focus + visibility often co-fire
	_lastWake = now;
	refreshQueue();
	loadPending("auto");
}
function onVisible() {
	if (document.visibilityState === "visible") wakePending();
}

// ── sending ─────────────────────────────────────────────────────────────────
async function send() {
	const cid = convId.value;
	const generation = viewGeneration;
	const text = input.value.trim();
	const ready = attachments.value.filter((a) => a.file_url);
	if (holdActive.value || attachments.value.some((a) => a.uploading || !a.file_url)) return;

	// Preserve the existing typed-card rescue; do not clear a newer draft after
	// its asynchronous lookup or continue sending into a different conversation.
	if (cid && !ready.length && isShowCardRequest(text)) {
		const before = pending.value.length;
		await loadPending("typed");
		if (!isCurrent(cid, generation)) return;
		if (pending.value.length > before) {
			if (input.value.trim() === text) {
				input.value = "";
				composer.value?.reset();
			}
			return;
		}
		if (input.value.trim() !== text || attachments.value.length) return;
	}
	if ((!text && !ready.length) || sending.value) return;
	errorBanner.value = "";
	const request = sendRecovery.stage(
		cid,
		input.value,
		ready,
		orderedPending.value.map((p) => p.token)
	);
	input.value = "";
	composer.value?.reset();
	// The preserved request uses durable file URLs; release only local thumbnails.
	attachments.value.forEach((a) => a.preview && URL.revokeObjectURL(a.preview));
	attachments.value = [];
	saveDraft();
	// Capture/send before awaiting scroll; routing never changes the payload.
	const completion = dispatchRequest(request);
	await scrollToBottom(true);
	if (isCurrent(cid, generation)) follow.value = false;
	await completion;
}

async function dispatchRequest(request) {
	const attempt = request.id;
	// The browser can keep a stalled POST pending indefinitely. A UI deadline
	// makes its preserved request checkable; it does not cancel server work.
	const deadline = setTimeout(() => {
		if (request.id === attempt && request.state === "sending")
			sendRecovery.settle(request, null);
	}, 30000);
	try {
		const outcome = await api.sendRecoverableMessage(request);
		if (request.id === attempt) {
			sendRecovery.settle(request, outcome);
			// Only a fresh send response can update global maintenance readiness.
			// A receipt read later describes the past, not current availability.
			if (["accepted", "confirmed"].includes(request.state)) clearHold();
			else if (request.state === "rejected" && request.result?.reason === "maintenance") {
				raiseHold(request.result.message);
				recheckMaintenance();
			}
		}
	} catch {
		// A delivery check may have settled this attempt while its POST was
		// stalled. A late transport error must not undo that evidence.
		if (request.id === attempt) sendRecovery.settle(request, null);
	} finally {
		clearTimeout(deadline);
	}
}

async function retryRequest(request) {
	if (request.conversation !== convId.value || sending.value || holdActive.value) return;
	if (!sendRecovery.retry(request)) return;
	await dispatchRequest(request);
}

async function checkRequest(request) {
	if (request.state !== "uncertain" || request.checking) return;
	const attempt = request.id;
	request.checking = true;
	request.note = "";
	try {
		const outcome = await boundedRead(api.checkDelivery(attempt));
		if (request.id !== attempt) return;
		sendRecovery.settle(request, outcome);
		if (request.state === "uncertain")
			request.note =
				"Delivery is still unknown. No second message was sent. Check again later or ask support to inspect this conversation.";
	} catch {
		if (request.id !== attempt || request.state !== "uncertain") return;
		request.note =
			"Delivery check did not finish. Check again when connected. No second message was sent.";
	} finally {
		if (request.id === attempt) request.checking = false;
	}
}

function editRequest(request, text) {
	if (request.state === "rejected" && request.conversation === convId.value) request.text = text;
}

function editAsNew(request) {
	if (request.state !== "uncertain" || request.conversation !== convId.value) return;
	const current = { text: input.value, attachments: attachments.value };
	if (hasDraft(current))
		recoveryState.parkedDrafts.push({
			...current,
			id: crypto.randomUUID(),
			conversation: convId.value,
		});
	input.value = request.text;
	attachments.value = request.attachments.map((file, i) => ({
		...file,
		key: `${request.id}-${i}`,
	}));
	sendRecovery.remove(request);
	saveDraft();
	errorBanner.value =
		"This is a new draft. The original request may already have executed; sending again could duplicate work.";
}
function downloadRecoveryBackup() {
	saveDraft();
	const payload = (draft) => ({
		text: draft.text,
		attachments: (draft.attachments || []).map((file) => ({
			name: file.name,
			file_url: file.file_url || null,
		})),
	});
	const backup = {
		warning:
			"Delivery status may have changed. Check the conversation before sending again. File links require access to the original Jarvis site. Unfinished uploads must be attached again.",
		requests: recoveryState.requests.map((r) => ({
			conversation: r.conversation,
			state: r.state,
			...payload(r),
		})),
		drafts: Object.fromEntries(
			Object.entries(recoveryState.drafts).map(([key, draft]) => [key, payload(draft)])
		),
		savedDrafts: recoveryState.parkedDrafts.map((draft) => ({
			conversation: draft.conversation,
			...payload(draft),
		})),
	};
	const url = URL.createObjectURL(
		new Blob([JSON.stringify(backup, null, 2)], { type: "application/json" })
	);
	const link = document.createElement("a");
	link.href = url;
	link.download = "jarvis-preserved-messages.json";
	document.body.appendChild(link);
	link.click();
	link.remove();
	setTimeout(() => URL.revokeObjectURL(url), 60000);
}
function reloadForUpdate() {
	window.location.reload();
}

// Outcome handling belongs to the currently mounted originating view. A request
// can finish while another chat (or no chat) is mounted; its state still survives.
watch(
	() => localRequests.value.map((r) => `${r.id}:${r.state}`).join("|"),
	async () => {
		const cid = convId.value;
		const generation = viewGeneration;
		for (const request of [...localRequests.value]) {
			if (!isCurrent(cid, generation)) return;
			const res = request.result;
			const discarded = discardedTokens(res);
			if (discarded.size) {
				pending.value = pending.value.filter((p) => !discarded.has(p.token));
				settledTokens.value = withExcluded(settledTokens.value, discarded);
			}
			if (request.state === "rejected") {
				// In-memory recovery cannot survive an automatic release reload.
				continue;
			}
			if (!["accepted", "confirmed"].includes(request.state)) continue;
			if (res.conversation_id && res.conversation_id !== cid) {
				saveDraft();
				request.conversation = res.conversation_id;
				for (const draft of recoveryState.parkedDrafts) {
					if (draft.conversation === cid) draft.conversation = res.conversation_id;
				}
				const source = recoveryState.drafts[cid];
				if (hasDraft(source)) {
					if (hasDraft(recoveryState.drafts[res.conversation_id])) {
						recoveryState.parkedDrafts.push({
							...source,
							id: request.id,
							conversation: res.conversation_id,
						});
					} else recoveryState.drafts[res.conversation_id] = source;
				}
				input.value = "";
				attachments.value = [];
				delete recoveryState.drafts[cid];
				router.replace(`/c/${res.conversation_id}`);
				store.loadConversations();
				return;
			}
			if (request.state === "confirmed") {
				sendRecovery.remove(request);
				if (res.ok === false)
					errorBanner.value =
						"The confirmation was processed, but one or more actions failed. Check the action receipts before continuing.";
			} else {
				if (
					res.queued &&
					!recoveryState.observedRuns[res.run_id] &&
					!recoveryState.queued[cid]
				) {
					queueGeneration++;
					recoveryState.queued[cid] = {
						run_id: res.run_id,
						state: "queued",
						position: res.queued_position,
					};
				}
				if (!res.queued && !recoveryState.observedRuns[res.run_id])
					recoveryState.starting[cid] = res.run_id;
				markCardsEarlier(pending.value, cid);
				olderCardsNote.value = !!res.older_cards_waiting;
			}
			await load(true);
			if (isCurrent(cid, generation)) await loadPending();
		}
	},
	{ immediate: true }
);

async function stop() {
	if (queuedTurn.value && !live.value) return cancelWaiting();
	const runId = live.value?.runId || recoveryState.starting[convId.value] || "";
	delete recoveryState.starting[convId.value];
	if (runId) recoveryState.observedRuns[runId] = true;
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
	const target = attachments.value;
	target.push(...staged);
	const cid = convId.value;
	const generation = viewGeneration;

	await Promise.all(
		staged.map(async (a) => {
			try {
				const up = await api.uploadFile(a.file);
				const row = target.find((x) => x.key === a.key);
				if (row) {
					row.file_url = up.file_url;
					row.uploading = false;
				}
			} catch (e) {
				const index = target.findIndex((x) => x.key === a.key);
				if (index !== -1) {
					if (target[index].preview) URL.revokeObjectURL(target[index].preview);
					target.splice(index, 1);
				}
				if (isCurrent(cid, generation))
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

// Preview a pending (not-yet-sent) attachment the user taps in the composer.
// Reuses the same bottom sheet as sent-message media; a synthesized canvas-like
// item lets previewKind route by the file_url extension. messageName is empty
// (no message exists yet) - only the html/svg CanvasFrame path needs one, and
// image/PDF (the common case) render straight from the session-authed file_url.
function onPreviewPending(a) {
	if (!a || !a.file_url) return;
	preview.value = {
		item: { file_url: a.file_url, title: a.name, name: a.file_url },
		messageName: "",
	};
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

	// JF-018: fence out a superseded pump's straggler before it touches anything.
	// Because a delta carries the CUMULATIVE text, an out-of-order one REWINDS the
	// reply mid-stream, and a stale run:end/run:error tears down a turn that has
	// already been taken over. Dropping is invisible to the user: every terminal
	// path below calls load(), and that durable re-fetch is what converges content.
	// Same placement and the same kind set as the desktop SPA — see
	// jarvis/public/js/shared/pump_fence.mjs for the mirrored comparison ladder.
	if (!admitEvent(eventFence, p)) return;
	if (
		["run:start", "assistant:delta", "run:end", "run:error", "turn:cancelled"].includes(
			p.kind
		) &&
		p.run_id
	) {
		recoveryState.observedRuns[p.run_id] = true;
		if (recoveryState.starting[convId.value] === p.run_id)
			delete recoveryState.starting[convId.value];
		queueGeneration++;
		if (queuedTurn.value?.run_id === p.run_id) delete recoveryState.queued[convId.value];
	}
	if (p.kind === "queue:position" && queuedTurn.value?.run_id === p.run_id) {
		queueGeneration++;
		queuedTurn.value.position = p.position;
	}
	if (p.kind === "turn:cancelled") {
		errorBanner.value = p.reason || "The waiting request was cancelled.";
		load();
	}

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
			// Auto-heal: poll for a parked card while this turn is in flight (self-stops
			// after it settles + a couple trailing reconciles).
			startPendingPoll();
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
			// C2 self-heal: a parked confirmation card whose best-effort action:pending
			// push was missed rides the terminal here (settlement/finalize) - surface it at
			// turn-end WITHOUT a manual reload. Deduped by token; a conv-less token ("")
			// binds to this conversation, mirroring the action:pending case below. Skip a
			// run the user STOPPED - its cards were swept server-side; don't resurrect them.
			if (!ignored && Array.isArray(p.pending)) {
				for (const card of p.pending) {
					if (
						!card.token ||
						card.token === inflightToken.value ||
						pending.value.some((x) => x.token === card.token)
					)
						continue;
					pending.value.push({
						token: card.token,
						tool: card.tool || "",
						summary: card.summary || "",
						preview: card.preview ?? null,
						conversation: card.conversation || conv,
						run_id: card.run_id,
						created_at: card.created_at ?? null,
						expires_at: card.expires_at ?? null,
						seq: card.seq ?? null,
						recent: card.recent !== false,
					});
				}
			}
			// The reply is durable now; reconcile against it (canvas items, final
			// formatting) instead of trusting the streamed copy.
			load();
			break;

		case "run:error":
			sendBusy.value = false;
			live.value = null;
			if (!ignored) {
				const info = turnErrorInfo(p.error, p.code);
				errorBanner.value = `${info.headline}. ${info.hint}`;
			}
			if (p.message_id)
				errorMeta.value = { ...errorMeta.value, [p.message_id]: p.code || "" };
			// C2 self-heal (mirror run:end): a card parked in a turn that then errors
			// must still auto-recover — drain p.pending here too, not only on run:end.
			// Deduped by token; a conv-less token ("") binds to this conversation; a
			// user-STOPPED run is skipped (its cards were swept server-side).
			if (!ignored && Array.isArray(p.pending)) {
				for (const card of p.pending) {
					if (
						!card.token ||
						card.token === inflightToken.value ||
						pending.value.some((x) => x.token === card.token)
					)
						continue;
					pending.value.push({
						token: card.token,
						tool: card.tool || "",
						summary: card.summary || "",
						preview: card.preview ?? null,
						conversation: card.conversation || conv,
						run_id: card.run_id,
						created_at: card.created_at ?? null,
						expires_at: card.expires_at ?? null,
						seq: card.seq ?? null,
						recent: card.recent !== false,
					});
				}
			}
			load();
			break;

		case "action:pending":
			if (
				!p.token ||
				p.token === inflightToken.value ||
				pending.value.some((x) => x.token === p.token)
			)
				return;
			pending.value.push({
				token: p.token,
				tool: p.tool || "",
				summary: p.summary || "",
				preview: p.preview ?? null,
				conversation: conv,
				run_id: p.run_id,
				created_at: p.created_at ?? null,
				expires_at: p.expires_at ?? null,
				seq: p.seq ?? null,
				recent: p.recent !== false,
			});
			scrollToBottom();
			// This push arrived; keep polling so a SIBLING card whose push was dropped
			// in the same turn still self-heals (mirrors the desktop SPA).
			startPendingPoll();
			break;

		case "canvas":
		case "conversation:renamed":
			load();
			break;

		case "import:finished":
			// Slice B: a background CSV import finished; the bench already posted
			// the "✓ ..." completion message into this conversation. The guard
			// above already confirmed this is the open conversation, so pull it
			// in the same way canvas/conversation:renamed do — a durable reload,
			// since this frame carries no message body to splice in by hand.
			load();
			break;

		case "macro:closed":
			// A macro run ended with something to say (it failed, it stopped at a
			// card) and the bench posted that as the last message of this
			// conversation (macros._post_closing_message). Same shape as
			// import:finished above: the frame carries no message body, so re-read.
			load();
			break;
	}
}

function onResolved(token, kind) {
	pending.value = pending.value.filter((p) => p.token !== token);
	decision.value = null;
	// A real discard (P0c: Deny now calls dismiss_tool) leaves a durable
	// "discarded" receipt chip, but dismiss_tool fires no agent turn - so
	// unlike Approve (whose continuation's run:start/run:end already reloads),
	// nothing else here would ever show it. Approve needs no explicit reload:
	// the run events do it.
	if (kind === "denied") load();
}

const onResync = () => {
	refreshQueue();
	load();
	loadPending();
};

watch(
	() => props.id,
	(id) => {
		saveDraft();
		viewGeneration++;
		queueGeneration++;
		loadGeneration++;
		convId.value = id === "new" ? "" : id;
		restoreDraft();
		store.clearUnread(convId.value);
		messages.value = [];
		conversation.value = null;
		pending.value = [];
		settledTokens.value = new Set(); // a settled token belonged to the previous conversation
		olderCardsNote.value = false; // the note was about the previous conversation's cards
		live.value = null;
		sendBusy.value = false;
		errorBanner.value = "";
		refreshQueue();
		stopPendingPoll(); // a poll from the previous conversation must not carry over
		load(true);
		loadPending();
	}
);

onMounted(async () => {
	refreshQueue();
	queuePoll = setInterval(() => {
		if (queuedTurn.value) refreshQueue();
	}, 10000);
	socket?.on("jarvis:event", onEvent);
	window.addEventListener("jv:resync", onResync);
	// Auto-heal: recover a card minted while the app was backgrounded, on return.
	window.addEventListener("focus", wakePending);
	document.addEventListener("visibilitychange", onVisible);
	// Opening the chat IS reading it — drop the list's dot straight away.
	store.clearUnread(convId.value);
	if (!store.loaded) store.loadConversations();
	load(true);
	loadPending();
	_cardClockTick = setInterval(() => (cardClock.value = Date.now()), 60000);
	try {
		settings.value = await api.getChatUiSettings();
	} catch {
		// The header subtitle and the mic are both optional; a failure here must
		// not stop the user from chatting.
	}
});
onUnmounted(() => {
	saveDraft();
	viewActive = false;
	viewGeneration++;
	socket?.off("jarvis:event", onEvent);
	window.removeEventListener("jv:resync", onResync);
	window.removeEventListener("focus", wakePending);
	document.removeEventListener("visibilitychange", onVisible);
	stopPendingPoll();
	clearInterval(_cardClockTick);
	clearInterval(queuePoll);
	// Draft attachments remain in tab memory for a later return to this chat.
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
		<!-- Release-nudge version pill (Slice 3b): always-on "how current is my
		     Jarvis" status; click opens What's-new. Hidden when the target
		     version is unknown (VersionPill's own v-if). -->
		<VersionPill />
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

	<div ref="scroller" class="jv-scroll jv-thread" @scroll.passive="onScroll">
		<div v-if="!items.length && !localRequests.length && !live && !loading" class="jv-empty">
			<BrandMark :size="52" :mood="holdActive ? 'upgrading' : 'star'" />
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
						:parked-note="parkedActions.get(it.key) || parkedByCard(pending, it.key)"
						:message-key="it.key"
						@applied="onActionApplied"
						@failed="onActionApplied"
						@parked="(n) => parkedActions.set(it.key, n)"
						@dismissed="dismissedActions.add(it.key)"
					/>
					<RecordCards v-if="it.view.cards" :data="it.view.cards" />
					<ChartCard v-for="(c, ci) in it.view.charts" :key="ci" :spec="c" />
					<a
						v-if="it.view.diagrams"
						class="jv-diagram-chip"
						:href="webChatUrl"
						target="_blank"
						rel="noopener"
					>
						<svg
							class="jv-diagram-chip-ic"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.7"
							stroke-linecap="round"
							stroke-linejoin="round"
							aria-hidden="true"
						>
							<rect x="3" y="3" width="7" height="6" rx="1" />
							<rect x="14" y="15" width="7" height="6" rx="1" />
							<path d="M6.5 9v4a2 2 0 0 0 2 2H14" />
						</svg>
						<span class="jv-diagram-chip-tx">
							<span class="jv-diagram-chip-t">{{
								it.view.diagrams > 1 ? it.view.diagrams + " diagrams" : "Diagram"
							}}</span>
							<span class="jv-diagram-chip-s">Open in web chat</span>
						</span>
						<svg
							class="jv-diagram-chip-go"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.7"
							stroke-linecap="round"
							stroke-linejoin="round"
							aria-hidden="true"
						>
							<path d="M7 17 17 7M8 7h9v9" />
						</svg>
					</a>
					<SkillChips :names="it.view.skills" />
					<!-- A cancelled / aged-out queued turn is a muted note, not a
					     failure card (same as the desktop chat). -->
					<div
						v-if="it.err && it.err.code === 'cancelled'"
						class="jv-stopped"
						role="status"
					>
						{{ it.err.headline }}
					</div>
					<div v-else-if="it.err" class="jv-msg-error">
						<strong>{{ it.err.headline }}</strong>
						<p>
							{{ it.err.hint }}
							<a
								v-if="it.err.statusUrl"
								class="jv-err-link"
								:href="it.err.statusUrl"
								target="_blank"
								rel="noopener noreferrer"
								>{{ it.err.statusLabel }}
								<span aria-hidden="true">&#8599;</span></a
							>
							<span v-if="it.err.hint" aria-hidden="true"> &middot; </span>
							<button
								type="button"
								class="jv-err-link"
								:aria-expanded="rawOpen.has(it.key) ? 'true' : 'false'"
								:aria-controls="`jv-err-raw-${it.key}`"
								@click="toggleRaw(it.key)"
							>
								{{ rawOpen.has(it.key) ? "Hide details" : "Details" }}
							</button>
						</p>
						<pre
							v-if="rawOpen.has(it.key)"
							:id="`jv-err-raw-${it.key}`"
							class="jv-msg-error-raw"
							>{{ it.msg.error }}</pre
						>
					</div>
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

		<SendRecoveryCard
			v-for="request in localRequests"
			:key="request.id"
			:request="request"
			:backup="downloadRecoveryBackup"
			@edit-new="editAsNew(request)"
			@reload="reloadForUpdate"
			:disabled="sending || holdActive"
			@retry="retryRequest(request)"
			@check="checkRequest(request)"
			@edit="editRequest(request, $event)"
			@discard="sendRecovery.remove(request)"
		/>

		<section
			v-for="draft in savedDrafts"
			:key="draft.id"
			class="jv-waiting-card"
			aria-label="Saved draft"
		>
			<strong>Another draft is saved</strong>
			<p>{{ draft.text }}</p>
			<ul v-if="draft.attachments.length">
				<li v-for="(file, i) in draft.attachments" :key="i">{{ file.name }}</li>
			</ul>
			<p>Use this draft to edit it. Your current draft will stay saved here.</p>
			<button type="button" class="jv-btn is-ghost" @click="useSavedDraft(draft)">
				Use this draft
			</button>
		</section>
		<section v-if="queuedTurn" class="jv-waiting-card" aria-label="Waiting request">
			<div role="status" aria-live="polite">
				<strong>{{
					queuedTurn.state === "queued" ? "Message queued" : "Starting…"
				}}</strong>
				<p>
					{{
						queuedTurn.state === "queued"
							? "Your message is saved and waiting for a reply."
							: "Your reply is being prepared."
					}}
				</p>
				<p v-if="queuedTurn.position">Queue position: {{ queuedTurn.position }}</p>
				<p v-if="queuedTurn.note">{{ queuedTurn.note }}</p>
			</div>
			<button type="button" class="jv-btn is-ghost" @click="refreshQueue">
				Check status
			</button>
			<button
				type="button"
				class="jv-btn is-ghost"
				:disabled="queuedTurn.cancelling"
				@click="cancelWaiting"
			>
				{{ queuedTurn.cancelling ? "Cancelling…" : "Cancel request" }}
			</button>
		</section>

		<!-- the turn in flight -->
		<template v-if="live">
			<div v-if="live.text" class="jv-msg-agent">
				<div class="jv-md" v-html="renderMarkdown(stripAgentBlocks(live.text))" />
			</div>
		</template>

		<ThinkingIndicator v-if="sending && !queuedTurn && !(live && live.text)" />

		<!-- aria-live so a card surfaced by auto-heal / the menu re-check is announced to a
		     screen reader, not silently inserted. display:contents = zero layout change. -->
		<div style="display: contents" role="status" aria-live="polite">
			<DecisionCard
				v-for="(p, pi) in orderedPending"
				:key="p.token"
				:summary="
					(orderedPending.length > 1 ? `${pi + 1} of ${orderedPending.length}: ` : '') +
					(p.summary || p.tool || `${agentName} needs your approval`)
				"
				:earlier="!isRecentCard(p)"
				:proposed="proposedLabel(p.created_at, cardClock)"
				@open="decision = p"
			/>
			<p v-if="showOlderCardsNote" class="jv-typehint">
				An earlier action card is still waiting — tap it to approve or discard.
			</p>
		</div>
		<!-- Both ways to approve, shown once under the stack. -->
		<p v-if="typedApprovalHint" class="jv-typehint">{{ typedApprovalHint }}</p>
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
		:disabled="holdActive"
		:auto-mode="!!conversation?.auto_mode"
		@send="send"
		@stop="stop"
		@attach="attach"
		@remove="removeAttachment"
		@preview="onPreviewPending"
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

				<!-- Layered re-check (phase 1): the PWA has no jump affordance to ride,
				     so the on-demand control lives here (no always-visible chrome).
				     Re-surfaces a parked confirmation the silent auto-heal missed. -->
				<button class="jv-row-btn" @click="(menuOpen = false), recheckPending()">
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
						<path d="M21 12a9 9 0 1 1-2.64-6.36" />
						<path d="M21 3v6h-6" />
					</svg>
					Show confirmation
				</button>
			</template>
		</div>
	</Sheet>

	<DecisionSheet
		:action="decision"
		:streaming="sending"
		@close="decision = null"
		@resolved="onResolved"
		@busy="onDecisionBusy"
	/>
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
.jv-msg-error p {
	margin: 2px 0 0;
}
.jv-err-link {
	font: inherit;
	color: inherit;
	background: none;
	border: 0;
	padding: 0;
	cursor: pointer;
	text-decoration: underline;
	text-underline-offset: 2px;
}
.jv-msg-error-raw {
	margin: 4px 0 0;
	font: inherit;
	white-space: pre-wrap;
	overflow-wrap: anywhere;
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

/* "Open in web chat" chip for a mermaid diagram the PWA can't draw. */
.jv-diagram-chip {
	display: flex;
	align-items: center;
	gap: 10px;
	margin: 6px 0;
	padding: 10px 12px;
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card);
	color: var(--ink9);
	text-decoration: none;
	-webkit-tap-highlight-color: transparent;
}
.jv-diagram-chip:active {
	background: var(--card2);
}
.jv-diagram-chip-ic {
	width: 30px;
	height: 30px;
	flex: 0 0 auto;
	padding: 6px;
	border-radius: 8px;
	background: var(--card2);
	color: var(--accent);
	box-sizing: border-box;
}
.jv-diagram-chip-tx {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
	line-height: 1.3;
}
.jv-diagram-chip-t {
	font-size: 14px;
	font-weight: 600;
}
.jv-diagram-chip-s {
	font-size: 12.5px;
	color: var(--ink5);
}
.jv-diagram-chip-go {
	width: 16px;
	height: 16px;
	flex: 0 0 auto;
	color: var(--ink4);
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

.jv-typehint {
	margin: 6px 4px 2px;
	font-size: 12px;
	line-height: 1.4;
	color: var(--ink5);
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
.jv-waiting-card {
	border: 1px solid var(--border2);
	border-radius: 12px;
	background: var(--card);
	padding: 14px;
	margin: 10px 0;
	font-size: 13px;
	overflow-wrap: anywhere;
}
.jv-waiting-card strong {
	color: var(--ink9);
}
.jv-waiting-card p,
.jv-waiting-card ul {
	color: var(--ink6);
	line-height: 1.5;
	margin: 8px 0;
	white-space: pre-wrap;
}
.jv-waiting-card .jv-btn {
	padding: 0 12px;
	margin: 6px 6px 0 0;
	font-size: 13px;
}
</style>
