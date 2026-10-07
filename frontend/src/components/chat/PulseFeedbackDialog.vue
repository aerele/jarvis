<!-- Periodic "business pulse" survey popup. Opened via the shared
     pulseFeedbackOpen ref (see @/lib/pulseFeedbackGate), same pattern as
     SessionFeedbackDialog + noticeGate's whatsNewOpen. The server is the real
     gate (pulse_last_period_key / pulse_offer_count on Jarvis User Settings,
     claimed by pulse_context()); this component only renders + submits. -->
<template>
	<Dialog
		:model-value="open"
		@update:model-value="onDialogModelUpdate"
		:options="{ title: `How is ${agentName} doing for your business?`, size: 'md' }"
	>
		<template #body-content>
			<span
				class="mb-3 inline-block rounded-full bg-surface-gray-2 px-2 py-0.5 text-xs font-semibold text-ink-gray-7"
			>
				{{ ctx?.period_label || "This month" }}
			</span>
			<div class="mb-4">
				<label class="mb-1.5 block text-sm font-medium"
					>Overall, how satisfied
					{{ isPrevious ? "were you last month" : "are you this month" }}?</label
				>
				<div class="flex gap-1 text-2xl">
					<button
						v-for="n in 5"
						:key="n"
						type="button"
						class="leading-none"
						:class="n <= stars ? 'text-ink-amber-3' : 'text-ink-gray-3'"
						@click="stars = n"
					>
						★
					</button>
				</div>
			</div>
			<div v-if="features.length" class="mb-4">
				<label class="mb-1.5 block text-sm font-medium">
					Which do you use most?
					<span class="font-normal text-ink-gray-5"
						>only what you{{
							isPrevious ? " used last month" : "'ve used this month"
						}}</span
					>
				</label>
				<div class="flex flex-wrap gap-1.5">
					<button
						v-for="f in features"
						:key="f"
						type="button"
						class="rounded-full border px-2.5 py-1 text-xs"
						:class="
							selectedFeatures.includes(f)
								? 'border-outline-gray-4 bg-surface-gray-2 font-medium'
								: 'border-outline-gray-2 text-ink-gray-6'
						"
						@click="toggleFeature(f)"
					>
						{{ FEATURE_LABELS[f] || f }}
					</button>
				</div>
			</div>
			<div class="mb-4">
				<label class="mb-1.5 block text-sm font-medium">
					Is {{ agentName }} solving your business use case? If so, what?
				</label>
				<FormControl v-model="useCaseText" type="textarea" :rows="3" />
			</div>
			<div>
				<label class="mb-1.5 block text-sm font-medium">
					Anything else? <span class="font-normal text-ink-gray-5">optional</span>
				</label>
				<FormControl v-model="note" type="textarea" :rows="2" />
			</div>
		</template>
		<template #actions>
			<div class="flex justify-between">
				<Button label="Maybe later" variant="subtle" @click="close" />
				<Button label="Send feedback" variant="solid" :disabled="!stars" @click="submit" />
			</div>
		</template>
	</Dialog>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { Dialog, Button, FormControl } from "frappe-ui";
import * as api from "@/api";
import { agentName } from "@/branding";
import {
	pulseFeedbackOpen,
	pulseFeedbackContext,
	closePulseFeedback,
} from "@/lib/pulseFeedbackGate";

// Mirrors jarvis.chat.feature_usage.FEATURES server-side (the allowlist
// pulse_context/_feature_keys filters through) - keep the two in step.
// voice_chat is deliberately NOT labeled "Voice chat": the capture path never
// sets a kind=Voice marker, so real recordings land as kind=Text same as typed
// notes, and this key actually covers notes and dictation broadly.
const FEATURE_LABELS = {
	file_box: "File box",
	dashboard_builder: "Dashboard builder",
	skills: "Skills",
	macros: "Macros",
	triggers_connectors: "Triggers & Connectors",
	voice_chat: "Notes and dictation",
	wiki: "Wiki",
};

const open = pulseFeedbackOpen;
const ctx = pulseFeedbackContext;
const features = computed(() => ctx.value?.features_offered || []);
// Server flag: the first days of a month ask about the month just ended.
const isPrevious = computed(() => !!ctx.value?.period_is_previous);

const stars = ref(0);
const selectedFeatures = ref([]);
const useCaseText = ref("");
const note = ref("");

watch(open, (isOpen) => {
	if (isOpen) {
		stars.value = 0;
		selectedFeatures.value = [];
		useCaseText.value = "";
		note.value = "";
	}
});

function toggleFeature(f) {
	const i = selectedFeatures.value.indexOf(f);
	if (i === -1) selectedFeatures.value.push(f);
	else selectedFeatures.value.splice(i, 1);
}

function close() {
	closePulseFeedback();
}

// A plain `v-model="open"` would let Dialog write `pulseFeedbackOpen.value =
// false` directly on Escape, the X button, or an outside click -- all close
// paths Dialog drives itself, bypassing closePulseFeedback() and its
// "dismissed this page load" bookkeeping (see pulseFeedbackGate.js). Routing
// every close through here, same as the "Maybe later" button already does,
// makes ALL dismissals count, not just the explicit one.
function onDialogModelUpdate(value) {
	if (!value) closePulseFeedback();
}

async function submit() {
	if (!stars.value) return;
	const offered = features.value;
	const selected = selectedFeatures.value;
	const text = useCaseText.value;
	const n = note.value;
	const periodKey = ctx.value?.period_key;
	closePulseFeedback();
	try {
		await api.submitPulseFeedback(stars.value, offered, selected, text, n, periodKey);
	} catch (e) {
		/* best-effort */
	}
}
</script>
