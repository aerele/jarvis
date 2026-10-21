<template>
	<div class="my-3 min-w-0 rounded-lg border border-outline-gray-2 p-3">
		<div class="mb-2 break-words text-sm font-medium">
			{{ artifact.title || __("Attachment") }}
		</div>
		<template v-if="htmlArtifact">
			<p class="mb-2 text-xs text-ink-gray-5">
				{{ __("Static preview. Download the file for its full content.") }}
			</p>
			<p v-if="error" role="status" class="text-sm text-ink-gray-5">{{ error }}</p>
			<iframe
				v-else-if="html"
				:srcdoc="html"
				sandbox=""
				referrerpolicy="no-referrer"
				:title="artifact.title || __('Shared attachment')"
				class="w-full min-w-0 rounded border-0"
				style="height: 340px"
			/>
			<p v-else role="status" class="text-sm text-ink-gray-5">
				{{ __("Loading attachment…") }}
			</p>
		</template>
		<a
			v-if="url"
			:href="url"
			target="_blank"
			rel="noopener noreferrer"
			class="block min-w-0 break-words text-sm text-ink-blue-2 underline"
			:download="downloadOnly ? artifact.title || '' : undefined"
		>
			<img
				v-if="artifact.type === 'image'"
				:src="url"
				:alt="artifact.title || __('Shared image')"
				loading="lazy"
				class="mb-2 max-h-80 max-w-full rounded object-contain"
			/>
			{{ __("Download {0}", [artifact.title || __("attachment")]) }}
		</a>
	</div>
</template>
<script setup>
import { ref, computed, watch, onBeforeUnmount } from "vue";
import { getSharedCanvas } from "@/api/chatSharing";
import { sharedFileUrl } from "@/lib/sharedTranscript";
import { __ } from "@/lib/i18n";
const props = defineProps({
	artifact: { type: Object, required: true },
	message: String,
	conversation: String,
	dark: Boolean,
});
const html = ref(""),
	error = ref("");
let generation = 0;
const url = computed(() => sharedFileUrl(props.artifact.file_url, props.conversation));
const htmlArtifact = computed(() => ["html", "dashboard"].includes(props.artifact.type));
const downloadOnly = computed(() => ["xlsx", "csv", "spreadsheet"].includes(props.artifact.type));
watch(
	() => [
		props.conversation,
		props.message,
		props.artifact.name,
		props.artifact.file_url,
		props.dark,
	],
	async () => {
		const gen = ++generation;
		html.value = "";
		error.value = "";
		if (!htmlArtifact.value) return;
		try {
			const result = await getSharedCanvas(props.message, props.artifact.name, props.dark);
			if (gen === generation) html.value = result?.content || "";
		} catch {
			if (gen === generation)
				error.value = __("Couldn't load this attachment. Reopen the chat to try again.");
		}
	},
	{ immediate: true }
);
onBeforeUnmount(() => ++generation);
</script>
