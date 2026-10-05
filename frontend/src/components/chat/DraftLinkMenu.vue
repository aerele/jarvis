<template>
	<Teleport to="body">
		<div ref="menu" class="jv-action-linkmenu" :style="[palette, position]">
			<button
				v-for="item in items"
				:key="item.value"
				type="button"
				@mousedown.prevent
				@click="$emit('pick', item)"
				@keydown.esc="$emit('close')"
			>
				<b>{{ item.value }}</b
				><span v-if="item.label">: {{ item.label }}</span>
			</button>
		</div>
	</Teleport>
</template>

<script setup>
import { onMounted, onBeforeUnmount, ref } from "vue";

const props = defineProps({
	anchor: { type: Object, required: true },
	items: { type: Array, required: true },
	palette: { type: Object, default: () => ({}) },
});
defineEmits(["pick", "close"]);
const menu = ref(null);
const position = ref({});
let observer;
let frame;
let boundsKey;

function place(force = true) {
	const rect = props.anchor.getBoundingClientRect();
	const key = [
		rect.left,
		rect.top,
		rect.width,
		rect.bottom,
		window.innerWidth,
		window.innerHeight,
	].join();
	if (!force && key === boundsKey) return;
	boundsKey = key;
	const hit = document.elementFromPoint(
		rect.left + rect.width / 2,
		(rect.top + rect.bottom) / 2
	);
	const margin = 8;
	const gap = 4;
	const width = Math.min(Math.max(rect.width, 300), window.innerWidth - 2 * margin);
	const below = window.innerHeight - rect.bottom - gap - margin;
	const above = rect.top - gap - margin;
	const up = below < 220 && above > below;
	position.value = {
		visibility: hit === props.anchor ? "visible" : "hidden",
		width: `${width}px`,
		left: `${Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin))}px`,
		maxHeight: `${Math.max(0, Math.min(220, up ? above : below))}px`,
		top: up ? "auto" : `${rect.bottom + gap}px`,
		bottom: up ? `${window.innerHeight - rect.top + gap}px` : "auto",
	};
}

function onScroll(event) {
	// A popup must not remain visible after its field scrolls behind a clipping
	// ancestor, but focus scrolling must not cancel the user's active search.
	if (menu.value?.contains(event.target)) return;
	place();
}

function followTransition() {
	// CSS transforms (the panel entrance animation) do not fire ResizeObserver.
	// Only the one open popup tracks its anchor; unchanged bounds do no writes.
	place(false);
	frame = requestAnimationFrame(followTransition);
}

onMounted(() => {
	place();
	frame = requestAnimationFrame(followTransition);
	window.addEventListener("resize", place);
	window.addEventListener("scroll", onScroll, true);
	observer = new ResizeObserver(place);
	observer.observe(props.anchor);
});
onBeforeUnmount(() => {
	window.removeEventListener("resize", place);
	window.removeEventListener("scroll", onScroll, true);
	observer?.disconnect();
	cancelAnimationFrame(frame);
});
</script>

<style scoped>
.jv-action-linkmenu {
	position: fixed;
	z-index: 91;
	box-sizing: border-box;
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 9px;
	box-shadow: 0 8px 24px rgba(20, 20, 30, 0.14);
	padding: 4px;
	overflow-y: auto;
	font-family: "Inter", system-ui, sans-serif;
}
.jv-action-linkmenu button {
	display: block;
	width: 100%;
	text-align: left;
	padding: 7px 9px;
	background: transparent;
	border: none;
	border-radius: 6px;
	font-family: inherit;
	font-size: 12.5px;
	color: var(--text-2);
	overflow-wrap: anywhere;
	cursor: pointer;
}
.jv-action-linkmenu button:hover,
.jv-action-linkmenu button:focus-visible {
	background: var(--surface-2);
	color: var(--text);
}
</style>
