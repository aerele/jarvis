<script setup>
import { computed, watch } from "vue";
import { brandLogoUrl } from "@/branding";
import { MOODS, faceShown } from "../lib/holidayFace";

// The Jarvis logo tile — identical to the native app's mark
// (jarvis_mobile/src/components/BrandMark.tsx): a violet rounded square with a
// white four-point "spark" star. This replaces the placeholder "J" the tile
// used to show in the new-chat hero, login, install banner and empty states.
// The star path is the web logo, verbatim from the native component (viewBox
// 0 0 24 24); size/radius follow the same ratios (radius 25%, star 57%).
// When the tenant has uploaded a whitelabel logo we render it in place.
//
// The face is the mobile twin of the desktop JarvisMark's: white pills on the same
// gradient, never a colour of its own. `mood="upgrading"` (Stream E) shows sleepy
// lids + a soft "z z z" while this tenant's agent is being upgraded; `mood="happy"`
// is a smile, played once (the holiday welcome). There is no `peek` (the desktop mark's
// hover/idle glance): a phone has no hover and nothing here needs it. The face stays
// rendered and cross-fades with the star, so the tile is never blank mid-change. The
// whitelabel <img> branch ignores all of it (a tenant logo stays put; the top-of-app hold
// strip carries the message).
const props = defineProps({
	size: { type: Number, default: 40 },
	// Corner radius in px; the default keeps the 25% tile the app always had.
	radius: { type: Number, default: null },
	mood: {
		type: String,
		default: "star",
		validator: (v) => MOODS.includes(v),
	},
});

// Tells a wrapper (HolidayMark) when the face is actually showing. Nothing else listens.
const emit = defineEmits(["face"]);

const showFace = computed(() => faceShown(props.mood));
watch(showFace, (shown) => emit("face", shown), { immediate: true });

const style = computed(() => ({
	width: `${props.size}px`,
	height: `${props.size}px`,
	borderRadius: `${props.radius ?? Math.round(props.size * 0.25)}px`,
	"--sz": `${props.size}px`,
}));
</script>

<template>
	<img
		v-if="brandLogoUrl"
		:src="brandLogoUrl"
		class="jv-mark jv-mark-img"
		:style="style"
		alt=""
	/>
	<span
		v-else
		class="jv-mark"
		:class="[`jv-mood-${mood}`, { 'jv-mark-face': showFace }]"
		:style="style"
	>
		<!-- The face fades in over the masked star (the global .jv-mark::before), which fades
		     out; both stay rendered so the cross-fade has something to fade. -->
		<span class="bm-face" aria-hidden="true">
			<span class="bm-eyes"><i class="bm-eye"></i><i class="bm-eye"></i></span>
			<span class="bm-mouth"></span>
		</span>
		<span v-if="mood === 'upgrading'" class="bm-zzz" aria-hidden="true"
			><i>z</i><i>z</i><i>z</i></span
		>
		<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
			<path d="M12 2.5 14 10 21.5 12 14 14 12 21.5 10 14 2.5 12 10 10Z" />
		</svg>
	</span>
</template>

<style scoped>
.jv-mark-img {
	object-fit: cover;
	display: block;
	flex-shrink: 0;
}

/* A face fades the masked star (the global .jv-mark::before) out as it fades in. */
.jv-mark::before {
	transition: opacity 0.25s ease;
}
.jv-mark-face::before {
	opacity: 0;
}
/* White pills, the mobile twin of JarvisMark's face (same .135/.2 proportions).
   #fff explicitly — the tile sets color:transparent, which would otherwise blank a
   currentColor fill. */
.bm-face {
	position: absolute;
	inset: 0;
	display: flex;
	flex-direction: column;
	align-items: center;
	justify-content: center;
	gap: calc(var(--sz) * 0.07);
	opacity: 0;
	transition: opacity 0.25s ease;
}
.jv-mark-face .bm-face {
	opacity: 1;
}
.bm-eyes {
	display: flex;
	align-items: center;
	justify-content: center;
	gap: calc(var(--sz) * 0.19);
}
.bm-eye {
	width: calc(var(--sz) * 0.135);
	height: calc(var(--sz) * 0.2);
	background: #fff;
	border-radius: 999px;
	transform-origin: center;
}
.bm-mouth {
	display: none;
}

/* HAPPY: smaller eyes and a smile, one bounce, then it holds. Plays once so a
   lingering mood prop can't leave it bouncing for ever. */
.jv-mood-happy .bm-face {
	gap: calc(var(--sz) * 0.05);
	animation: bm-cheer 1.5s ease-in-out 1 both;
}
.jv-mood-happy .bm-eye {
	width: calc(var(--sz) * 0.1);
	height: calc(var(--sz) * 0.14);
}
.jv-mood-happy .bm-mouth {
	display: block;
	width: calc(var(--sz) * 0.42);
	height: calc(var(--sz) * 0.34);
	border: calc(var(--sz) * 0.07) solid #fff;
	border-color: transparent transparent #fff transparent;
	border-radius: 50%;
	margin-top: calc(var(--sz) * -0.1);
}
@keyframes bm-cheer {
	0%,
	100% {
		transform: translateY(3%) scale(1);
	}
	50% {
		transform: translateY(-12%) scale(1.05);
	}
}

/* UPGRADING: sleepy lids, the mobile twin of JarvisMark's "upgrading" mood (drooped
   to heavy lids that slow-blink, over a soft breath). */
.jv-mood-upgrading .bm-face {
	animation: bm-breathe 4.6s ease-in-out infinite;
}
.jv-mood-upgrading .bm-eye {
	height: calc(var(--sz) * 0.055);
	animation: bm-lid 4.6s ease-in-out infinite;
}
.jv-mood-upgrading .bm-eye:first-child {
	transform: rotate(-13deg);
}
.jv-mood-upgrading .bm-eye:last-child {
	transform: rotate(13deg);
}
@keyframes bm-breathe {
	0%,
	100% {
		transform: scale(0.99);
	}
	50% {
		transform: scale(1.02);
	}
}
@keyframes bm-lid {
	0%,
	100% {
		height: calc(var(--sz) * 0.055);
	}
	50% {
		height: calc(var(--sz) * 0.022);
	}
}
/* The sleep "z z z", white on the gradient, rising and fading in the corner.
   Bounded travel so it fades before the tile's clipped edge. */
.bm-zzz {
	position: absolute;
	top: 9%;
	right: 8%;
	display: flex;
	align-items: flex-end;
	gap: 1px;
	line-height: 1;
	pointer-events: none;
}
.bm-zzz i {
	font-style: normal;
	font-weight: 800;
	color: #fff;
	opacity: 0;
}
.bm-zzz i:nth-child(1) {
	font-size: calc(var(--sz) * 0.13);
	animation: bm-z 3.2s ease-out infinite;
}
.bm-zzz i:nth-child(2) {
	font-size: calc(var(--sz) * 0.17);
	animation: bm-z 3.2s ease-out 0.6s infinite;
}
.bm-zzz i:nth-child(3) {
	font-size: calc(var(--sz) * 0.22);
	animation: bm-z 3.2s ease-out 1.2s infinite;
}
@keyframes bm-z {
	0% {
		opacity: 0;
		transform: translateY(20%) scale(0.7);
	}
	30% {
		opacity: 0.85;
	}
	100% {
		opacity: 0;
		transform: translateY(-30%) scale(1.05);
	}
}
/* One calm static frame per mood: the face still communicates, it just stops moving.
   Happy holds its smile. */
@media (prefers-reduced-motion: reduce) {
	.bm-face,
	.bm-eye,
	.bm-zzz i {
		animation: none !important;
	}
	.jv-mood-upgrading .bm-eye {
		height: calc(var(--sz) * 0.055);
	}
	.bm-zzz i {
		opacity: 0.8;
	}
}
</style>
