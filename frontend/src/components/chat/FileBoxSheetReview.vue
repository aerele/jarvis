<template>
	<Dialog
		:modelValue="true"
		:disableOutsideClickToClose="true"
		:options="{ title: 'Review this file', size: '4xl' }"
		@update:modelValue="(open) => !open && onClosed(review)"
	>
		<template #body-header>
			<div class="mb-6 flex items-center justify-between gap-3">
				<DialogTitle class="text-xl font-semibold text-ink-gray-9"
					>Review this file</DialogTitle
				>
				<button
					type="button"
					class="rounded p-2 text-ink-gray-7 focus-visible:outline focus-visible:outline-2"
					aria-label="Close review"
					@click="onClosed(review)"
				>
					<FeatherIcon name="x" class="size-4" aria-hidden="true" />
				</button>
			</div>
		</template>
		<template #body-content>
			<div class="filebox-sheet-review max-h-[70dvh] overflow-y-auto overscroll-contain">
				<SheetDetail
					:name="review.name"
					:showOpenChat="false"
					:onChanged="() => onChanged(review)"
					:onDecided="() => onClosed(review)"
				/>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
import { Dialog, FeatherIcon } from "frappe-ui";
import { DialogTitle } from "reka-ui";
import SheetDetail from "@/pages/approvals/SheetDetail.vue";

const props = defineProps({
	session: { type: Object, required: true },
	onChanged: { type: Function, required: true },
	onClosed: { type: Function, required: true },
});
// Capture this keyed instance's session. A late child callback must never use a
// newer dialog's name or conversation, even after this instance is unmounted.
const review = props.session;
</script>

<style scoped>
@media (max-width: 640px) {
	.filebox-sheet-review :deep(.jv-sheet-record-heading) {
		flex-direction: column;
		align-items: stretch;
	}
}
</style>
