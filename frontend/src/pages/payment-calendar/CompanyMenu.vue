<template>
	<div ref="cell" class="filter company-filter">
		<span id="company-label" class="field-label">Company</span>
		<button
			id="company-trigger"
			ref="trigger"
			type="button"
			class="filter-button"
			aria-labelledby="company-label company-value"
			aria-haspopup="listbox"
			:aria-expanded="String(open)"
			:disabled="disabled"
			@click="emit('update:open', !open)"
		>
			<span id="company-value" class="value">{{ modelValue || "Select a company" }}</span>
			<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
		</button>
		<div v-if="open" id="company-menu" class="menu">
			<input
				id="company-search"
				ref="search"
				v-model="query"
				type="search"
				role="combobox"
				aria-label="Search companies"
				aria-expanded="true"
				aria-controls="company-options"
				placeholder="Search companies…"
				autocomplete="off"
				@keydown.down.prevent="focusOption(0)"
			/>
			<div
				id="company-options"
				ref="list"
				role="listbox"
				aria-label="Companies"
				@keydown="cycle"
			>
				<button
					v-for="company in matching"
					:key="company.name"
					type="button"
					role="option"
					class="option"
					:class="{ active: company.name === modelValue }"
					:aria-selected="String(company.name === modelValue)"
					@click="choose(company.name)"
				>
					{{ company.name
					}}<span class="check">{{ company.name === modelValue ? "✓" : "" }}</span>
				</button>
				<p v-if="!matching.length" class="schedule-empty">No matching companies.</p>
			</div>
		</div>
	</div>
</template>
<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { OVERLAY_PRIORITY, useOverlay } from "./overlayStack";

const props = defineProps({
	companies: { type: Array, default: () => [] },
	modelValue: { type: String, default: "" },
	open: { type: Boolean, default: false },
	disabled: { type: Boolean, default: false },
});
const emit = defineEmits(["update:open", "select"]);

const cell = ref(null),
	trigger = ref(null),
	search = ref(null),
	list = ref(null),
	query = ref("");
const matching = computed(() => {
	const needle = query.value.toLowerCase();
	return props.companies.filter((company) => company.name.toLowerCase().includes(needle));
});

// Escape closes this menu only when it is the top overlay, and hands focus back.
useOverlay(
	() => props.open,
	OVERLAY_PRIORITY.company,
	() => closeAndFocus()
);

watch(
	() => props.open,
	(open) => {
		if (open) {
			query.value = "";
			document.addEventListener("pointerdown", closeOutside);
			void nextTick(() => search.value?.focus());
		} else document.removeEventListener("pointerdown", closeOutside);
	},
	{ immediate: true }
);
onBeforeUnmount(() => document.removeEventListener("pointerdown", closeOutside));

function closeOutside(event) {
	if (!cell.value?.contains(event.target)) emit("update:open", false);
}
function closeAndFocus() {
	emit("update:open", false);
	void nextTick(() => trigger.value?.focus());
}
function choose(name) {
	emit("select", name);
	closeAndFocus();
}

const optionButtons = () => [...(list.value?.querySelectorAll('[role="option"]') || [])];
function focusOption(index) {
	optionButtons()[index]?.focus();
}
// ArrowDown / ArrowUp walk the options and wrap at both ends.
function cycle(event) {
	const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
	const buttons = optionButtons();
	if (!step || !buttons.length) return;
	event.preventDefault();
	const at = buttons.indexOf(document.activeElement);
	buttons[(at + step + buttons.length) % buttons.length].focus();
}
</script>
