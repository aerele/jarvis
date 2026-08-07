<template>
	<!-- Jarvis mini chat. Visual language follows the "Jarvis Side Chat" design
	     board (gradient brand mark, tinted starter cards, pill composer) rather
	     than design.md's gray chrome — a deliberate, recorded divergence for this
	     surface. Kept mounted and toggled with v-show so the conversation,
	     scroll position and draft survive a close/reopen. -->
	<div
		v-show="open"
		class="jvp-root"
		:class="{ 'jvp-root--expanding': leaving }"
		:style="rootStyle"
		role="dialog"
		:aria-label="`${brandName} chat`"
		@keydown.esc.stop="$emit('close')"
	>
		<div
			class="jvp-panel"
			:class="{ 'jvp-panel--dark': isDark, 'jvp-panel--resizing': resizing }"
			ref="panelEl"
			tabindex="-1"
		>
			<!-- Resize handles on the three open edges (top / left / right) plus the
			     two top corners. The panel is anchored at its FAB-side bottom corner,
			     so a drag grows it toward the open page; each edge shows a grabber so
			     it reads as resizable. Widget.vue persists the size. -->
			<template v-if="layout">
				<div
					class="jvp-rz jvp-rz--top"
					aria-hidden="true"
					title="Drag to resize"
					@pointerdown="onResizeDown($event, 'y')"
				>
					<span class="jvp-rz-grip"></span>
				</div>
				<div
					class="jvp-rz jvp-rz--left"
					aria-hidden="true"
					title="Drag to resize"
					@pointerdown="onResizeDown($event, 'x')"
				>
					<span class="jvp-rz-grip"></span>
				</div>
				<div
					class="jvp-rz jvp-rz--right"
					aria-hidden="true"
					title="Drag to resize"
					@pointerdown="onResizeDown($event, 'x')"
				>
					<span class="jvp-rz-grip"></span>
				</div>
				<div
					class="jvp-rz jvp-rz--tl"
					role="button"
					tabindex="0"
					aria-label="Resize chat window. Drag, or use arrow keys."
					title="Drag to resize"
					@pointerdown="onResizeDown($event, 'both')"
					@keydown="onResizeKey"
				></div>
				<div
					class="jvp-rz jvp-rz--tr"
					role="button"
					tabindex="0"
					aria-label="Resize chat window. Drag, or use arrow keys."
					title="Drag to resize"
					@pointerdown="onResizeDown($event, 'both')"
					@keydown="onResizeKey"
				></div>
			</template>
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
						class="jvp-fullchat"
						type="button"
						aria-label="Open in full chat"
						@click="$emit('open-full')"
					>
						<span>Full chat</span>
						<svg
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.7"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M7 17 17 7M8 7h9v9" />
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

			<div class="jvp-body" ref="bodyEl" @scroll.passive="onBodyScroll">
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

					<div v-else class="jvp-msgs" @click="onTranscriptClick">
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
							<div class="jvp-think" role="status" aria-live="polite">
								<span class="jvp-think-dots" aria-hidden="true"
									><i></i><i></i><i></i
								></span>
								<span class="jvp-think-tx">{{ thinkingLabel }}</span>
							</div>
						</div>

						<div v-if="loadError && shownMessages.length" class="jvp-inline-err">
							<span class="jvp-err">{{ loadError }}</span>
							<button class="jvp-btn-subtle" type="button" @click="retryLast">
								Retry
							</button>
						</div>

						<!-- A turn that failed server-side. Shows the same headline the
						     full chat uses; the raw provider text (an OAuth 401 arrives
						     as a JSON blob) stays folded away behind "Show details" so it
						     cannot swamp a 400px panel. -->
						<div v-if="turnError" class="jvp-turn-err" role="alert">
							<div class="jvp-turn-err-h">{{ turnErrorHeadline }}</div>
							<div v-if="turnErrorHint" class="jvp-turn-err-hint">
								{{ turnErrorHint }}
							</div>
							<pre v-if="turnErrorOpen" class="jvp-turn-err-raw">{{
								turnError
							}}</pre>
							<div class="jvp-turn-err-acts">
								<button class="jvp-btn-subtle" type="button" @click="retryLast">
									Retry
								</button>
								<button
									v-if="turnErrorHasDetail"
									class="jvp-btn-subtle"
									type="button"
									:aria-expanded="turnErrorOpen ? 'true' : 'false'"
									@click="turnErrorOpen = !turnErrorOpen"
								>
									{{ turnErrorOpen ? "Hide details" : "Show details" }}
								</button>
							</div>
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

			<!-- Jump to latest. stickToBottom already refuses to drag a reader who
			     has scrolled up back down mid-reply, so without this arrow a long
			     streamed answer left them stranded with no way back to the newest
			     text but a manual scroll. Sits above the composer, over the body. -->
			<button
				v-if="showScrollDown && readinessResolved && readiness !== 'gate'"
				class="jvp-jump"
				type="button"
				title="Jump to latest"
				aria-label="Jump to latest"
				@click="jumpToBottom"
			>
				<svg viewBox="0 0 24 24" aria-hidden="true">
					<path
						d="M6 9.5 L12 15.5 L18 9.5"
						fill="none"
						stroke="currentColor"
						stroke-width="2.2"
						stroke-linecap="round"
						stroke-linejoin="round"
					/>
				</svg>
			</button>

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
						@input="onComposerInput"
						@keydown="onComposerKey"
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
import { resizeFrom } from "./panel_size.mjs";
import { greetingLine, suggestionsFor } from "./panel_welcome.mjs";
import { classifyReadiness, degradedMessage } from "./panel_readiness.mjs";
import { emptyStream, applyEvent, applyEventEx, visibleMessages } from "./chat_stream.mjs";
import { ONBOARDING_URL } from "./config.mjs";
import {
	listConversations,
	getConversation,
	sendMessage,
	stopRun,
	confirmTool,
	listPendingConfirmations,
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
	// True while handing off to the full web chat: the root animates open to
	// fullscreen (see .jvp-root--expanding) so the transition reads as the panel
	// growing INTO the big chat rather than a hard cut.
	leaving: { type: Boolean, default: false },
});
const emit = defineEmits(["close", "open-full", "dismiss-context", "resize", "resize-commit"]);

// ---- Resize: drag any of the three open edges (top / left / right) or a top
// corner to grow the window. The panel is anchored at its FAB-side bottom corner,
// so a drag enlarges it toward the open page. Each handle passes a mode ('y' =
// height only, 'x' = width only, 'both' = a corner); resizeFrom (panel_size.mjs)
// floors the result at the default and Widget's panelLayout re-clamps it to the
// viewport, and Widget owns the localStorage. ----
const resizing = ref(false);
let resizeStart = null;

function onResizeMove(e) {
	if (!resizeStart) return;
	const m = resizeStart.mode;
	const dx = m === "y" ? 0 : e.clientX - resizeStart.x;
	const dy = m === "x" ? 0 : e.clientY - resizeStart.y;
	emit("resize", resizeFrom(resizeStart, resizeStart.side, dx, dy));
}

function endResize() {
	if (!resizeStart) return;
	resizeStart = null;
	resizing.value = false;
	window.removeEventListener("pointermove", onResizeMove);
	window.removeEventListener("pointerup", endResize);
	window.removeEventListener("pointercancel", endResize);
	emit("resize-commit");
}

function onResizeDown(e, mode = "both") {
	const l = props.layout;
	if (!l || (e.button != null && e.button !== 0)) return;
	resizeStart = {
		x: e.clientX,
		y: e.clientY,
		width: l.width,
		height: l.height,
		side: l.side,
		mode,
	};
	resizing.value = true;
	window.addEventListener("pointermove", onResizeMove);
	window.addEventListener("pointerup", endResize);
	window.addEventListener("pointercancel", endResize);
	e.preventDefault();
}

// Keyboard resize: arrows nudge the size by a step, using the SAME grow/shrink
// semantics as the drag (up = taller, "outward" = wider), floored at the default
// by resizeFrom. Each press is its own commit so the choice survives immediately.
function onResizeKey(e) {
	const l = props.layout;
	if (!l) return;
	const STEP = 24;
	const map = {
		ArrowUp: [0, -STEP],
		ArrowDown: [0, STEP],
		ArrowLeft: [-STEP, 0],
		ArrowRight: [STEP, 0],
	};
	const d = map[e.key];
	if (!d) return;
	e.preventDefault();
	emit("resize", resizeFrom({ width: l.width, height: l.height }, l.side, d[0], d[1]));
	emit("resize-commit");
}

// Delegated click for the in-message "open in full chat" chips that
// panel_markdown swaps in for content this panel can't draw (diagrams, charts,
// record cards). The chip lives inside a v-html bubble, so it cannot bind a Vue
// handler directly.
function onTranscriptClick(e) {
	if (e.target?.closest?.(".jvp-view-chip")) emit("open-full");
}

const panelEl = ref(null);
const bodyEl = ref(null);
const textareaEl = ref(null);

const convId = ref("");
const messages = ref([]);
const stream = ref(emptyStream());
const loading = ref(false);
const loadError = ref("");
// A turn that FAILED server-side (run:error), kept separate from loadError on
// purpose. run:error also sets `reload`, and load() clears loadError on entry —
// so routing a failed turn through loadError meant the message was wiped by the
// reload it triggers, and the panel showed nothing at all. A dead LLM credential
// (auth_permanent) looked exactly like a reply that never came.
const turnError = ref("");
const turnErrorOpen = ref(false); // "Show details" disclosure
// Mirrors the full chat's turnErrorInfo / classifyTurnErrorCode
// (frontend/src/lib/errors.js, formerly ChatView.vue's own ERROR_HEADLINES /
// classifyErrorCode before #702) so both surfaces name the same failure the
// same way. This widget is a separate Desk-bundled build with no import path
// into frontend/src/lib, so the mapping is kept here as its own copy - keep
// this in sync by hand whenever errors.js's taxonomy changes (#702 review:
// this copy had silently drifted once already, still showing the old
// generic "Something went wrong" for the exact failure #702 was filed on).
// Raw provider errors are unreadable here - an OAuth 401 arrives as a
// multi-line JSON blob - so the headline is what shows and the raw text
// hides behind "Show details".
const _ERROR_HEADLINES = {
	unreachable: "I couldn't reach the assistant",
	timeout: "That took too long",
	provider: "The model is busy right now",
	"recovery-expired": "This took too long, so I stopped waiting",
	gateway: "A temporary problem interrupted this",
	internal: "Something went wrong",
	cancelled: "This message was cancelled",
};
const _ERROR_HINTS = {
	unreachable: "Check your connection, then try again.",
	timeout: "This can happen on a large request. Try again, or ask for less at once.",
	provider:
		"This looks like a provider limit or billing issue. Check your plan, then try again.",
	"recovery-expired": "Send your message again to start a fresh run.",
	gateway: "This is usually a brief hiccup on our side. Try sending your message again.",
	internal: "Try again. If it keeps happening, contact support.",
};
function classifyErrorCode(raw) {
	const low = String(raw ?? "").toLowerCase();
	if (
		low.startsWith("you cancelled this message") ||
		low.startsWith("waited too long in the queue")
	)
		return "cancelled";
	if (low.startsWith("unexpected worker error")) return "internal";
	if (
		low.includes("ws open failed") ||
		low.includes("unreachable") ||
		low.includes("connection timed out")
	)
		return "unreachable";
	if (low.includes("recovery window")) return "recovery-expired";
	if (low.includes("timed out") || low.includes("timeout") || low.includes("deadline"))
		return "timeout";
	if (
		[
			"quota",
			"rate limit",
			"rate-limit",
			"cooldown",
			"overloaded",
			"insufficient",
			"credit",
			"billing",
		].some((k) => low.includes(k))
	)
		return "provider";
	// #702: a run that reached here already started - a mid-run gateway/relay
	// hiccup, not "internal". See errors.js's classifyTurnErrorCode for the
	// full reasoning (this is the fallback that regressed once already).
	return "gateway";
}
const turnErrorCode = computed(() => classifyErrorCode(turnError.value));
const turnErrorHeadline = computed(
	() => _ERROR_HEADLINES[turnErrorCode.value] || "Something went wrong"
);
const turnErrorHint = computed(() => _ERROR_HINTS[turnErrorCode.value] || "");
// Only offer the raw text when it says more than the headline already does.
const turnErrorHasDetail = computed(() => {
	const t = (turnError.value || "").trim();
	return !!t && t !== turnErrorHeadline.value;
});
function clearTurnError() {
	turnError.value = "";
	turnErrorOpen.value = false;
}
const draft = ref("");
const sending = ref(false);
const composerFocused = ref(false);
const resolving = ref("");
const lastSent = ref("");
// Prompt recall, matching the full chat: Up walks back through prompts sent from
// this panel, Down walks forward and finally restores whatever was being typed.
// Guarded on caret position so it never fights normal multi-line editing.
const promptHistory = ref([]);
const histIdx = ref(null);
const histDraft = ref("");
function onComposerInput() {
	// Typing leaves history navigation. Without this, recalling a prompt, editing
	// it, then pressing Down would throw the edit away for the next entry.
	histIdx.value = null;
	autoGrow();
}
function onComposerKey(e) {
	const el = e.target;
	if (
		e.key === "ArrowUp" &&
		(draft.value === "" || el.selectionStart === 0) &&
		promptHistory.value.length
	) {
		e.preventDefault();
		if (histIdx.value === null) {
			histDraft.value = draft.value;
			histIdx.value = promptHistory.value.length;
		}
		if (histIdx.value > 0) {
			histIdx.value -= 1;
			draft.value = promptHistory.value[histIdx.value];
			nextTick(() => {
				const p = draft.value.length;
				el.setSelectionRange(p, p);
				autoGrow();
			});
		}
		return;
	}
	if (
		e.key === "ArrowDown" &&
		histIdx.value !== null &&
		el.selectionStart === draft.value.length
	) {
		e.preventDefault();
		if (histIdx.value < promptHistory.value.length - 1) {
			histIdx.value += 1;
			draft.value = promptHistory.value[histIdx.value];
		} else {
			histIdx.value = null;
			draft.value = histDraft.value;
		}
		nextTick(() => {
			const p = draft.value.length;
			el.setSelectionRange(p, p);
			autoGrow();
		});
	}
}
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

// Typing dots while a turn is in flight but has no visible text yet. The last
// clause is what makes the panel show a "typing" state for a turn it did NOT
// start (the same conversation open in the full web chat or another tab):
// jarvis:event frames are published to the user, so run:start reaches every
// session and sets `live` with empty text before the first token arrives.
const thinking = computed(() => {
	const s = stream.value;
	return sending.value || (s.busy && !s.live) || (!!s.live && !s.live.text);
});
// Caption beside the typing dots. The full web chat labels this wait ("Waking up
// your assistant…" / "Working on it…"); the mini panel showed bare dots, which
// read as hung on a slow turn. Same wording as the SPA's liveStatus so the two
// surfaces say the same thing. (The SPA also names the running tool; the panel
// is deliberately text-only and does not track tool frames, so it stays generic
// rather than inventing a phrase it cannot back up.)
const thinkingLabel = computed(() =>
	stream.value.status === "waking" ? "Waking up your assistant…" : "Working on it…"
);
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

// Sticky-bottom guard: auto-scroll ONLY while the reader is already at the newest
// end. Without it, scrollToBottom on every streamed token yanked a long reply back
// to its tail, so only the last screenful was ever visible in the small panel and
// the rest could not be read until the turn ended (the full SPA guards the same
// way via pinnedToBottom). A user scroll updates the flag; a send / load / new
// chat re-pins so a fresh turn always snaps to the bottom.
const pinnedToBottom = ref(true);
// Reveals the jump-to-latest arrow. Deliberately a wider gap than the pin
// threshold: between the two the panel has stopped following the reply but the
// reader is still close enough that an arrow would be noise.
const showScrollDown = ref(false);
function distanceFromBottom() {
	const el = bodyEl.value;
	if (!el) return 0;
	return el.scrollHeight - el.scrollTop - el.clientHeight;
}
function onBodyScroll() {
	const d = distanceFromBottom();
	pinnedToBottom.value = d <= 80;
	showScrollDown.value = d > 140;
}
function stickToBottom() {
	// Growing content must never RE-PIN a reader who scrolled up: only their own
	// scroll does that (onBodyScroll, on the @scroll listener). Just keep the
	// jump-to-latest arrow's visibility honest as the panel grows.
	if (pinnedToBottom.value) {
		scrollToBottom();
		// Following means we are at the newest text: clear any arrow a mid-growth
		// scroll event flipped on, or it lingers pointing nowhere.
		showScrollDown.value = false;
	} else showScrollDown.value = distanceFromBottom() > 140;
}
// Arrow click: re-pin so the panel follows the rest of the reply again.
function jumpToBottom() {
	pinnedToBottom.value = true;
	showScrollDown.value = false;
	scrollToBottom();
}
// The arrow has to track the CONTENT, not the delivery path. stickToBottom()
// runs only on live realtime frames (see onRealtime), so when a reply lands via
// the polling safety net instead, nothing recomputed it: a reader parked at the
// top of a long answer was stranded with the reply running thousands of pixels
// past the fold and no arrow to follow it. Watching the thread itself covers
// every path. MutationObserver, not ResizeObserver: the latter reports the
// scroll BOX, whose size never changes as the thread inside it grows.
let bodyMO = null;
let arrowTick = null;
function watchBodyGrowth() {
	if (bodyMO || !bodyEl.value) return;
	bodyMO = new MutationObserver(() => {
		// Streaming mutates on nearly every token; one coalesced check per burst
		// is enough and keeps this off the hot path.
		if (arrowTick) return;
		arrowTick = window.setTimeout(() => {
			arrowTick = null;
			stickToBottom();
		}, 120);
	});
	bodyMO.observe(bodyEl.value, { subtree: true, childList: true, characterData: true });
}
function unwatchBodyGrowth() {
	bodyMO?.disconnect();
	bodyMO = null;
	if (arrowTick) {
		window.clearTimeout(arrowTick);
		arrowTick = null;
	}
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
	// ...and "invisible" has to include the scroll position. load() re-runs in
	// place when a turn settles, and forcing the panel back to the newest message
	// there is what yanked a reader away from the answer they were reading.
	const _convAtEntry = convId.value;
	const _hadThread = messages.value.length > 0;
	const _keepScrollTop =
		_hadThread && !pinnedToBottom.value && bodyEl.value ? bodyEl.value.scrollTop : null;
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
		// Seed recall from what was actually asked in this conversation. The panel
		// is usually opened fresh on a Desk page, so without this Up would do
		// nothing until you had already sent something in this page load.
		promptHistory.value = messages.value
			.filter((m) => m.role === "user" && typeof m.content === "string" && m.content.trim())
			.map((m) => m.content);
		histIdx.value = null;
		histDraft.value = "";
		// Resync open write confirmations. Without this a card raised while the
		// panel was closed (or a dropped realtime frame) never shows here, even
		// though the full chat has it. Best-effort: chat must work without it.
		try {
			const pc = await listPendingConfirmations(convId.value);
			const rows = (pc && pc.data && pc.data.pending) || [];
			stream.value = {
				...stream.value,
				pending: rows.map((r) => ({
					token: r.token,
					tool: r.tool || "",
					summary: r.summary || r.preview || "",
				})),
			};
		} catch (e) {
			/* leave whatever the live stream captured */
		}
		if (_keepScrollTop !== null && bodyEl.value && convId.value === _convAtEntry) {
			// In-place refresh of the thread already on screen, with the reader
			// parked somewhere in it. Put them back exactly where they were and
			// leave the blank run alone so it keeps trimming as the answer grows.
			await nextTick();
			// Re-apply for a moment: the message list was just replaced, so on this
			// tick the panel is still short and a single assignment clamps against a
			// small scrollHeight, which would strand the reader near the top.
			bodyEl.value.scrollTop = _keepScrollTop;
			const deadline = Date.now() + 900;
			const hold = () => {
				if (!bodyEl.value || pinnedToBottom.value || Date.now() > deadline) return;
				if (Math.abs(bodyEl.value.scrollTop - _keepScrollTop) > 2) {
					bodyEl.value.scrollTop = _keepScrollTop;
				}
				requestAnimationFrame(hold);
			};
			requestAnimationFrame(hold);
			pinnedToBottom.value = false;
			showScrollDown.value = true;
		} else {
			// A genuinely fresh open: land on the newest message.
			pinnedToBottom.value = true;
			showScrollDown.value = false;
			await scrollToBottom();
		}
	} catch (e) {
		loadError.value = "Could not load your conversation.";
	} finally {
		loading.value = false;
	}
}

function startNewChat() {
	convId.value = "";
	messages.value = [];
	// Keep the fence watermarks: the panel can rebind to the SAME conversation
	// (list[0]) on reopen, and a wiped fence would readmit a superseded pump's
	// straggler — the dead-banner resurrection the fence exists to prevent.
	// New runs get fresh entries; old entries are three ints per run_id.
	stream.value = { ...emptyStream(), fence: stream.value.fence };
	loadError.value = "";
	// The failure belonged to the conversation being left behind.
	clearTurnError();
	draft.value = "";
	// Recall belongs to the conversation you are in, not to the panel.
	promptHistory.value = [];
	histIdx.value = null;
	histDraft.value = "";
	pinnedToBottom.value = true;
	showScrollDown.value = false;
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
	// A new attempt supersedes the previous failure's notice.
	clearTurnError();
	lastSent.value = text;

	// Optimistic echo so the panel feels immediate. run:end reloads from the
	// durable record, which replaces this.
	messages.value.push({ name: `local-${Date.now()}`, role: "user", content: text });
	// Recall history: skip an immediate repeat so holding Up isn't a wall of the
	// same line, and reset the cursor so the next Up starts from the newest.
	if (promptHistory.value[promptHistory.value.length - 1] !== text)
		promptHistory.value.push(text);
	histIdx.value = null;
	histDraft.value = "";
	draft.value = "";
	attachments.value = [];
	await nextTick();
	autoGrow();
	// Land on the new turn first, so you see your question and the answer start
	// without reaching for the arrow — then STOP following, so the answer grows
	// downward instead of sliding its own opening up out of the panel as it is
	// written. Staying pinned is what made a long reply unreadable here too.
	await scrollToBottom();
	pinnedToBottom.value = false;
	showScrollDown.value = false;

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
	clearTurnError();
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

	// A failed turn on the Desk chat widget - report it (a socket event, so the
	// global handler never sees it). window.jarvisReportError is installed by
	// jarvis_error_reporter.bundle.js.
	if (payload?.kind === "run:error") {
		window.jarvisReportError?.({
			surface: "desk_chat",
			error_code: payload.code || "run_error",
			error_class: "RunError",
			message: payload.error || "The turn failed.",
			conversation: conv,
			run_id: payload.run_id || "",
		});
	}

	const { state: next } = applyEventEx(stream.value, payload);

	// NB: we deliberately do NOT stop polling when a realtime frame arrives.
	// Realtime gives the smooth live stream, but the relay can drop the TAIL
	// (the final deltas / run:end) after delivering the first part — which left
	// the panel frozen on a partial reply ("only the text up to some word shows")
	// until a manual reload. Polling stays on as the safety net and settles only
	// once the DURABLE message is complete (streaming=0, see startPolling), so a
	// dropped tail self-heals within one poll cycle.
	if (next.reload) {
		// Clear the flag before reloading so a second frame cannot double-fetch.
		stream.value = { ...next, reload: false, error: "" };
		sending.value = false;
		// A failed turn has to outlive the reload it just triggered, so it goes to
		// turnError, which load() leaves alone. Sending it to loadError meant
		// load()'s own reset erased it before it ever rendered.
		if (next.error) turnError.value = next.error;
		load();
		return;
	}

	stream.value = next;
	if (next.live) {
		sending.value = false;
		stickToBottom();
	}
	if (next.error) turnError.value = next.error;
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
		if (!isOpen) {
			unwatchBodyGrowth();
			return;
		}
		await nextTick();
		watchBodyGrowth();
		// Focus the box, not the panel shell: opening the widget is always a
		// prelude to typing, and focusing the shell cost a click every time.
		// Escape still closes, since the keydown bubbles to the root handler.
		(textareaEl.value || panelEl.value)?.focus();
		ensureRealtime();
		await load();
		// The composer only renders once readiness resolves, so on a cold open the
		// textarea did not exist for the focus above. Claim it now that it does,
		// unless the reader has already put focus somewhere else in the panel.
		await nextTick();
		const ae = document.activeElement;
		// Claim focus only when nothing else has meaningfully taken it: a bare
		// <body> (the usual cold-open state) or somewhere inside the panel.
		const focusIsLoose = !ae || ae === document.body || !!panelEl.value?.contains(ae);
		if (props.open && textareaEl.value && focusIsLoose) textareaEl.value.focus();
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
			const gaveUp = !stream.value.live;
			// settle(), not a bare `sending = false`: giving up has to clear
			// stream.busy too, or `thinking` stays true and the panel shows the
			// typing dots and "Jarvis did not reply" at the same time — it claims to
			// be working on a turn it has just abandoned.
			settle();
			if (gaveUp) loadError.value = "Jarvis did not reply. Try again.";
			return;
		}
		if (!convId.value) return;
		try {
			const conv = await getConversation(convId.value);
			const msgs = Array.isArray(conv && conv.messages) ? conv.messages : [];
			const next = visibleMessages(msgs);
			// A COMPLETE assistant turn landed (streaming=0): adopt it and stop.
			// The streaming guard is what lets polling run alongside realtime as a
			// safety net without ever settling on a half-written reply, and it is
			// what recovers a reply whose streamed tail the relay dropped.
			const last = next[next.length - 1];
			if (next.length > before && last && last.role === "assistant" && !last.streaming) {
				messages.value = msgs;
				settle();
				stickToBottom();
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
			readinessNotice.value =
				readiness.value === "degraded" ? degradedMessage(r, brandName) : "";
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
	unwatchBodyGrowth();
	unwatchTheme?.();
	if (rtTimer) {
		window.clearInterval(rtTimer);
		rtTimer = null;
	}
	stopPolling();
	if (rtBound) window.frappe?.realtime?.off?.("jarvis:event", onRealtime);
	// Drop any drag listeners if we unmount mid-resize (no commit — nothing to save).
	window.removeEventListener("pointermove", onResizeMove);
	window.removeEventListener("pointerup", endResize);
	window.removeEventListener("pointercancel", endResize);
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
/* Handoff to the full web chat: the window grows to fill the screen, its
   corners flatten and its shadow deepens, so it reads as the panel opening INTO
   the big chat. Widget.vue drives the fullscreen size + a dimming backdrop; this
   just animates the box. Only during the handoff, so live resizing stays snappy. */
.jvp-root--expanding {
	transition: left 0.32s cubic-bezier(0.4, 0, 0.2, 1), top 0.32s cubic-bezier(0.4, 0, 0.2, 1),
		width 0.32s cubic-bezier(0.4, 0, 0.2, 1), height 0.32s cubic-bezier(0.4, 0, 0.2, 1);
}
.jvp-root--expanding .jvp-panel {
	transition: border-radius 0.32s ease, box-shadow 0.32s ease;
	border-radius: 6px;
	box-shadow: 0 40px 120px -20px rgba(24, 20, 50, 0.5), 0 12px 40px -8px rgba(24, 20, 50, 0.3);
}
@media (prefers-reduced-motion: reduce) {
	.jvp-root--expanding,
	.jvp-root--expanding .jvp-panel {
		transition: none;
	}
}
.jvp-panel {
	pointer-events: auto;
	position: relative;
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

/* ---- resize handles ----
   Thin strips along the three open edges (top / left / right) plus two top
   corners. Each edge carries an always-present grabber so the panel plainly
   reads as resizable; the strips sit in the panel's border zone, inset from the
   corners, clear of the header buttons and the composer. */
.jvp-rz {
	position: absolute;
	z-index: 6;
	touch-action: none;
	user-select: none;
	-webkit-user-select: none;
}
.jvp-rz--top {
	top: 0;
	left: 14px;
	right: 14px;
	height: 8px;
	cursor: ns-resize;
	display: grid;
	place-items: center;
}
.jvp-rz--left {
	top: 14px;
	bottom: 14px;
	left: 0;
	width: 8px;
	cursor: ew-resize;
	display: grid;
	place-items: center;
}
.jvp-rz--right {
	top: 14px;
	bottom: 14px;
	right: 0;
	width: 8px;
	cursor: ew-resize;
	display: grid;
	place-items: center;
}
/* The visible cue: a small grabber bar, always faintly shown, brightening to the
   accent on hover or while dragging. */
.jvp-rz-grip {
	display: block;
	border-radius: 999px;
	background: var(--jv-ink-3);
	opacity: 0.4;
	transition: opacity 0.12s ease, background-color 0.12s ease;
}
.jvp-rz--top .jvp-rz-grip {
	width: 28px;
	height: 3px;
}
.jvp-rz--left .jvp-rz-grip,
.jvp-rz--right .jvp-rz-grip {
	width: 3px;
	height: 28px;
}
.jvp-panel:hover .jvp-rz-grip {
	opacity: 0.55;
}
.jvp-rz:hover .jvp-rz-grip,
.jvp-panel--resizing .jvp-rz-grip {
	opacity: 1;
	background: var(--jv-accent);
}
/* Corners sit above the edge strips and resize both axes at once. */
.jvp-rz--tl,
.jvp-rz--tr {
	top: 0;
	width: 15px;
	height: 15px;
	z-index: 7;
}
.jvp-rz--tl {
	left: 0;
	cursor: nwse-resize;
}
.jvp-rz--tr {
	right: 0;
	cursor: nesw-resize;
}
.jvp-rz--tl:focus-visible,
.jvp-rz--tr:focus-visible {
	outline: 2px solid var(--jv-accent);
	outline-offset: -3px;
	border-radius: 8px;
}
@media (prefers-reduced-motion: reduce) {
	.jvp-rz-grip {
		transition: none;
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
/* Highlighted "go to the full web chat" affordance. Replaces the old bare
   maximize icon, which people did not notice: a labelled accent pill that fills
   on hover, so the way out to the big chat reads at a glance. */
.jvp-fullchat {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	height: 29px;
	padding: 0 10px;
	border: 1px solid var(--jv-accent);
	border-radius: 8px;
	background: transparent;
	color: var(--jv-accent);
	font: inherit;
	font-size: 12.5px;
	font-weight: 600;
	white-space: nowrap;
	cursor: pointer;
	transition: background-color 0.12s ease, color 0.12s ease;
}
.jvp-fullchat svg {
	width: 14px;
	height: 14px;
}
.jvp-fullchat:hover {
	background: var(--jv-accent);
	color: #fff;
}
.jvp-fullchat:focus-visible {
	outline: 2px solid var(--jv-accent);
	outline-offset: 2px;
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
/* Jump-to-latest arrow. The panel is a flex column, so align-self parks it at
   the right edge and the negative top margin floats it over the last lines of
   the body. The margins cancel out (-44 + 36 + 8 = 0), so showing or hiding it
   never shifts the composer below. */
.jvp-jump {
	position: relative;
	z-index: 3;
	align-self: flex-end;
	margin: -44px 14px 8px 0;
	width: 36px;
	height: 36px;
	display: flex;
	align-items: center;
	justify-content: center;
	padding: 0;
	border: 1px solid var(--jv-rule-2);
	border-radius: 50%;
	background: var(--jv-surface);
	color: var(--jv-ink-2);
	cursor: pointer;
	box-shadow: 0 2px 10px rgba(0, 0, 0, 0.16);
}
.jvp-jump:hover {
	color: var(--jv-ink);
	border-color: var(--jv-ink-3);
}
.jvp-jump:focus-visible {
	outline: 2px solid var(--jv-accent);
	outline-offset: 2px;
}
.jvp-jump svg {
	width: 18px;
	height: 18px;
	display: block;
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

/* ---- failed turn ---- */
.jvp-turn-err {
	margin: 4px 0 2px;
	padding: 10px 12px;
	border: 1px solid var(--jv-danger);
	border-radius: 10px;
	background: var(--jv-surface-2, transparent);
	display: flex;
	flex-direction: column;
	gap: 8px;
}
.jvp-turn-err-h {
	font-size: 13px;
	font-weight: 600;
	color: var(--jv-danger);
}
.jvp-turn-err-hint {
	font-size: 11.5px;
	line-height: 1.4;
	color: var(--jv-text-2, inherit);
	opacity: 0.85;
}
.jvp-turn-err-raw {
	margin: 0;
	max-height: 160px;
	overflow: auto;
	font-size: 11px;
	line-height: 1.45;
	white-space: pre-wrap;
	word-break: break-word;
	color: var(--jv-text-2, inherit);
	opacity: 0.85;
}
.jvp-turn-err-acts {
	display: flex;
	align-items: center;
	gap: 8px;
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

/* "Open in full chat" chip: stands in for content this panel can't draw
   (diagram / chart / record cards). Injected inside a v-html bubble, so it is
   reached with :deep(). */
.jvp-m-bot :deep(.jvp-view-chip) {
	display: flex;
	align-items: center;
	gap: 10px;
	width: 100%;
	margin: 4px 0;
	padding: 9px 11px;
	border: 1px solid var(--jv-rule-2);
	border-radius: 11px;
	background: var(--jv-surface);
	color: var(--jv-ink);
	font: inherit;
	text-align: left;
	cursor: pointer;
	transition: border-color 0.12s ease, background-color 0.12s ease;
}
.jvp-m-bot :deep(.jvp-view-chip:hover) {
	border-color: var(--jv-accent);
	background: var(--jv-chip-3);
}
.jvp-m-bot :deep(.jvp-view-chip:focus-visible) {
	outline: 2px solid var(--jv-accent);
	outline-offset: 1px;
}
.jvp-m-bot :deep(.jvp-view-chip-ic) {
	width: 26px;
	height: 26px;
	flex: 0 0 auto;
	padding: 5px;
	border-radius: 8px;
	background: var(--jv-chip-3);
	color: var(--jv-accent);
	box-sizing: border-box;
}
.jvp-m-bot :deep(.jvp-view-chip-tx) {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
	line-height: 1.3;
}
.jvp-m-bot :deep(.jvp-view-chip-t) {
	font-size: 13.5px;
	font-weight: 600;
}
.jvp-m-bot :deep(.jvp-view-chip-s) {
	font-size: 12px;
	color: var(--jv-ink-2);
}
.jvp-m-bot :deep(.jvp-view-chip-go) {
	width: 15px;
	height: 15px;
	flex: 0 0 auto;
	color: var(--jv-ink-3);
}
.jvp-m-bot :deep(.jvp-view-chip:hover .jvp-view-chip-go) {
	color: var(--jv-accent);
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
/* The caption beside the dots ("Working on it…"), matching the web chat. */
.jvp-think-tx {
	font-size: 13px;
	line-height: 1.2;
	color: var(--jv-ink-2);
	white-space: nowrap;
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
