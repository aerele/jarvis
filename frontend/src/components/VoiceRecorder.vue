<template>
	<div class="flex flex-wrap items-center gap-2">
		<template v-if="!rec.supported">
			<Button
				v-if="compact"
				variant="ghost"
				icon="mic-off"
				:disabled="true"
				:tooltip="'Voice recording is not supported in this browser'"
			/>
			<span v-else class="text-sm text-ink-gray-5">
				Voice recording isn't supported in this browser - type your note instead.
			</span>
		</template>

		<!-- compact (embedded in the composer toolbar): icon-only, minimal chrome -->
		<template v-else-if="compact">
			<template v-if="phase === 'recording'">
				<Button
					variant="solid"
					theme="red"
					icon="square"
					:tooltip="'Stop'"
					@click="finish"
				/>
				<span
					class="inline-flex items-center gap-1.5 rounded-full bg-surface-gray-2 px-2 py-0.5 text-xs text-ink-gray-7"
				>
					<span class="size-1.5 animate-pulse rounded-full bg-surface-red-5" />
					{{ clock }}
				</span>
				<Button variant="ghost" icon="x" :tooltip="'Cancel'" @click="discard" />
			</template>
			<Button
				v-else-if="phase === 'transcribing'"
				variant="ghost"
				:loading="true"
				:disabled="true"
				:tooltip="'Transcribing…'"
			/>
			<Button v-else variant="ghost" icon="mic" :tooltip="'Record'" @click="begin" />
		</template>

		<template v-else-if="phase === 'recording'">
			<Button variant="solid" theme="red" label="Stop" iconLeft="square" @click="finish" />
			<span class="flex items-center gap-1.5 text-sm text-ink-gray-7">
				<span class="size-2 animate-pulse rounded-full bg-surface-red-5" />
				{{ clock }} / 5:00
			</span>
			<Button variant="ghost" label="Cancel" @click="discard" />
		</template>

		<template v-else-if="phase === 'transcribing'">
			<Button variant="subtle" label="Transcribing" :loading="true" :disabled="true" />
			<span class="text-sm text-ink-gray-5">Turning your recording into text…</span>
		</template>

		<template v-else>
			<Button variant="subtle" label="Record a note" iconLeft="mic" @click="begin" />
			<span class="text-sm text-ink-gray-5">
				Up to 5 minutes - you can edit the text before saving.
			</span>
		</template>
	</div>
</template>

<script setup>
// VoiceRecorder - the Business-tab record→transcribe control. Wraps the shared
// useAudioRecorder composable (300 s hard cap lives there; onAutoStop still
// transcribes) and voice.transcribeAudio, then hands the verbatim text to the
// parent via @transcript - the parent owns the editable textarea + Save.
import { ref, computed, onBeforeUnmount } from "vue";
import { Button, toast } from "frappe-ui";
import { useAudioRecorder } from "@/composables/useAudioRecorder";
import { transcribeAudio } from "@/api/voice";
import { errMessage as errMsg } from "@/lib/errors";

defineProps({
	// Compact/embedded mode for the Personalise ChatComposer toolbar: icon-only
	// mic (idle), a minimal Stop + m:ss pill + Cancel cluster (recording), and a
	// loading icon-button (transcribing) - no helper sentences. Default false
	// keeps the standalone Business-tab rendering byte-for-byte. All recording/
	// transcribe logic and the @transcript(text, durationS) emit are identical.
	compact: { type: Boolean, default: false },
});

const emit = defineEmits(["transcript"]);

const rec = useAudioRecorder({
	onAutoStop: (r) => {
		toast.info("Recording stopped at the 5-minute limit - transcribing.");
		transcribe(r);
	},
});

// UI phase on top of the recorder's own state: 'transcribing' has no recorder
// equivalent (the mic is already released while the upload runs).
const phase = ref("idle"); // 'idle' | 'recording' | 'transcribing'

const clock = computed(() => {
	const s = rec.durationS || 0;
	return Math.floor(s / 60) + ":" + String(Math.max(0, s) % 60).padStart(2, "0");
});

async function begin() {
	await rec.start();
	if (rec.state === "error") {
		toast.error(rec.error || "Couldn't start the microphone.");
		return;
	}
	if (rec.state === "recording") phase.value = "recording";
}

async function finish() {
	if (phase.value !== "recording") return;
	phase.value = "transcribing";
	const r = await rec.stop();
	if (!r || !r.blob || !r.blob.size) {
		phase.value = "idle";
		if (rec.state === "error") toast.error(rec.error || "Recording failed. Try again.");
		return;
	}
	await transcribe(r);
}

async function transcribe(r) {
	phase.value = "transcribing";
	try {
		const res = await transcribeAudio(r.blob, { durationS: r.durationS });
		const text = ((res && res.text) || "").trim();
		// Second arg carries the recording length so consumers can tag the
		// note kind as Voice (Personalise composer); older handlers that take
		// only (text) simply ignore it.
		if (text) emit("transcript", text, r.durationS || 0);
		else toast.error("Nothing was transcribed - try again closer to the microphone.");
	} catch (e) {
		toast.error(errMsg(e));
	} finally {
		phase.value = "idle";
	}
}

function discard() {
	rec.cancel();
	phase.value = "idle";
}

onBeforeUnmount(() => rec.cancel());
</script>
