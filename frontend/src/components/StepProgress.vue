<!--
  Two renderings of one stepped indicator (design.md §4.3), chosen by
  `variant`, both sharing the same tokens (bg-surface-gray-7 fill / -3 track,
  h-1 rounded-full, text-ink-gray-8 caption) so neither reads as a different
  widget:

  - variant="bar" (default): the onboarding WAIT screens. ONE continuous
    track with a single fill span, plus one caption line above it - "Step N
    of M · <current step name>". This replaced a six-tile-labels-over-six-
    segments layout that read as a duplicated row (user report, 2026-08-16):
    the per-step labels and the caption said the same thing twice, and the
    caller's own explanation line under the bar already names the current
    step, so nothing here needs to name it again. `role="progressbar"` is
    correct for this shape - one number, 0-100 - unlike the list below,
    which has no single number to expose.

  - variant="steps": the wizard RAIL at the top of every onboarding screen
    (Details / Plan / Pay / Connect). This is real navigation with four
    always-visible step names, not a wait indicator. It keeps the original
    `role="list"` / `role="listitem"` treatment with `aria-current="step"`
    on the current item - a progressbar exposes one number and cannot name
    which of four steps is current without an aria-valuetext hack, and list
    semantics say that directly. Untouched by the 2026-08-16 wait-bar
    redesign; it was never one of the screens the complaint was about.
-->
<template>
	<div class="w-full">
		<p v-if="label" :id="labelId" class="mb-2.5 text-base font-medium text-ink-gray-8">
			{{ label }}
		</p>

		<!-- variant="bar": one track, one fill. -->
		<div
			v-if="variant === 'bar'"
			class="h-1 w-full overflow-hidden rounded-full bg-surface-gray-3"
			role="progressbar"
			aria-valuemin="0"
			aria-valuemax="100"
			:aria-valuenow="indeterminate ? undefined : valueNow"
			:aria-label="label ? undefined : ariaLabel"
			:aria-labelledby="label ? labelId : undefined"
		>
			<span
				class="step-progress-fill block h-full rounded-full bg-surface-gray-7"
				:class="{ 'step-progress-fill--indeterminate': indeterminate }"
				:style="{ width: fillPercent + '%' }"
			></span>
		</div>

		<!-- variant="steps": the wizard rail, unchanged shape. -->
		<div
			v-else
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
				<span v-if="step.label" class="text-p-sm" :class="labelClass(i)">
					{{ step.label }}
				</span>
				<span v-else class="sr-only">{{ `Step ${i + 1} of ${steps.length}` }}</span>
				<span class="h-1 rounded-full" :class="segmentClass(i)"></span>
			</div>
		</div>
	</div>
</template>

<script setup>
import { computed, useId } from "vue";

const props = defineProps({
	/** One entry per step. In variant="steps", `label` is the visible name. */
	steps: { type: Array, required: true },
	/**
	 * 0-based index of the current step. The current step counts as filled
	 * along with every step before it (same rule the variant="steps" rail
	 * below already uses, `i <= currentIndex`). -1 is the all-done/empty
	 * sentinel: there is no current step because nothing is left, so the bar
	 * reads 100%, not 0%.
	 */
	currentIndex: { type: Number, required: true },
	/** Which shape to render: the wait bar (default) or the wizard rail's list. */
	variant: { type: String, default: "bar", validator: (v) => ["bar", "steps"].includes(v) },
	/**
	 * The current step's own state is unknown (nothing observed yet), so it
	 * cannot claim to be "done". In variant="bar" the fill still sits at the
	 * completed fraction but pulses instead of asserting further progress;
	 * see waitPhases.phaseProgress for the source of this flag - it is never
	 * guessed here.
	 */
	indeterminate: { type: Boolean, default: false },
	/** Optional summary caption above the indicator, e.g. "Step 2 of 6 · Workspace". */
	label: { type: String, default: "" },
	/** Accessible name when there is no visible `label`. */
	ariaLabel: { type: String, default: "Setup steps" },
});

const labelId = useId();

// Inclusive fill: the current step counts as filled, same as the
// variant="steps" rail's `i <= currentIndex` segments. currentIndex < 0 is
// the all-done/empty sentinel (no step is current), so it fills to 100
// rather than reading as "nothing has happened yet".
const fillPercent = computed(() => {
	const total = props.steps.length;
	if (total <= 0) return 0;
	if (props.currentIndex < 0) return 100;
	const filled = Math.min(props.currentIndex + 1, total);
	return Math.min(100, Math.max(0, (filled / total) * 100));
});

const valueNow = computed(() => Math.round(fillPercent.value));

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
/* variant="bar": the fill's width animates smoothly toward each new
   fraction rather than jumping. */
.step-progress-fill {
	transition: width 0.4s ease;
}
/* Same muted pulse the segmented rail already used on its current segment,
   reused here on the whole fill: "we don't currently know" reads the same
   way in both variants rather than inventing a second treatment. */
.step-progress-fill--indeterminate,
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
	.step-progress-fill--indeterminate,
	.step-progress-segment--indeterminate {
		animation: none;
		opacity: 0.75;
	}
}
</style>
