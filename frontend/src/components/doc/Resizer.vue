<template>
	<div class="relative" :style="{ width: sidebarWidth + 'px' }">
		<slot v-bind="{ sidebarResizing, sidebarWidth }" />
		<div
			class="absolute top-0 z-10 h-full w-1 cursor-col-resize bg-surface-gray-4 opacity-0 transition-opacity hover:opacity-100"
			:class="{
				'opacity-100': sidebarResizing,
				'left-0': side === 'right',
				'right-0': side === 'left',
			}"
			@mousedown="startResize"
		/>
	</div>
</template>

<script setup>
// CRM Resizer port (DESIGN-V3 §6.1, R3 §3): resizable side panel with a 1px
// drag handle on the inner edge, ±10px snap to the default width, and the
// chosen width persisted to localStorage['jarvis-docpanel-width'].
import { ref, onBeforeUnmount } from "vue";

const props = defineProps({
	defaultWidth: { type: Number, default: 352 },
	minWidth: { type: Number, default: 256 },
	maxWidth: { type: Number, default: 480 },
	side: { type: String, default: "right" }, // 'left' | 'right'
});

const STORAGE_KEY = "jarvis-docpanel-width";

function clamp(w) {
	return Math.min(props.maxWidth, Math.max(props.minWidth, w));
}

const stored = Number(localStorage.getItem(STORAGE_KEY));
const sidebarWidth = ref(stored ? clamp(stored) : props.defaultWidth);
const sidebarResizing = ref(false);

function resize(e) {
	sidebarResizing.value = true;
	document.body.classList.add("select-none", "cursor-col-resize");
	let w = props.side === "left" ? e.clientX : window.innerWidth - e.clientX;
	// snap to the default within ±10px
	if (w > props.defaultWidth - 10 && w < props.defaultWidth + 10) w = props.defaultWidth;
	sidebarWidth.value = clamp(w);
}

function stopResize() {
	document.body.classList.remove("select-none", "cursor-col-resize");
	localStorage.setItem(STORAGE_KEY, String(sidebarWidth.value));
	sidebarResizing.value = false;
	document.removeEventListener("mousemove", resize);
	document.removeEventListener("mouseup", stopResize);
}

function startResize() {
	document.addEventListener("mousemove", resize);
	document.addEventListener("mouseup", stopResize);
}

onBeforeUnmount(() => {
	document.removeEventListener("mousemove", resize);
	document.removeEventListener("mouseup", stopResize);
});
</script>
