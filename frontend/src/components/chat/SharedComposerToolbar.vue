<template>
	<div class="shared-toolbar">
		<button
			v-for="control in controls"
			:key="control.label"
			type="button"
			disabled
			:class="{ 'shared-model': control.model }"
			:aria-label="__(control.label)"
			:title="__(control.reason)"
		>
			<svg
				width="17"
				height="17"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="1.7"
				stroke-linecap="round"
				stroke-linejoin="round"
				aria-hidden="true"
			>
				<path :d="control.path" />
			</svg>
			<span v-if="control.model">{{ __("Your default model") }}</span>
		</button>
	</div>
</template>
<script setup>
import { computed } from "vue";
import { __ } from "@/lib/i18n";
const props = defineProps({ side: { type: String, required: true } });
// Same toolbar positions as private chat. No private-chat handlers are imported.
// Working per-turn controls require the shared API capability, not an owner API.
const controls = computed(() =>
	props.side === "left"
		? [
				{
					label: "Ground on wiki",
					reason: "Wiki selection is not available in shared chats yet.",
					path: "M4 3h14a2 2 0 0 1 2 2v16H6a2 2 0 0 1-2-2V3Zm0 14h16M8 7h8",
				},
				{
					label: "Choose connector",
					reason: "Connector selection is not available in shared chats yet.",
					path: "M9 3v4m6-4v4M7 7h10v4a5 5 0 0 1-10 0V7Zm5 9v5",
				},
				{
					label: "Automatic actions",
					reason: "Shared chats always ask for confirmation.",
					path: "m13 2-3 8H4l7 12 3-8h6L13 2Z",
				},
		  ]
		: [
				{
					label: "Voice dictation",
					reason: "Voice dictation is not available in shared chats yet.",
					path: "M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0V5Zm-4 5v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8",
				},
				{
					label: "Choose model",
					reason: "Shared chats use your own default model and thinking settings.",
					model: true,
					path: "M4 5c0-4 16-4 16 0s-16 4-16 0Zm0 0v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0",
				},
		  ]
);
</script>
<style scoped>
.shared-toolbar {
	display: flex;
	align-items: center;
	gap: 4px;
	min-width: 0;
}
button {
	display: flex;
	align-items: center;
	justify-content: center;
	flex: none;
	width: 30px;
	height: 30px;
	border: none;
	border-radius: 7px;
	background: transparent;
	color: var(--text-3);
	opacity: 0.55;
	cursor: not-allowed;
}
.shared-model {
	width: auto;
	min-width: 0;
	gap: 6px;
	border: 1px solid var(--border);
	border-radius: 20px;
	padding: 0 10px;
	background: var(--surface-1);
	font-size: 12px;
}
.shared-model span {
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
@media (max-width: 380px) {
	.shared-model {
		width: 30px;
		padding: 0;
		border-radius: 7px;
	}
	.shared-model span {
		display: none;
	}
}
</style>
