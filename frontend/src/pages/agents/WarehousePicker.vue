<template>
	<fieldset :disabled="disabled" class="min-w-0">
		<legend class="mb-1.5 text-xs text-ink-gray-5">{{ label }}</legend>
		<p class="mb-2 text-sm text-ink-gray-6">Select up to 10 warehouses in this company.</p>
		<ul
			v-if="selected.length"
			class="mb-3 flex flex-wrap gap-2"
			aria-label="Selected warehouses"
		>
			<li
				v-for="name in selected"
				:key="name"
				class="flex max-w-full items-center gap-2 rounded border border-outline-gray-2 px-2 py-1 text-sm"
			>
				<span class="min-w-0 break-words">{{ name }}</span>
				<button
					type="button"
					class="shrink-0 rounded px-1 focus-visible:outline focus-visible:outline-2"
					:aria-label="`Remove ${name}`"
					@click="remove(name)"
				>
					×
				</button>
			</li>
		</ul>
		<input
			ref="searchInput"
			v-model="query"
			type="search"
			aria-label="Search warehouses"
			placeholder="Search warehouses"
			class="mb-2 w-full rounded border border-outline-gray-2 px-3 py-2 text-sm"
		/>
		<p v-if="loading" role="status" class="py-2 text-sm text-ink-gray-5">
			Loading warehouses…
		</p>
		<div
			v-else-if="filtered.length"
			class="max-h-56 overflow-y-auto rounded border border-outline-gray-2"
		>
			<label
				v-for="name in filtered"
				:key="name"
				class="flex cursor-pointer items-start gap-2 px-3 py-2 text-sm"
			>
				<input
					type="checkbox"
					class="mt-1 shrink-0"
					:checked="selected.includes(name)"
					:disabled="selected.length >= 10 && !selected.includes(name)"
					@change="toggle(name, $event.target.checked)"
				/>
				<span class="min-w-0 break-words">{{ name }}</span>
			</label>
		</div>
		<p v-else class="py-2 text-sm text-ink-gray-5">
			{{
				query
					? "No warehouses match this search."
					: "Choose a company and load its warehouse list."
			}}
		</p>
		<p class="mt-2 text-xs text-ink-gray-5" aria-live="polite">
			{{ selected.length }} of 10 selected
		</p>
		<p v-if="selected.length > 10" class="mt-1 text-xs text-ink-red-4" role="alert">
			This saved setup has more than 10 warehouses. Remove warehouses before you save it.
		</p>
	</fieldset>
</template>

<script setup>
import { computed, nextTick, ref } from "vue";

const props = defineProps({
	modelValue: { type: Array, default: () => [] },
	options: { type: Array, default: () => [] },
	label: { type: String, default: "Warehouses to count" },
	loading: { type: Boolean, default: false },
	disabled: { type: Boolean, default: false },
});
const emit = defineEmits(["update:modelValue"]);
const query = ref("");
const searchInput = ref(null);
const selected = computed(() => [
	...new Set(props.modelValue.filter((name) => typeof name === "string" && name)),
]);
const filtered = computed(() => {
	const search = query.value.trim().toLocaleLowerCase();
	return [...new Set(props.options.filter((name) => typeof name === "string" && name))].filter(
		(name) => name.toLocaleLowerCase().includes(search)
	);
});
function toggle(name, checked) {
	if (props.disabled) return;
	if (checked) {
		if (!selected.value.includes(name) && selected.value.length < 10)
			emit("update:modelValue", [...selected.value, name]);
	} else
		emit(
			"update:modelValue",
			selected.value.filter((value) => value !== name)
		);
}
function remove(name) {
	if (props.disabled) return;
	emit(
		"update:modelValue",
		selected.value.filter((value) => value !== name)
	);
	nextTick(() => searchInput.value?.focus());
}
</script>
