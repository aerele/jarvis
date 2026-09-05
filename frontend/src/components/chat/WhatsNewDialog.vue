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

			<!-- notes, newest first: version heading via {{ }} (never v-html), body
			     via renderMarkdown (escape-first, XSS-safe) in a prose block -->
			<div v-else class="flex flex-col gap-6">
				<section v-for="note in notes" :key="note.version">
					<div class="flex flex-wrap items-baseline gap-2">
						<h3 class="text-base font-semibold text-ink-gray-9">
							{{ note.version }}
						</h3>
						<span v-if="note.title" class="text-sm text-ink-gray-6">
							{{ note.title }}
						</span>
					</div>
					<div
						class="prose prose-sm mt-1 max-w-none"
						v-html="renderMarkdown(note.body)"
					></div>
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
import { ref, watch } from "vue";
import { Dialog, Button, FeatherIcon, call } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import { renderMarkdown } from "@/markdown";
import { notice, whatsNewOpen } from "@/noticeGate";
import { agentName } from "@/branding";

const whatsNewTitle = `What's new in ${agentName}`;

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
