<script setup>
import { onBeforeUnmount, ref, watch } from "vue";
import { registerSheet } from "../lib/sheetFocus";
import { fitViewport } from "../lib/viewportFit.js";

const props = defineProps({
	open: { type: Boolean, default: false },
	label: { type: String, default: "" },
	// Full screen (the draft edit and preview sheets): the whole screen, notch to home bar, kept
	// to what the virtual keyboard leaves. Off, the sheet is exactly what it has always been.
	full: { type: Boolean, default: false },
});
const emit = defineEmits(["close"]);
const panel = ref(null);
const root = ref(null);
let release;
let unfit;
watch(
	[root, () => props.full],
	([el, full]) => {
		unfit?.();
		unfit = el && full ? fitViewport(el, window) : null;
	},
	{ flush: "post" }
);
onBeforeUnmount(() => unfit?.());
watch(
	panel,
	(root) => {
		release?.();
		release = root ? registerSheet(root, () => emit("close")) : null;
	},
	{ flush: "post" }
);
onBeforeUnmount(() => release?.());
</script>

<template>
	<Transition name="jv-sheet">
		<div v-if="props.open" ref="root" class="jv-sheet-root" :class="{ 'is-full': props.full }">
			<div class="jv-sheet-scrim" @click="emit('close')" />
			<div
				ref="panel"
				class="jv-sheet jv-safe-bottom"
				:class="{ 'is-full': props.full }"
				role="dialog"
				aria-modal="true"
				:aria-label="label || undefined"
				tabindex="-1"
			>
				<div v-if="!props.full" class="jv-sheet-grab" />
				<slot />
			</div>
		</div>
	</Transition>
</template>

<style scoped>
.jv-sheet-root {
	position: fixed;
	inset: 0;
	z-index: 60;
	display: flex;
	flex-direction: column;
	justify-content: flex-end;
}
.jv-sheet-scrim {
	position: absolute;
	inset: 0;
	background: var(--scrim);
}
.jv-sheet {
	position: relative;
	max-height: 88dvh;
	display: flex;
	flex-direction: column;
	background: var(--card);
	border-radius: 22px 22px 0 0;
	border-top: 1px solid var(--border);
	overflow: hidden;
}
.jv-sheet-grab {
	width: 38px;
	height: 4px;
	margin: 8px auto 6px;
	flex: none;
	border-radius: 999px;
	background: var(--card3);
}

.jv-sheet-enter-active,
.jv-sheet-leave-active {
	transition: opacity 0.18s ease;
}
.jv-sheet-enter-active .jv-sheet,
.jv-sheet-leave-active .jv-sheet {
	transition: transform 0.24s cubic-bezier(0.32, 0.72, 0, 1);
}
.jv-sheet-enter-from,
.jv-sheet-leave-to {
	opacity: 0;
}
.jv-sheet-enter-from .jv-sheet,
.jv-sheet-leave-to .jv-sheet {
	transform: translateY(100%);
}
@media (prefers-reduced-motion: reduce) {
	.jv-sheet-enter-active .jv-sheet,
	.jv-sheet-leave-active .jv-sheet {
		transition: none;
	}
}
/* Full screen: top to bottom of what is visible (see lib/viewportFit.js for the two variables), the
   notch cleared at the top and the home bar at the bottom (.jv-safe-bottom), square corners. */
.jv-sheet-root.is-full {
	inset: auto;
	top: var(--jv-vv-top, 0px);
	left: 0;
	right: 0;
	height: var(--jv-vv-height, 100dvh);
}
.jv-sheet.is-full {
	height: 100%;
	max-height: none;
	padding-top: env(safe-area-inset-top);
	/* no home-bar padding while the keyboard covers it (viewportFit sets --jv-kb-bottom) */
	padding-bottom: var(--jv-kb-bottom, env(safe-area-inset-bottom));
	border-radius: 0;
	border-top: 0;
}
</style>
