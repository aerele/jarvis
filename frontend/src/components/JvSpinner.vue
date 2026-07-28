<template>
	<!-- With a label: a column so the text sits under the spinner and the whole
	     thing is announced once, as a single status region. -->
	<div v-if="label" class="jv-spin-stack" role="status" aria-live="polite">
		<span class="jv-spin" :class="tier" :style="sizeStyle" aria-hidden="true">
			<span class="jv-spin-halo"></span>
			<span class="jv-spin-dots"><i></i><i></i><i></i></span>
			<span class="jv-spin-core">
				<svg viewBox="0 0 24 24" fill="#fff" aria-hidden="true">
					<path :d="BRAND_STAR_PATH" />
				</svg>
			</span>
		</span>
		<span class="jv-spin-label">{{ label }}</span>
	</div>

	<!-- Without a label: just the spinner, so it drops into a button or a table
	     cell without an extra wrapper affecting layout. -->
	<span
		v-else
		class="jv-spin"
		:class="tier"
		:style="sizeStyle"
		role="status"
		aria-label="Loading"
	>
		<span class="jv-spin-halo"></span>
		<span class="jv-spin-dots"><i></i><i></i><i></i></span>
		<span class="jv-spin-core">
			<svg viewBox="0 0 24 24" fill="#fff" aria-hidden="true">
				<path :d="BRAND_STAR_PATH" />
			</svg>
		</span>
	</span>
</template>

<script setup>
/**
 * JvSpinner - the ONE loading indicator in Jarvis.
 *
 * Distilled from the onboarding completion animation (SetupNeuralNet.vue): the
 * same gradient core, the same four-point spark, the same breathing halo and
 * inward-orbiting pulse dots, minus the twelve ERP module labels. Using the one
 * mark for every wait in the product means a customer waiting on a Connect and a
 * customer waiting on their workspace being built are looking at the same
 * product, not two.
 *
 * Do not add a second spinner. If a surface cannot fit this one, resize the
 * surface.
 */
import { computed } from "vue";
import { BRAND_STAR_PATH } from "@/lib/brand";

/**
 * MIN_SIZE is a hard floor, not a default.
 *
 * Below 20px the spark has to render inside a disc of roughly seven pixels and
 * turns to mush, and the halo stops reading as a glow and starts reading as a
 * blurred edge. Rather than ship a degraded mark at small sizes (which would
 * mean maintaining a second, stripped-down spinner and losing the single-mark
 * property), the product simply never draws one smaller. This costs nothing in
 * practice: 20px is already the most common loading size in the app.
 */
const MIN_SIZE = 20;

/** Below this, fine detail is eased back so it does not read as noise. */
const LG_SIZE = 36;

const props = defineProps({
	size: { type: Number, default: MIN_SIZE },
	/** Optional caption rendered beneath the spinner and announced politely. */
	label: { type: String, default: "" },
});

const resolvedSize = computed(() => {
	if (props.size < MIN_SIZE) {
		if (import.meta.env.DEV) {
			console.warn(
				`[JvSpinner] size ${props.size} is below the ${MIN_SIZE}px floor and was clamped. ` +
					`The brand spark is not legible below ${MIN_SIZE}px. Resize the container rather ` +
					`than the spinner.`
			);
		}
		return MIN_SIZE;
	}
	return props.size;
});

/**
 * Tier is derived, never passed in. A caller that could choose its own tier
 * could pick one that does not match its size, which is the only way this
 * component can render wrong.
 */
const tier = computed(() => (resolvedSize.value < LG_SIZE ? "jv-spin--md" : "jv-spin--lg"));

const sizeStyle = computed(() => ({ "--jv-spin-size": `${resolvedSize.value}px` }));
</script>

<style scoped>
.jv-spin-stack {
	display: inline-flex;
	flex-direction: column;
	align-items: center;
	gap: 12px;
}
.jv-spin-label {
	font-size: 13.5px;
	color: var(--text-2, #56555f);
	text-align: center;
}

.jv-spin {
	position: relative;
	display: inline-block;
	flex: none;
	vertical-align: middle;
	width: var(--jv-spin-size);
	height: var(--jv-spin-size);
}

/* The halo is the "breathing" of the onboarding core. It sits slightly outside
   the footprint, so the element is allowed to overflow its own box. */
.jv-spin-halo {
	position: absolute;
	inset: -6%;
	border-radius: 50%;
	background: radial-gradient(circle, rgba(139, 92, 246, 0.3) 0%, transparent 68%);
	animation: jv-spin-breathe 2.2s ease-in-out infinite;
}

.jv-spin-core {
	position: absolute;
	border-radius: 50%;
	display: grid;
	place-items: center;
	/* Matches JarvisMark: --brand-grad is theme-invariant and defined on :root,
	   with a literal fallback so the spinner is still correct if it is ever
	   rendered outside the app stylesheet (an isolated test harness). */
	background: var(--brand-grad, linear-gradient(135deg, #6e8bff, #8b5cf6));
	box-shadow: 0 2px 7px rgba(139, 92, 246, 0.4);
}
.jv-spin-core svg {
	width: 62%;
	height: 62%;
}

.jv-spin-dots {
	position: absolute;
	inset: 0;
	animation: jv-spin-rotate 1.5s linear infinite;
}
.jv-spin-dots i {
	position: absolute;
	top: 50%;
	left: 50%;
	border-radius: 50%;
	animation: jv-spin-twinkle 1.5s ease-in-out infinite;
}
/* Staggered so the three dots do not pulse in unison, which reads as a
   flashing ring rather than as motion. */
.jv-spin-dots i:nth-child(2) {
	animation-delay: -0.5s;
}
.jv-spin-dots i:nth-child(3) {
	animation-delay: -1s;
}

/* --- md tier (20 to 35px): halo eased back, tighter glow falloff so the dots
       stay as dots instead of blooming into each other at small sizes. --- */
.jv-spin--md .jv-spin-halo {
	opacity: 0.55;
}
.jv-spin--md .jv-spin-core {
	inset: 25%;
}
.jv-spin--md .jv-spin-dots i {
	width: calc(var(--jv-spin-size) * 0.16);
	height: calc(var(--jv-spin-size) * 0.16);
	margin: calc(var(--jv-spin-size) * -0.08);
	background: radial-gradient(
		circle,
		#fff 0%,
		var(--brand-1, #6e8bff) 55%,
		rgba(110, 139, 255, 0) 88%
	);
}
.jv-spin--md .jv-spin-dots i:nth-child(1) {
	transform: rotate(0deg) translateY(calc(var(--jv-spin-size) * -0.45));
}
.jv-spin--md .jv-spin-dots i:nth-child(2) {
	transform: rotate(120deg) translateY(calc(var(--jv-spin-size) * -0.45));
}
.jv-spin--md .jv-spin-dots i:nth-child(3) {
	transform: rotate(240deg) translateY(calc(var(--jv-spin-size) * -0.45));
}

/* --- lg tier (36px+): full detail, exactly the onboarding core. --- */
.jv-spin--lg .jv-spin-core {
	inset: 26%;
}
.jv-spin--lg .jv-spin-dots i {
	width: calc(var(--jv-spin-size) * 0.15);
	height: calc(var(--jv-spin-size) * 0.15);
	margin: calc(var(--jv-spin-size) * -0.075);
	background: radial-gradient(
		circle,
		#fff 0%,
		var(--brand-1, #6e8bff) 42%,
		rgba(110, 139, 255, 0) 74%
	);
}
.jv-spin--lg .jv-spin-dots i:nth-child(1) {
	transform: rotate(0deg) translateY(calc(var(--jv-spin-size) * -0.47));
}
.jv-spin--lg .jv-spin-dots i:nth-child(2) {
	transform: rotate(120deg) translateY(calc(var(--jv-spin-size) * -0.47));
}
.jv-spin--lg .jv-spin-dots i:nth-child(3) {
	transform: rotate(240deg) translateY(calc(var(--jv-spin-size) * -0.47));
}

@keyframes jv-spin-rotate {
	to {
		transform: rotate(360deg);
	}
}
@keyframes jv-spin-breathe {
	0%,
	100% {
		transform: scale(0.82);
		opacity: 0.5;
	}
	50% {
		transform: scale(1.12);
		opacity: 1;
	}
}
@keyframes jv-spin-twinkle {
	0%,
	100% {
		opacity: 0.5;
	}
	50% {
		opacity: 1;
	}
}

/* One calm static frame, matching how SetupNeuralNet.vue handles the same
   preference. The mark still reads as "working"; it just stops moving. */
@media (prefers-reduced-motion: reduce) {
	.jv-spin-halo,
	.jv-spin-dots,
	.jv-spin-dots i {
		animation: none;
	}
	.jv-spin-halo {
		opacity: 0.8;
	}
	.jv-spin-dots i {
		opacity: 1;
	}
}
</style>
