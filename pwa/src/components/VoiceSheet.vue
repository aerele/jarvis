<script setup>
import { ref, watch } from "vue";
// The SAME recorder the desktop SPA's composer mic uses — one MediaRecorder
// wrapper, one set of permission/format/duration-cap bugs, fixed once. The
// transcript endpoint is shared too (api.transcribeAudio re-exports the SPA's
// voice module), so the phone inherits the admin-configured STT credentials
// exactly like the web does.
import { useAudioRecorder } from "@shared/composables/useAudioRecorder.js";
import Sheet from "./Sheet.vue";
import { transcribeAudio } from "../api";
import { formatDuration } from "../lib/time";

const props = defineProps({ open: { type: Boolean, default: false } });
const emit = defineEmits(["close", "transcript"]);

const busy = ref(false);
const error = ref("");

const rec = useAudioRecorder({
	// The recorder hard-stops at 300s. If that fires while the user is still
	// holding the sheet open, transcribe what we got rather than dropping it.
	onAutoStop: (take) => finish(take, "Recording stopped at the 5-minute limit."),
});

watch(
	() => props.open,
	(open) => {
		error.value = "";
		busy.value = false;
		if (open) rec.start();
		else rec.cancel();
	}
);

async function finish(take, note = "") {
	if (!take?.blob) {
		emit("close");
		return;
	}
	busy.value = true;
	error.value = note;
	try {
		const r = await transcribeAudio(take.blob, { durationS: take.durationS });
		const text = (r?.text || "").trim();
		if (!text) {
			error.value = "Nothing was picked up. Try again, closer to the mic.";
			busy.value = false;
			return;
		}
		emit("transcript", text);
		emit("close");
	} catch (e) {
		error.value = e?.message || "Couldn't transcribe that.";
	} finally {
		busy.value = false;
	}
}

async function stop() {
	const take = await rec.stop();
	await finish(take);
}

function cancel() {
	rec.cancel();
	emit("close");
}
</script>

<template>
	<Sheet :open="props.open" @close="cancel">
		<div class="jv-voice">
			<div class="jv-voice-mic" :class="{ 'is-live': rec.state === 'recording' }">
				<svg
					viewBox="0 0 24 24"
					width="30"
					height="30"
					fill="none"
					stroke="currentColor"
					stroke-width="1.9"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
					<path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3" />
				</svg>
			</div>

			<div class="jv-voice-title">
				{{
					busy
						? "Transcribing…"
						: rec.state === "recording"
						? formatDuration(rec.durationS)
						: "Ready"
				}}
			</div>
			<div class="jv-voice-sub">
				{{
					busy
						? "Turning your words into text."
						: rec.state === "recording"
						? "Speak, then tap Done. The text lands in the composer."
						: "Tap Done when you've finished."
				}}
			</div>

			<div v-if="error || rec.error" class="jv-voice-error">{{ error || rec.error }}</div>

			<div class="jv-voice-actions">
				<button class="jv-btn is-ghost" @click="cancel">Cancel</button>
				<button
					class="jv-btn is-primary"
					:disabled="busy || rec.state !== 'recording'"
					@click="stop"
				>
					Done
				</button>
			</div>
		</div>
	</Sheet>
</template>

<style scoped>
.jv-voice {
	display: flex;
	flex-direction: column;
	align-items: center;
	gap: 8px;
	padding: 22px 24px 24px;
	text-align: center;
}
.jv-voice-mic {
	display: grid;
	place-items: center;
	width: 76px;
	height: 76px;
	margin-bottom: 6px;
	border-radius: 999px;
	background: var(--accent-bg);
	color: var(--accent);
}
.jv-voice-mic.is-live {
	animation: jv-mic 1.6s ease-in-out infinite;
}
@keyframes jv-mic {
	0%,
	100% {
		box-shadow: 0 0 0 0 rgba(130, 105, 248, 0.35);
	}
	50% {
		box-shadow: 0 0 0 12px rgba(130, 105, 248, 0);
	}
}
@media (prefers-reduced-motion: reduce) {
	.jv-voice-mic.is-live {
		animation: none;
	}
}
.jv-voice-title {
	font-size: 20px;
	font-weight: 600;
	font-variant-numeric: tabular-nums;
	color: var(--ink9);
}
.jv-voice-sub {
	font-size: 13px;
	line-height: 1.45;
	color: var(--ink5);
}
.jv-voice-error {
	margin-top: 4px;
	padding: 10px 12px;
	border-radius: 10px;
	background: var(--red-bg);
	color: var(--red);
	font-size: 12.5px;
	line-height: 1.4;
}
.jv-voice-actions {
	display: flex;
	gap: 10px;
	align-self: stretch;
	margin-top: 14px;
}
.jv-btn {
	flex: 1;
	height: 48px;
	border: 0;
	border-radius: 12px;
	font: inherit;
	font-size: 15px;
	font-weight: 600;
	cursor: pointer;
}
.jv-btn.is-primary {
	background: var(--accent-solid);
	color: #fff;
}
.jv-btn.is-ghost {
	border: 1px solid var(--border2);
	background: var(--card);
	color: var(--ink8);
}
.jv-btn:disabled {
	opacity: 0.55;
}
</style>
