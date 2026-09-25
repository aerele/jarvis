<template>
	<!-- One wiki note a File Box run wants on the shared org wiki, held for a
	     Jarvis reviewer (the dropper too, with a reviewer role). The body is what
	     lands: shown in full, never truncated. -->
	<div :aria-busy="busy ? 'true' : 'false'">
		<div v-if="!note && !loaded" class="flex justify-start">
			<JvSpinner label="Loading the wiki note…" />
		</div>

		<div v-else-if="!note" role="status" class="text-sm text-ink-gray-6">
			This wiki note isn't waiting for your review.
		</div>

		<template v-else>
			<p class="truncate text-sm text-ink-gray-6">{{ meta }}</p>
			<p v-if="summary" class="mt-2 text-base text-ink-gray-8">{{ summary }}</p>

			<div
				v-if="changed"
				role="alert"
				class="mt-3 flex items-center gap-3 rounded bg-surface-amber-1 px-3 py-2 text-sm text-ink-amber-3"
			>
				<span class="flex-1">
					This note changed since you opened it. Reload it to review the new text.
				</span>
				<Button variant="subtle" size="sm" label="Reload" @click="reload" />
			</div>

			<template v-if="body">
				<div class="mt-3 flex justify-end">
					<Button
						variant="ghost"
						size="sm"
						:label="showSource ? 'View rendered' : 'View source'"
						@click="showSource = !showSource"
					/>
				</div>
				<pre
					v-if="showSource"
					class="mt-1 max-h-[60vh] overflow-auto whitespace-pre-wrap rounded border p-3 text-sm text-ink-gray-7"
					>{{ body }}</pre
				>
				<!-- renderMarkdown escapes HTML first and links only http(s) -->
				<div
					v-else
					class="prose prose-sm mt-1 max-h-[60vh] max-w-none overflow-auto rounded border p-3"
					v-html="bodyHtml"
				/>
				<div v-if="long" class="mt-1 text-2xs text-ink-amber-6">
					Long note — scroll the box above to review the full text before approving.
				</div>
			</template>
			<div v-else class="mt-3 text-xs italic text-ink-gray-5">
				(no body — a metadata-only page refresh)
			</div>

			<div v-if="gone" role="status" class="mt-3 text-sm text-ink-gray-6">
				This note is no longer waiting for review.
			</div>
			<template v-else>
				<div v-if="live.needs_retry" class="mt-3 text-sm text-ink-red-5">
					{{ landing }}
				</div>

				<div class="mt-4 flex flex-wrap items-center gap-2">
					<Button
						v-if="live.needs_retry"
						variant="solid"
						label="Retry"
						:loading="busy === 'retry'"
						:disabled="busy !== null"
						@click="retry"
					/>
					<Button
						v-else-if="live.can_approve"
						variant="solid"
						theme="green"
						label="Approve"
						:loading="busy === 'approve'"
						:disabled="busy !== null || changed"
						@click="approve"
					/>
					<Button
						variant="subtle"
						theme="red"
						label="Reject"
						:loading="busy === 'reject'"
						:disabled="busy !== null"
						@click="reject"
					/>
				</div>
			</template>
		</template>
	</div>
</template>

<script setup>
// Detail + decision for one wiki write proposal on the Approval Board. The note is
// snapshotted when opened: Approve sends that snapshot's digest, so the server
// refuses a proposal that changed since. `proposal` is the live list row (null
// once it leaves the list).
import { ref, computed, watch } from "vue";
import { Button, toast } from "frappe-ui";
import JvSpinner from "@/components/JvSpinner.vue";
import { approveWikiWrite, rejectWikiWrite, retryWikiWrite } from "@/api";
import { renderMarkdown } from "@/markdown";
import { errHtml } from "@/lib/errors";
import {
	proposalHeadline,
	proposalBody,
	isLongBody,
	dropperLabel,
	landingMessage,
} from "@/lib/wikiReview";

const props = defineProps({
	name: { type: String, required: true },
	proposal: { type: Object, default: null },
	loaded: { type: Boolean, default: false },
	// Callbacks, not emits: Vue drops an unmounted instance's emits, and switching
	// rows mid-decision unmounts this detail. `onChanged` asks for a fresh list.
	onDecided: { type: Function, default: null },
	onChanged: { type: Function, default: null },
});
const decided = (r) => props.onDecided && props.onDecided(r);
const listChanged = (r) => props.onChanged && props.onChanged(r);

// A rendered link hides its URL behind its text, so a note whose RENDER has one
// opens as source (judged on the renderer's output, not by re-parsing the body).
const RENDERED_LINK = /<a\s[^>]*href=/i;

const note = ref(null); // what the reviewer reads (and approves)
const showSource = ref(false);
const busy = ref(null); // "approve" | "reject" | "retry" | null

function open(p) {
	note.value = p;
	showSource.value = RENDERED_LINK.test(renderMarkdown(proposalBody(p)));
}
const mine = (p) => (p && p.name === props.name ? p : null);
open(mine(props.proposal));
watch(
	() => props.proposal,
	(p) => {
		if (!note.value && mine(p)) open(p);
	}
);

const live = computed(() => props.proposal || note.value);
const gone = computed(() => !!note.value && !props.proposal && props.loaded);
const changed = computed(
	() => !!note.value && !!props.proposal && props.proposal.wiki_digest !== note.value.wiki_digest
);
const meta = computed(
	() => proposalHeadline(note.value) + " · dropped by " + dropperLabel(note.value)
);
const summary = computed(() => (note.value.preview && note.value.preview.summary) || "");
const body = computed(() => proposalBody(note.value));
const bodyHtml = computed(() => renderMarkdown(body.value));
const long = computed(() => isLongBody(note.value));
const landing = computed(() => landingMessage(live.value));

function reload() {
	if (props.proposal) open(props.proposal);
}

async function approve() {
	if (busy.value || changed.value || !note.value) return;
	busy.value = "approve";
	try {
		const r = await approveWikiWrite(props.name, note.value.wiki_digest);
		if (r && r.applied) {
			toast.success("Wiki note approved and recorded");
			decided(r);
		} else {
			// approved, but the write did not land: the row stays, now actionable
			// (Retry, or Reject if it can't land at all). Reuse the same copy the
			// needs_retry banner shows, so a page_full reason never tells the
			// reviewer to "Use Retry" when Retry can only ever fail again.
			toast.warning(landingMessage({ apply_reason: r && r.reason }));
			listChanged(r);
		}
	} catch (e) {
		// refused (it changed, or was decided elsewhere): show what is there now
		toast.error(errHtml(e));
		listChanged();
	} finally {
		busy.value = null;
	}
}

async function reject() {
	if (busy.value) return;
	busy.value = "reject";
	try {
		await rejectWikiWrite(props.name);
		toast.success("Wiki note rejected — nothing was written");
		decided();
	} catch (e) {
		toast.error(errHtml(e));
		listChanged();
	} finally {
		busy.value = null;
	}
}

async function retry() {
	if (busy.value) return;
	busy.value = "retry";
	try {
		const r = await retryWikiWrite(props.name);
		if (r && r.applied) {
			toast.success("Wiki note recorded");
			decided(r);
		} else {
			toast.warning("Still did not land — check the failure reason");
			listChanged(r);
		}
	} catch (e) {
		toast.error(errHtml(e));
		listChanged();
	} finally {
		busy.value = null;
	}
}
</script>
