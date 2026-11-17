<!--
  The small bar that appears over text selected inside ONE assistant reply
  (aerele/jarvis#2191): "Reply" quotes the selection into the composer. The host
  passes the actions it offers and handles them (`@action="(key, text) => ..."`).

  It shows on mouseup / keyup (not while a drag is still selecting) and hides when the
  selection collapses, the page scrolls or Escape is pressed. Its buttons take the
  click on mousedown.prevent so pressing one does not clear the selection first.
  On a touch screen (the PWA passes `below`) it shows once a long-press selection
  settles, sits BELOW the selection (the phone's own Copy menu is above it), and a tap
  is taken on touchend so it lands before the tap can clear the selection.
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
			@touchstart.prevent
			@touchend.prevent="pick(a.key)"
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
	// Place the bar under the selection (touch screens).
	below: { type: Boolean, default: false },
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
	const under = rect.bottom + (props.below ? 14 : 8);
	const fitsUnder = under + 44 <= window.innerHeight;
	const width = 40 + props.actions.length * 90;
	pos.value = {
		top: Math.round(
			props.below ? (fitsUnder ? under : Math.max(8, above)) : above >= 8 ? above : under
		),
		left: Math.round(
			Math.min(
				Math.max(8, rect.left + rect.width / 2 - width / 2),
				window.innerWidth - width - 8
			)
		),
	};
}

// After the browser has settled the selection for this mouseup / keyup / touchend.
function schedule(delay = 0) {
	clearTimeout(timer);
	timer = setTimeout(place, delay);
}
const onPointerUp = () => schedule(0);
// A keyboard selection (Shift + arrows) shows the bar on key release; releasing the
// Escape that just hid it must not bring it back (the selection is still there).
const onKeyUp = (e) => {
	if (e.key !== "Escape") schedule(0);
};
const onTouchEnd = () => schedule(350);

// A touch selection is made and adjusted with handles, which fire no mouseup: place
// the bar once the selection has stopped changing.
let touching = false;
const onTouchStart = () => (touching = true);

function onSelectionChange() {
	const sel = window.getSelection && window.getSelection();
	if (!sel || sel.isCollapsed) {
		clearTimeout(timer);
		if (pos.value) hide();
		return;
	}
	if (touching) schedule(450);
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
	document.addEventListener("mouseup", onPointerUp);
	document.addEventListener("keyup", onKeyUp);
	document.addEventListener("touchstart", onTouchStart, { passive: true });
	document.addEventListener("touchend", onTouchEnd, { passive: true });
	document.addEventListener("selectionchange", onSelectionChange);
	document.addEventListener("keydown", onKeydown);
	window.addEventListener("scroll", hide, true);
	window.addEventListener("resize", hide);
});
onBeforeUnmount(() => {
	clearTimeout(timer);
	document.removeEventListener("mouseup", onPointerUp);
	document.removeEventListener("keyup", onKeyUp);
	document.removeEventListener("touchstart", onTouchStart);
	document.removeEventListener("touchend", onTouchEnd);
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
	background: var(--surface, var(--card));
	border: 1px solid var(--border, #e8e8ec);
	border-radius: 9px;
	box-shadow: 0 4px 16px rgba(0, 0, 0, 0.18);
}
.jv-selbar-btn {
	padding: 5px 10px;
	-webkit-tap-highlight-color: transparent;
	font-size: 13px;
	line-height: 1.2;
	color: var(--text, var(--ink9));
	background: transparent;
	border: 0;
	border-radius: 6px;
	cursor: pointer;
	white-space: nowrap;
}
.jv-selbar-btn:hover,
.jv-selbar-btn:focus-visible {
	background: var(--surface-2, var(--accent-bg));
}
</style>
