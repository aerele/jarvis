<template>
	<div class="relative" ref="root">
		<button
			type="button"
			:class="[
				'flex h-7 w-full items-center justify-between gap-1 rounded px-2 text-base focus:outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-3',
				variant === 'ghost'
					? 'text-ink-gray-5 hover:bg-surface-gray-2'
					: 'bg-surface-gray-2 text-ink-gray-8 hover:bg-surface-gray-3',
			]"
			:aria-label="ariaLabel"
			:aria-expanded="open"
			aria-haspopup="listbox"
			@click="toggle"
			@keydown.down.prevent="open ? move(1) : openMenu(0)"
			@keydown.up.prevent="open ? move(-1) : openMenu(options.length - 1)"
			@keydown.enter.prevent="open ? choose(hi) : openMenu(currentIndex)"
			@keydown.esc="close"
		>
			<span class="truncate">{{ currentLabel }}</span>
			<FeatherIcon name="chevron-down" class="size-4 shrink-0 text-ink-gray-5" />
		</button>
		<!-- Inline listbox (NO reka SelectPortal): it lives in the panel's own DOM so a
		     click on it is not read as an outside-click, and it opens - the FormControl
		     select it replaces would not open at all inside the teleported Popover. -->
		<ul
			v-if="open"
			role="listbox"
			:aria-label="ariaLabel"
			class="absolute left-0 z-30 mt-1 max-h-60 min-w-full overflow-auto whitespace-nowrap rounded-lg bg-surface-modal p-1 shadow-2xl ring-1 ring-black ring-opacity-5"
		>
			<li
				v-for="(o, idx) in options"
				:key="o.value"
				role="option"
				:aria-selected="o.value === modelValue"
				class="flex cursor-pointer items-center justify-between gap-3 rounded px-2 py-1.5 text-base text-ink-gray-8"
				:class="idx === hi ? 'bg-surface-gray-2' : ''"
				@mousedown.prevent="choose(idx)"
				@mousemove="hi = idx"
			>
				<span>{{ o.label }}</span>
				<FeatherIcon
					v-if="o.value === modelValue"
					name="check"
					class="size-4 shrink-0 text-ink-gray-7"
				/>
			</li>
		</ul>
	</div>
</template>

<script setup>
// PanelSelect - a portal-free, globally-themed {label,value} select for the filter
// panel (the operator picker AND the value selects). frappe-ui's FormControl select renders options through a reka SelectPortal
// that will NOT open inside FilterGroup's teleported Popover (the same portal trap
// SortButton dodged). This keeps the listbox inline in the panel's own DOM and styles
// it from GLOBAL surface/ink tokens that resolve inside a teleport - JvCombo's
// paletteVars-based styling renders transparent there. Contract mirrors the old
// FormControl: options [{label,value}], modelValue, emit update:modelValue.
import { ref, computed, watch, onBeforeUnmount } from "vue";
import { FeatherIcon } from "frappe-ui";

const props = defineProps({
	modelValue: { type: String, default: "" },
	options: { type: Array, default: () => [] },
	ariaLabel: { type: String, default: undefined },
	// "filled" (default, a select box) or "ghost" (a subtle text trigger, e.g. "+ Add Filter")
	variant: { type: String, default: "filled" },
});
const emit = defineEmits(["update:modelValue"]);

const root = ref(null);
const open = ref(false);
const hi = ref(-1); // highlighted index while the menu is open

const currentIndex = computed(() => props.options.findIndex((o) => o.value === props.modelValue));
const currentLabel = computed(() => {
	const o = props.options[currentIndex.value];
	return (o && o.label) || props.modelValue || "";
});

function openMenu(start) {
	open.value = true;
	hi.value = start >= 0 && start < props.options.length ? start : 0;
	document.addEventListener("mousedown", onDocMouseDown, true);
}
function close() {
	if (!open.value) return;
	open.value = false;
	hi.value = -1;
	document.removeEventListener("mousedown", onDocMouseDown, true);
}
function toggle() {
	open.value ? close() : openMenu(currentIndex.value);
}
function move(d) {
	const n = props.options.length;
	if (n) hi.value = (hi.value + d + n) % n;
}
function choose(idx) {
	const o = props.options[idx];
	if (o) emit("update:modelValue", o.value);
	close();
}
// A click anywhere outside closes the menu WITHOUT dismissing the parent Popover,
// because this listener only closes our own listbox.
function onDocMouseDown(e) {
	if (root.value && !root.value.contains(e.target)) close();
}
// The row can be retargeted to another field mid-open; keep the highlight in range.
watch(
	() => props.options,
	() => {
		if (open.value) hi.value = Math.min(Math.max(hi.value, 0), props.options.length - 1);
	}
);
onBeforeUnmount(() => document.removeEventListener("mousedown", onDocMouseDown, true));
</script>
