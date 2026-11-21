<!--
  The user's likely next messages under the latest reply (jarvis.chat.suggest_replies),
  as Frappe Flow offers them (flow/harness/ui/core/src/components/SuggestedReplies.vue):
  a tap sends that message; when the reply asked something that takes several answers
  (multiSelect), the chips tick and one Send sends them together, joined by ", ".
  Shared by the desktop chat and the PWA (through @shared): the colours fall back from
  the desktop palette (theme.js) to the PWA's (pwa/src/index.css).
-->
<template>
	<div class="jv-followup-wrap">
		<div
			class="jv-followups"
			role="group"
			:aria-label="multiSelect ? 'Pick answers' : 'Suggested replies'"
		>
			<button
				v-for="reply in replies"
				:key="reply"
				type="button"
				class="jv-followup"
				:class="{ on: picked.includes(reply) }"
				:title="reply"
				:aria-pressed="multiSelect ? picked.includes(reply) : undefined"
				:disabled="disabled"
				@click="tap(reply)"
			>
				<svg
					v-if="multiSelect"
					class="jv-followup-box"
					width="14"
					height="14"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
					aria-hidden="true"
				>
					<rect x="3" y="3" width="18" height="18" rx="4" />
					<path v-if="picked.includes(reply)" d="m8 12 3 3 5-6" />
				</svg>
				<span class="jv-followup-text">{{ reply }}</span>
			</button>
		</div>
		<button
			v-if="multiSelect"
			type="button"
			class="jv-followup-send"
			:class="{ hidden: !picked.length }"
			:disabled="disabled || !picked.length"
			@click="emit('send', replies.filter((r) => picked.includes(r)).join(', '))"
		>
			Send
			<svg
				width="13"
				height="13"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2.2"
				stroke-linecap="round"
				stroke-linejoin="round"
				aria-hidden="true"
			>
				<path d="M12 19V5" />
				<path d="m5 12 7-7 7 7" />
			</svg>
		</button>
	</div>
</template>

<script setup>
import { ref } from "vue";

const props = defineProps({
	replies: { type: Array, required: true },
	multiSelect: { type: Boolean, default: false },
	disabled: { type: Boolean, default: false },
});
const emit = defineEmits(["send"]);

const picked = ref([]);

// On a touch screen they come up as the run ends, under a finger that may be going
// elsewhere: a tap in their first moment does nothing (as Flow does).
const shownAt = Date.now();
const coarse =
	typeof window !== "undefined" && !!window.matchMedia?.("(pointer: coarse)")?.matches;
function tap(reply) {
	if (props.disabled || (coarse && Date.now() - shownAt < 400)) return;
	if (!props.multiSelect) return emit("send", reply);
	picked.value = picked.value.includes(reply)
		? picked.value.filter((r) => r !== reply)
		: [...picked.value, reply];
}
</script>

<style scoped>
.jv-followup-wrap {
	display: flex;
	flex-direction: column;
	align-items: flex-start;
	gap: 8px;
	margin-top: 10px;
}
.jv-followups {
	display: flex;
	flex-wrap: wrap;
	gap: 6px;
	max-width: 100%;
}
.jv-followup {
	display: inline-flex;
	align-items: center;
	gap: 7px;
	max-width: 100%;
	padding: 7px 12px;
	background: var(--surface, var(--card));
	border: 1px solid var(--border);
	border-radius: 9px;
	font-family: inherit;
	font-size: 13px;
	line-height: 1.35;
	text-align: left;
	color: var(--text-2, var(--ink7));
	cursor: pointer;
	transition: border-color 0.12s, background 0.12s, color 0.12s;
}
.jv-followup:hover:not(:disabled) {
	border-color: var(--border-2, var(--border2));
	background: var(--surface-2, var(--card2));
	color: var(--text, var(--ink9));
}
.jv-followup.on {
	border-color: var(--cta, var(--accent));
	color: var(--text, var(--ink9));
}
.jv-followup:disabled {
	opacity: 0.55;
	cursor: default;
}
.jv-followup-text {
	min-width: 0;
	overflow-wrap: anywhere;
}
.jv-followup-box {
	flex: none;
	color: var(--text-3, var(--ink5));
}
.jv-followup.on .jv-followup-box {
	color: var(--cta, var(--accent));
}
.jv-followup-send {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	padding: 6px 12px;
	border: 0;
	border-radius: 8px;
	background: var(--cta, var(--inv-bg));
	color: var(--cta-fg, var(--inv-ink));
	font-family: inherit;
	font-size: 13px;
	font-weight: 600;
	cursor: pointer;
}
/* keeps its room while hidden, so ticking the first answer does not shift the chat */
.jv-followup-send.hidden {
	visibility: hidden;
}
</style>
