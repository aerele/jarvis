<template>
	<div class="border-t py-4 first:border-t-0">
		<!-- jarvis#1062 P0-1 (production-readiness audit): the toggle is
		     scoped to this header button ONLY - a real <button>, not a <div>
		     with @click, so it is keyboard-reachable (Enter/Space, native)
		     and gets a visible focus-visible ring (P1-5) for free. The
		     content below is a SEPARATE sibling with its own @click.stop
		     belt-and-suspenders guard, so a click anywhere inside it (the
		     textarea included) can never reach this handler regardless of
		     how the slot content is structured. -->
		<button
			v-if="collapsible"
			type="button"
			class="flex h-8 max-w-fit items-center gap-1.5 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-outline-gray-3"
			@click="toggle"
		>
			<span
				class="lucide-chevron-right h-4 shrink-0 text-ink-gray-9 transition-transform duration-300 ease-in-out"
				:class="{ 'rotate-90': isOpened }"
				aria-hidden="true"
			/>
			<span class="text-base font-semibold text-ink-gray-9">{{ label }}</span>
			<slot name="header-suffix" />
		</button>
		<div v-else class="flex h-8 max-w-fit items-center gap-1.5">
			<span class="text-base font-semibold text-ink-gray-9">{{ label }}</span>
			<slot name="header-suffix" />
		</div>
		<transition
			enter-active-class="duration-300 ease-in"
			leave-active-class="duration-300 ease-out"
			enter-to-class="max-h-[200px] overflow-hidden"
			leave-from-class="max-h-[200px] overflow-hidden"
			enter-from-class="max-h-0 overflow-hidden"
			leave-to-class="max-h-0 overflow-hidden"
		>
			<div v-show="isOpened" class="pt-2" @click.stop>
				<slot />
			</div>
		</transition>
	</div>
</template>

<script setup>
// DocSection - CRM CollapsibleSection port (DESIGN-V3 §6.1): chevron header
// (rotate-90 when open), max-height transition, sections separated by border-t
// (first:border-t-0). #header-suffix is additive (status badges next to the
// label, e.g. macro "Summarized prompt").
//
// jarvis#1062 P0-1: production-readiness audit reported the Advanced (JSON)
// panel (ConfigForm.vue) re-collapsing on a click inside its own expanded
// textarea, with the chevron desynced from the content's open/closed state.
// Neither reproduced here - an isolated DocSection mount and a full
// ConfigForm mount both show a content click leaving `isOpened` (and
// therefore both the chevron and the content) untouched, exactly as this
// file's structure already implies (the header and the content are
// SIBLINGS; only the header ever called toggle()). Most likely explanation:
// a stale/different deployed build the audit ran against, or a live-only
// CSS-transition screenshot-timing artifact on the chevron's own rotation
// (a real click still flips the single `isOpened` ref both read from; jsdom
// has no transition to lag). Hardened anyway, in case the real trigger is
// something this reasoning missed: the header is now a real <button> (was a
// <div> with @click - unreachable by keyboard and had no focus ring, a
// separate defect worth fixing regardless) and the content wrapper carries
// its own @click.stop, so a click inside it can never reach toggle() no
// matter how the slotted content is restructured later.
import { ref } from "vue";

const props = defineProps({
	label: { type: String, default: "" },
	opened: { type: Boolean, default: true },
	collapsible: { type: Boolean, default: true },
});

const isOpened = ref(props.opened);

function toggle() {
	isOpened.value = !isOpened.value;
}
</script>
