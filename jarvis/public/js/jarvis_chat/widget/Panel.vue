<template>
	<!-- Jarvis mini chat. Visual language follows the "Jarvis Side Chat" design
	     board (gradient brand mark, tinted starter cards, pill composer) rather
	     than design.md's gray chrome — a deliberate, recorded divergence for this
	     surface. Kept mounted and toggled with v-show so the conversation,
	     scroll position and draft survive a close/reopen. -->
	<div
		v-show="open"
		class="jvp-root"
		:style="rootStyle"
		role="dialog"
		:aria-label="`${brandName} chat`"
		@keydown.esc.stop="$emit('close')"
	>
		<div class="jvp-panel" :class="{ 'jvp-panel--dark': isDark }" ref="panelEl" tabindex="-1">
			<div class="jvp-head">
				<div class="jvp-avatar">
					<img v-if="brandLogoUrl" :src="brandLogoUrl" class="jvp-avatar-img" alt="" />
					<svg v-else viewBox="0 0 24 24" fill="#fff" aria-hidden="true">
						<path
							d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z"
						/>
					</svg>
					<i class="jvp-online" aria-hidden="true"></i>
				</div>
				<div class="jvp-title">{{ brandName }}</div>
				<div class="jvp-actions">
					<button
						class="jvp-ib"
						type="button"
						aria-label="New chat"
						@click="startNewChat"
					>
						<svg
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.6"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M12 5v14M5 12h14" />
						</svg>
					</button>
					<button
						class="jvp-ib"
						type="button"
						aria-label="Open full chat"
						@click="$emit('open-full')"
					>
						<svg
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.6"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M15 3h6v6M21 3l-7 7M10 21H4v-6M4 21l7-7" />
						</svg>
					</button>
					<button
						class="jvp-ib"
						type="button"
						aria-label="Close"
						@click="$emit('close')"
					>
						<svg
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.6"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M18 6 6 18M6 6l12 12" />
						</svg>
					</button>
				</div>
			</div>

			<div v-if="contextText" class="jvp-ctx">
				<svg
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.6"
					stroke-linecap="round"
					stroke-linejoin="round"
					aria-hidden="true"
				>
					<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
					<path d="M14 2v6h6" />
				</svg>
				<div class="jvp-ctx-txt">
					Viewing <b>{{ contextText }}</b>
				</div>
				<button
					class="jvp-ib jvp-ib--sm"
					type="button"
					aria-label="Stop using this page as context"
					@click="$emit('dismiss-context')"
				>
					<svg
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="1.6"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M18 6 6 18M6 6l12 12" />
					</svg>
				</button>
			</div>

			<div class="jvp-body" ref="bodyEl">
				<!-- Never onboarded (readiness === "gate"): the panel cannot possibly
				     chat, so the whole body - welcome, history, composer below - is
				     replaced by a compact setup nudge instead of a chat box that can
				     only fail. Mirrors OnboardingGate.vue's full-screen poster, sized
				     for 400px. -->
				<div v-if="readiness === 'gate'" class="jvp-nudge">
					<div class="jvp-hero">
						<svg viewBox="0 0 24 24" fill="#fff" aria-hidden="true">
							<path
								d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z"
							/>
						</svg>
					</div>
					<div class="jvp-nudge-h">Finish setting up {{ brandName }}</div>
					<p class="jvp-nudge-s">
						<template v-if="canOnboard">
							This workspace isn't connected to an AI agent yet. Complete a short
							setup to start chatting with {{ brandName }} about your ERPNext data.
						</template>
						<template v-else>
							{{ brandName }} isn't set up for this workspace yet. Please ask your
							administrator (a System Manager) to complete onboarding.
						</template>
					</p>
					<button
						v-if="canOnboard"
						type="button"
						class="jvp-nudge-btn"
						@click="goOnboard"
					>
						Complete setup
						<span aria-hidden="true">→</span>
					</button>
				</div>

				<template v-else>
					<div v-if="loading" class="jvp-center">Restoring your last conversation…</div>

					<div v-else-if="loadError && !shownMessages.length" class="jvp-center">
						<div class="jvp-err">{{ loadError }}</div>
						<button class="jvp-btn-subtle" type="button" @click="load">Retry</button>
					</div>

					<!-- Welcome: brand mark, greeting, and starting points. -->
					<div
						v-else-if="!shownMessages.length && !stream.live && !thinking"
						class="jvp-welcome"
					>
						<div class="jvp-hero">
							<svg viewBox="0 0 24 24" fill="#fff" aria-hidden="true">
								<path
									d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z"
								/>
							</svg>
						</div>
						<div class="jvp-greet">{{ greeting }}</div>
						<p class="jvp-greet-sub">
							<template v-if="contextText">
								Jarvis can see <b>{{ contextText }}</b> while you are on this page.
							</template>
							<template v-else>
								Ask about your ERP data, run a workflow, or draft something.
							</template>
						</p>
						<div class="jvp-cards">
							<button
								v-for="(s, i) in suggestions"
								:key="s.title"
								class="jvp-card"
								type="button"
								@click="useSuggestion(s.prompt)"
							>
								<span
									class="jvp-card-ic"
									:class="`jvp-card-ic--${i % 4}`"
									aria-hidden="true"
								>
									<svg
										viewBox="0 0 24 24"
										fill="none"
										stroke="currentColor"
										stroke-width="1.7"
										stroke-linecap="round"
										stroke-linejoin="round"
									>
										<path :d="CARD_ICONS[i % CARD_ICONS.length]" />
									</svg>
								</span>
								<span class="jvp-card-txt">
									<span class="jvp-card-t">{{ s.title }}</span>
									<span class="jvp-card-p">{{ s.prompt }}</span>
								</span>
							</button>
						</div>
					</div>

					<div v-else class="jvp-msgs">
						<template v-for="m in shownMessages" :key="m.name">
							<div v-if="m.role === 'user'" class="jvp-row jvp-row--user">
								<div class="jvp-m-user">{{ m.content }}</div>
							</div>
							<div v-else class="jvp-row">
								<div class="jvp-m-avatar" aria-hidden="true">
									<svg viewBox="0 0 24 24" fill="#fff">
										<path
											d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z"
										/>
									</svg>
								</div>
								<div class="jvp-m-bot jv-md" v-html="renderReply(m.content)"></div>
							</div>
						</template>

						<div v-if="stream.live && stream.live.text" class="jvp-row">
							<div class="jvp-m-avatar" aria-hidden="true">
								<svg viewBox="0 0 24 24" fill="#fff">
									<path
										d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z"
									/>
								</svg>
							</div>
							<div
								class="jvp-m-bot jv-md"
								v-html="renderReply(stream.live.text)"
							></div>
						</div>

						<!-- Waiting for the first token: a labelled state, not a bare
					     spinner, so the user knows the turn was accepted. -->
						<div v-else-if="thinking" class="jvp-row">
							<div class="jvp-m-avatar" aria-hidden="true">
								<svg viewBox="0 0 24 24" fill="#fff">
									<path
										d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z"
									/>
								</svg>
							</div>
							<div
								class="jvp-think"
								role="status"
								aria-live="polite"
								aria-label="Jarvis is typing"
							>
								<span class="jvp-think-dots" aria-hidden="true"
									><i></i><i></i><i></i
								></span>
							</div>
						</div>

						<div v-if="loadError && shownMessages.length" class="jvp-inline-err">
							<span class="jvp-err">{{ loadError }}</span>
							<button class="jvp-btn-subtle" type="button" @click="retryLast">
								Retry
							</button>
						</div>
					</div>
				</template>
			</div>

			<!-- Degraded: onboarded once, but is_ready_for_chat currently says no
			     (e.g. expired LLM creds, a stalled container). The chat stays fully
			     usable - unlike the gate above, this is not a hard block - it just
			     warns that a send may fail right now. -->
			<div v-if="readiness === 'degraded'" class="jvp-notice">
				<svg
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.8"
					stroke-linecap="round"
					stroke-linejoin="round"
					aria-hidden="true"
				>
					<path d="M12 9v4M12 17h.01" />
					<path
						d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"
					/>
				</svg>
				<span>{{ readinessNotice }}</span>
			</div>

			<div v-if="stream.pending.length" class="jvp-pending">
				<div v-for="p in stream.pending" :key="p.token" class="jvp-pending-row">
					<div class="jvp-pending-txt">
						{{ p.summary || "Jarvis wants to make a change." }}
					</div>
					<div class="jvp-pending-acts">
						<button class="jvp-btn-subtle" type="button" @click="$emit('open-full')">
							Review in full chat
						</button>
						<button
							class="jvp-btn-solid"
							type="button"
							:disabled="resolving === p.token"
							@click="resolvePending(p.token)"
						>
							{{ resolving === p.token ? "Confirming…" : "Confirm" }}
						</button>
					</div>
				</div>
			</div>

			<!-- Hidden entirely in the gate state, not merely disabled: there is
			     nothing to send to yet, and a live-but-disabled composer would
			     imply chat almost works. The "degraded" state keeps this, on
			     purpose - see the jvp-notice banner above.
			     Also hidden while the verdict is still unresolved. Rendering a
			     live composer before the check returns let a user send into a
			     workspace that turned out to be gated: the send succeeded, then
			     the gate replaced the body and swallowed the message and any
			     reply, with no error shown. Waiting costs nothing in practice -
			     the panel mounts at Desk page load and is toggled with v-show, so
			     the check has almost always resolved before it is first opened. -->
			<div v-if="readinessResolved && readiness !== 'gate'" class="jvp-foot">
				<!-- attached files, above the input -->
				<div v-if="attachments.length" class="jvp-atts">
					<span v-for="a in attachments" :key="a.file_url" class="jvp-att">
						<svg
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.7"
							stroke-linecap="round"
							stroke-linejoin="round"
							aria-hidden="true"
						>
							<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
							<path d="M14 2v6h6" />
						</svg>
						<span class="jvp-att-n">{{ a.file_name }}</span>
						<button
							class="jvp-att-x"
							type="button"
							:aria-label="`Remove ${a.file_name}`"
							@click="removeAttachment(a.file_url)"
						>
							<svg
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.8"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="M18 6 6 18M6 6l12 12" />
							</svg>
						</button>
					</span>
				</div>

				<input ref="fileEl" type="file" multiple hidden @change="onFilePicked" />

				<div
					class="jvp-comp"
					:class="{ 'jvp-comp--focus': composerFocused, 'jvp-comp--rec': recording }"
				>
					<button
						class="jvp-cib"
						type="button"
						aria-label="Attach a file"
						:disabled="uploading"
						@click="pickFile"
					>
						<svg
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.7"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path
								d="m21.4 11.1-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.9-2.9l8.5-8.5"
							/>
						</svg>
					</button>
					<textarea
						class="jvp-comp-text"
						ref="textareaEl"
						rows="1"
						:placeholder="
							contextText ? `Ask about ${contextText}…` : 'Ask Jarvis anything…'
						"
						v-model="draft"
						@focus="composerFocused = true"
						@blur="composerFocused = false"
						@input="autoGrow"
						@keydown.enter.exact.prevent="send"
					></textarea>
					<button
						v-if="sttEnabled"
						class="jvp-cib"
						:class="{ 'jvp-cib--rec': recording }"
						type="button"
						:aria-label="recording ? 'Stop recording' : 'Dictate a message'"
						:disabled="transcribing"
						@click="toggleVoice"
					>
						<svg
							v-if="!recording"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.7"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<rect x="9" y="2" width="6" height="11" rx="3" />
							<path d="M19 10a7 7 0 0 1-14 0M12 17v5" />
						</svg>
						<span v-else class="jvp-wave" aria-hidden="true"
							><i></i><i></i><i></i><i></i
						></span>
					</button>
					<button
						v-if="stream.live"
						class="jvp-send jvp-send--stop"
						type="button"
						aria-label="Stop generating"
						@click="stop"
					>
						<svg
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.7"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<rect x="7" y="7" width="10" height="10" rx="2" />
						</svg>
					</button>
					<button
						v-else
						class="jvp-send"
						type="button"
						aria-label="Send message"
						:disabled="!canSend"
						@click="send"
					>
						<svg
							viewBox="0 0 24 24"
							fill="none"
							stroke="#fff"
							stroke-width="1.8"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M12 19V5M5 12l7-7 7 7" />
						</svg>
					</button>
				</div>
				<div v-if="hint" class="jvp-foot-note">{{ hint }}</div>
			</div>
		</div>
	</div>
</template>

<script setup>
import { computed, ref, watch, nextTick, onMounted, onBeforeUnmount } from "vue";
import { contextLabel } from "./desk_context.mjs";
import { isDarkNow, watchTheme } from "./desk_theme.mjs";
import { renderReply } from "./panel_markdown.mjs";
import { greetingLine, suggestionsFor } from "./panel_welcome.mjs";
import { classifyReadiness, degradedMessage } from "./panel_readiness.mjs";
import { emptyStream, applyEvent, visibleMessages } from "./chat_stream.mjs";
import { ONBOARDING_URL } from "./config.mjs";
import {
	listConversations,
	getConversation,
	sendMessage,
	stopRun,
	confirmTool,
	uploadFile,
	transcribeAudio,
	getChatUiSettings,
	isReadyForChat,
} from "./panel_api.mjs";

const props = defineProps({
	open: { type: Boolean, default: false },
	context: { type: Object, default: null },
	// Computed by panel_anchor.panelLayout from wherever the user dragged the
	// FAB. The panel is a floating mini window, so it has no fixed home.
	layout: { type: Object, default: null },
});
defineEmits(["close", "open-full", "dismiss-context"]);

const panelEl = ref(null);
const bodyEl = ref(null);
const textareaEl = ref(null);

const convId = ref("");
const messages = ref([]);
const stream = ref(emptyStream());
const loading = ref(false);
const loadError = ref("");
const draft = ref("");
const sending = ref(false);
const composerFocused = ref(false);
const resolving = ref("");
const lastSent = ref("");
const isDark = ref(false);
let unwatchTheme = null;
const fileEl = ref(null);
const attachments = ref([]);
const uploading = ref(false);
const sttEnabled = ref(false);
const recording = ref(false);
const transcribing = ref(false);
let recorder = null;
let recChunks = [];
let recStartedAt = 0;

// Chat-readiness gate: null until resolved, then "ready" | "gate" | "degraded"
// (panel_readiness.mjs). Resolved ONCE in onMounted below and cached for the
// panel's lifetime - this component is kept mounted and toggled with v-show
// (see the template comment up top), so "once per mount" already means "once
// per Desk page load", not once per open. There is deliberately no poll here: a
// workspace's onboarding state does not change while a chat panel sits open.
//
// It starts null, NOT "ready". Defaulting to "ready" rendered a live composer
// during the round-trip, so on a gated workspace a fast user could send a
// message that the arriving verdict then hid along with its reply, silently.
// Mirrors AppShell.vue's `gatedOnboarding = ref(null)` and its `shellReady`
// hold for the same reason.
const readiness = ref(null);
const readinessResolved = computed(() => readiness.value !== null);
const readinessNotice = ref("");
// Only an admin who can actually reach the wizard gets the CTA button in the
// gate state, mirroring OnboardingGate.vue's isSystemManager split. Read off
// frappe.boot.user.roles - core Frappe bootinfo (User.load_user), already
// present on every Desk page, not a Jarvis boot field - so this needs no
// backend change. Matches jarvis/permissions.py's JARVIS_ADMIN_ROLES.
const deskUserRoles = window.frappe?.boot?.user?.roles || [];
const canOnboard =
	deskUserRoles.includes("System Manager") || deskUserRoles.includes("Jarvis Admin");

const contextText = computed(() => contextLabel(props.context));

// The panel is positioned, not docked: left/top/width/height all come from the
// FAB's current spot so dragging the launcher moves its window with it.
const rootStyle = computed(() => {
	const l = props.layout;
	if (!l) return { display: "none" };
	return {
		left: `${l.left}px`,
		top: `${l.top}px`,
		width: `${l.width}px`,
		height: `${l.height}px`,
	};
});
// Tool rows and empty shells are filtered out: this panel is text-only, and
// the raw list is mostly machine chatter (see chat_stream.visibleMessages).
const shownMessages = computed(() => visibleMessages(messages.value));
// Whitelabel: the desk widget reads branding from bootinfo (set_jarvis_boot),
// synchronously so there's no flash. Blank => Jarvis defaults.
const brandName = (window.frappe?.boot?.jarvis_agent_name || "").trim() || "Jarvis";
const brandLogoUrl = (window.frappe?.boot?.jarvis_brand_logo_url || "").trim();
// A turn is in flight from the moment the POST is away until the first token
// lands. Without this the panel looks inert for the whole worker round-trip.
const greeting = computed(() => {
	const who =
		window.frappe?.boot?.user?.full_name || window.frappe?.session?.user_fullname || "";
	return greetingLine(new Date().getHours(), who);
});
const suggestions = computed(() => suggestionsFor(props.context));

// Lucide-shaped paths for the starter-card chips, indexed alongside suggestions.
const CARD_ICONS = [
	"M3 3v18h18M7 15l4-4 3 3 5-6", // trending analysis
	"M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z", // draft / act
	"m21 21-4.3-4.3M11 19a8 8 0 1 1 0-16 8 8 0 0 1 0 16z", // search
	"M12 2v20M2 12h20", // fallback
];

// A suggestion is a starting point, not a command: it fills the composer so
// the user can edit before sending.
function useSuggestion(prompt) {
	draft.value = prompt;
	nextTick(() => {
		autoGrow();
		textareaEl.value?.focus();
	});
}

const thinking = computed(() => sending.value || (stream.value.busy && !stream.value.live));
const canSend = computed(
	() =>
		(draft.value.trim().length > 0 || attachments.value.length > 0) &&
		!sending.value &&
		!uploading.value &&
		!stream.value.live
);
const hint = computed(() => {
	if (recording.value) return "Listening… click the mic to stop";
	if (transcribing.value) return "Transcribing…";
	if (uploading.value) return "Uploading…";
	if (stream.value.live) return "Jarvis is replying…";
	if (sending.value) return "Sending…";
	return ""; // idle needs no caption
});

async function scrollToBottom() {
	await nextTick();
	if (bodyEl.value) bodyEl.value.scrollTop = bodyEl.value.scrollHeight;
}

function autoGrow() {
	const el = textareaEl.value;
	if (!el) return;
	el.style.height = "auto";
	el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
}

// The panel's contract is to continue where the user left off, so the first
// open resolves the newest conversation and restores it. A user with no history
// gets the empty state, and an id is minted on first send.
async function load() {
	// Only blank the panel when there is nothing on screen yet; a refresh over
	// an existing thread should be invisible.
	loading.value = messages.value.length === 0;
	loadError.value = "";
	try {
		if (!convId.value) {
			const list = await listConversations();
			convId.value = Array.isArray(list) && list.length ? list[0].name : "";
		}
		if (!convId.value) {
			if (!messages.value.length) messages.value = [];
			return;
		}
		const conv = await getConversation(convId.value);
		messages.value = Array.isArray(conv?.messages) ? conv.messages : [];
		await scrollToBottom();
	} catch (e) {
		loadError.value = "Could not load your conversation.";
	} finally {
		loading.value = false;
	}
}

function startNewChat() {
	convId.value = "";
	messages.value = [];
	stream.value = emptyStream();
	loadError.value = "";
	draft.value = "";
	nextTick(() => textareaEl.value?.focus());
}

function pickFile() {
	fileEl.value?.click();
}

async function onFilePicked(e) {
	const files = Array.from(e.target.files || []);
	e.target.value = ""; // let the same file be picked again
	if (!files.length) return;
	uploading.value = true;
	loadError.value = "";
	try {
		for (const f of files) {
			attachments.value.push(await uploadFile(f));
		}
	} catch (err) {
		loadError.value = "That file could not be attached.";
	} finally {
		uploading.value = false;
	}
}

function removeAttachment(url) {
	attachments.value = attachments.value.filter((a) => a.file_url !== url);
}

// Same SPA onboarding route OnboardingGate.vue's own "Complete setup" button
// pushes to (frontend/src/router/index.js "/onboarding"), reached here by a
// full navigation since the wizard lives in the SPA, not this widget.
function goOnboard() {
	window.location.assign(ONBOARDING_URL);
}

// The mic button lives in the footer, and the footer unmounts the moment the
// readiness verdict comes back "gate". Without this, a recording started during
// the round-trip loses its only stop control: MediaRecorder and the getUserMedia
// stream keep running with the mic light on, and onstop never fires, so the
// audio is lost too. Stop it from outside the template instead.
function abortRecording() {
	if (!recording.value) return;
	try {
		recorder?.stop();
	} catch {
		// Already stopped or torn down; the track cleanup below still matters.
	}
	recorder?.stream?.getTracks?.().forEach((t) => t.stop());
	recording.value = false;
}

watch(readiness, (v) => {
	if (v === "gate") abortRecording();
});

// Hold-free toggle: click to start, click to stop. The transcript lands in the
// composer rather than sending, so a misheard word can be fixed first.
async function toggleVoice() {
	if (transcribing.value) return;
	if (recording.value) {
		recorder?.stop();
		return;
	}
	if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
		loadError.value = "Recording is not supported in this browser.";
		return;
	}
	try {
		const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
		recChunks = [];
		recStartedAt = Date.now();
		recorder = new MediaRecorder(stream);
		recorder.ondataavailable = (ev) => {
			if (ev.data && ev.data.size) recChunks.push(ev.data);
		};
		recorder.onstop = async () => {
			recording.value = false;
			stream.getTracks().forEach((t) => t.stop());
			const blob = new Blob(recChunks, { type: recorder.mimeType || "audio/webm" });
			recChunks = [];
			if (!blob.size) return;
			transcribing.value = true;
			try {
				const res = await transcribeAudio(blob, (Date.now() - recStartedAt) / 1000);
				const text = (res && res.text) || "";
				if (text) {
					draft.value = draft.value ? `${draft.value} ${text}` : text;
					await nextTick();
					autoGrow();
					textareaEl.value?.focus();
				}
			} catch (err) {
				loadError.value = "Could not transcribe that recording.";
			} finally {
				transcribing.value = false;
			}
		};
		recorder.start();
		recording.value = true;
	} catch (err) {
		loadError.value = "Microphone permission was refused.";
	}
}

async function send() {
	// Belt and braces: the composer is hidden whenever readiness is "gate" or
	// still unresolved (see the template), but a stray call (e.g. Enter fired
	// before that re-render) must not reach the backend for a workspace that was
	// never onboarded. Unresolved counts too: sending into a verdict that has not
	// landed is exactly how a message got swallowed by the arriving gate.
	if (readiness.value === null || readiness.value === "gate") return;
	const text = draft.value.trim();
	const atts = attachments.value.slice();
	if ((!text && !atts.length) || sending.value || stream.value.live) return;
	sending.value = true;
	loadError.value = "";
	lastSent.value = text;

	// Optimistic echo so the panel feels immediate. run:end reloads from the
	// durable record, which replaces this.
	messages.value.push({ name: `local-${Date.now()}`, role: "user", content: text });
	draft.value = "";
	attachments.value = [];
	await nextTick();
	autoGrow();
	await scrollToBottom();

	try {
		// Context is read at SEND time, not at open time: a conversation outlives
		// the page it started on, and pinning it would leave the agent silently
		// answering about the wrong record after a navigation.
		const res = await sendMessage(convId.value, text, props.context, atts);
		if (res?.conversation_id) convId.value = res.conversation_id;
		stream.value = { ...stream.value, busy: true };
		ensureRealtime();
		startPolling();
	} catch (e) {
		sending.value = false;
		loadError.value = "Could not send. Your message was not delivered.";
	}
}

function retryLast() {
	if (!lastSent.value) return;
	draft.value = lastSent.value;
	loadError.value = "";
	// Drop the optimistic echo that never made it to the server.
	const i = messages.value.findIndex((m) => String(m.name).startsWith("local-"));
	if (i !== -1) messages.value.splice(i, 1);
	send();
}

async function stop() {
	if (!stream.value.live) return;
	try {
		await stopRun(convId.value, stream.value.live.runId);
	} catch (e) {
		/* the run ends on its own; nothing useful to say here */
	}
}

async function resolvePending(token) {
	if (resolving.value) return;
	resolving.value = token;
	try {
		await confirmTool(token, convId.value);
		stream.value = applyEvent(stream.value, { kind: "action:resolved", token });
	} catch (e) {
		loadError.value = "Could not confirm that action.";
	} finally {
		resolving.value = "";
	}
}

// The Desk already holds an authenticated socket, so the panel joins it rather
// than dialling a second one the way the PWA has to.
function onRealtime(payload) {
	const conv = payload?.conversation_id || payload?.conversation;
	if (!conv || conv !== convId.value) return;

	const next = applyEvent(stream.value, payload);

	stopPolling();
	if (next.reload) {
		// Clear the flag before reloading so a second frame cannot double-fetch.
		stream.value = { ...next, reload: false, error: "" };
		sending.value = false;
		if (next.error) loadError.value = next.error;
		load();
		return;
	}

	stream.value = next;
	if (next.live) {
		sending.value = false;
		scrollToBottom();
	}
	if (next.error) loadError.value = next.error;
}

// Load lazily on first open, not at mount: the FAB is on every Desk page and
// most page views never open the panel.
// Refresh on EVERY open, not just the first. A reply can land while the panel
// is closed, and the old first-open-only rule left the user staring at a stale
// or empty thread until they reloaded the page. `load()` keeps the messages it
// already has on screen while refetching, so this does not flash.
watch(
	() => props.open,
	async (isOpen) => {
		if (!isOpen) return;
		await nextTick();
		panelEl.value?.focus();
		ensureRealtime();
		load();
	}
);

// ---- delivery: realtime first, polling as the safety net ----
//
// frappe.realtime is not guaranteed to exist when this widget mounts (the FAB
// boots on every Desk page, sometimes before the socket layer). A plain
// optional-chained subscribe fails SILENTLY there and never retries, which
// leaves the panel on "Working..." forever while the reply sits in the
// database. So: retry the subscribe, and poll while a turn is in flight so the
// answer arrives even if the socket never does.
let rtBound = false;
let rtTries = 0;
let rtTimer = null;

function bindRealtime() {
	if (rtBound) return true;
	const rt = window.frappe && window.frappe.realtime;
	if (!rt || typeof rt.on !== "function") return false;
	rt.on("jarvis:event", onRealtime);
	rtBound = true;
	return true;
}

function ensureRealtime() {
	if (bindRealtime() || rtTimer) return;
	rtTimer = window.setInterval(() => {
		rtTries += 1;
		if (bindRealtime() || rtTries > 20) {
			window.clearInterval(rtTimer);
			rtTimer = null;
		}
	}, 500);
}

let pollTimer = null;
let pollTicks = 0;

function stopPolling() {
	if (pollTimer) {
		window.clearInterval(pollTimer);
		pollTimer = null;
	}
	pollTicks = 0;
}

// Ends the in-flight state once an answer is on screen, whichever path
// delivered it.
function settle() {
	sending.value = false;
	stream.value = { ...stream.value, live: null, busy: false, reload: false };
	stopPolling();
}

function startPolling() {
	stopPolling();
	const before = shownMessages.value.length;
	pollTimer = window.setInterval(async () => {
		pollTicks += 1;
		// ~2 minutes, then give up rather than hammer the site forever.
		if (pollTicks > 48) {
			stopPolling();
			sending.value = false;
			if (!stream.value.live) loadError.value = "Jarvis did not reply. Try again.";
			return;
		}
		if (!convId.value) return;
		try {
			const conv = await getConversation(convId.value);
			const msgs = Array.isArray(conv && conv.messages) ? conv.messages : [];
			const next = visibleMessages(msgs);
			// A new assistant turn landed: adopt it and stop.
			const last = next[next.length - 1];
			if (next.length > before && last && last.role === "assistant") {
				messages.value = msgs;
				settle();
				scrollToBottom();
			}
		} catch (e) {
			/* transient - keep polling */
		}
	}, 2500);
}

onMounted(() => {
	isDark.value = isDarkNow();
	unwatchTheme = watchTheme((d) => {
		isDark.value = d;
	});
	ensureRealtime();
	// The mic only exists when the site has STT configured.
	getChatUiSettings()
		.then((cfg) => {
			sttEnabled.value = Boolean(cfg && cfg.stt_enabled);
		})
		.catch(() => {
			sttEnabled.value = false;
		});
	// Resolved once per mount, not on every open/keystroke - see the `readiness`
	// declaration above for why that is still "once per Desk page load".
	isReadyForChat()
		.then((r) => {
			readiness.value = classifyReadiness(r);
			readinessNotice.value = readiness.value === "degraded" ? degradedMessage(r) : "";
		})
		.catch(() => {
			// Fail OPEN, same as classifyReadiness(null): a flaky/unreachable check
			// must never strand a real user behind the gate.
			readiness.value = classifyReadiness(null);
			readinessNotice.value = "";
		});
});

onBeforeUnmount(() => {
	// A live recording outlives this component otherwise: the mic stays hot and
	// the getUserMedia tracks are never released.
	abortRecording();
	unwatchTheme?.();
	if (rtTimer) {
		window.clearInterval(rtTimer);
		rtTimer = null;
	}
	stopPolling();
	if (rtBound) window.frappe?.realtime?.off?.("jarvis:event", onRealtime);
});

defineExpose({ load, startNewChat, convId });
</script>

<style scoped>
/* Palette lifted from the "Jarvis Side Chat" design board. Scoped to the panel
   so it cannot leak into Desk chrome; dark values follow the Desk theme flag. */
.jvp-panel {
	--jv-grad: linear-gradient(140deg, #8b7cf7, #6a56e8);
	--jv-accent: #6a56e8;
	--jv-surface: #ffffff;
	--jv-rule: #eeeeee;
	--jv-rule-2: #e9e9ea;
	--jv-ink: #1f272e;
	--jv-ink-2: #8a9096;
	--jv-ink-3: #b0b6bb;
	--jv-bot-bg: #f5f4f8;
	--jv-bot-bd: #eeedf4;
	--jv-comp-bg: #fafafa;
	--jv-comp-bd: #e2e2e2;
	--jv-chip-0: #f1f1f2;
	--jv-chip-1: #e4f0e7;
	--jv-chip-2: #fbeeddff;
	--jv-chip-3: #eae7fb;
	--jv-danger: #c0392b;
	--jv-warn: #b7791f;
	--jv-warn-bg: #fdf3e2;
	--jv-warn-bd: #f3e0bb;
}
.jvp-panel--dark {
	--jv-surface: #1e1d23;
	--jv-rule: #2a2833;
	--jv-rule-2: #2e2c36;
	--jv-ink: #eceaf2;
	--jv-ink-2: #9a97a6;
	--jv-ink-3: #6e6b7a;
	--jv-bot-bg: #26242e;
	--jv-bot-bd: #302e3a;
	--jv-comp-bg: #24222b;
	--jv-comp-bd: #34313f;
	--jv-chip-0: #2b2933;
	--jv-chip-1: #1d2f25;
	--jv-chip-2: #33291b;
	--jv-chip-3: #2a2540;
	--jv-danger: #ff8a80;
	--jv-warn: #f0c265;
	--jv-warn-bg: #332a18;
	--jv-warn-bd: #4a3c22;
}

/* A mini chat window, not a full-height dock: left/top/width/height are set
   inline from panel_anchor.panelLayout() so the window follows the FAB
   wherever the user dragged it. */
.jvp-root {
	position: fixed;
	z-index: 1029; /* under Frappe modals (1050), over page content */
	display: flex;
	pointer-events: none;
}
.jvp-panel {
	pointer-events: auto;
	max-width: 100%;
	display: flex;
	flex-direction: column;
	flex: 1;
	min-height: 0;
	background: var(--jv-surface);
	border: 1px solid var(--jv-rule-2);
	border-radius: 22px;
	box-shadow: 0 24px 60px -12px rgba(24, 20, 50, 0.28), 0 8px 20px -8px rgba(24, 20, 50, 0.16);
	overflow: hidden;
	font-size: 14px;
	color: var(--jv-ink);
	outline: none;
}
@media (prefers-reduced-motion: no-preference) {
	.jvp-panel {
		animation: jvp-in 120ms ease-out;
	}
}
@keyframes jvp-in {
	from {
		opacity: 0;
		transform: scale(0.98);
	}
	to {
		opacity: 1;
		transform: scale(1);
	}
}

/* ---- header ---- */
.jvp-head {
	flex: none;
	display: flex;
	align-items: center;
	gap: 11px;
	padding: 13px 15px;
	border-bottom: 1px solid var(--jv-rule);
}
.jvp-avatar {
	position: relative;
	width: 30px;
	height: 30px;
	flex: 0 0 auto;
	border-radius: 9px;
	background: var(--jv-grad);
	display: grid;
	place-items: center;
}
.jvp-avatar svg {
	width: 17px;
	height: 17px;
}
.jvp-avatar-img {
	width: 100%;
	height: 100%;
	object-fit: cover;
	border-radius: inherit;
}
.jvp-online {
	position: absolute;
	right: -2px;
	bottom: -2px;
	width: 10px;
	height: 10px;
	border-radius: 50%;
	background: #3ad07e;
	border: 2px solid var(--jv-surface);
}
.jvp-title {
	flex: 1;
	font-size: 14.5px;
	font-weight: 600;
	color: var(--jv-ink);
}
.jvp-actions {
	display: flex;
	align-items: center;
	gap: 2px;
}
.jvp-ib {
	width: 29px;
	height: 29px;
	flex: none;
	border: none;
	background: transparent;
	border-radius: 7px;
	color: var(--jv-ink-2);
	cursor: pointer;
	display: grid;
	place-items: center;
	transition: background-color 0.12s ease, color 0.12s ease;
}
.jvp-ib:hover {
	background: var(--jv-chip-0);
	color: var(--jv-ink);
}
.jvp-ib:focus-visible {
	outline: 2px solid var(--jv-accent);
	outline-offset: 1px;
}
.jvp-ib svg {
	width: 16px;
	height: 16px;
}
.jvp-ib--sm {
	width: 24px;
	height: 24px;
}
.jvp-ib--sm svg {
	width: 14px;
	height: 14px;
}

/* ---- context chip ---- */
.jvp-ctx {
	flex: none;
	margin: 11px 15px 0;
	display: flex;
	align-items: center;
	gap: 8px;
	border: 1px solid var(--jv-rule-2);
	border-radius: 11px;
	padding: 7px 8px 7px 10px;
}
.jvp-ctx svg {
	width: 15px;
	height: 15px;
	flex: none;
	color: var(--jv-ink-2);
}
.jvp-ctx-txt {
	flex: 1;
	min-width: 0;
	font-size: 12px;
	color: var(--jv-ink-2);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jvp-ctx-txt b {
	font-weight: 600;
	color: var(--jv-ink);
}

/* ---- body ---- */
.jvp-body {
	flex: 1;
	min-width: 0;
	min-height: 0;
	overflow-y: auto;
	overflow-x: hidden; /* long tokens wrap; the panel never scrolls sideways */
	padding: 16px 15px;
}
.jvp-center {
	height: 100%;
	display: flex;
	flex-direction: column;
	align-items: center;
	justify-content: center;
	gap: 10px;
	text-align: center;
	color: var(--jv-ink-2);
	font-size: 13.5px;
}
.jvp-err {
	font-size: 13px;
	color: var(--jv-danger);
}
.jvp-inline-err {
	display: flex;
	align-items: center;
	gap: 10px;
	flex-wrap: wrap;
}

/* ---- welcome ---- */
.jvp-welcome {
	min-width: 0;
	display: flex;
	flex-direction: column;
	align-items: center;
	padding: 24px 9px 8px;
}
.jvp-hero {
	width: 52px;
	height: 52px;
	border-radius: 14px;
	background: var(--jv-grad);
	display: grid;
	place-items: center;
	box-shadow: 0 14px 30px -10px rgba(106, 86, 232, 0.6);
}
.jvp-hero svg {
	width: 29px;
	height: 29px;
}
.jvp-greet {
	font-size: 22px;
	font-weight: 700;
	color: var(--jv-ink);
	margin-top: 18px;
	text-align: center;
	letter-spacing: -0.01em;
}
.jvp-greet-sub {
	margin: 7px 0 0;
	font-size: 13.5px;
	line-height: 1.55;
	color: var(--jv-ink-2);
	text-align: center;
}
.jvp-greet-sub b {
	font-weight: 600;
	color: var(--jv-ink);
}
.jvp-cards {
	display: flex;
	flex-direction: column;
	gap: 10px;
	width: 100%;
	margin-top: 28px;
}
.jvp-card {
	display: flex;
	align-items: flex-start;
	gap: 12px;
	text-align: left;
	border: 1px solid var(--jv-rule-2);
	border-radius: 12px;
	padding: 13px 14px;
	background: transparent;
	font: inherit;
	cursor: pointer;
	transition: border-color 0.12s ease, background-color 0.12s ease;
}
.jvp-card:hover {
	border-color: var(--jv-accent);
}
.jvp-card:focus-visible {
	outline: 2px solid var(--jv-accent);
	outline-offset: 1px;
}
.jvp-card-ic {
	width: 30px;
	height: 30px;
	flex: 0 0 auto;
	border-radius: 9px;
	display: grid;
	place-items: center;
	color: var(--jv-ink);
}
.jvp-card-ic svg {
	width: 16px;
	height: 16px;
}
.jvp-card-ic--0 {
	background: var(--jv-chip-0);
}
.jvp-card-ic--1 {
	background: var(--jv-chip-1);
}
.jvp-card-ic--2 {
	background: var(--jv-chip-2);
}
.jvp-card-ic--3 {
	background: var(--jv-chip-3);
}
.jvp-card-txt {
	min-width: 0;
	overflow-wrap: anywhere;
}
.jvp-card-t {
	display: block;
	font-size: 13.5px;
	font-weight: 600;
	color: var(--jv-ink);
}
.jvp-card-p {
	display: block;
	font-size: 12.5px;
	color: var(--jv-ink-2);
	margin-top: 2px;
	line-height: 1.4;
}

/* ---- onboarding nudge (readiness === "gate") ----
   Same shell as .jvp-welcome (reuses .jvp-hero) since it replaces it - a
   compact stand-in for OnboardingGate.vue's full-screen poster. */
.jvp-nudge {
	min-width: 0;
	height: 100%;
	display: flex;
	flex-direction: column;
	align-items: center;
	justify-content: center;
	text-align: center;
	padding: 20px 14px;
}
.jvp-nudge-h {
	font-size: 17px;
	font-weight: 700;
	color: var(--jv-ink);
	margin-top: 16px;
	letter-spacing: -0.01em;
}
.jvp-nudge-s {
	margin: 8px 0 0;
	font-size: 13px;
	line-height: 1.55;
	color: var(--jv-ink-2);
}
.jvp-nudge-btn {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	margin-top: 18px;
	padding: 9px 16px;
	border: none;
	border-radius: 10px;
	background: var(--jv-grad);
	color: #fff;
	font: inherit;
	font-size: 13.5px;
	font-weight: 600;
	cursor: pointer;
}
.jvp-nudge-btn:hover {
	opacity: 0.92;
}
.jvp-nudge-btn:focus-visible {
	outline: 2px solid var(--jv-accent);
	outline-offset: 2px;
}

/* ---- messages ---- */
.jvp-msgs {
	display: flex;
	flex-direction: column;
	gap: 14px;
	min-width: 0;
}
.jvp-row {
	display: flex;
	gap: 9px;
	align-items: flex-start;
	min-width: 0;
}
.jvp-row--user {
	justify-content: flex-end;
}
.jvp-m-avatar {
	width: 27px;
	height: 27px;
	flex: 0 0 auto;
	border-radius: 9px;
	background: var(--jv-grad);
	display: grid;
	place-items: center;
	margin-top: 2px;
}
.jvp-m-avatar svg {
	width: 15px;
	height: 15px;
}
.jvp-m-user {
	max-width: 270px;
	background: var(--jv-grad);
	color: #fff;
	padding: 9px 13px;
	border-radius: 16px 16px 5px 16px;
	font-size: 14px;
	line-height: 1.5;
	white-space: pre-wrap;
	overflow-wrap: anywhere;
	box-shadow: 0 8px 18px -10px rgba(106, 86, 232, 0.7);
}
.jvp-m-bot {
	min-width: 0;
	max-width: calc(100% - 36px);
	background: var(--jv-bot-bg);
	border: 1px solid var(--jv-bot-bd);
	border-radius: 5px 15px 15px 15px;
	padding: 11px 13px;
	font-size: 14px;
	line-height: 1.5;
	color: var(--jv-ink);
	overflow-wrap: anywhere;
}

/* ---- rendered markdown inside an assistant bubble ----
   :deep because the HTML comes from v-html and carries no scope id. */
.jv-md :deep(.jv-md-p) {
	margin: 0 0 8px;
}
.jv-md :deep(.jv-md-p:last-child) {
	margin-bottom: 0;
}
.jv-md :deep(.jv-md-h) {
	margin: 12px 0 6px;
	font-size: 14px;
	font-weight: 600;
	color: var(--jv-ink);
}
.jv-md :deep(.jv-md-h:first-child) {
	margin-top: 0;
}
.jv-md :deep(.jv-md-list) {
	margin: 0 0 8px;
	padding-left: 18px;
}
.jv-md :deep(.jv-md-list:last-child) {
	margin-bottom: 0;
}
.jv-md :deep(.jv-md-list li) {
	margin: 3px 0;
}
.jv-md :deep(.jv-md-list li::marker) {
	color: var(--jv-ink-3);
}
.jv-md :deep(strong) {
	font-weight: 600;
	color: var(--jv-ink);
}
.jv-md :deep(.jv-md-link) {
	color: var(--jv-accent);
	text-decoration: none;
}
.jv-md :deep(.jv-md-link:hover) {
	text-decoration: underline;
}
.jv-md :deep(.jv-md-code) {
	font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
	font-size: 12px;
	background: var(--jv-chip-0);
	border-radius: 4px;
	padding: 1px 4px;
	overflow-wrap: anywhere;
}
.jv-md :deep(.jv-md-pre) {
	margin: 8px 0;
	padding: 9px 10px;
	border-radius: 9px;
	background: var(--jv-chip-0);
	font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
	font-size: 12px;
	line-height: 1.45;
	overflow-x: auto; /* code scrolls inside its own box, never the panel */
}
.jv-md :deep(.jv-md-quote) {
	margin: 8px 0;
	padding-left: 10px;
	border-left: 2px solid var(--jv-rule-2);
	color: var(--jv-ink-2);
}

/* Tables are the reason 400px needs care: let the wrapper scroll rather than
   the panel, and keep numeric columns aligned. */
.jv-md :deep(.jv-md-tablewrap) {
	margin: 9px 0;
	overflow-x: auto;
	border: 1px solid var(--jv-rule-2);
	border-radius: 9px;
}
.jv-md :deep(.jv-md-table) {
	border-collapse: collapse;
	width: 100%;
	font-size: 12.5px;
	white-space: nowrap;
}
.jv-md :deep(.jv-md-table th),
.jv-md :deep(.jv-md-table td) {
	padding: 7px 10px;
	border-bottom: 1px solid var(--jv-rule-2);
	text-align: left;
}
.jv-md :deep(.jv-md-table th) {
	font-weight: 600;
	color: var(--jv-ink-2);
	background: var(--jv-chip-0);
	font-size: 11.5px;
}
.jv-md :deep(.jv-md-table tr:last-child td) {
	border-bottom: none;
}
.jv-md :deep(.jv-md-table td[align="right"]),
.jv-md :deep(.jv-md-table th[align="right"]) {
	text-align: right;
	font-variant-numeric: tabular-nums;
}

/* ---- waiting for a reply ---- */
.jvp-think {
	display: flex;
	align-items: center;
	gap: 9px;
	background: var(--jv-bot-bg);
	border: 1px solid var(--jv-bot-bd);
	border-radius: 5px 15px 15px 15px;
	padding: 11px 13px;
	color: var(--jv-ink-2);
}
.jvp-think-dots {
	display: inline-flex;
	align-items: flex-end;
	gap: 3px;
	height: 10px;
}
.jvp-think-dots i {
	width: 5px;
	height: 5px;
	border-radius: 999px;
	background: var(--jv-accent);
	opacity: 0.35;
}
@media (prefers-reduced-motion: no-preference) {
	.jvp-think-dots i {
		animation: jvp-dot 1.2s infinite ease-in-out;
	}
	.jvp-think-dots i:nth-child(2) {
		animation-delay: 0.15s;
	}
	.jvp-think-dots i:nth-child(3) {
		animation-delay: 0.3s;
	}
}
@keyframes jvp-dot {
	0%,
	80%,
	100% {
		transform: translateY(0);
		opacity: 0.35;
	}
	40% {
		transform: translateY(-5px);
		opacity: 1;
	}
}

/* ---- degraded notice (readiness === "degraded") ----
   Amber, not the red .jv-danger tokens: this is a warning that a send MIGHT
   fail, not a report that something already did. */
.jvp-notice {
	flex: none;
	display: flex;
	align-items: flex-start;
	gap: 8px;
	margin: 10px 15px 0;
	padding: 9px 11px;
	border: 1px solid var(--jv-warn-bd);
	border-radius: 11px;
	background: var(--jv-warn-bg);
	color: var(--jv-warn);
	font-size: 12.5px;
	line-height: 1.5;
}
.jvp-notice svg {
	width: 15px;
	height: 15px;
	flex: none;
	margin-top: 1px;
}

/* ---- pending write confirmation ---- */
.jvp-pending {
	flex: none;
	border-top: 1px solid var(--jv-rule);
	padding: 11px 15px;
}
.jvp-pending-row {
	display: flex;
	flex-direction: column;
	gap: 9px;
}
.jvp-pending-txt {
	font-size: 13px;
	line-height: 1.45;
	color: var(--jv-ink);
}
.jvp-pending-acts {
	display: flex;
	gap: 8px;
	justify-content: flex-end;
}

/* ---- composer ---- */
.jvp-foot {
	flex: none;
	padding: 12px 15px 14px;
	border-top: 1px solid var(--jv-rule);
}
.jvp-comp {
	display: flex;
	align-items: flex-end;
	gap: 8px;
	border: 1px solid var(--jv-comp-bd);
	border-radius: 14px;
	padding: 7px 8px 7px 12px;
	background: var(--jv-comp-bg);
	transition: border-color 0.12s ease, box-shadow 0.12s ease;
}
.jvp-comp--focus,
.jvp-comp:focus-within {
	border-color: var(--jv-accent);
	box-shadow: 0 0 0 3px rgba(106, 86, 232, 0.12);
}
.jvp-comp-text {
	flex: 1;
	min-width: 0;
	border: none;
	background: transparent;
	resize: none;
	font: inherit;
	font-size: 14px;
	color: var(--jv-ink);
	line-height: 1.5;
	padding: 5px 0;
	max-height: 120px;
	outline: none;
}
.jvp-comp-text::placeholder {
	color: var(--jv-ink-3);
}
.jvp-send {
	width: 33px;
	height: 33px;
	flex: 0 0 auto;
	border: none;
	border-radius: 10px;
	background: var(--jv-grad);
	display: grid;
	place-items: center;
	cursor: pointer;
	transition: opacity 0.12s ease;
}
.jvp-send svg {
	width: 17px;
	height: 17px;
}
.jvp-send:hover {
	opacity: 0.9;
}
.jvp-send:focus-visible {
	outline: 2px solid var(--jv-accent);
	outline-offset: 2px;
}
.jvp-send[disabled] {
	background: var(--jv-chip-0);
	cursor: not-allowed;
}
.jvp-send[disabled] svg {
	stroke: var(--jv-ink-3);
}
.jvp-send--stop {
	background: var(--jv-chip-0);
	color: var(--jv-ink);
}
.jvp-send--stop svg {
	stroke: currentColor;
}
/* attachments + inline composer buttons */
.jvp-atts {
	display: flex;
	flex-wrap: wrap;
	gap: 6px;
	margin-bottom: 8px;
}
.jvp-att {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	max-width: 100%;
	border: 1px solid var(--jv-rule-2);
	border-radius: 9px;
	padding: 4px 6px 4px 8px;
	font-size: 12px;
	color: var(--jv-ink-2);
	background: var(--jv-chip-0);
}
.jvp-att svg {
	width: 13px;
	height: 13px;
	flex: none;
}
.jvp-att-n {
	max-width: 150px;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jvp-att-x {
	width: 18px;
	height: 18px;
	flex: none;
	border: none;
	background: transparent;
	color: var(--jv-ink-2);
	cursor: pointer;
	display: grid;
	place-items: center;
	border-radius: 5px;
}
.jvp-att-x:hover {
	color: var(--jv-ink);
}
.jvp-att-x svg {
	width: 12px;
	height: 12px;
}

.jvp-cib {
	width: 29px;
	height: 29px;
	flex: 0 0 auto;
	align-self: flex-end;
	border: none;
	background: transparent;
	border-radius: 8px;
	color: var(--jv-ink-2);
	cursor: pointer;
	display: grid;
	place-items: center;
	transition: background-color 0.12s ease, color 0.12s ease;
}
.jvp-cib:hover:not([disabled]) {
	background: var(--jv-chip-0);
	color: var(--jv-ink);
}
.jvp-cib:focus-visible {
	outline: 2px solid var(--jv-accent);
	outline-offset: 1px;
}
.jvp-cib[disabled] {
	opacity: 0.5;
	cursor: not-allowed;
}
.jvp-cib svg {
	width: 17px;
	height: 17px;
}
.jvp-cib--rec {
	color: var(--jv-accent);
}
.jvp-comp--rec {
	border-color: var(--jv-accent);
}

/* live level bars while recording */
.jvp-wave {
	display: inline-flex;
	align-items: center;
	gap: 2px;
	height: 15px;
}
.jvp-wave i {
	width: 2.5px;
	height: 100%;
	border-radius: 2px;
	background: var(--jv-accent);
	transform: scaleY(0.3);
}
@media (prefers-reduced-motion: no-preference) {
	.jvp-wave i {
		animation: jvp-wave 0.9s infinite ease-in-out;
	}
	.jvp-wave i:nth-child(2) {
		animation-delay: 0.15s;
	}
	.jvp-wave i:nth-child(3) {
		animation-delay: 0.3s;
	}
	.jvp-wave i:nth-child(4) {
		animation-delay: 0.45s;
	}
}
@keyframes jvp-wave {
	0%,
	100% {
		transform: scaleY(0.3);
	}
	50% {
		transform: scaleY(1);
	}
}

.jvp-foot-note {
	text-align: center;
	font-size: 11px;
	color: var(--jv-ink-3);
	margin-top: 8px;
}

/* ---- buttons ---- */
.jvp-btn-subtle {
	height: 29px;
	padding: 0 11px;
	border: 1px solid var(--jv-rule-2);
	border-radius: 9px;
	background: transparent;
	color: var(--jv-ink);
	font: inherit;
	font-size: 13px;
	cursor: pointer;
}
.jvp-btn-subtle:hover {
	background: var(--jv-chip-0);
}
.jvp-btn-solid {
	height: 29px;
	padding: 0 13px;
	border: none;
	border-radius: 9px;
	background: var(--jv-grad);
	color: #fff;
	font: inherit;
	font-size: 13px;
	font-weight: 600;
	cursor: pointer;
}
.jvp-btn-solid[disabled] {
	opacity: 0.6;
	cursor: not-allowed;
}
</style>
