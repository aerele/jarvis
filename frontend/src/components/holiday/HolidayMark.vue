<template>
	<JarvisMark v-if="!wearing" v-bind="markProps" />
	<component
		:is="clickable ? 'button' : 'span'"
		v-else
		class="jvh"
		:class="{ 'jvh-button': clickable }"
		:type="clickable ? 'button' : undefined"
		:aria-label="clickable ? 'Show the greeting again' : undefined"
		:style="wrapperStyle"
		:data-design="art.design"
		:data-look="lookOn ? '' : undefined"
		@mouseenter="hovered = true"
		@mouseleave="hovered = false"
		@click="clickable && replay()"
	>
		<JarvisMark v-bind="markProps" @face="faceActive = $event" />
		<Transition name="jvh">
			<!-- v-html is safe here: the markup is built by worn.mjs from constants and the
			     design key the answer reader has already checked against its own list. -->
			<svg
				v-if="layerOn"
				class="jvh-layer"
				:class="{ 'jvh-pop': popOn }"
				data-testid="holiday-worn"
				:data-design="art.design"
				:data-state="layerState"
				viewBox="-16 -16 88 88"
				:style="layerStyle"
				aria-hidden="true"
				focusable="false"
				v-html="markup"
			></svg>
		</Transition>
	</component>
</template>

<script setup>
// JarvisMark that wears today's holiday item while its face shows. With nothing to
// wear (an ordinary day, a brand logo or custom agent name, an upgrade hold,
// worn:false) this renders JarvisMark alone, exactly as before, and class/style
// fall through to it. Wearing, the root is a wrapper that takes them instead, and
// the item is drawn on a layer OUTSIDE the mark, which clips.
// (This note is here, not in the template: a root-level HTML comment would make
// the root a fragment in dev builds and stop attributes falling through.)
//
// The big welcome mark (`welcome`) smiles with its item for about 2.5 s when it first
// appears on a lead-up day, the day itself or a birthday. On those days it is also a
// button that plays the smile again; on an ordinary day, in white label and in the
// upgrade hold it is not a button at all.
import { computed, getCurrentInstance, nextTick, onBeforeUnmount, ref, watch } from "vue";
import JarvisMark from "@/components/JarvisMark.vue";
import { brand } from "@/branding";
import { holidayTouch } from "@/holidayTouch";
import { loadWorn, loadedWorn } from "@/lib/holidayWorn";
import { DESIGNS } from "@jsshared/holiday/answer.mjs";

// The same props as JarvisMark (the call sites swap one tag for the other). Anything
// else, class and style included, falls through to the root.
const props = defineProps({
	size: { type: Number, default: 56 },
	radius: { type: Number, default: 14 },
	mood: { type: String, default: "star" },
	hoverPeek: { type: Boolean, default: false },
	peek: { type: Boolean, default: false },
	idlePeek: { type: Boolean, default: false },
	/** The big welcome mark: smile once per mount while there is something to say. */
	welcome: { type: Boolean, default: false },
	/**
	 * The welcome stage drives the mark instead of the mark's own timers: {face, item,
	 * happy, look}. The face shows while `face` is set, the item while `item` is set,
	 * `happy` is the smile and `look` is [dx, dy] in the 56-unit tile space. Null (the
	 * default) is the mark's own behaviour.
	 */
	stage: { type: Object, default: null },
	/**
	 * The pointer is over the mark but something laid on top of it (the stage's replay
	 * button) receives the mouse events, so it reports the hover instead.
	 */
	hover: { type: Boolean, default: false },
});

const WELCOME_SMILE_MS = 2500;
const SMILE_KINDS = ["leadup", "day", "birthday"];
// SVG ids must be unique per mark on the page (gradients, clips), so key them on the
// component instance.
const idPrefix = `jvh${getCurrentInstance().uid}`;

holidayTouch.start();

// What today asks the mark to wear, or null.
const art = computed(() => {
	const answer = holidayTouch.answer.value;
	return answer.kind !== "none" && answer.worn && DESIGNS.includes(answer.design)
		? answer
		: null;
});
// Never on a branded mark (a custom logo has no face, and a custom agent name means the
// product is not presented as Jarvis) and never in the upgrade hold, whose sleepy face
// is telling the user something.
const wearing = computed(() => !!art.value && !brand.isWhitelabeled && props.mood !== "upgrading");

// ---- the lazy art. The module is kept for the life of the page, so a mark mounted
// later (every new reply) draws the item at once instead of popping it in again. A
// failed load leaves the mark plain.
const worn = ref(loadedWorn());
function ensureWorn() {
	return worn.value ? Promise.resolve(worn.value) : loadWorn().then((mod) => (worn.value = mod));
}
watch(
	wearing,
	(yes) => {
		if (yes) ensureWorn();
	},
	{ immediate: true }
);

// ---- the welcome smile: about 2.5 s with the item, once per mount, and again on a click.
// The 2.5 s starts when the item first shows (or when the art turns out not to load), so
// a slow chunk does not eat the smile. Reduced motion holds the smiling frame, since
// JarvisMark already stops the face moving.
const smiling = ref(false);
const canSmile = computed(
	() => wearing.value && props.welcome && SMILE_KINDS.includes(art.value?.kind)
);
// On the stage the replay button over the mark does the clicking, not the mark.
const clickable = computed(() => canSmile.value && !props.stage);
let smiled = false;
let smileTimer = null;
let smileRun = 0;
let gone = false;
async function smile() {
	const run = ++smileRun;
	clearTimeout(smileTimer);
	smiling.value = true;
	await ensureWorn();
	await nextTick(); // the item is drawn now
	if (gone || run !== smileRun) return;
	smileTimer = setTimeout(() => (smiling.value = false), WELCOME_SMILE_MS);
}
async function replay() {
	clearTimeout(smileTimer);
	smiling.value = false; // off for a tick, so the face and the item start over
	await nextTick();
	if (!gone) smile();
}
watch(
	canSmile,
	(ok) => {
		if (!ok || smiled) return;
		smiled = true;
		smile();
	},
	{ immediate: true }
);

const effectiveMood = computed(() => {
	if (props.stage) return props.stage.happy && props.mood === "star" ? "happy" : props.mood;
	return smiling.value && props.mood === "star" ? "happy" : props.mood;
});
const markProps = computed(() => ({
	size: props.size,
	radius: props.radius,
	mood: effectiveMood.value,
	hoverPeek: props.hoverPeek,
	peek: props.peek || !!(props.stage && props.stage.face),
	idlePeek: props.idlePeek,
}));

// ---- the face: JarvisMark says when a mood, peek or idle blink shows it; a hover on
// a hoverPeek mark is pure CSS there, so it is tracked here.
const faceActive = ref(false);
const hovered = ref(false);
const hoverOn = computed(() => hovered.value || props.hover);
const faceShown = computed(() => faceActive.value || (props.hoverPeek && hoverOn.value));
// Once the mark stops wearing (the answer ended, a logo was set) the wrapper and the
// JarvisMark under it are replaced: what they knew about hover and face is stale.
watch(wearing, (yes) => {
	if (!yes) {
		hovered.value = false;
		faceActive.value = false;
	}
});

const layerOn = computed(
	() => wearing.value && faceShown.value && !!worn.value && (!props.stage || props.stage.item)
);
const layerState = computed(() =>
	effectiveMood.value === "happy"
		? "happy"
		: effectiveMood.value === "thinking"
		? "thinking"
		: "peek"
);
// The item pops on for a hover, a smile or a reply in progress. The mark's own idle
// blink, which happens unprompted every few seconds, only fades it in.
const popOn = computed(
	() => hoverOn.value || props.peek || !!props.stage || effectiveMood.value !== "star"
);

// The stage points the eyes at something (the sky, the flame): a fixed offset that
// replaces the mark's own glancing until the smile or the next beat.
const lookOn = computed(() => !!(props.stage && props.stage.look) && !props.stage.happy);
const wrapperStyle = computed(() => {
	const style = clickable.value ? { borderRadius: `${props.radius}px` } : {};
	if (lookOn.value) {
		const unit = props.size / 56;
		style["--jvh-lx"] = `${props.stage.look[0] * unit}px`;
		style["--jvh-ly"] = `${props.stage.look[1] * unit}px`;
	}
	return Object.keys(style).length ? style : undefined;
});

// ---- reduced motion, read live: the OS setting can change while the page is open.
const reducedMotion = ref(false);
let motionQuery = null;
function onMotionChange(event) {
	reducedMotion.value = event.matches;
}
function stopWatchingMotion() {
	motionQuery?.removeEventListener("change", onMotionChange);
	motionQuery = null;
}
watch(
	wearing,
	(yes) => {
		if (!yes) return stopWatchingMotion();
		if (motionQuery || typeof window.matchMedia !== "function") return;
		motionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
		reducedMotion.value = motionQuery.matches;
		motionQuery.addEventListener("change", onMotionChange);
	},
	{ immediate: true }
);
onBeforeUnmount(() => {
	gone = true;
	clearTimeout(smileTimer);
	stopWatchingMotion();
});

const markup = computed(() =>
	layerOn.value
		? worn.value.wornMarkup(art.value.design, {
				idPrefix,
				radius: (props.radius / props.size) * 56,
				small: props.size <= 32,
				motion: !reducedMotion.value,
		  })
		: ""
);
// The art is drawn in the 56-unit tile space with 16 units of room on every side.
const layerStyle = computed(() => {
	const unit = props.size / 56;
	return {
		left: `${-16 * unit}px`,
		top: `${-16 * unit}px`,
		width: `${88 * unit}px`,
		height: `${88 * unit}px`,
	};
});
</script>

<style scoped>
.jvh {
	position: relative;
	display: inline-grid;
	flex: none;
	line-height: 0;
}
/* The welcome mark on a holiday is a button: nothing of the browser's button look. */
.jvh-button {
	appearance: none;
	background: none;
	border: 0;
	padding: 0;
	margin: 0;
	font: inherit;
	color: inherit;
	cursor: pointer;
}
.jvh-button:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 3px;
}
/* The stage's "look": the face holds a fixed offset instead of its own glancing loop. */
.jvh[data-look] :deep(.jv-face) {
	animation: none !important;
	transform: translate(var(--jvh-lx), var(--jvh-ly));
	transition: opacity 0.25s ease, transform 0.35s ease-in-out;
}
.jvh-layer {
	position: absolute;
	overflow: visible;
	pointer-events: none;
}
/* The item fades on as the face appears and fades off with it. For a hover, a smile
   or a reply in progress it also pops. Reduced motion keeps the fade and drops the pop. */
.jvh-enter-active {
	animation: jvh-fade 0.18s ease-out both;
}
.jvh-enter-active.jvh-pop :deep(.acc) {
	animation: jvh-pop 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) both;
}
.jvh-leave-active {
	transition: opacity 0.2s ease;
}
.jvh-leave-to {
	opacity: 0;
}
@keyframes jvh-fade {
	from {
		opacity: 0;
	}
}
@keyframes jvh-pop {
	from {
		opacity: 0;
		transform: translateY(-5px) scale(0.86);
	}
}
@media (prefers-reduced-motion: reduce) {
	.jvh-enter-active.jvh-pop :deep(.acc) {
		animation: none;
	}
}
</style>
