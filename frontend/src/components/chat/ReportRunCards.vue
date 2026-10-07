<template>
	<section class="jv-rrc" aria-label="Background reports">
		<div v-for="it in items" :key="it.run" class="jv-rrc-item" :class="'is-' + it.status">
			<div class="jv-rrc-text">
				<div class="jv-rrc-name">
					{{ it.report_name
					}}<span v-if="it.filters" class="jv-rrc-filters"> · {{ it.filters }}</span>
				</div>
				<div class="jv-rrc-state" role="status">{{ stateText(it) }}</div>
			</div>
			<button
				v-if="it.status === 'ready'"
				type="button"
				class="jv-rrc-show"
				:disabled="busy"
				@click="emit('show', it)"
			>
				Show results
			</button>
		</div>
	</section>
</template>

<script setup>
import { timeAgo } from "@/utils/datetime";

// A background (prepared) report this chat started: preparing, then ready with a
// button that asks Jarvis for the results (the finished run is reused, not re-run).
defineProps({
	items: { type: Array, required: true },
	busy: { type: Boolean, default: false }, // a reply is running
});
const emit = defineEmits(["show"]);

function stateText(it) {
	if (it.status === "ready") return `Ready ${timeAgo(it.ready_at)}`;
	if (it.status === "failed")
		return "Couldn't prepare this report. Tell Jarvis which filter to change.";
	return "Preparing in the background…";
}
</script>

<style scoped>
.jv-rrc {
	display: flex;
	flex-direction: column;
	gap: 8px;
	margin: 12px 0;
}
.jv-rrc-item {
	display: flex;
	align-items: center;
	gap: 12px;
	padding: 12px 14px;
	border: 1px solid var(--border);
	background: var(--surface-1);
	border-radius: 12px;
}
.jv-rrc-text {
	min-width: 0;
	flex: 1;
}
.jv-rrc-name {
	font-size: 13.5px;
	font-weight: 600;
	color: var(--text);
	overflow-wrap: anywhere;
}
.jv-rrc-filters {
	font-weight: 400;
	color: var(--text-2);
}
.jv-rrc-state {
	margin-top: 3px;
	font-size: 12.5px;
	color: var(--text-3);
}
.is-ready .jv-rrc-state {
	color: var(--text-2);
}
.is-failed .jv-rrc-state {
	color: var(--danger, var(--text-2));
}
.jv-rrc-show {
	flex-shrink: 0;
	padding: 8px 16px;
	background: var(--cta);
	border: 1px solid var(--cta);
	border-radius: 8px;
	font-family: inherit;
	font-size: 13px;
	font-weight: 600;
	color: var(--cta-fg);
	cursor: pointer;
}
.jv-rrc-show:disabled {
	opacity: 0.5;
	cursor: default;
}
.jv-rrc-show:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
</style>
