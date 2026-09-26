<template>
	<!-- One editable control for a panel field (lib/docFields.panelField). Emits the
	     raw control value: "Yes"/"No" for a check, a string otherwise. -->
	<select
		v-if="field.control === 'select'"
		:id="id"
		:class="inputClass"
		:value="field.value"
		:aria-invalid="invalid ? 'true' : 'false'"
		:aria-describedby="describedby"
		@change="$emit('update', $event.target.value)"
	>
		<option v-for="o in field.options" :key="o" :value="o">{{ o }}</option>
	</select>
	<input
		v-else-if="field.control === 'check'"
		:id="id"
		type="checkbox"
		class="size-4"
		:checked="field.value === 'Yes'"
		:aria-describedby="describedby"
		@change="$emit('update', $event.target.checked ? 'Yes' : 'No')"
	/>
	<textarea
		v-else-if="field.control === 'text'"
		:id="id"
		rows="2"
		:class="inputClass"
		:value="field.value"
		:aria-invalid="invalid ? 'true' : 'false'"
		:aria-describedby="describedby"
		@input="$emit('update', $event.target.value)"
	/>
	<template v-else>
		<input
			:id="id"
			:type="type"
			:class="inputClass"
			:value="field.value"
			:list="field.control === 'link' ? id + '-list' : undefined"
			:aria-invalid="invalid ? 'true' : 'false'"
			:aria-describedby="describedby"
			@input="$emit('update', $event.target.value)"
		/>
		<datalist v-if="field.control === 'link'" :id="id + '-list'">
			<option v-for="o in suggestions" :key="o" :value="o" />
		</datalist>
	</template>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
	field: { type: Object, required: true },
	id: { type: String, required: true },
	invalid: { type: Boolean, default: false },
	error: { type: Boolean, default: false },
	describedby: { type: String, default: undefined },
	suggestions: { type: Array, default: () => [] },
});
defineEmits(["update"]);

const TYPES = { number: "number", date: "date", datetime: "datetime-local", time: "time" };
const type = computed(() => TYPES[props.field.control] || "text");
const inputClass = computed(() => [
	"w-full rounded border px-2 py-1.5 text-base text-ink-gray-8 focus:outline-none focus:ring-2 focus:ring-outline-gray-3",
	props.error
		? "border-outline-red-2 bg-surface-red-1"
		: props.invalid
		? "border-outline-amber-2 bg-surface-amber-1"
		: "border-outline-gray-2 bg-surface-white",
]);
</script>
