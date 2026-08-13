<template>
	<!-- The brand mark. Single source of truth for the brand glyph so the
	     onboarding wizard, the onboarding gate poster, the chat avatars (and any
	     future setup surface) can't drift apart on a brand refresh. When the
	     tenant has uploaded a whitelabel logo we render it in place of the
	     default gradient spark.

	     `mood` turns the mark into a tiny reacting face for the moments that have
	     one: eyes that glance while a reply is being written (thinking), widen
	     while the mic is capturing (listening), and a one-off smile when the
	     answer lands (happy). Default "star" renders exactly the resting spark, so
	     the existing call sites need no change. The face is white on the same
	     brand gradient as the spark - it never introduces a colour of its own, and
	     the whitelabel <img> branch ignores mood entirely. -->
	<img
		v-if="brandLogoUrl"
		class="jv-mark jv-mark-img"
		:src="brandLogoUrl"
		:style="markStyle"
		alt=""
	/>
	<span
		v-else
		class="jv-mark"
		:class="[`jv-mood-${mood}`, { 'jv-hoverpeek': hoverPeek, 'jv-peek-on': peek }]"
		:style="markStyle"
	>
		<svg
			class="jv-star"
			:width="Math.round(size * 0.55)"
			:height="Math.round(size * 0.55)"
			viewBox="0 0 24 24"
			fill="#fff"
		>
			<path :d="BRAND_STAR_PATH" />
		</svg>
		<span v-if="hasFace" class="jv-face" aria-hidden="true">
			<span class="jv-eyes"><i class="jv-eye"></i><i class="jv-eye"></i></span>
			<span class="jv-mouth"></span>
		</span>
	</span>
</template>

<script setup>
import { computed } from "vue";
import { brandLogoUrl } from "@/branding";
import { BRAND_STAR_PATH } from "@/lib/brand";

const props = defineProps({
	size: { type: Number, default: 56 },
	radius: { type: Number, default: 14 },
	/**
	 * "star"      resting spark (default, byte-identical to the old mark)
	 * "thinking"  eyes glance around  - while a reply is being written
	 * "listening" eyes widen + pulse  - while voice/mic is capturing
	 * "happy"     smile, plays once   - the moment an answer lands
	 */
	mood: {
		type: String,
		default: "star",
		validator: (v) => ["star", "thinking", "listening", "happy"].includes(v),
	},
	/** Sidebar/brand use: reveal a friendly blink on hover, calm otherwise. */
	hoverPeek: { type: Boolean, default: false },
	/** Externally-driven peek: show the blinking eyes while true. Lets a larger
	    surface (e.g. the whole sidebar brand card) drive the same reveal that
	    hoverPeek gives on the mark alone. */
	peek: { type: Boolean, default: false },
});

// The face element only exists when something can show it: an active mood, or a
// hover-peek surface (where mood stays "star" but hover reveals the eyes).
const hasFace = computed(() => props.mood !== "star" || props.hoverPeek || props.peek);

const markStyle = computed(() => ({
	width: `${props.size}px`,
	height: `${props.size}px`,
	borderRadius: `${props.radius}px`,
	"--jv-mark-size": `${props.size}px`,
}));
</script>

<style scoped>
.jv-mark {
	position: relative;
	display: grid;
	place-items: center;
	flex-shrink: 0;
	overflow: hidden;
	/* --brand-grad is defined on :root in main.css (theme-invariant). The literal
	   fallback keeps the mark correct if this component is ever rendered outside
	   the app's stylesheet (e.g. an isolated story or a test harness). */
	background: var(--brand-grad, linear-gradient(135deg, #6e8bff, #8b5cf6));
}
/* A tenant logo fills the same square footprint; cover keeps it edge-to-edge
   at any aspect ratio without distortion. */
.jv-mark-img {
	object-fit: cover;
	display: block;
	flex-shrink: 0;
	background: var(--surface-2, #f0f0f4);
}

/* Star and face share one centred cell; we cross-fade between them. */
.jv-star {
	grid-area: 1 / 1;
	transition: opacity 0.25s ease;
}
.jv-face {
	grid-area: 1 / 1;
	position: absolute;
	inset: 0;
	display: flex;
	flex-direction: column;
	align-items: center;
	justify-content: center;
	gap: calc(var(--jv-mark-size) * 0.07);
	opacity: 0;
	transition: opacity 0.25s ease;
	transform-origin: center;
}
.jv-eyes {
	display: flex;
	align-items: center;
	justify-content: center;
	gap: calc(var(--jv-mark-size) * 0.19);
}
.jv-eye {
	width: calc(var(--jv-mark-size) * 0.135);
	height: calc(var(--jv-mark-size) * 0.2);
	background: #fff;
	border-radius: 999px;
	transform-origin: center;
}
.jv-mouth {
	display: none;
}

/* Any active mood hides the star and shows the face. Written as
   :not(.jv-mood-star) so a mood added later toggles correctly on its own,
   without having to be appended to a per-mood list here. */
.jv-mark:not(.jv-mood-star) .jv-star {
	opacity: 0;
}
.jv-mark:not(.jv-mood-star) .jv-face {
	opacity: 1;
}

/* THINKING - eyes drift up and glance side to side, with a soft blink. */
.jv-mood-thinking .jv-face {
	animation: jvm-lookaround 3.4s ease-in-out infinite;
}
.jv-mood-thinking .jv-eye {
	animation: jvm-blink 3.4s infinite;
}
@keyframes jvm-lookaround {
	0%,
	100% {
		transform: translate(-16%, -8%);
	}
	28% {
		transform: translate(-16%, -8%);
	}
	52% {
		transform: translate(18%, -4%);
	}
	70% {
		transform: translate(0, -12%) scale(0.96, 1.04);
	}
}
@keyframes jvm-blink {
	0%,
	46%,
	100% {
		transform: scaleY(1);
	}
	49%,
	52% {
		transform: scaleY(0.14);
	}
}

/* LISTENING - eyes widen and pulse, attentive. */
.jv-mood-listening .jv-face {
	animation: jvm-attend 1.9s ease-in-out infinite;
}
.jv-mood-listening .jv-eye {
	animation: jvm-tall 1.9s ease-in-out infinite;
}
@keyframes jvm-attend {
	0%,
	100% {
		transform: scale(1);
	}
	50% {
		transform: scale(1.1);
	}
}
@keyframes jvm-tall {
	0%,
	100% {
		height: calc(var(--jv-mark-size) * 0.2);
	}
	50% {
		height: calc(var(--jv-mark-size) * 0.26);
	}
}

/* HAPPY - smaller eyes + a smile, one bounce, then it holds. Plays once so a
   lingering mood prop can't leave it bouncing forever. */
.jv-mood-happy .jv-face {
	gap: calc(var(--jv-mark-size) * 0.05);
	/* Plays once, ~1.5s. ChatView's markAnswerLanded (HAPPY_HOLD_MS) clears the
	   mood after the same 1500ms, so the smile reverts to the resting star exactly
	   as it finishes; keep the two durations in sync. */
	animation: jvm-cheer 1.5s ease-in-out 1 both;
}
.jv-mood-happy .jv-eye {
	width: calc(var(--jv-mark-size) * 0.1);
	height: calc(var(--jv-mark-size) * 0.14);
}
.jv-mood-happy .jv-mouth {
	display: block;
	width: calc(var(--jv-mark-size) * 0.42);
	height: calc(var(--jv-mark-size) * 0.34);
	border: calc(var(--jv-mark-size) * 0.07) solid #fff;
	border-color: transparent transparent #fff transparent;
	border-radius: 50%;
	margin-top: calc(var(--jv-mark-size) * -0.1);
}
@keyframes jvm-cheer {
	0%,
	100% {
		transform: translateY(3%) scale(1);
	}
	50% {
		transform: translateY(-12%) scale(1.05);
	}
}

/* PEEK - resting star until triggered, then a friendly blink. Triggered by
   hovering the mark itself (hoverPeek) or by the `peek` prop, so a larger
   surface (e.g. the whole sidebar brand card) can drive the same reveal. Only
   meaningful while mood is "star" (the face is otherwise hidden). */
.jv-hoverpeek:hover .jv-star,
.jv-peek-on .jv-star {
	opacity: 0;
}
.jv-hoverpeek:hover .jv-face,
.jv-peek-on .jv-face {
	opacity: 1;
}
.jv-hoverpeek:hover .jv-eye,
.jv-peek-on .jv-eye {
	animation: jvm-blink 1.8s ease-in-out infinite;
}

/* One calm static frame per mood, matching how JvSpinner/SetupNeuralNet handle
   the same preference: the face still communicates, it just stops moving. */
@media (prefers-reduced-motion: reduce) {
	.jv-face,
	.jv-eye,
	.jv-hoverpeek:hover .jv-eye,
	.jv-peek-on .jv-eye {
		animation: none !important;
	}
	.jv-mood-thinking .jv-eye {
		transform: translateY(-8%);
	}
}
</style>
