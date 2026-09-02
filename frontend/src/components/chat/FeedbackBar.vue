<!--
  Post-reply feedback line for the main chat. A quiet soft prompt bar under a
  long reply: "Was this reply helpful?" + Yes/No. Yes is one tap; No opens an
  optional "what went wrong" box. The rating commits the moment it is tapped
  (the parent submits immediately), so an abandoned note box still records the
  down. When/whether it appears is decided by the parent via lib/feedbackGate.
-->
<template>
	<div class="jvfb-slot" :class="{ gone: state === 'gone' }">
		<div v-if="state === 'idle'" class="jvfb-bar">
			<span class="jvfb-q">Was this reply helpful?</span>
			<span class="jvfb-acts">
				<button class="jvfb-btn up" type="button" @click="rateUp" aria-label="Helpful">
					<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
						<path
							d="M9 21h8.2a2 2 0 0 0 1.96-1.6l1.4-7A2 2 0 0 0 18.6 10H14V5a2.5 2.5 0 0 0-2.5-2.5L8 11v10zM2 11h4v10H2z"
						/>
					</svg>
					Yes
				</button>
				<button
					class="jvfb-btn down"
					type="button"
					@click="openComment"
					aria-label="Not helpful"
				>
					<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
						<path
							d="M15 3H6.8a2 2 0 0 0-1.96 1.6l-1.4 7A2 2 0 0 0 5.4 14H10v5a2.5 2.5 0 0 0 2.5 2.5L16 13V3zM22 3h-4v10h4z"
						/>
					</svg>
					No
				</button>
			</span>
		</div>

		<div v-else-if="state === 'comment'" class="jvfb-cmt">
			<span class="jvfb-lead">What went wrong? <span class="opt">(optional)</span></span>
			<textarea
				ref="ta"
				v-model="note"
				class="jvfb-ta"
				maxlength="1000"
				rows="2"
				placeholder="Tell Jarvis what to fix…"
				@keydown.enter.exact.prevent="send"
			></textarea>
			<div class="jvfb-row">
				<button class="jvfb-send" type="button" @click="send">Send</button>
				<button class="jvfb-skip" type="button" @click="skip">Skip</button>
			</div>
		</div>

		<div v-else-if="state === 'done'" class="jvfb-done">
			<svg
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2.4"
				aria-hidden="true"
			>
				<path d="M20 6 9 17l-5-5" />
			</svg>
			Thanks for the feedback
		</div>
	</div>
</template>

<script setup>
import { nextTick, ref } from "vue";

const emit = defineEmits(["rate", "close"]);

const state = ref("idle");
const note = ref("");

function toDone() {
	state.value = "done";
	setTimeout(() => {
		state.value = "gone";
		emit("close");
	}, 1600);
}

function rateUp() {
	emit("rate", { rating: "up", note: "" });
	toDone();
}

function openComment() {
	// Commit the down immediately; the note (if any) folds in on Send.
	emit("rate", { rating: "down", note: "" });
	state.value = "comment";
	nextTick(() => {
		const el = document.activeElement;
		// focus the textarea without stealing it back if the user already moved on
		if (el && el.tagName !== "TEXTAREA") {
			const ta = document.querySelector(".jvfb-ta");
			if (ta) ta.focus();
		}
	});
}

function send() {
	const text = (note.value || "").trim();
	if (text) emit("rate", { rating: "down", note: text });
	toDone();
}

function skip() {
	toDone();
}
</script>

<style scoped>
.jvfb-slot {
	margin-top: 12px;
	transition: opacity 0.35s ease;
}
.jvfb-slot.gone {
	opacity: 0;
}

/* soft prompt bar */
.jvfb-bar {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 14px;
	max-width: 440px;
	padding: 9px 11px 9px 14px;
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 11px;
}
.jvfb-q {
	font-size: 12.5px;
	font-weight: 500;
	color: var(--text-2);
}
.jvfb-acts {
	display: inline-flex;
	gap: 8px;
	flex: 0 0 auto;
}
.jvfb-btn {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	font-size: 12.5px;
	font-weight: 600;
	cursor: pointer;
	color: var(--text-2);
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 8px;
	padding: 5px 11px;
	transition: color 0.15s, border-color 0.15s;
}
.jvfb-btn svg {
	width: 14px;
	height: 14px;
}
.jvfb-btn:hover {
	color: var(--text);
	border-color: var(--text-3);
}
.jvfb-btn.up:hover {
	color: var(--green);
	border-color: var(--green);
}
.jvfb-btn.down:hover {
	color: var(--red);
	border-color: var(--red);
}
.jvfb-btn:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}

/* comment box */
.jvfb-cmt {
	display: flex;
	flex-direction: column;
	gap: 8px;
	max-width: 440px;
}
.jvfb-lead {
	font-size: 11.5px;
	font-weight: 500;
	color: var(--text-3);
}
.jvfb-lead .opt {
	opacity: 0.7;
}
.jvfb-ta {
	width: 100%;
	resize: vertical;
	min-height: 54px;
	font-family: inherit;
	font-size: 13px;
	line-height: 1.45;
	color: var(--text);
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 9px;
	padding: 8px 10px;
	outline: none;
}
.jvfb-ta:focus {
	border-color: var(--cta);
}
.jvfb-ta::placeholder {
	color: var(--text-3);
}
.jvfb-row {
	display: flex;
	align-items: center;
	gap: 10px;
}
.jvfb-send {
	font-size: 12.5px;
	font-weight: 600;
	color: var(--cta-fg);
	background: var(--cta);
	border: 0;
	border-radius: 8px;
	padding: 6px 14px;
	cursor: pointer;
}
.jvfb-skip {
	font-size: 12px;
	color: var(--text-3);
	background: transparent;
	border: 0;
	cursor: pointer;
}
.jvfb-skip:hover {
	color: var(--text-2);
}

/* confirmation */
.jvfb-done {
	display: inline-flex;
	align-items: center;
	gap: 7px;
	font-size: 12px;
	font-weight: 500;
	color: var(--green);
}
.jvfb-done svg {
	width: 14px;
	height: 14px;
}

@media (prefers-reduced-motion: reduce) {
	.jvfb-slot {
		transition: none;
	}
}
</style>
