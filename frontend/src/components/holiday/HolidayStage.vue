<template>
	<div
		ref="areaEl"
		class="jv-stage"
		:style="{ '--s': scale, '--rest-lift': `${lift}px` }"
		data-testid="holiday-stage"
		:data-phase="scene.phase.value"
		:data-design="answer.design"
	>
		<!-- The art: a sibling of the scroller, never inside it, so it cannot change
		     scrollHeight. It lets every pointer event through and is hidden from screen
		     readers. -->
		<div ref="hostEl" class="jv-stage-art" aria-hidden="true" data-testid="holiday-art"></div>
		<div ref="scrollEl" class="jv-welcome-scroll jv-stage-scroll">
			<div class="jv-stage-col">
				<div class="jv-stage-top">
					<div ref="heroEl" class="jv-stage-hero" :style="heroStyle">
						<div
							ref="markEl"
							class="jv-stage-mark"
							:style="{ width: `${markSize}px`, height: `${markSize}px` }"
						>
							<HolidayMark
								:size="markSize"
								:radius="Math.round(markSize / 4)"
								:mood="mood"
								:stage="scene.mark"
								:hover="markHover"
							/>
							<button
								v-if="!holdActive"
								type="button"
								class="jv-stage-replay"
								aria-label="Replay the greeting"
								data-testid="stage-replay"
								:style="{ borderRadius: `${Math.round(markSize / 4)}px` }"
								@click="scene.replay()"
								@mouseenter="markHover = true"
								@mouseleave="markHover = false"
							></button>
						</div>
						<!-- Both greetings sit in one grid cell, so the swap never changes the
						     height. The heading is named by its visible text alone. -->
						<h1
							ref="h1El"
							class="jv-stage-h1"
							:aria-labelledby="showDay ? dayId : normalId"
							data-testid="welcome-greeting"
						>
							<span
								:id="normalId"
								class="jv-stage-t"
								:class="swapClass(!showDay)"
								:aria-hidden="showDay"
								>{{ normalText }}</span
							>
							<span
								:id="dayId"
								class="jv-stage-t"
								:class="swapClass(showDay)"
								:aria-hidden="!showDay"
								>{{ dayText }}</span
							>
						</h1>
						<div class="jv-stage-sub">
							<div
								class="jv-stage-t"
								:class="swapClass(!showDay)"
								:aria-hidden="showDay"
							>
								<slot name="sub" />
							</div>
							<p
								class="jv-stage-subd jv-stage-t"
								:class="swapClass(showDay)"
								:aria-hidden="!showDay"
							>
								{{ DAY_SUB_LINE }}
							</p>
						</div>
					</div>
				</div>
				<div
					ref="bottomEl"
					class="jv-stage-bottom"
					:class="{ 'is-hidden': scene.cardsHidden.value }"
					:style="{ '--fade': `${scene.fadeMs.value}ms` }"
					data-testid="stage-bottom"
				>
					<slot name="bottom" />
				</div>
			</div>
		</div>
	</div>
</template>

<script setup>
// The scene-day welcome layout: a big Jarvis and the greeting, with the scene's art on a
// layer over it, above a bottom block (starter cards, Payment Calendar link, capability
// catalog). ChatView uses this only when lib/holidayStage.js says so; otherwise the normal
// WelcomeHero layout is untouched.
//
// Where the hero sits: at rest, at a 0.6 / 0.4 split of the space above the bottom block.
// While a scene may still play it is centred in the whole stage instead, never above the
// toran (lib/holidayScenes.js sceneClearance; the scene keeps its fireworks below the
// toran itself). When the scene ends it glides up to its rest place. The move is a
// transform on the hero, so layout and the scroll height never change; the worn item is
// inside the mark, so it moves with it.
//
// The worn item (a hat, an umbrella, a crescent) hangs above the mark's top. The whole of it
// plus 12 px stays below the top of the stage, so it never slides under the page's top bar:
// the centring clearance includes its reach, and so does the rest place, through a minimum
// space above the hero (--rest-lift). Its reach is the real box of the drawn layer when it
// is mounted, else the table in scene-geometry.mjs (WORN_OVERHANG).
//
// While a scene may still play the greeting is the person's normal one and the bottom
// block is hidden (visibility, so Tab skips it and nothing moves). The scene, its deadline
// and what ends it live in lib/useHolidayScene.js; this file is the DOM around it.
import {
	computed,
	getCurrentInstance,
	nextTick,
	onBeforeUnmount,
	onMounted,
	ref,
	watch,
} from "vue";
import { useRoute } from "vue-router";
import HolidayMark from "@/components/holiday/HolidayMark.vue";
import { holidayTouch } from "@/holidayTouch";
import { heroOffset, restLift, wornOverhangPx, wornTop } from "@/lib/holidayStage";
import { sceneClearance } from "@/lib/holidayScenes";
import { createHolidayScene } from "@/lib/useHolidayScene";
import { DAY_SUB_LINE, headingFor } from "@jsshared/holiday/copy.mjs";
import { heroFloorTop } from "@jsshared/holiday/scene-geometry.mjs";

const props = defineProps({
	/** The ordinary greeting ("Good morning"). */
	greeting: { type: String, required: true },
	firstName: { type: String, default: "" },
	/** "upgrading" during the maintenance hold, else "star". */
	mood: { type: String, default: "star" },
	holdActive: { type: Boolean, default: false },
	/** Nothing typed in the composer. */
	composerEmpty: { type: Boolean, default: true },
});

const uid = getCurrentInstance().uid;
const normalId = `jvs-t${uid}`;
const dayId = `jvs-d${uid}`;
const areaEl = ref(null);
const hostEl = ref(null);
const scrollEl = ref(null);
const heroEl = ref(null);
const markEl = ref(null);
const h1El = ref(null);
const bottomEl = ref(null);
// The replay button covers the mark, so it tells the mark when the pointer is over it.
const markHover = ref(false);

// ---- size: the mark is at most 96 px and the heading at most 40 px, at any width
const scale = ref(1);
const markSize = computed(() => Math.round(72 * scale.value));
function measureScale() {
	const width = areaEl.value ? areaEl.value.clientWidth : 0;
	if (width > 0) scale.value = Math.round(Math.min(width / 800, 1.25) * 1000) / 1000;
}

// ---- the worn item's reach above the mark, in tile units as measured on the drawn layer
// (0 until it is mounted). It only grows for the life of the page: a sway or a pop must not
// make the layout breathe.
const wornMeasured = ref(0);
const overhang = computed(() =>
	wornOverhangPx({
		design: holidayTouch.answer.value.design,
		markSize: markSize.value,
		measured: wornMeasured.value,
	})
);
// The scroller's own top padding (it is already part of the space above the hero).
const padTop = ref(12);
// The least space (px) above the hero in the layout, so the item clears the top at rest.
const lift = computed(() => restLift({ overhangPx: overhang.value, padTop: padTop.value }));
function measureWorn() {
	const acc = markEl.value && markEl.value.querySelector('[data-testid="holiday-worn"] .acc');
	if (!acc || typeof acc.getBBox !== "function") return;
	try {
		wornMeasured.value = Math.max(wornMeasured.value, -acc.getBBox().y);
	} catch {
		// an element not rendered yet has no box: the table stands
	}
}
// A bigger reach moves the rest place, so the centring is read again once it is on the page,
// and the scene reads the boxes again (the floor row is fitted to the hero's bottom).
watch(overhang, async () => {
	await nextTick();
	if (scene.centred.value) measureHero();
	relayoutNow();
});

// ---- where the hero sits: centred while a scene may play, at rest otherwise
const centredShift = ref(0);
const heroStyle = computed(() => {
	const centred = scene.centred.value;
	return {
		transform: `translateY(${centred ? centredShift.value : 0}px)`,
		// No glide while centred (a re-measure must not drift); the way up at the end.
		"--glide": `${centred ? 0 : scene.glideMs.value}ms`,
	};
});
// The layout sizes measureHero reads, as one string: what the centring was computed from.
// When it differs from the last measure, the centring is stale. (Layout sizes only:
// offsetTop and offsetHeight ignore the transform, so the shift itself never changes it.)
let measuredKey = "";
function layoutKey() {
	const [area, hero, scroll] = [areaEl.value, heroEl.value, scrollEl.value];
	if (!area || !hero || !scroll) return "";
	return [area.clientHeight, hero.offsetHeight, hero.offsetTop, scroll.scrollHeight].join(":");
}
// Measuring again while the hero is shifted gives the same answer, for the same reason.
function measureHero() {
	const area = areaEl.value;
	const hero = heroEl.value;
	if (!area || !hero || !scrollEl.value) return;
	measuredKey = layoutKey();
	centredShift.value = heroOffset({
		areaH: area.clientHeight,
		heroH: hero.offsetHeight,
		restTop: hero.offsetTop,
		minTop: sceneClearance(holidayTouch.answer.value.design, scale.value),
		wornTop: wornTop(overhang.value),
		maxBottom: scrollEl.value.scrollHeight,
		floorTop: floorTop(),
	});
}
// Where the scene's floor row reaches up to under the hero at its smallest (undefined for a
// design with nothing tall there): the hero is kept above it. The card band ends where the
// row's baseline is.
function floorTop() {
	const [area, bottom] = [areaEl.value, bottomEl.value];
	if (!area || !bottom) return undefined;
	const box = bottom.getBoundingClientRect();
	if (!box.height) return undefined; // not laid out yet: there is no ground to measure
	return heroFloorTop({
		design: holidayTouch.answer.value.design,
		s: scale.value,
		hostH: area.clientHeight,
		cardsBottomPx: box.bottom - area.getBoundingClientRect().top,
	});
}

// ---- the greeting
const answer = computed(() => holidayTouch.answer.value);
const name = computed(() => (props.firstName || "").trim());
const normalText = computed(() =>
	name.value ? `${props.greeting}, ${name.value}` : props.greeting
);
const dayText = computed(() => headingFor(answer.value.occasion, name.value) || normalText.value);
const showDay = computed(() => scene.text.swapped);
// A swap fades in; "instant" (rest, or a settle) has no motion at all.
const swapClass = (on) => ({ "is-off": !on, "is-instant": scene.text.instant });

// ---- the scene
const prefersReduced = () =>
	typeof window.matchMedia === "function" &&
	window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const scene = createHolidayScene({
	answer: () => holidayTouch.answer.value,
	host: () => hostEl.value,
	getBoxes,
	composerEmpty: () => props.composerEmpty,
	reduced: prefersReduced,
	idPrefix: `jvs${uid}`,
	fontsReady: () =>
		document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve(),
});

// Boxes of the hero (mark, heading and sub-line), the mark, the heading and the bottom block,
// in px relative to the art layer. The hero's bottom is where the floor row keeps clear.
function getBoxes() {
	const base = hostEl.value.getBoundingClientRect();
	const rel = (el) => {
		const r = el.getBoundingClientRect();
		return { x: r.left - base.left, y: r.top - base.top, w: r.width, h: r.height };
	};
	return {
		host: { w: base.width, h: base.height },
		hero: rel(heroEl.value),
		mark: rel(markEl.value),
		heading: rel(h1El.value),
		cards: rel(bottomEl.value),
	};
}

// Why the scene is or is not playing, readable from the console (window.__holidayTouch).
watch(scene.reason, (reason) => holidayTouch.setSceneReason(reason), { immediate: true });

// Hands the scene fresh boxes, but only while one is playing and the page is all there (the
// screen may have gone, or the scene ended, between the change and the next tick).
function relayoutNow() {
	const phase = scene.phase.value;
	if (phase !== "playing" && phase !== "replaying") return;
	if (![hostEl, heroEl, markEl, h1El, bottomEl].every((r) => r.value)) return;
	scene.relayout(getBoxes());
}

// ---- what ends a scene: input, a click or key anywhere (never intercepted), a scroll of the
// stage, a change of the stage area itself, the hold, a route change. Leaving the screen stops it.
const route = useRoute();
watch(
	() => props.composerEmpty,
	(empty) => !empty && scene.settle("early")
);
watch(
	() => props.holdActive,
	(hold) => hold && scene.settle("early")
);
watch(
	() => route && route.fullPath,
	() => scene.settle("early")
);

const onKey = () => scene.settle("early");
const SCROLLS = ["wheel", "touchmove"];
let sizes = new Map();
let observer = null;
let wornWatch = null;
// Only the stage area (the window or the sidebar changed) ends a scene. The hero and the
// bottom block change on their own while a scene plays: the web font swapping in, the
// suggestions, banner or catalog arriving. That is not the person doing anything, so the
// scene keeps playing and reads the boxes again.
//
// An observer's first reading of an element is its size at that moment, not "no change":
// the scale or the web font may have been applied after the mount-time measure and before
// the observer started. So a first reading of the hero or the bottom block counts as a
// change when the layout is no longer what the centring was measured from.
function onResize(entries) {
	let areaChanged = false;
	let layoutChanged = false;
	for (const entry of entries) {
		const { width, height } = entry.contentRect;
		const last = sizes.get(entry.target);
		sizes.set(entry.target, { width, height });
		const same =
			last && Math.abs(last.width - width) < 1 && Math.abs(last.height - height) < 1;
		if (!last && entry.target !== areaEl.value && layoutKey() !== measuredKey) {
			layoutChanged = true;
			continue;
		}
		if (!last || same) continue;
		if (entry.target === areaEl.value) areaChanged = true;
		else layoutChanged = true;
	}
	if (areaChanged) {
		measureScale();
		scene.settle("early");
	} else if (layoutChanged) {
		if (scene.centred.value) measureHero();
		// After the new shift is on the page, so the scene reads the mark where it now is.
		nextTick(relayoutNow);
	}
}

onMounted(async () => {
	measureScale();
	if (scrollEl.value && typeof getComputedStyle === "function") {
		const pad = parseFloat(getComputedStyle(scrollEl.value).paddingTop);
		if (Number.isFinite(pad)) padTop.value = pad;
	}
	measureWorn();
	measureHero();
	// Passive, capturing, and never calling preventDefault or stopPropagation: a click on
	// the sidebar, the composer or a link still does its normal job.
	window.addEventListener("pointerdown", scene.onPointerDown, { capture: true, passive: true });
	window.addEventListener("keydown", onKey, { capture: true, passive: true });
	// Scrolling the stage moves the page under the art, which does not follow it.
	for (const type of SCROLLS) scrollEl.value?.addEventListener(type, onKey, { passive: true });
	scene.begin();
	await nextTick();
	// The scale just set (and any font that has swapped since) is on the page now; the first
	// measure above read the hero before it.
	if (layoutKey() !== measuredKey) {
		if (scene.centred.value) measureHero();
		nextTick(relayoutNow);
	}
	// The item's layer is mounted after the art module loads, and again on a replay.
	if (typeof MutationObserver === "function" && markEl.value) {
		wornWatch = new MutationObserver(measureWorn);
		wornWatch.observe(markEl.value, { childList: true, subtree: true });
	}
	if (typeof ResizeObserver === "function" && areaEl.value) {
		observer = new ResizeObserver(onResize);
		[areaEl.value, heroEl.value, bottomEl.value].forEach((el) => el && observer.observe(el));
	}
});
onBeforeUnmount(() => {
	window.removeEventListener("pointerdown", scene.onPointerDown, { capture: true });
	window.removeEventListener("keydown", onKey, { capture: true });
	for (const type of SCROLLS) scrollEl.value?.removeEventListener(type, onKey);
	if (observer) observer.disconnect();
	if (wornWatch) wornWatch.disconnect();
	scene.stop();
});
</script>

<style scoped>
.jv-stage {
	position: relative;
	flex: 1;
	min-height: 0;
	display: flex;
	flex-direction: column;
	/* Nothing in the stage (the worn item above the mark included) paints outside it or
	   above the page's top bar: its own stacking context, clipped to its box. */
	isolation: isolate;
	overflow: hidden;
}
/* Above the scroller's plain content, below the greeting (see .jv-stage-hero). */
.jv-stage-art {
	position: absolute;
	inset: 0;
	z-index: 1;
	overflow: hidden;
	pointer-events: none;
}
.jv-stage-scroll {
	flex: 1;
	min-height: 0;
	overflow-y: auto;
	display: flex;
	flex-direction: column;
	align-items: center;
	padding: 12px 32px 16px;
}
/* The day sub-line looks like the orienting line ChatView renders in the slot. */
.jv-stage-subd {
	font-size: 15px;
	color: var(--text-2);
	line-height: 1.5;
	margin: 12px 0 0;
}
.jv-stage-col {
	width: 100%;
	flex: 1 0 auto;
	display: flex;
	flex-direction: column;
	text-align: center;
}
/* The mark and greeting take 0.6 of the free space above them and leave 0.4 below. */
.jv-stage-top {
	flex: 1 1 auto;
	display: flex;
	flex-direction: column;
	align-items: center;
}
.jv-stage-top::before {
	content: "";
	flex: 6 1 0;
	/* Room for the worn item above the mark at rest (see the script's header). */
	min-height: var(--rest-lift, 0px);
}
.jv-stage-top::after {
	content: "";
	flex: 4 1 0;
}
.jv-stage-hero {
	position: relative;
	z-index: 2;
	transition: transform var(--glide, 0ms) cubic-bezier(0.2, 0.8, 0.2, 1);
	flex: none;
	width: 100%;
	max-width: 680px;
	display: flex;
	flex-direction: column;
	align-items: center;
}
.jv-stage-mark {
	position: relative;
	display: inline-flex;
	flex: none;
}
.jv-stage-replay {
	position: absolute;
	inset: 0;
	appearance: none;
	background: none;
	border: 0;
	padding: 0;
	margin: 0;
	cursor: pointer;
}
.jv-stage-replay:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 3px;
}
.jv-stage-h1 {
	display: grid;
	margin: calc(44px * var(--s)) 0 0;
	font-size: calc(32px * var(--s));
	font-weight: 640;
	line-height: 1.2;
	letter-spacing: -0.03em;
	overflow-wrap: anywhere;
}
.jv-stage-sub {
	display: grid;
}
/* Greeting and sub-line pairs share one grid cell; the one not shown is invisible, not gone. */
.jv-stage-h1 > .jv-stage-t,
.jv-stage-sub > .jv-stage-t {
	grid-area: 1 / 1;
	min-width: 0;
	transition: opacity 0.52s cubic-bezier(0.2, 0.8, 0.2, 1);
}
.jv-stage-t.is-off {
	opacity: 0;
	visibility: hidden;
	transition: opacity 0.25s ease, visibility 0s 0.25s;
}
.jv-stage-t.is-instant {
	transition: none;
}
.jv-stage-bottom {
	flex: none;
	width: 100%;
	max-width: 680px;
	margin: 0 auto;
	transition: opacity var(--fade, 0ms) ease;
}
/* visibility, not display: nothing moves, and Tab skips what is hidden. */
.jv-stage-bottom.is-hidden {
	opacity: 0;
	visibility: hidden;
	transition: none;
}
@media (prefers-reduced-motion: reduce) {
	.jv-stage-t,
	.jv-stage-hero,
	.jv-stage-bottom {
		transition: none !important;
	}
}
</style>
