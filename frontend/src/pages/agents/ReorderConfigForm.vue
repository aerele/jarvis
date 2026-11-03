<template>
	<form class="space-y-4" @submit.prevent="save">
		<p class="text-sm text-ink-gray-6">
			Use a complete, clean Negative Stock run for today and the full company Warehouse root.
			This review uses saved ERPNext purchase reorder levels. It does not create a Material
			Request.
		</p>
		<FormControl label="Negative Stock run" v-model="negativeRun" :disabled="saving" />
		<p class="text-sm text-ink-gray-6">
			This release holds changing receipt costs, landed costs and stock adjustments for
			manual stock review.
		</p>
		<FormControl
			type="number"
			label="Maximum review rows (1–100)"
			v-model="limit"
			:disabled="saving"
		/>
		<p v-if="error || validationErrors.reorder" role="alert" class="text-sm text-ink-red-4">
			{{ error || validationErrors.reorder }}
		</p>
		<Button type="submit" label="Save settings" :loading="saving" />
	</form>
</template>
<script setup>
import { ref, watch } from "vue";
import { Button, FormControl } from "frappe-ui";
const props = defineProps({
	config: { type: Object, default: () => ({}) },
	validationErrors: { type: Object, default: () => ({}) },
	saving: Boolean,
});
const emit = defineEmits(["save", "dirty"]);
const negativeRun = ref(props.config.negative_stock_run || "");
const limit = ref(props.config.max_proposals || 50);
const error = ref("");
watch([negativeRun, limit], () => emit("dirty", true));
function save() {
	const count = Number(limit.value);
	if (!negativeRun.value.trim() || !Number.isInteger(count) || count < 1 || count > 100) {
		error.value = "Enter a Negative Stock run and a row limit from 1 to 100.";
		return;
	}
	error.value = "";
	emit("save", {
		policy_version: "erpnext-reorder-v1",
		negative_stock_run: negativeRun.value.trim(),
		max_proposals: count,
	});
}
</script>
