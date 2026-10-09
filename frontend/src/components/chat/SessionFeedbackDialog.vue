<!-- Once-per-session "How did this session go?" popup. Opened via the shared
     sessionFeedbackOpen ref (see @/lib/sessionFeedbackGate), same pattern as
     WhatsNewDialog + noticeGate's whatsNewOpen. The server is the real gate
     (session_feedback_asked_at); this component only renders + submits. -->
<template>
	<Dialog
		v-model="open"
		:options="{ title: 'How did this session go?', size: 'sm' }"
		@close="skip"
	>
		<template #body-content>
			<p class="mb-3 text-sm text-ink-gray-6">
				Takes 2 seconds. Helps us improve {{ brand.agentName }}.
			</p>
			<div class="flex flex-col gap-1.5">
				<button
					v-for="c in CHIPS"
					:key="c.value"
					type="button"
					class="flex items-center gap-2 rounded-md border px-2.5 py-2 text-left text-sm"
					:class="
						selected === c.value
							? 'border-outline-gray-4 bg-surface-gray-2 font-medium'
							: 'border-outline-gray-2'
					"
					@click="selected = c.value"
				>
					<span>{{ c.emoji }}</span>
					<span>{{ c.label }}</span>
				</button>
			</div>
			<div v-if="showNote" class="mt-3">
				<label class="mb-1 block text-xs font-medium text-ink-gray-6">
					What went wrong? (optional)
				</label>
				<FormControl v-model="note" type="text" placeholder="What happened" />
			</div>
		</template>
		<template #actions>
			<div class="flex justify-end gap-2">
				<Button label="Skip" variant="subtle" @click="skip" />
				<Button label="Send" variant="solid" :disabled="!selected" @click="submit" />
			</div>
		</template>
	</Dialog>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { Dialog, Button, FormControl } from "frappe-ui";
import * as api from "@/api";
import { brand } from "@/branding";
import {
	sessionFeedbackOpen,
	sessionFeedbackConversation,
	closeSessionFeedback,
} from "@/lib/sessionFeedbackGate";

const CHIPS = [
	{ value: "Great", emoji: "🎉", label: "Great, got what I needed" },
	{ value: "Good", emoji: "🙂", label: "Good, mostly helpful" },
	{ value: "Okay", emoji: "😐", label: "Okay, so-so" },
	{ value: "Not great", emoji: "😕", label: "Not great, missed the mark" },
	{ value: "Frustrating", emoji: "😞", label: "Frustrating, wasted my time" },
];
const LOW_CHIPS = new Set(["Okay", "Not great", "Frustrating"]);

const open = sessionFeedbackOpen;
const selected = ref(null);
const note = ref("");
const showNote = computed(() => LOW_CHIPS.has(selected.value));

watch(open, (isOpen) => {
	if (isOpen) {
		selected.value = null;
		note.value = "";
	}
});

async function submit() {
	if (!selected.value) return;
	const conversation = sessionFeedbackConversation.value;
	closeSessionFeedback();
	try {
		await api.submitSessionFeedback(conversation, selected.value, note.value);
	} catch (e) {
		/* best-effort, matches the existing thumbs feedback contract */
	}
}

// Also wired to the Dialog's `@close` (Escape / the ghost X button / an
// outside click) so ANY dismissal claims the offer, not just this explicit
// button — otherwise session_feedback_asked_at never gets stamped, the next
// reply re-polls as still due, and the popup reopens on top of it, silently
// burning through feedbackGate's IGNORE_CAP on repeat Escapes. Safe to call
// more than once: closeSessionFeedback()/submitSessionFeedback are idempotent
// no-ops on an already-closed dialog, and the explicit Skip click here never
// re-triggers Dialog's `close` emit (that only fires from Dialog's OWN
// internal state changes, not from this ref being set externally).
async function skip() {
	const conversation = sessionFeedbackConversation.value;
	closeSessionFeedback();
	try {
		await api.submitSessionFeedback(conversation, null, "");
	} catch (e) {
		/* best-effort */
	}
}
</script>
