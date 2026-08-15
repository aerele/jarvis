<script setup>
import { watch } from "vue";

// Bottom sheet — the phone's dialog. A centred modal is a desktop idiom; on a
// phone the thumb is at the bottom, so that is where a decision belongs.
const props = defineProps({ open: { type: Boolean, default: false } });
const emit = defineEmits(["close"]);

// While a sheet is up the thread behind it must not scroll under the user's
// finger.
watch(
	() => props.open,
	(open) => {
		document.body.style.overflow = open ? "hidden" : "";
	}
);
</script>

<template>
	<Transition name="jv-sheet">
		<div v-if="props.open" class="jv-sheet-root">
			<div class="jv-sheet-scrim" @click="emit('close')" />
			<div class="jv-sheet jv-safe-bottom" role="dialog" aria-modal="true">
				<div class="jv-sheet-grab" />
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
</style>
