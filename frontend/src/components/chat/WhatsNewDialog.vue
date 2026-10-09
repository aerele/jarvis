<template>
	<Dialog v-model="open" :options="{ title: whatsNewTitle, size: 'lg' }">
		<template #body-content>
			<!-- loading: between open and the notes() resolve -->
			<div v-if="loading" class="flex justify-center py-10">
				<JvSpinner :size="28" label="Loading release notes…" />
			</div>

			<!-- error: friendly, never raw control-plane text -->
			<div
				v-else-if="error"
				class="flex flex-col items-center gap-2 py-10 text-center text-sm text-ink-gray-6"
			>
				<FeatherIcon name="alert-triangle" class="size-5 text-ink-amber-3" />
				<span>Couldn't load release notes. Please try again in a moment.</span>
				<Button label="Retry" variant="subtle" @click="load(true)" />
			</div>

			<!-- empty: nothing newer than this workspace -->
			<div
				v-else-if="!notes.length"
				class="flex flex-col items-center gap-2 py-10 text-center text-sm text-ink-gray-6"
			>
				<FeatherIcon name="check-circle" class="size-5 text-ink-green-3" />
				<span>You're all caught up.</span>
			</div>

			<!-- notes, newest first: version badge via {{ }} (never v-html), body
			     via renderMarkdown (escape-first, XSS-safe). Styling is
			     component-scoped on purpose: the app ships no Tailwind Typography
			     plugin (so `prose` was inert here) and the chat's jv-md-* styles
			     are scoped to Message.vue, so this panel styles the markup itself. -->
			<div v-else class="wn-list">
				<section v-for="note in notes" :key="note.version" class="wn-card">
					<header class="wn-card-head">
						<h3 class="wn-badge">{{ note.version }}</h3>
						<span v-if="note.title" class="wn-card-title">{{ note.title }}</span>
					</header>
					<div class="wn-body" v-html="renderMarkdown(note.body)"></div>
				</section>
			</div>
		</template>

		<template #actions>
			<Button label="Close" variant="solid" class="w-full" @click="open = false" />
		</template>
	</Dialog>
</template>

<script setup>
// WhatsNewDialog: the fetch-on-click "What's new" panel (Slice 3b), opened from
// the version pill, the soft banner, and the hard gate (all via the shared
// `whatsNewOpen` ref in noticeGate). Modeled on ConnectPhoneDialog / NoteDetailModal
// (frappe-ui Dialog, portals to <body>).
//
// The bench owns the version, so notes() takes no arguments (it derives
// track + since from jarvis.__version__). The result is cached per target
// version so re-opening the panel never refetches - `notice.version` is stable
// for the page's lifetime (a hard-gate recheck forces a full reload).
import { computed, ref, watch } from "vue";
import { Dialog, Button, FeatherIcon, call } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import { renderMarkdown } from "@/markdown";
import { notice, whatsNewOpen } from "@/noticeGate";
import { brand } from "@/branding";

const whatsNewTitle = computed(() => `What's new in ${brand.agentName}`);

// Module-scope cache keyed by target version, so it survives close/reopen (and
// is shared across the ChatView and hard-gate instances of this dialog).
const NOTES_CACHE = new Map();

const open = whatsNewOpen;
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
		// A flagged fetch failure degrades to the friendly error state, NOT the
		// "all caught up" empty state - and is never cached, so Retry refetches.
		if (res && res.error) {
			error.value = true;
			return;
		}
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

watch(open, (isOpen) => {
	if (isOpen) load();
});
</script>

<style scoped>
.wn-list {
	display: flex;
	flex-direction: column;
	gap: 14px;
}

/* Each release is its own card, so stacked notes read as distinct entries. */
.wn-card {
	border: 1px solid var(--ink-gray-3, #e5e7eb);
	border-radius: 12px;
	padding: 16px 18px;
	background: var(--surface-white, #fff);
}

.wn-card-head {
	display: flex;
	flex-wrap: wrap;
	align-items: baseline;
	gap: 8px;
	margin-bottom: 10px;
}

/* Version as an accent pill (matches the update-gate badge). Kept an <h3> so
   each release stays a screen-reader sub-heading under the dialog title. */
.wn-badge {
	display: inline-block;
	margin: 0;
	padding: 3px 10px;
	border-radius: 999px;
	font-size: 12px;
	font-weight: 600;
	letter-spacing: 0.2px;
	color: #6e5cf6;
	background: rgba(110, 92, 246, 0.12);
}

.wn-card-title {
	font-size: 13px;
	color: var(--ink-gray-6, #6b7280);
}

/* Markdown body. The renderer emits jv-md-* classes and bare <strong>/<em>/
   <a>; style them here via :deep() since the v-html'd nodes carry no scope
   attribute. */
.wn-body {
	font-size: 14px;
	line-height: 1.6;
	color: var(--ink-gray-7, #4b5563);
}
.wn-body :deep(.jv-md-p) {
	margin: 0 0 8px;
}
.wn-body :deep(.jv-md-p:last-child) {
	margin-bottom: 0;
}
/* A bold-only line is how notes label a section (e.g. "**✨ New**"); give it a
   little air above so sections separate, and let the emphasis carry weight. */
.wn-body :deep(strong) {
	font-weight: 600;
	color: var(--ink-gray-9, #171717);
}
.wn-body :deep(.jv-md-list + .jv-md-p) {
	margin-top: 12px;
}
.wn-body :deep(.jv-md-list) {
	margin: 4px 0 10px;
	padding-left: 20px;
	list-style: disc;
}
.wn-body :deep(ol.jv-md-list) {
	list-style: decimal;
}
.wn-body :deep(.jv-md-list li) {
	margin: 3px 0;
}
.wn-body :deep(.jv-md-list:last-child) {
	margin-bottom: 0;
}
.wn-body :deep(.jv-md-h) {
	font-size: 14px;
	font-weight: 600;
	color: var(--ink-gray-9, #171717);
	margin: 14px 0 6px;
}
.wn-body :deep(.jv-md-hr) {
	border: none;
	border-top: 1px solid var(--ink-gray-3, #e5e7eb);
	margin: 12px 0;
}
.wn-body :deep(.jv-md-code) {
	font-family: var(--font-mono, ui-monospace, monospace);
	font-size: 12.5px;
	padding: 1px 5px;
	border-radius: 5px;
	background: var(--surface-gray-2, #f3f4f6);
}
.wn-body :deep(.jv-md-link) {
	color: #6e5cf6;
	text-decoration: underline;
}
.wn-body :deep(.jv-md-quote) {
	margin: 8px 0;
	padding-left: 10px;
	border-left: 3px solid var(--ink-gray-3, #e5e7eb);
	color: var(--ink-gray-6, #6b7280);
}
</style>
