<!--
  The delivery notice under a user's chat bubble: what happened to this send and
  what the user can safely do next. Presentational only, like Message.vue: the
  parent owns the state machine and decides what each action does.

  One notice, four tones, so the colour says how worried to be:
    - error: "Not sent" (also a rejected send). Nothing ran; Retry is safe.
    - warn:  "Delivery not confirmed". It may have run; check before resending.
    - busy:  "Checking delivery…".
    - ok:    "Delivery confirmed".
  Before this component every state was the same small red line, so a confirmed
  delivery looked like a failure.

  The parent's note often opens by repeating the title ("Delivery not confirmed.
  Check delivery before sending again."), because the same sentence is also shown
  alone in a toast. Under the title that reads as a stutter, so the lead is cut
  here and the shared copy in lib/sendDelivery.js stays whole. While a check
  runs the note is still the previous state's, so every state's title is cut.

  Two structural rules tests rely on: the head is the first role="status" and
  holds only the title, and the first button is the retry/check action.
-->
<template>
	<div class="jv-delivery" :class="'jv-delivery--' + view.tone">
		<div class="jv-delivery-head" role="status">
			<span class="jv-delivery-icon" aria-hidden="true">
				<JvSpinner v-if="state === 'checking'" :size="20" color="currentColor" />
				<svg
					v-else
					width="16"
					height="16"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path v-for="d in view.icon" :key="d" :d="d" />
				</svg>
			</span>
			<span class="jv-delivery-title">{{ view.title }}</span>
		</div>
		<p v-if="detail" class="jv-delivery-note" role="status">{{ detail }}</p>
		<div class="jv-delivery-actions">
			<button
				v-if="view.action"
				class="jv-delivery-btn jv-delivery-btn--primary"
				:disabled="busy"
				@click="emit('retry')"
			>
				{{ view.action }}
			</button>
			<button
				v-if="recoverable"
				class="jv-delivery-btn"
				:disabled="busy"
				@click="emit('retry-same')"
			>
				Retry same request
			</button>
			<a
				v-if="conversation"
				class="jv-delivery-btn"
				:href="'/jarvis/c/' + encodeURIComponent(conversation)"
				target="_blank"
				rel="noopener noreferrer"
				>View conversation</a
			>
			<button
				v-if="recoverable || state === 'delivered'"
				class="jv-delivery-btn jv-delivery-btn--quiet"
				:disabled="busy"
				@click="emit('dismiss')"
			>
				Dismiss
			</button>
		</div>
	</div>
</template>

<script setup>
import { computed } from "vue";
import JvSpinner from "@/components/JvSpinner.vue";

const props = defineProps({
	// "" and "rejected" both mean a send that did not go through.
	state: { type: String, default: "" },
	note: { type: String, default: "" },
	conversation: { type: String, default: "" },
});
const emit = defineEmits(["retry", "dismiss", "retry-same"]);

const view = computed(() => VIEWS[props.state] || NOT_SENT);
// A check or a same-request retry is in flight. Its actions stay where they
// were, locked, so the panel does not jump and nothing can be fired twice.
const busy = computed(() => props.state === "checking");
const recoverable = computed(() => ["uncertain", "checking"].includes(props.state));
// Any state's title, not only the current one: while a check runs the note is
// still the previous state's sentence.
const detail = computed(() => {
	const lead = LEADS.find((l) => props.note.startsWith(l));
	return lead ? props.note.slice(lead.length) : props.note;
});

const CIRCLE = "M12 3a9 9 0 1 0 0 18a9 9 0 0 0 0-18z";
const NOT_SENT = {
	tone: "error",
	title: "Not sent",
	action: "Retry",
	icon: [CIRCLE, "M12 8v4.5", "M12 16h.01"],
};
const VIEWS = {
	uncertain: {
		tone: "warn",
		title: "Delivery not confirmed",
		action: "Check delivery",
		icon: [
			"M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z",
			"M12 9v4",
			"M12 17h.01",
		],
	},
	checking: { tone: "busy", title: "Checking delivery…", action: "Check delivery" },
	delivered: {
		tone: "ok",
		title: "Delivery confirmed",
		icon: [CIRCLE, "M8.5 12.5l2.5 2.5 4.5-5"],
	},
};
const LEADS = [NOT_SENT, ...Object.values(VIEWS)].map((v) => v.title + ". ");
</script>

<style scoped>
.jv-delivery {
	--jv-delivery-tone: var(--red);
	--jv-delivery-fill: var(--red-bg);
	--jv-delivery-edge: var(--red-bd);
	display: flex;
	flex-direction: column;
	gap: 8px;
	box-sizing: border-box;
	width: fit-content;
	max-width: min(78%, 440px);
	margin-top: 6px;
	padding: 10px 12px;
	border: 1px solid var(--jv-delivery-edge);
	/* echoes the bubble above it, which is also squared at the bottom right */
	border-radius: 12px 12px 4px 12px;
	background: var(--jv-delivery-fill);
	text-align: left;
}
.jv-delivery--warn {
	--jv-delivery-tone: var(--amber);
	--jv-delivery-fill: var(--amber-bg);
	--jv-delivery-edge: var(--amber-bd);
}
.jv-delivery--ok {
	--jv-delivery-tone: var(--green);
	--jv-delivery-fill: var(--green-bg);
	--jv-delivery-edge: var(--green-bd);
}
.jv-delivery--busy {
	--jv-delivery-tone: var(--text-2);
	--jv-delivery-fill: var(--surface-2);
	--jv-delivery-edge: var(--border);
}
.jv-delivery-head {
	display: flex;
	align-items: center;
	gap: 8px;
	color: var(--jv-delivery-tone);
}
/* A fixed slot, so the title starts on the same line whether the slot holds
   the 16px glyph or the 20px spinner. */
.jv-delivery-icon {
	display: flex;
	align-items: center;
	justify-content: center;
	flex: none;
	width: 20px;
	height: 20px;
}
.jv-delivery-title {
	font-size: 13px;
	font-weight: 600;
	line-height: 20px;
}
/* note and actions share the title's left edge (icon slot + gap) */
.jv-delivery-note,
.jv-delivery-actions {
	padding-left: 28px;
}
.jv-delivery-note {
	margin: 0;
	font-size: 12.5px;
	line-height: 1.5;
	color: var(--text-2);
	overflow-wrap: anywhere;
}
.jv-delivery-actions {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 6px;
}
.jv-delivery-btn {
	display: inline-flex;
	align-items: center;
	height: 28px;
	padding: 0 10px;
	border: 1px solid var(--border-2);
	border-radius: 7px;
	background: var(--surface);
	color: var(--text);
	font: inherit;
	font-size: 12.5px;
	font-weight: 500;
	line-height: 1;
	text-decoration: none;
	white-space: nowrap;
	cursor: pointer;
}
.jv-delivery-btn:hover {
	background: var(--surface-1);
}
.jv-delivery-btn--primary,
.jv-delivery-btn--primary:hover {
	border-color: var(--cta);
	background: var(--cta);
	color: var(--cta-fg);
}
.jv-delivery-btn--primary:hover {
	opacity: 0.88;
}
.jv-delivery-btn--quiet,
.jv-delivery-btn--quiet:hover {
	border-color: transparent;
	background: transparent;
	color: var(--text-2);
}
.jv-delivery-btn--quiet:hover {
	color: var(--text);
	text-decoration: underline;
	text-underline-offset: 2px;
}
.jv-delivery-btn:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
/* no hover feedback either: a locked button must not look clickable */
.jv-delivery-btn:disabled {
	opacity: 0.5;
	cursor: default;
	pointer-events: none;
}
@media (max-width: 640px) {
	.jv-delivery {
		max-width: 92%;
	}
}
/* fingers need a taller target than a mouse pointer does */
@media (pointer: coarse) {
	.jv-delivery-btn {
		height: 36px;
		padding: 0 12px;
	}
}
</style>
