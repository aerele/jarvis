<template>
	<BrandMark v-if="!wearing" v-bind="markProps" />
	<component
		:is="clickable ? 'button' : 'span'"
		v-else
		class="jvh"
		:class="{ 'jvh-button': clickable }"
		:type="clickable ? 'button' : undefined"
		:aria-label="clickable ? tapLabel : undefined"
		:style="wrapperStyle"
		:data-design="art.design"
		:data-look="lookOn ? '' : undefined"
		@click="clickable && onTap()"
	>
		<BrandMark v-bind="markProps" @face="faceActive = $event" />
		<Transition name="jvh">
			<!-- v-html is safe here: the markup is built by worn.mjs from constants and the
			     design key the answer reader has already checked against its own list. -->
			<svg
				v-if="layerOn"
				class="jvh-layer"
				data-testid="holiday-worn"
				:data-design="art.design"
				data-state="happy"
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
// Keep in step with frontend/src/components/holiday/HolidayMark.vue (the desktop original).
//
// BrandMark that wears today's holiday item while its face shows. With nothing to wear
// (an ordinary day, a logo or custom agent name, the upgrade hold, worn:false) this is
// BrandMark alone, exactly as before. Wearing, the root is a wrapper and the item is drawn
// on a layer OUTSIDE the mark, which clips.
// (This note is here, not in the template: a root-level HTML comment would make the root
// a fragment in dev builds.)
//
// The welcome mark smiles with its item for about 2.5 s when it first appears on a lead-up
// day, the day itself or a birthday, and is then a button that plays the smile again. On
// an ordinary day, in white label and in the upgrade hold it is not a button at all.
// A phone has no hover, and the PWA has no idle blink: the face shows for the smile only, so
// the item is always in its "happy" state (the desktop's peek and thinking states are not used).
import { computed, getCurrentInstance, nextTick, onBeforeUnmount, ref, watch } from "vue";
import BrandMark from "./BrandMark.vue";
import { wornAnswer } from "../holidayTouch";
import { loadWorn, loadedWorn } from "../lib/holidayWorn";
import { effectiveMood as moodFor, stageMood } from "../lib/holidayFace";

const props = defineProps({
	size: { type: Number, default: 56 },
	radius: { type: Number, default: 14 },
	mood: { type: String, default: "star" },
	/**
	 * The welcome scene drives the mark instead of its own smile: {face, item, happy, look}.
	 * The face shows while `face` is set, the item while `item` is set, `happy` is the smile
	 * and `look` is [dx, dy] in the 56-unit tile space. A tap is then the stage's to answer
	 * (the "tap" event). Null (the default) is the mark's own behaviour.
	 */
	stage: { type: Object, default: null },
});
const emit = defineEmits(["tap"]);

const WELCOME_SMILE_MS = 2500;
const SMILE_KINDS = ["leadup", "day", "birthday"];
// SVG ids must be unique per mark on the page (gradients, clips): key them on the instance.
const idPrefix = `jvh${getCurrentInstance().uid}`;

// What today asks the mark to wear, or null (see wornAnswer). Never in the upgrade hold
// either, whose sleepy face is telling the user something.
const art = wornAnswer;
const wearing = computed(() => !!art.value && props.mood !== "upgrading");

// ---- the lazy art. Kept for the life of the page, so a mark mounted later draws the item
// at once. A failed load leaves the mark plain.
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

// ---- the welcome smile: about 2.5 s with the item, once per mount, and again on a tap.
// The 2.5 s starts when the item first shows, or after ART_WAIT_MS if the art has not
// arrived (a failed or stalled import), so a slow chunk does not eat the smile and a stuck
// one cannot hold it for ever. Reduced motion holds the smiling frame.
const ART_WAIT_MS = 3000;
const smiling = ref(false);
const clickable = computed(() => wearing.value && SMILE_KINDS.includes(art.value?.kind));
let smiled = false;
let smileTimer = null;
const artTimers = new Set(); // one per wait: a replay must not cancel an earlier wait's timer
let smileRun = 0;
let gone = false;
function artOrGiveUp() {
	let timer;
	const giveUp = new Promise((resolve) => {
		timer = setTimeout(resolve, ART_WAIT_MS);
		artTimers.add(timer);
	});
	return Promise.race([ensureWorn(), giveUp]).finally(() => {
		clearTimeout(timer);
		artTimers.delete(timer);
	});
}
async function smile() {
	const run = ++smileRun;
	clearTimeout(smileTimer);
	smiling.value = true;
	await artOrGiveUp();
	await nextTick(); // the item is drawn now
	if (gone || run !== smileRun) return;
	smileTimer = setTimeout(() => (smiling.value = false), WELCOME_SMILE_MS);
}
function onTap() {
	if (props.stage) emit("tap");
	else replay();
}
async function replay() {
	clearTimeout(smileTimer);
	smiling.value = false; // off for a tick, so the face and the item start over
	await nextTick();
	if (!gone) smile();
}
watch(
	clickable,
	(ok) => {
		if (!ok || smiled || props.stage) return;
		smiled = true;
		smile();
	},
	{ immediate: true }
);

// What a tap does: the stage replays the scene, otherwise the mark smiles again.
const tapLabel = computed(() =>
	props.stage ? "Replay the holiday scene" : "Show the greeting again"
);

const effectiveMood = computed(() =>
	props.stage ? stageMood(props.mood, props.stage) : moodFor(props.mood, smiling.value)
);
const markProps = computed(() => ({
	size: props.size,
	radius: props.radius,
	mood: effectiveMood.value,
}));

// BrandMark says when its face shows. Once the mark stops wearing (the answer ended) the
// wrapper is replaced, so what it knew is stale.
const faceActive = ref(false);
watch(wearing, (yes) => {
	if (!yes) faceActive.value = false;
});
const layerOn = computed(
	() => wearing.value && faceActive.value && !!worn.value && (!props.stage || props.stage.item)
);

// The stage points the eyes at something (the sky, the flame) until the smile.
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
	artTimers.forEach(clearTimeout);
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
	-webkit-tap-highlight-color: transparent;
}
.jvh-button:focus-visible {
	outline: 2px solid var(--accent);
	outline-offset: 3px;
}
/* The stage's "look": the eyes hold a fixed offset. */
.jvh[data-look] :deep(.bm-eyes) {
	transform: translate(var(--jvh-lx), var(--jvh-ly));
	transition: transform 0.35s ease-in-out;
}
.jvh-layer {
	position: absolute;
	overflow: visible;
	pointer-events: none;
}
/* The item fades on as the face appears and pops with the smile; it fades off with the
   face. Reduced motion keeps the fade and drops the pop. */
.jvh-enter-active {
	animation: jvh-fade 0.18s ease-out both;
}
.jvh-enter-active :deep(.acc) {
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
	.jvh-enter-active :deep(.acc) {
		animation: none;
	}
}
</style>
