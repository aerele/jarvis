<template>
	<div class="filebox-source-control">
		<Button
			variant="subtle"
			:label="
				!state && error
					? 'Retry source check'
					: current
					? 'Current source'
					: 'Use as source'
			"
			:loading="loading || saving"
			:disabled="
				current || state?.supported === false || state?.candidate_available === false
			"
			@click="!state ? load() : select()"
		/>
		<span v-if="state?.supported === false" class="filebox-source-name"
			>Source selection is unavailable</span
		>
		<span v-else-if="state?.candidate_available === false" class="filebox-source-name">{{
			state.preview_available === false
				? "This attachment is unavailable"
				: "This attachment cannot be used as a source"
		}}</span>
		<span v-else-if="state?.status === 'lost'" class="filebox-source-name"
			>Source file unavailable</span
		>
		<span
			v-else-if="state?.source && !current"
			class="filebox-source-name"
			:title="state.source.file_name"
		>
			Current source: {{ state.source.file_name }}
		</span>
		<p v-if="error" role="alert" class="filebox-source-error">{{ error }}</p>
	</div>
</template>

<script setup>
import { watch } from "vue";
import { Button } from "frappe-ui";
import { useFileBoxSource } from "@/composables/useFileBoxSource";

const props = defineProps({
	conversation: { type: String, required: true },
	message: { type: String, required: true },
	attachment: { type: Object, required: true },
});
const emit = defineEmits(["availability"]);
const { state, loading, saving, error, current, load, select } = useFileBoxSource(props);
watch(
	() => state.value?.preview_available,
	(available) => {
		if (typeof available === "boolean")
			emit("availability", {
				conversation: props.conversation,
				message: props.message,
				item: props.attachment.name,
				file_url: props.attachment.file_url,
				available,
			});
	}
);
</script>

<style scoped>
.filebox-source-control {
	flex-shrink: 0;
	max-width: 100%;
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 8px;
	padding: 10px 14px;
	border-bottom: 1px solid var(--border);
}
.filebox-source-name {
	flex: 1;
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	font-size: 12px;
	color: var(--text-3);
}
.filebox-source-error {
	flex-basis: 100%;
	margin: 0;
	overflow-wrap: anywhere;
	color: var(--text-3);
	font-size: 12px;
}
</style>
