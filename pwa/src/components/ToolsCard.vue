<script setup>
import { ref } from "vue";

// What the agent DID, as one collapsible card rather than a bubble per call.
// Used twice: live (steps arriving on tool:start/tool:end) and settled (the
// persisted `tool`-role rows, grouped). Collapsed by default once settled —
// the answer is the point; the steps are the receipt.
const props = defineProps({
	title: { type: String, required: true },
	steps: { type: Array, default: () => [] },
	live: { type: Boolean, default: false },
	defaultOpen: { type: Boolean, default: false },
});

const open = ref(props.defaultOpen);

// Icons by intent, so a glance at the card reads as "it searched, then wrote".
const ICONS = {
	search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.35-4.35",
	plus: "M12 5v14M5 12h14",
	edit: "M17 3a2.83 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z",
	trash: "M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6",
	mail: "M4 4h16v16H4zM22 6l-10 7L2 6",
	chart: "M18 20V10M12 20V4M6 20v-6",
	file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
	tool: "M14.7 6.3a4 4 0 0 1-5 5L4 17v3h3l5.7-5.7a4 4 0 0 1 5-5l2-2-3-3z",
};

function icon(name) {
	const n = String(name || "").toLowerCase();
	if (/search|list|find|query/.test(n)) return ICONS.search;
	if (/create|new/.test(n)) return ICONS.plus;
	if (/update|edit|set|amend/.test(n)) return ICONS.edit;
	if (/delete|cancel/.test(n)) return ICONS.trash;
	if (/mail|email|send/.test(n)) return ICONS.mail;
	if (/report|chart|aggregate|export/.test(n)) return ICONS.chart;
	if (/doc|get|read|schema|pdf/.test(n)) return ICONS.file;
	return ICONS.tool;
}
</script>

<template>
	<div class="jv-tools">
		<button class="jv-tools-head" @click="open = !open">
			<span v-if="props.live" class="jv-spinner" />
			<span class="jv-tools-title">{{ props.title }}</span>
			<svg
				class="jv-chev"
				:class="{ 'is-open': open }"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="m6 9 6 6 6-6" />
			</svg>
		</button>

		<div v-if="open" class="jv-tools-body">
			<div v-for="s in props.steps" :key="s.id" class="jv-step">
				<span class="jv-step-icon" :class="{ 'is-error': s.status === 'error' }">
					<svg
						viewBox="0 0 24 24"
						width="12"
						height="12"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path :d="icon(s.toolName || s.title)" />
					</svg>
				</span>
				<span class="jv-step-title">{{ s.title }}</span>
				<span v-if="s.status === 'running'" class="jv-spinner" />
				<svg
					v-else-if="s.status === 'error'"
					class="jv-step-mark is-error"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2.4"
					stroke-linecap="round"
				>
					<path d="M18 6 6 18M6 6l12 12" />
				</svg>
				<svg
					v-else
					class="jv-step-mark is-done"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2.4"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path d="M20 6 9 17l-5-5" />
				</svg>
			</div>
		</div>
	</div>
</template>

<style scoped>
.jv-tools {
	border: 1px solid var(--border);
	border-radius: 12px;
	background: var(--card);
	overflow: hidden;
}
.jv-tools-head {
	display: flex;
	align-items: center;
	gap: 9px;
	width: 100%;
	padding: 10px 12px;
	border: 0;
	background: transparent;
	font: inherit;
	text-align: left;
	cursor: pointer;
}
.jv-tools-title {
	flex: 1;
	min-width: 0;
	font-size: 12.5px;
	font-weight: 500;
	color: var(--ink6);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-chev {
	width: 15px;
	height: 15px;
	flex: none;
	color: var(--ink4);
	transition: transform 0.15s ease;
}
.jv-chev.is-open {
	transform: rotate(180deg);
}
.jv-tools-body {
	padding: 2px 12px 10px;
}
.jv-step {
	display: flex;
	align-items: center;
	gap: 9px;
	padding: 5px 0;
}
.jv-step-icon {
	display: grid;
	place-items: center;
	width: 22px;
	height: 22px;
	flex: none;
	border-radius: 6px;
	background: var(--card2);
	color: var(--ink6);
}
.jv-step-icon.is-error {
	color: var(--red);
}
.jv-step-title {
	flex: 1;
	min-width: 0;
	font-size: 12.5px;
	line-height: 1.35;
	color: var(--ink6);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-step-mark {
	width: 14px;
	height: 14px;
	flex: none;
}
.jv-step-mark.is-done {
	color: var(--green);
}
.jv-step-mark.is-error {
	color: var(--red);
}
.jv-spinner {
	width: 13px;
	height: 13px;
	flex: none;
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
