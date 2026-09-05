<template>
	<Dialog
		:modelValue="modelValue"
		:options="{ title: 'Compact this chat', size: 'sm' }"
		@update:modelValue="$emit('update:modelValue', $event)"
	>
		<template #body-content>
			<p class="text-sm text-ink-gray-6">Older turns are summarised, not deleted.</p>
			<FormControl
				v-model="hint"
				class="mt-3"
				type="textarea"
				label="Anything to keep? (optional)"
				placeholder="e.g. keep the invoice inputs"
				:maxlength="500"
			/>
			<p v-if="busyReason" class="mt-3 text-xs text-ink-gray-5">{{ busyReason }}</p>
		</template>
		<template #actions>
			<div class="flex justify-end gap-2">
				<Button label="Cancel" @click="$emit('update:modelValue', false)" />
				<Button
					label="Compact"
					variant="solid"
					:disabled="!!busyReason || submitting"
					:loading="submitting"
					@click="confirm"
				/>
			</div>
		</template>
	</Dialog>
</template>

<script setup>
import { ref, watch } from "vue";
import { Dialog, Button, FormControl } from "frappe-ui";

const props = defineProps({
	modelValue: { type: Boolean, default: false },
	busyReason: { type: String, default: "" },
	submitting: { type: Boolean, default: false },
	initialHint: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "confirm"]);
const hint = ref(props.initialHint);
watch(
	() => props.modelValue,
	(open) => {
		if (open) hint.value = props.initialHint;
	}
);
function confirm() {
	emit("confirm", hint.value.trim());
}
</script>
