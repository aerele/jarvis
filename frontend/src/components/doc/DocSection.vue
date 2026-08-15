<template>
	<div class="border-t py-4 first:border-t-0">
		<div
			class="flex h-8 max-w-fit items-center gap-1.5"
			:class="{ 'cursor-pointer': collapsible }"
			@click="collapsible && toggle()"
		>
			<span
				v-if="collapsible"
				class="lucide-chevron-right h-4 text-ink-gray-9 transition-all duration-300 ease-in-out"
				:class="{ 'rotate-90': isOpened }"
				aria-hidden="true"
			/>
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
			<div v-show="isOpened" class="pt-2">
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
