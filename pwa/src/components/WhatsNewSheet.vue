<script setup>
// WhatsNewSheet: the fetch-on-click "What's new" panel (Slice 3b), mirroring
// the desktop SPA's WhatsNewDialog.vue - but as a bottom Sheet (modeled on
// DecisionSheet.vue), since there is no frappe-ui Dialog anywhere in this app.
// Mounted ONCE at the app shell (App.vue) and opened from three places - the
// version pill, the soft banner, and the hard gate - all via the shared
// `whatsNewOpen` ref in noticeGate.js.
//
// The bench owns the version, so notes() takes no arguments (it derives track
// + since from jarvis.__version__). The result is cached per target version so
// re-opening the sheet never refetches - `notice.version` is stable for the
// page's lifetime (a hard-gate recheck forces a full reload).
import { ref, watch } from "vue";
import { call } from "frappe-ui";
import Sheet from "./Sheet.vue";
import { renderMarkdown } from "@shared/markdown.js";
import { notice, whatsNewOpen } from "../noticeGate";
import { agentName } from "@/branding";

// Module-scope cache keyed by target version, so it survives close/reopen.
const NOTES_CACHE = new Map();

const loading = ref(false);
const error = ref(false);
const notes = ref([]);

async function load(force = false) {
	const key = notice.version || "";
	if (!force && NOTES_CACHE.has(key)) {
		notes.value = NOTES_CACHE.get(key);
		loading.value = false;
		error.value = false;
		return;
	}
	loading.value = true;
	error.value = false;
	try {
		const res = await call("jarvis.release_notice.notes");
		const list = (res && res.notes) || [];
		NOTES_CACHE.set(key, list);
		notes.value = list;
	} catch (e) {
		// Never surface raw CP prose - a lapsed customer (AdminAuthError) or an
		// unreachable admin both land here as the same friendly error state.
		error.value = true;
	} finally {
		loading.value = false;
	}
}

watch(whatsNewOpen, (isOpen) => {
	if (isOpen) load();
});

function close() {
	whatsNewOpen.value = false;
}
</script>

<template>
	<Sheet :open="whatsNewOpen" @close="close">
		<div class="jv-wnew">
			<div class="jv-wnew-head">
				<div class="jv-wnew-title">What's new in {{ agentName }}</div>
				<button class="jv-icon-btn" aria-label="Close" @click="close">
					<svg
						viewBox="0 0 24 24"
						width="18"
						height="18"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
					>
						<path d="M18 6 6 18M6 6l12 12" />
					</svg>
				</button>
			</div>

			<div class="jv-wnew-body">
				<!-- loading: between open and the notes() resolve -->
				<div v-if="loading" class="jv-wnew-state">
					<span class="jv-spinner" />
				</div>

				<!-- error: friendly, never raw control-plane text -->
				<div v-else-if="error" class="jv-wnew-state is-error">
					<span>Couldn't load release notes. Please try again in a moment.</span>
					<button class="jv-wnew-retry" type="button" @click="load(true)">Retry</button>
				</div>

				<!-- empty: nothing newer than this workspace -->
				<div v-else-if="!notes.length" class="jv-wnew-state">
					<span>You're all caught up.</span>
				</div>

				<!-- notes, newest first: version heading via {{ }} (never v-html),
				     body via renderMarkdown (escape-first, XSS-safe) in a prose block -->
				<div v-else class="jv-wnew-notes">
					<section v-for="note in notes" :key="note.version">
						<div class="jv-wnew-note-head">
							<h3 class="jv-wnew-note-version">{{ note.version }}</h3>
							<span v-if="note.title" class="jv-wnew-note-title">{{
								note.title
							}}</span>
						</div>
						<div
							class="prose prose-sm max-w-none jv-wnew-note-body"
							v-html="renderMarkdown(note.body)"
						></div>
					</section>
				</div>
			</div>
		</div>
	</Sheet>
</template>

<style scoped>
/* This sheet is reachable from the hard gate (UpdateNoticeGate, z-index 1000)
   via its "See what's new" link, so it must render above it - Sheet.vue's own
   z-index (60) is meant for the ordinary case of one sheet over the thread.
   No :deep() needed: a child component's ROOT node carries this component's
   own scope attribute too (same convention as ChatView.vue's ".jv-tools"
   comment), and .jv-sheet-root is Sheet.vue's root. */
.jv-sheet-root {
	z-index: 1100;
}

.jv-wnew {
	display: flex;
	flex-direction: column;
	min-height: 0;
	max-height: 80dvh;
}
.jv-wnew-head {
	display: flex;
	align-items: center;
	gap: 6px;
	padding: 2px 8px 10px 20px;
	border-bottom: 1px solid var(--border);
	flex: none;
}
.jv-wnew-title {
	flex: 1;
	min-width: 0;
	font-size: 16px;
	font-weight: 600;
	color: var(--ink9);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-wnew-body {
	flex: 1;
	min-height: 0;
	overflow-y: auto;
	padding: 16px 20px 24px;
}
.jv-wnew-state {
	display: flex;
	flex-direction: column;
	align-items: center;
	gap: 12px;
	padding: 40px 16px;
	font-size: 13.5px;
	color: var(--ink5);
	text-align: center;
}
.jv-wnew-state.is-error {
	color: var(--ink6);
}
.jv-wnew-retry {
	padding: 8px 16px;
	border: 1px solid var(--border2);
	border-radius: 9px;
	background: var(--card);
	color: var(--ink8);
	font: inherit;
	font-size: 13px;
	font-weight: 600;
	cursor: pointer;
}
.jv-wnew-notes {
	display: flex;
	flex-direction: column;
	gap: 22px;
}
.jv-wnew-note-head {
	display: flex;
	flex-wrap: wrap;
	align-items: baseline;
	gap: 8px;
}
.jv-wnew-note-version {
	margin: 0;
	font-size: 15px;
	font-weight: 700;
	color: var(--ink9);
}
.jv-wnew-note-title {
	font-size: 13px;
	color: var(--ink6);
}
.jv-wnew-note-body {
	margin-top: 4px;
	color: var(--ink7);
}
.jv-spinner {
	width: 20px;
	height: 20px;
	border-radius: 50%;
	border: 2px solid var(--card3);
	border-top-color: var(--accent);
	animation: jv-spin 0.7s linear infinite;
}
@keyframes jv-spin {
	to {
		transform: rotate(360deg);
	}
}
</style>
