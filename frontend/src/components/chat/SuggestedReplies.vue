<!--
  The user's likely next messages under the latest reply (jarvis.chat.suggest_replies),
  after Frappe Flow's suggested replies (flow/harness/ui/core/src/components/SuggestedReplies.vue).
  A tap never sends: it ticks that one suggestion (one at a time; tapping it again unticks
  it) and lights the Send button beside them, so a stray tap costs nothing. The chat
  locks its composer while one is chosen ("pick" tells it), so what goes out is exactly
  the chosen text. Shared by the desktop chat and the PWA (through @shared): the colours
  fall back from the desktop palette (theme.js) to the PWA's (pwa/src/index.css).
-->
<template>
	<div class="jv-followup-wrap">
		<div class="jv-followups" role="radiogroup" aria-label="Suggested replies">
			<button
				v-for="reply in replies"
				:key="reply"
				type="button"
				role="radio"
				class="jv-followup"
				:class="{ on: picked === reply }"
				:title="reply"
				:aria-checked="picked === reply"
				:disabled="disabled"
				@click="tap(reply)"
			>
				<svg
					class="jv-followup-tick"
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
					<circle cx="12" cy="12" r="9" />
					<path v-if="picked === reply" d="m8 12.5 3 3 5-6" />
				</svg>
				<span class="jv-followup-text">{{ reply }}</span>
			</button>
		</div>
		<button
			type="button"
			class="jv-followup-send"
			:class="{ hidden: !picked }"
			:disabled="disabled || !picked"
			@click="send"
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
import { onBeforeUnmount, ref } from "vue";

const props = defineProps({
	replies: { type: Array, required: true },
	disabled: { type: Boolean, default: false },
});
// pick: the chosen text, or "" when none is (the chat locks its composer meanwhile).
// send: the chosen text, on Send.
const emit = defineEmits(["pick", "send"]);

const picked = ref("");
function choose(reply) {
	picked.value = reply;
	emit("pick", reply);
}

// On a touch screen they come up as the run ends, under a finger that may be going
// elsewhere: a tap in their first moment does nothing (as Flow does).
const shownAt = Date.now();
const coarse =
	typeof window !== "undefined" && !!window.matchMedia?.("(pointer: coarse)")?.matches;
function tap(reply) {
	if (props.disabled || (coarse && Date.now() - shownAt < 400)) return;
	choose(picked.value === reply ? "" : reply);
}
function send() {
	if (props.disabled || !picked.value) return;
	const text = picked.value;
	choose("");
	emit("send", text);
}
// Gone (a new reply, a sent message, the switch turned off): never leave the composer locked.
onBeforeUnmount(() => {
	if (picked.value) emit("pick", "");
});
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
	background: var(--surface-2, var(--card2));
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
.jv-followup-tick {
	flex: none;
	color: var(--text-3, var(--ink5));
}
.jv-followup.on .jv-followup-tick {
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
	box-shadow: 0 0 0 3px color-mix(in srgb, var(--cta, var(--accent)) 22%, transparent);
}
/* keeps its room while hidden, so choosing one does not shift the chat */
.jv-followup-send.hidden {
	visibility: hidden;
}
</style>
