<script setup>
import { ref, watch } from "vue";
import { copyText } from "@shared/lib/actionSummary.js";

// "Reference: <id>" on a failed confirmation (round 2): the confirmation's own id,
// for support. The phone's copy of the desktop's FailureReference.vue, in the PWA's
// palette. Copy, or select the text when the clipboard is unavailable or refuses.
// The phone shows no receipt chips (the thread renders no tool rows), so this is
// mounted only in DecisionSheet: a failed confirmation's reference shows there while
// the sheet is open, and nowhere on the phone after a reload.
const props = defineProps({
	id: { type: String, default: "" },
});

const codeEl = ref(null);
const status = ref("");
watch(
	() => props.id,
	() => (status.value = "")
);

async function copy() {
	if (await copyText(props.id)) {
		status.value = "Copied.";
		return;
	}
	selectId();
	status.value = "Selected. Copy it with your device's copy command.";
}

function selectId() {
	const el = codeEl.value;
	const sel =
		typeof window !== "undefined" && window.getSelection ? window.getSelection() : null;
	if (!el || !sel) return;
	const range = document.createRange();
	range.selectNodeContents(el);
	sel.removeAllRanges();
	sel.addRange(range);
}
</script>

<template>
	<div v-if="props.id" class="jv-ref">
		<span>Reference:</span>
		<code ref="codeEl" class="jv-ref-id">{{ props.id }}</code>
		<button
			type="button"
			class="jv-ref-copy"
			:aria-label="`Copy reference ${props.id}`"
			@click="copy"
		>
			Copy
		</button>
		<span class="jv-ref-status" role="status">{{ status }}</span>
	</div>
</template>

<style scoped>
.jv-ref {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 6px;
	margin-top: 8px;
	font-size: 12px;
	color: var(--ink7);
}
.jv-ref-id {
	font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
	font-size: 11.5px;
	padding: 2px 6px;
	border: 1px solid var(--border);
	border-radius: 6px;
	background: var(--card2);
	overflow-wrap: anywhere;
	user-select: all;
}
.jv-ref-copy {
	min-height: 32px;
	padding: 0 12px;
	border: 1px solid var(--border2);
	border-radius: 8px;
	background: var(--card);
	color: var(--ink8);
	font-size: 12px;
}
.jv-ref-status {
	color: var(--ink5);
}
</style>
