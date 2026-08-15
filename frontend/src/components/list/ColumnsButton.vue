<template>
	<Popover placement="bottom-end">
		<template #target="{ togglePopover }">
			<Button label="Columns" iconLeft="columns" @click="togglePopover()" />
		</template>
		<template #body>
			<div
				class="my-2 min-w-40 rounded-lg bg-surface-modal p-1.5 shadow-2xl ring-1 ring-black ring-opacity-5"
			>
				<div
					v-for="column in columns"
					:key="column.key"
					class="flex items-center justify-between gap-6 rounded px-2 py-1.5 text-base text-ink-gray-8 hover:bg-surface-gray-2"
				>
					<Checkbox
						:label="column.label || column.key"
						:modelValue="!hidden.includes(column.key)"
						:disabled="isLastVisible(column)"
						@update:modelValue="(v) => setVisible(column, v)"
					/>
				</div>
				<div class="mt-1.5 border-t border-outline-gray-2 pt-1.5">
					<Button
						variant="ghost"
						label="Reset to default"
						class="w-full !justify-start !text-ink-gray-5"
						@click="reset"
					/>
				</div>
			</div>
		</template>
	</Popover>
</template>

<script setup>
// ColumnsButton - column show/hide with per-page persistence (DESIGN-V3 §14 F2):
// checkbox list over all columns + "Reset to default"; hidden keys persist in
// useStorage('jarvis-cols-'+storageKey). No width editing / no reorder this wave.
// Emits update:hidden (immediately on mount and on every change) so ListPage
// can filter the visible columns.
import { watch } from "vue";
import { useStorage } from "@vueuse/core";
import { Popover, Button, Checkbox } from "frappe-ui";

const props = defineProps({
	columns: { type: Array, default: () => [] },
	storageKey: { type: String, required: true },
});

const emit = defineEmits(["update:hidden"]);

const hidden = useStorage(`jarvis-cols-${props.storageKey}`, []);

watch(hidden, (v) => emit("update:hidden", [...(v || [])]), { immediate: true, deep: true });

function isLastVisible(column) {
	if (hidden.value.includes(column.key)) return false;
	const visible = props.columns.filter((c) => !hidden.value.includes(c.key));
	return visible.length <= 1;
}

function setVisible(column, visible) {
	if (visible) {
		hidden.value = hidden.value.filter((k) => k !== column.key);
	} else {
		if (isLastVisible(column)) return;
		if (!hidden.value.includes(column.key)) hidden.value = [...hidden.value, column.key];
	}
}

function reset() {
	hidden.value = [];
}
</script>
