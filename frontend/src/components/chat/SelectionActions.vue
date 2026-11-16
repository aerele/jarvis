<!--
  The small bar that appears over text selected inside ONE assistant reply
  (aerele/jarvis#2191): "Reply" quotes the selection into the composer. The host
  passes the actions it offers and handles them (`@action="(key, text) => ..."`).

  It shows on mouseup / keyup (not while a drag is still selecting) and hides when the
  selection collapses, the page scrolls or Escape is pressed. Its buttons take the
  click on mousedown.prevent so pressing one does not clear the selection first.
  Rendered in place (position: fixed), not teleported: the theme's colours are set
  on the chat root, so the bar must stay inside it.
-->
<template>
	<div
		v-if="pos"
		class="jv-selbar"
		role="toolbar"
		aria-label="Selected text"
		:style="{ top: pos.top + 'px', left: pos.left + 'px' }"
	>
		<button
			v-for="a in actions"
			:key="a.key"
			type="button"
			class="jv-selbar-btn"
			@mousedown.prevent
			@click="pick(a.key)"
		>
			{{ a.label }}
		</button>
	</div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from "vue";
import { selectedReplyText } from "@jsshared/reply_quote.mjs";

const props = defineProps({
	// The element the replies live in; a selection outside it is ignored.
	root: { type: Object, default: null },
	// A reply's body, e.g. ".jv-amsg .jv-md-body".
	selector: { type: String, required: true },
	// [{key, label}]
	actions: { type: Array, required: true },
});
const emit = defineEmits(["action"]);

const pos = ref(null);
let text = "";
let timer = null;

function hide() {
	pos.value = null;
	text = "";
}

function place() {
	const sel = window.getSelection && window.getSelection();
	const t = selectedReplyText(sel, props.root, props.selector);
	if (!t) return hide();
	const rect = sel.getRangeAt(0).getBoundingClientRect();
	if (!rect || (!rect.width && !rect.height)) return hide();
	text = t;
	const above = rect.top - 44;
	const width = 40 + props.actions.length * 90;
	pos.value = {
		top: Math.round(above >= 8 ? above : rect.bottom + 8),
		left: Math.round(
			Math.min(
				Math.max(8, rect.left + rect.width / 2 - width / 2),
				window.innerWidth - width - 8
			)
		),
	};
}

// After the browser has settled the selection for this mouseup / keyup.
function schedule() {
	clearTimeout(timer);
	timer = setTimeout(place, 0);
}

function onSelectionChange() {
	const sel = window.getSelection && window.getSelection();
	if (pos.value && (!sel || sel.isCollapsed)) hide();
}

function onKeydown(e) {
	if (e.key === "Escape" && pos.value) hide();
}

function pick(key) {
	const t = text;
	hide();
	window.getSelection()?.removeAllRanges();
	if (t) emit("action", key, t);
}

onMounted(() => {
	document.addEventListener("mouseup", schedule);
	document.addEventListener("keyup", schedule);
	document.addEventListener("selectionchange", onSelectionChange);
	document.addEventListener("keydown", onKeydown);
	window.addEventListener("scroll", hide, true);
	window.addEventListener("resize", hide);
});
onBeforeUnmount(() => {
	clearTimeout(timer);
	document.removeEventListener("mouseup", schedule);
	document.removeEventListener("keyup", schedule);
	document.removeEventListener("selectionchange", onSelectionChange);
	document.removeEventListener("keydown", onKeydown);
	window.removeEventListener("scroll", hide, true);
	window.removeEventListener("resize", hide);
});

defineExpose({ hide });
</script>

<style scoped>
.jv-selbar {
	position: fixed;
	z-index: 60;
	display: flex;
	gap: 2px;
	padding: 3px;
	background: var(--surface);
	border: 1px solid var(--border);
	border-radius: 9px;
	box-shadow: 0 4px 16px rgba(0, 0, 0, 0.18);
}
.jv-selbar-btn {
	padding: 5px 10px;
	font-size: 13px;
	line-height: 1.2;
	color: var(--text);
	background: transparent;
	border: 0;
	border-radius: 6px;
	cursor: pointer;
	white-space: nowrap;
}
.jv-selbar-btn:hover,
.jv-selbar-btn:focus-visible {
	background: var(--surface-2);
}
</style>
