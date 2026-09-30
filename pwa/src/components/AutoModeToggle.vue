<script setup>
import { computed } from "vue";

// The composer's auto-mode control (#581). One icon button in the PWA's own
// .jv-icon-btn shape; on = the accent border and colour the model sheet uses for
// a selected control. Locked (the chat already runs in auto mode) keeps the
// colour and the tooltip, so it is aria-disabled, never the disabled attribute.
const props = defineProps({
	on: { type: Boolean, default: false },
	locked: { type: Boolean, default: false },
	disabled: { type: Boolean, default: false },
});
const emit = defineEmits(["toggle"]);

const tip = computed(() =>
	props.locked
		? "Auto mode is on for this chat. Deletes, cancels and amends still ask."
		: props.on
		? "Auto mode is on for this chat. Click to turn it off before you send."
		: "Auto mode: apply changes without asking in this chat"
);

function onClick() {
	if (props.locked) return;
	emit("toggle");
}
</script>

<template>
	<button
		type="button"
		class="jv-icon-btn jv-auto"
		:class="{ 'is-on': props.on, 'is-locked': props.locked }"
		:title="tip"
		:aria-label="tip"
		:aria-pressed="props.on ? 'true' : 'false'"
		:aria-disabled="props.locked ? 'true' : null"
		:disabled="props.disabled && !props.locked"
		@click="onClick"
	>
		<svg
			viewBox="0 0 24 24"
			width="19"
			height="19"
			fill="currentColor"
			stroke="currentColor"
			stroke-width="1.6"
			stroke-linejoin="round"
		>
			<path d="M5 7.5v9l6-4.5z" />
			<path d="M13.5 7.5v9l6-4.5z" />
		</svg>
		<span v-if="props.on" class="jv-auto-label">Auto</span>
	</button>
</template>

<style scoped>
.jv-auto {
	grid-auto-flow: column;
	gap: 5px;
	color: var(--ink5);
}
.jv-auto.is-on {
	width: auto;
	padding: 0 9px;
	border: 1px solid var(--accent);
	color: var(--accent);
}
.jv-auto.is-locked {
	cursor: default;
}
.jv-auto.is-locked:active {
	background: transparent;
}
.jv-auto:disabled {
	opacity: 0.4;
	cursor: default;
}
.jv-auto-label {
	font-size: 12px;
	font-weight: 500;
}
</style>
