<script setup>
// One editable value of a child row in the record draft panel: a grid cell, or a
// field of the row's full form (DraftRowForm). Moved out of ChatView's grid so the
// two render every fieldtype the same way; the link search stays in ChatView, which
// owns the one shared popup, so this only reports focus/typing and the pick.
import DraftLinkMenu from "@/components/chat/DraftLinkMenu.vue";
import { cellOptions } from "@/lib/docFields";
import { checkToYesNo } from "@/lib/draftApply";

const props = defineProps({
	field: { type: Object, required: true },
	row: { type: Object, required: true },
	// The link-search key for this value ("t:{table index}:{row __key}:{fieldname}", the row's stable key).
	cellKey: { type: String, required: true },
	readOnly: { type: Boolean, default: false },
	// In the row form a long-text field gets a textarea; a grid cell stays one line.
	wide: { type: Boolean, default: false },
	// The grid has no visible label per cell, so it names the control; the row form
	// ties a real <label> to the control through its id instead.
	label: { type: String, default: "" },
	// A read-only value is named by the id of the visible label that sits beside it
	// (the row form), instead of an aria-label that would repeat the text.
	labelledby: { type: String, default: "" },
	controlId: { type: String, default: "" },
	// The ids of the texts that describe this value ("Required", "was X"), and whether
	// the field is required: both go on the control for a screen reader.
	describedby: { type: String, default: "" },
	required: { type: Boolean, default: false },
	draftLink: { type: Object, required: true },
	palette: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["link-search", "link-blur", "link-pick", "link-close"]);

const NUMBER = ["Int", "Float", "Currency", "Percent", "Rating"];
const LONG_TEXT = [
	"Small Text",
	"Text",
	"Long Text",
	"Text Editor",
	"Code",
	"Markdown Editor",
	"JSON",
];
// A "Read Only" fieldtype is a computed display field: never an input.
const isReadOnly = () => props.readOnly || props.field.fieldtype === "Read Only";
// A read-only Check reads Yes/No, as its control would.
const shown = () => {
	const v = props.row[props.field.fieldname];
	if (props.field.fieldtype === "Check") return checkToYesNo(v);
	return v == null || v === "" ? "-" : v;
};

// The option a Check select shows. The row may hold what the agent proposed
// ("Yes", "false", true), so map it to the 1/0 options for display only; the row is
// rewritten when the user picks. A blank cell (a new row) is unticked, as in Desk, so
// it shows No; that writes nothing either.
const checkValue = () => (checkToYesNo(props.row[props.field.fieldname]) === "Yes" ? "1" : "0");

// Escape with this input's suggestion menu open closes only the menu: the key stops
// here, so the row form around it stays open. With no menu it goes on up and closes
// the form.
function onEscape(ev) {
	if (
		props.draftLink.open &&
		props.draftLink.key === props.cellKey &&
		props.draftLink.items.length
	) {
		ev.preventDefault();
		ev.stopPropagation();
		emit("link-close");
	}
}

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
	<span
		v-if="isReadOnly()"
		class="jv-cell-ro"
		:id="controlId || undefined"
		role="textbox"
		aria-readonly="true"
		:aria-label="label || undefined"
		:aria-labelledby="labelledby || undefined"
		:aria-describedby="describedby || undefined"
		>{{ shown() }}</span
	>
	<template v-else-if="field.fieldtype === 'Link'">
		<input
			class="jv-action-input"
			v-model="row[field.fieldname]"
			:id="controlId || undefined"
			:aria-label="label || undefined"
			:aria-describedby="describedby || undefined"
			:aria-required="required ? 'true' : undefined"
			@input="search"
			@focus="search"
			@keydown.esc="onEscape"
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
		:aria-describedby="describedby || undefined"
		:aria-required="required ? 'true' : undefined"
		v-model="row[field.fieldname]"
	/>
	<input
		v-else-if="field.fieldtype === 'Datetime' || field.fieldtype === 'Time'"
		:type="field.fieldtype === 'Datetime' ? 'datetime-local' : 'time'"
		class="jv-action-input"
		:id="controlId || undefined"
		:aria-label="label || undefined"
		:aria-describedby="describedby || undefined"
		:aria-required="required ? 'true' : undefined"
		v-model="row[field.fieldname]"
	/>
	<input
		v-else-if="field.fieldtype === 'Date'"
		type="date"
		class="jv-action-input"
		:id="controlId || undefined"
		:aria-label="label || undefined"
		:aria-describedby="describedby || undefined"
		:aria-required="required ? 'true' : undefined"
		v-model="row[field.fieldname]"
	/>
	<select
		v-else-if="field.fieldtype === 'Select'"
		class="jv-action-input jv-action-sel"
		:id="controlId || undefined"
		:aria-label="label || undefined"
		:aria-describedby="describedby || undefined"
		:aria-required="required ? 'true' : undefined"
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
		:aria-describedby="describedby || undefined"
		:aria-required="required ? 'true' : undefined"
		:value="checkValue()"
		@change="row[field.fieldname] = $event.target.value"
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
		:aria-describedby="describedby || undefined"
		:aria-required="required ? 'true' : undefined"
		v-model="row[field.fieldname]"
	></textarea>
	<input
		v-else
		class="jv-action-input"
		:id="controlId || undefined"
		:aria-label="label || undefined"
		:aria-describedby="describedby || undefined"
		:aria-required="required ? 'true' : undefined"
		v-model="row[field.fieldname]"
	/>
</template>

<style scoped>
/* The draft panel's input look (ChatView's .jv-action-input), owned here: ChatView's
   scoped style reaches only a child's single root element, so a Link cell (an input
   plus its menu) and every field of the row form would otherwise get the browser's
   plain controls. */
.jv-action-input {
	width: 100%;
	box-sizing: border-box;
	font-family: inherit;
	font-size: 13px;
	line-height: 1.45;
	color: var(--text);
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 7px;
	padding: 6px 9px;
	outline: none;
	resize: vertical;
	transition: border-color 0.12s, box-shadow 0.12s;
}
.jv-action-input:focus {
	border-color: var(--cta);
	box-shadow: 0 0 0 3px var(--cta-bg);
}
.jv-action-sel {
	cursor: pointer;
	appearance: auto;
}
</style>
