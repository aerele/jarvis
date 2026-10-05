<!--
  "Reference: <id>" on a failed confirmation (round 2): the confirmation's own id,
  which a person can quote to support (the bench's error.reference). Monospace, with
  a Copy button; when the clipboard is unavailable or refuses (an insecure page, a
  denied permission) the text is selected instead so the person can copy it by hand.
  Renders nothing without an id. Used on the failed card (ActionError), the receipt
  chip and the Approval Board; the phone has its own copy in pwa/src/components.
-->
<template>
	<div v-if="id" class="jv-ref">
		<span class="jv-ref-label">Reference:</span>
		<code ref="codeEl" class="jv-ref-id">{{ id }}</code>
		<button
			type="button"
			class="jv-ref-copy"
			:aria-label="`Copy reference ${id}`"
			@click="copy"
		>
			Copy
		</button>
		<span class="jv-ref-status" role="status">{{ status }}</span>
	</div>
</template>

<script setup>
import { ref, watch } from "vue";
import { copyText } from "../lib/actionSummary.js";

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

<style scoped>
/* Its own palette with the chat's variables when present, so it is styled on the
   Approval Board too (as PendingCard does). */
.jv-ref {
	--ref-text-2: var(--text-2, #4a4a4f);
	--ref-text-3: var(--text-3, #6d6d76);
	--ref-border: var(--border, #e8e8ec);
	--ref-surface: var(--surface-1, #f7f7f8);
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 6px;
	margin-top: 6px;
	font-size: 12px;
	color: var(--ref-text-2);
}
html[data-theme="dark"] .jv-ref {
	--ref-text-2: var(--text-2, #b6b6c0);
	--ref-text-3: var(--text-3, #7e7e8a);
	--ref-border: var(--border, #2c2c34);
	--ref-surface: var(--surface-1, #1d1d22);
}
.jv-ref-id {
	font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
	font-size: 11.5px;
	padding: 1px 5px;
	border: 1px solid var(--ref-border);
	border-radius: 5px;
	background: var(--ref-surface);
	overflow-wrap: anywhere;
	user-select: all;
}
.jv-ref-copy {
	font-size: 11.5px;
	padding: 1px 8px;
	border: 1px solid var(--ref-border);
	border-radius: 5px;
	background: transparent;
	color: inherit;
	cursor: pointer;
}
.jv-ref-copy:focus-visible {
	outline: 2px solid currentColor;
	outline-offset: 1px;
}
.jv-ref-status {
	color: var(--ref-text-3);
}
</style>
