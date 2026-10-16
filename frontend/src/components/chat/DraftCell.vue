<script setup>
// One editable value of a child row in the record draft panel: a grid cell, or a
// field of the row's full form (DraftRowForm). Moved out of ChatView's grid so the
// two render every fieldtype the same way; the link search stays in ChatView, which
// owns the one shared popup, so this only reports focus/typing and the pick.
import DraftLinkMenu from "@/components/chat/DraftLinkMenu.vue";
import { cellOptions } from "@/lib/docFields";

const props = defineProps({
	field: { type: Object, required: true },
	row: { type: Object, required: true },
	// The link-search key for this value ("t:{table index}:{row index}:{fieldname}").
	cellKey: { type: String, required: true },
	readOnly: { type: Boolean, default: false },
	// In the row form a long-text field gets a textarea; a grid cell stays one line.
	wide: { type: Boolean, default: false },
	// The grid has no visible label per cell, so it names the control; the row form
	// ties a real <label> to the control through its id instead.
	label: { type: String, default: "" },
	controlId: { type: String, default: "" },
	draftLink: { type: Object, required: true },
	palette: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["link-search", "link-blur", "link-pick", "link-close"]);

const NUMBER = ["Int", "Float", "Currency", "Percent"];
const LONG_TEXT = ["Small Text", "Text", "Long Text", "Text Editor", "Code", "Markdown Editor"];

function search(ev) {
	emit(
		"link-search",
		props.cellKey,
		() => props.row[props.field.fieldname],
		props.field.options,
		ev
	);
}
</script>

<template>
	<span v-if="readOnly" class="jv-cell-ro">{{ row[field.fieldname] }}</span>
	<template v-else-if="field.fieldtype === 'Link'">
		<input
			class="jv-action-input"
			v-model="row[field.fieldname]"
			:id="controlId || undefined"
			:aria-label="label || undefined"
			@input="search"
			@focus="search"
			@blur="emit('link-blur', $event)"
			autocomplete="off"
		/>
		<DraftLinkMenu
			v-if="draftLink.open && draftLink.key === cellKey && draftLink.items.length"
			:anchor="draftLink.anchor"
			:items="draftLink.items"
			:palette="palette"
			@pick="(it) => emit('link-pick', (v) => (row[field.fieldname] = v), it)"
			@close="emit('link-close')"
		/>
	</template>
	<input
		v-else-if="NUMBER.includes(field.fieldtype)"
		type="number"
		class="jv-action-input"
		:id="controlId || undefined"
		:aria-label="label || undefined"
		v-model="row[field.fieldname]"
	/>
	<input
		v-else-if="field.fieldtype === 'Date'"
		type="date"
		class="jv-action-input"
		:id="controlId || undefined"
		:aria-label="label || undefined"
		v-model="row[field.fieldname]"
	/>
	<select
		v-else-if="field.fieldtype === 'Select'"
		class="jv-action-input jv-action-sel"
		:id="controlId || undefined"
		:aria-label="label || undefined"
		v-model="row[field.fieldname]"
	>
		<option v-for="o in cellOptions(field, row[field.fieldname])" :key="o" :value="o">
			{{ o }}
		</option>
	</select>
	<select
		v-else-if="field.fieldtype === 'Check'"
		class="jv-action-input jv-action-sel"
		:id="controlId || undefined"
		:aria-label="label || undefined"
		v-model="row[field.fieldname]"
	>
		<option value="1">Yes</option>
		<option value="0">No</option>
	</select>
	<textarea
		v-else-if="wide && LONG_TEXT.includes(field.fieldtype)"
		class="jv-action-input"
		rows="3"
		:id="controlId || undefined"
		:aria-label="label || undefined"
		v-model="row[field.fieldname]"
	></textarea>
	<input
		v-else
		class="jv-action-input"
		:id="controlId || undefined"
		:aria-label="label || undefined"
		v-model="row[field.fieldname]"
	/>
</template>
