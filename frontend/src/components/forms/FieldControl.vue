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
	<!-- Grows with its text so a long value (a Small Text / Text field such as
	     Supplier Details) is readable without dragging the corner (#760); past
	     TEXT_MAX_PX it scrolls. Still resizable by hand. -->
	<textarea
		v-else-if="field.control === 'text'"
		:id="id"
		ref="textEl"
		rows="3"
		:class="inputClass"
		:style="{ maxHeight: TEXT_MAX_PX + 'px' }"
		:value="field.value"
		:aria-invalid="invalid ? 'true' : 'false'"
		:aria-describedby="describedby"
		@input="onTextInput"
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
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = defineProps({
	field: { type: Object, required: true },
	id: { type: String, required: true },
	invalid: { type: Boolean, default: false },
	error: { type: Boolean, default: false },
	describedby: { type: String, default: undefined },
	suggestions: { type: Array, default: () => [] },
});
const emit = defineEmits(["update"]);

// A text field fits its content up to this height (about 14 lines), then scrolls.
const TEXT_MAX_PX = 320;
const textEl = ref(null);

function fitText() {
	const el = textEl.value;
	if (!el) return;
	el.style.height = "auto"; // shrink first, so deleted text gives the space back
	const border = el.offsetHeight - el.clientHeight;
	el.style.height = Math.min(el.scrollHeight + border, TEXT_MAX_PX) + "px";
}

function onTextInput(event) {
	emit("update", event.target.value);
	fitText();
}

// Refit when the value is set from outside (another record picked, a reset) and when
// the panel's width changes the wrapping.
watch(
	() => props.field.value,
	() => nextTick(fitText)
);
let resizeObserver = null;
onMounted(() => {
	fitText();
	if (textEl.value && typeof ResizeObserver !== "undefined") {
		let width = textEl.value.clientWidth;
		resizeObserver = new ResizeObserver(() => {
			const now = textEl.value?.clientWidth;
			if (now !== width) {
				width = now;
				fitText();
			}
		});
		resizeObserver.observe(textEl.value);
	}
});
onBeforeUnmount(() => resizeObserver?.disconnect());

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
