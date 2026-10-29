<template>
	<div
		ref="areaEl"
		class="jv-stage"
		:style="{ '--s': scale }"
		data-testid="holiday-stage"
		:data-phase="scene.phase.value"
	>
		<!-- The art: a sibling of the scroller, never inside it, so it cannot change
		     scrollHeight. It lets every pointer event through and is hidden from screen
		     readers. -->
		<div ref="hostEl" class="jv-stage-art" aria-hidden="true" data-testid="holiday-art"></div>
		<div class="jv-welcome-scroll jv-stage-scroll">
			<div class="jv-stage-col">
				<div class="jv-stage-top">
					<div ref="heroEl" class="jv-stage-hero">
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
// The scene-day welcome layout: a big centred Jarvis and the greeting at a 0.6 / 0.4 split
// of the space above the bottom block (starter cards, Payment Calendar link, capability
// catalog), with the scene's art on a layer over it. ChatView uses this only when
// lib/holidayStage.js says so; otherwise the normal WelcomeHero layout is untouched.
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
import { createHolidayScene } from "@/lib/useHolidayScene";
import { DAY_SUB_LINE, headingFor } from "@jsshared/holiday/copy.mjs";

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

// Boxes of the mark, heading and bottom block, in px relative to the art layer.
function getBoxes() {
	const base = hostEl.value.getBoundingClientRect();
	const rel = (el) => {
		const r = el.getBoundingClientRect();
		return { x: r.left - base.left, y: r.top - base.top, w: r.width, h: r.height };
	};
	return {
		host: { w: base.width, h: base.height },
		mark: rel(markEl.value),
		heading: rel(h1El.value),
		cards: rel(bottomEl.value),
	};
}

// ---- what ends a scene: input, a click or key anywhere (never intercepted), a change of
// the stage area itself, the hold, a route change. Leaving the screen stops it.
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
let sizes = new Map();
let observer = null;
// Only the stage area (the window or the sidebar changed) ends a scene. The hero and the
// bottom block change on their own while a scene plays: the web font swapping in, the
// suggestions, banner or catalog arriving. That is not the person doing anything, so the
// scene keeps playing and reads the boxes again.
function onResize(entries) {
	let areaChanged = false;
	let layoutChanged = false;
	for (const entry of entries) {
		const { width, height } = entry.contentRect;
		const last = sizes.get(entry.target);
		sizes.set(entry.target, { width, height });
		if (!last || (Math.abs(last.width - width) < 1 && Math.abs(last.height - height) < 1)) {
			continue;
		}
		if (entry.target === areaEl.value) areaChanged = true;
		else layoutChanged = true;
	}
	if (areaChanged) {
		measureScale();
		scene.settle("early");
	} else if (layoutChanged) {
		scene.relayout(getBoxes());
	}
}

onMounted(async () => {
	measureScale();
	// Passive, capturing, and never calling preventDefault or stopPropagation: a click on
	// the sidebar, the composer or a link still does its normal job.
	window.addEventListener("pointerdown", scene.onPointerDown, { capture: true, passive: true });
	window.addEventListener("keydown", onKey, { capture: true, passive: true });
	scene.begin();
	await nextTick();
	if (typeof ResizeObserver === "function" && areaEl.value) {
		observer = new ResizeObserver(onResize);
		[areaEl.value, heroEl.value, bottomEl.value].forEach((el) => el && observer.observe(el));
	}
});
onBeforeUnmount(() => {
	window.removeEventListener("pointerdown", scene.onPointerDown, { capture: true });
	window.removeEventListener("keydown", onKey, { capture: true });
	if (observer) observer.disconnect();
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
}
.jv-stage-top::after {
	content: "";
	flex: 4 1 0;
}
.jv-stage-hero {
	position: relative;
	z-index: 2;
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
	.jv-stage-bottom {
		transition: none !important;
	}
}
</style>
