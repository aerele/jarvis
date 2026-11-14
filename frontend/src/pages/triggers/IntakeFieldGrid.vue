<template>
	<!-- One aligned row per field: checkbox with its label, then a small muted type hint on its own line below, so a long label never overlaps it.
	     1 column on a phone, 2 from sm, 3 from xl. -->
	<ul
		class="grid max-h-64 grid-cols-1 gap-x-4 gap-y-2 overflow-auto rounded-md border p-3 sm:grid-cols-2 xl:grid-cols-3"
		data-testid="intake-check-grid"
	>
		<li
			v-for="f in fields"
			:key="f.fieldname"
			class="flex min-w-0 flex-col gap-0.5"
			data-testid="intake-check-row"
		>
			<FormControl
				type="checkbox"
				class="min-w-0 break-words"
				:label="label(f)"
				:modelValue="selected.includes(f.fieldname)"
				:disabled="disabled"
				@update:modelValue="(v) => $emit('toggle', f.fieldname, v)"
			/>
			<span class="break-words pl-6 text-p-xs text-ink-gray-7" data-testid="type-hint">
				{{ hint(f) }}
			</span>
		</li>
	</ul>
</template>

<script setup>
import { FormControl } from "frappe-ui";

defineProps({
	fields: { type: Array, default: () => [] },
	selected: { type: Array, default: () => [] },
	disabled: { type: Boolean, default: false },
});
defineEmits(["toggle"]);

const label = (f) => (f.mandatory ? `${f.label} (required)` : f.label);
// Same hint for header fields and table columns: a Link names what it matches.
const hint = (f) =>
	f.fieldtype === "Link" ? `Link, match by ${f.options || "name"}` : f.fieldtype || "";
</script>
