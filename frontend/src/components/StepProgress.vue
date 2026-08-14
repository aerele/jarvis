<!--
  The ONE stepped progress indicator (design.md §4.3): flat segments, one per
  step, filling left to right. No numbered circles, no connector lines, no
  checkmark dots. Replaces two treatments that used to disagree - the
  onboarding rail's own markup and two frappe-ui `<Progress intervals>` bars -
  with a single component both now render through.

  Accessibility: `role="list"` / `role="listitem"` with `aria-current="step"`
  on the current item, not `role="progressbar"`. A progressbar exposes one
  number (0-100); it cannot name individual steps or say which one is
  current without a parallel aria-valuetext hack. A list of steps, one of
  them current, is exactly what this widget is, and list semantics say that
  directly - it's also what the original rail already did, so keeping it is
  not a new accessibility surface, only removing a divergent one.
-->
<template>
	<div class="w-full">
		<p v-if="label" :id="labelId" class="mb-2.5 text-base font-medium text-ink-gray-8">
			{{ label }}
		</p>
		<div
			class="flex w-full items-stretch gap-2"
			role="list"
			:aria-label="label ? undefined : ariaLabel"
			:aria-labelledby="label ? labelId : undefined"
		>
			<div
				v-for="(step, i) in steps"
				:key="step.id ?? i"
				role="listitem"
				class="flex flex-1 flex-col gap-1.5"
				:aria-current="i === currentIndex ? 'step' : undefined"
			>
				<span
					v-if="step.label"
					class="text-p-sm"
					:class="[
						labelClass(i),
						collapseLabels ? 'step-progress-label--collapsible' : '',
					]"
				>
					{{ step.label }}
				</span>
				<!-- No visible per-step label (the two wait bars have none): keep an
					 accessible name on the list item anyway rather than an unnamed
					 "listitem, 2 of 3" announcement. -->
				<span v-else class="sr-only">{{ `Step ${i + 1} of ${steps.length}` }}</span>
				<span class="h-1 rounded-full" :class="segmentClass(i)"></span>
			</div>
		</div>
	</div>
</template>

<script setup>
import { useId } from "vue";

const props = defineProps({
	/** One entry per segment. `label` is optional per-step text above it. */
	steps: { type: Array, required: true },
	/** 0-based index of the current step. Steps at or before it are filled. */
	currentIndex: { type: Number, required: true },
	/**
	 * The current step's own state is unknown (nothing observed yet), so it
	 * cannot claim to be "done". Renders the current segment as a muted pulse
	 * instead of a solid fill. See waitPhases.phaseProgress for the source of
	 * this flag - it is never guessed here.
	 */
	indeterminate: { type: Boolean, default: false },
	/** Optional summary caption above the row, e.g. "Step 2 of 3". */
	label: { type: String, default: "" },
	/** Accessible name for the list when there is no visible `label`. */
	ariaLabel: { type: String, default: "Setup steps" },
	/**
	 * Visually hide the per-step labels on narrow screens (they stay readable
	 * to screen readers). For bars with many short-labeled steps - the connect
	 * wait's six - a phone-width card cannot fit a label per segment; the
	 * caption and the caller's own explanation line carry the words there.
	 * 719px matches the onboarding wait block's own stacking breakpoint.
	 */
	collapseLabels: { type: Boolean, default: false },
});

const labelId = useId();

function labelClass(i) {
	if (i === props.currentIndex) return "font-medium text-ink-gray-9";
	if (i < props.currentIndex) return "text-ink-gray-7";
	return "text-ink-gray-5";
}

function segmentClass(i) {
	if (i === props.currentIndex && props.indeterminate) {
		return "step-progress-segment--indeterminate bg-surface-gray-3";
	}
	return i <= props.currentIndex ? "bg-surface-gray-7" : "bg-surface-gray-3";
}
</script>

<style scoped>
/* Same muted pulse the old .ob-progress--indeterminate rule drew on frappe-ui
   Progress's middle interval: reuses the upcoming/track colour rather than a
   new one, so "we don't currently know" reads the same here as it does on
   the waiting dot next to it (waitPhases.js). */
.step-progress-segment--indeterminate {
	animation: step-progress-pulse 1.6s ease-in-out infinite;
}
@keyframes step-progress-pulse {
	0%,
	100% {
		opacity: 0.5;
	}
	50% {
		opacity: 1;
	}
}
@media (prefers-reduced-motion: reduce) {
	.step-progress-segment--indeterminate {
		animation: none;
		opacity: 0.75;
	}
}
/* collapseLabels: the sr-only recipe, applied only under the breakpoint so
   the label stays announced while taking no visual space. */
@media (max-width: 719px) {
	.step-progress-label--collapsible {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
}
</style>
