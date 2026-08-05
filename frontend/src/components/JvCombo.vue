<!-- Themed select / combobox - a CSS-styleable replacement for native <select>
     and <input list=datalist>, whose popups can't be styled to match the app.
     `options` accepts strings or {value,label}. `allowCustom` makes it a
     free-text combobox (type your own value, filtered suggestions); otherwise
     it's a plain picker. Emits update:modelValue with the chosen value. -->
<template>
	<div class="jvc" ref="root">
		<div
			class="jvc-field"
			:class="{ 'jvc-open': open, 'jvc-dis': !editable }"
			role="button"
			:tabindex="allowCustom ? -1 : 0"
			:aria-expanded="open"
			@click="onFieldClick"
			@keydown.down.prevent="openAnd(0)"
			@keydown.enter.prevent="onEnter"
			@keydown.esc="open = false"
		>
			<input
				v-if="allowCustom"
				ref="inputEl"
				class="jvc-input"
				:value="modelValue"
				:id="id"
				:placeholder="placeholder"
				:disabled="!editable"
				:autocomplete="autocomplete"
				:aria-required="ariaRequired ? 'true' : undefined"
				@input="onInput"
				@focus="open = true"
				@keydown.down.prevent.stop="move(1)"
				@keydown.up.prevent.stop="move(-1)"
				@keydown.enter.prevent.stop="onEnter"
				@keydown.esc.stop="open = false"
			/>
			<span v-else class="jvc-val" :class="{ 'jvc-ph': !displayLabel }"
				><slot name="selected" :label="displayLabel" :placeholder="placeholder">{{
					displayLabel || placeholder
				}}</slot></span
			>
			<svg
				class="jvc-chev"
				viewBox="0 0 24 24"
				fill="none"
				stroke="currentColor"
				stroke-width="2"
				stroke-linecap="round"
				stroke-linejoin="round"
			>
				<path d="M6 9l6 6 6-6" />
			</svg>
		</div>
		<ul v-if="open && editable && filtered.length" class="jvc-menu" role="listbox">
			<li
				v-for="(o, idx) in filtered"
				:key="o.value"
				role="option"
				:aria-selected="o.value === modelValue"
				class="jvc-opt"
				:class="{ 'jvc-on': o.value === modelValue, 'jvc-hi': idx === hi }"
				@mousedown.prevent="choose(o)"
				@mousemove="hi = idx"
			>
				<span class="jvc-opt-l"
					><slot name="option" :option="o">{{ o.label }}</slot></span
				>
				<svg
					v-if="o.value === modelValue"
					class="jvc-check"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2.5"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path d="M20 6L9 17l-5-5" />
				</svg>
			</li>
		</ul>
	</div>
</template>

<script setup>
import { ref, computed, watch, onBeforeUnmount } from "vue";
const props = defineProps({
	modelValue: { type: String, default: "" },
	options: { type: Array, default: () => [] },
	placeholder: { type: String, default: "" },
	editable: { type: Boolean, default: true },
	allowCustom: { type: Boolean, default: false },
	// Passthroughs so a JvCombo can stand in for a labelled/native form field:
	// `id` lets an external <label for> + aria-required target the input, and
	// `autocomplete` preserves the browser's saved-value hints.
	id: { type: String, default: undefined },
	autocomplete: { type: String, default: undefined },
	ariaRequired: { type: Boolean, default: false },
});
// "enter" fires when the user presses Enter without picking a suggestion, so a
// host can wire Enter-to-submit (the native <input> it replaces had that).
const emit = defineEmits(["update:modelValue", "enter"]);
const root = ref(null);
const inputEl = ref(null);
const open = ref(false);
const hi = ref(-1);

const norm = computed(() =>
	(props.options || []).map((o) => (typeof o === "string" ? { value: o, label: o } : o))
);
const displayLabel = computed(() => {
	const f = norm.value.find((o) => o.value === props.modelValue);
	return f ? f.label : props.allowCustom ? props.modelValue : "";
});
const filtered = computed(() => {
	if (!props.allowCustom) return norm.value;
	const q = (props.modelValue || "").trim().toLowerCase();
	if (!q) return norm.value;
	const hit = norm.value.filter((o) => o.label.toLowerCase().includes(q));
	return hit.length ? hit : norm.value; // never blank the list while typing a custom id
});

function onFieldClick() {
	if (!props.editable) return;
	if (props.allowCustom) {
		inputEl.value && inputEl.value.focus();
		open.value = true;
	} else open.value = !open.value;
}
// Reset the highlight on every keystroke: the filtered list is recomputed from
// the new text, so a stale `hi` would make Enter pick an unrelated option.
function onInput(e) {
	emit("update:modelValue", e.target.value);
	open.value = true;
	hi.value = -1;
}
function choose(o) {
	emit("update:modelValue", o.value);
	open.value = false;
	hi.value = -1;
}
function move(d) {
	if (!open.value) {
		open.value = true;
		return;
	}
	const n = filtered.value.length;
	if (n) hi.value = (hi.value + d + n) % n;
}
function onEnter() {
	if (open.value && hi.value >= 0 && filtered.value[hi.value]) {
		choose(filtered.value[hi.value]);
		return;
	}
	// No suggestion picked - keep whatever the user typed and let the host act on Enter.
	open.value = false;
	emit("enter");
}
function openAnd(i) {
	if (props.editable) {
		open.value = true;
		hi.value = i;
	}
}

function onDocClick(e) {
	if (root.value && !root.value.contains(e.target)) open.value = false;
}
watch(open, (v) => {
	if (v) document.addEventListener("mousedown", onDocClick);
	else document.removeEventListener("mousedown", onDocClick);
});
onBeforeUnmount(() => document.removeEventListener("mousedown", onDocClick));
</script>

<style scoped>
.jvc {
	position: relative;
	width: 100%;
}
.jvc-field {
	display: flex;
	align-items: center;
	gap: 8px;
	width: 100%;
	min-height: 40px;
	padding: 9px 12px;
	border: 1px solid var(--border);
	border-radius: 8px;
	background: var(--surface);
	color: var(--text);
	font-size: 14px;
	font-family: inherit;
	cursor: pointer;
	box-sizing: border-box;
}
.jvc-field:hover {
	border-color: var(--border-2);
}
.jvc-open {
	border-color: var(--cta-bd);
}
.jvc-dis {
	cursor: default;
	opacity: 0.7;
}
.jvc-input {
	flex: 1;
	min-width: 0;
	border: 0;
	outline: none;
	background: transparent;
	color: inherit;
	font: inherit;
	padding: 0;
}
/* The inner input is not the control the customer sees - .jvc-field is, and it
   carries the border. Any ring on the input would therefore be drawn INSIDE that
   border, which is the "box inside a box" look. Focus is signalled on the wrapper
   instead: a tinted border while anything inside has focus, and for keyboard users
   a real ring OUTSIDE the border (positive outline-offset), matching the
   outline: 2px solid var(--cta) / offset 2px idiom used elsewhere in the app. */
.jvc-input:focus,
.jvc-input:focus-visible {
	outline: none;
}
.jvc-field:focus-within {
	border-color: var(--cta-bd);
}
/* Two selectors, because there are two ways this control can hold focus. A plain
   picker has no inner input, so the wrapper itself is the tab stop and matches
   :focus-visible directly. An allowCustom combo puts a real <input> inside, and
   THAT is the element the customer tabs to and types into - the wrapper never
   matches :focus-visible in that case, so keying the ring off it alone left the
   editable combos (model id, base URL) with no keyboard focus indicator at all.
   :has keeps the ring on the wrapper, outside its border, rather than drawing a
   second box inside it. */
.jvc-field:focus-visible,
.jvc-field:has(.jvc-input:focus-visible) {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
.jvc-val {
	flex: 1;
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jvc-ph {
	color: var(--text-3);
}
.jvc-chev {
	width: 16px;
	height: 16px;
	flex: none;
	color: var(--text-3);
}
.jvc-menu {
	position: absolute;
	z-index: 30;
	top: calc(100% + 4px);
	left: 0;
	right: 0;
	margin: 0;
	padding: 5px;
	list-style: none;
	background: var(--surface);
	border: 1px solid var(--border);
	border-radius: 10px;
	box-shadow: 0 12px 32px -8px rgba(15, 23, 42, 0.25);
	max-height: 258px;
	overflow-y: auto;
}
.jvc-opt {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 8px;
	padding: 8px 10px;
	border-radius: 7px;
	font-size: 14px;
	cursor: pointer;
	color: var(--text);
}
.jvc-hi,
.jvc-opt:hover {
	background: var(--surface-2);
}
.jvc-on {
	font-weight: 600;
}
.jvc-check {
	width: 15px;
	height: 15px;
	color: var(--cta);
	flex: none;
}
</style>
