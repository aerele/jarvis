<template>
	<div ref="root" class="jvs-root">
		<!-- The art: its own layer from the top of the screen to the top of the composer, so it
		     runs under the bar buttons. It lets every touch through and is hidden from screen
		     readers. The theme is set here, on its own wrapper: "system" dark leaves no
		     data-theme on <html>, and the art's dark colours key on it. -->
		<div
			v-if="active"
			class="jvs-art"
			:data-theme="artTheme"
			:style="{ height: `${hostH}px` }"
			aria-hidden="true"
			data-testid="holiday-art"
		>
			<div ref="hostEl" class="jvs-host"></div>
		</div>
		<slot v-bind="slotProps" />
	</div>
</template>

<script setup>
// Keep in step with frontend/src/components/holiday/HolidayStage.vue (the desktop original),
// which this is the phone twin of. How it differs:
//  - It does not own the welcome markup: the new-chat view keeps its hero and starter cards and
//    gets the scene's state through the default slot. It finds the pieces to measure by their
//    data-hs attributes: "hero", "mark", "heading" and "cards" (absent when not shown).
//  - The art layer is fixed to the screen, from its top to the top of the composer.
//  - The hero never moves (no centring, no glide), so the starters fade (visibility) instead of
//    the hero shifting; a replay fades them too, because the floor row stands in their band.
//  - It asks whether the screen has room before any art or showing is used (landscape, a short
//    phone: rest), and gives way to the top banners (install, announcement, update).
//  - A change of the screen's size ends a scene only when the window really changed size.
//
// While a scene may still play, the greeting is the person's normal one and the starters are
// hidden (visibility, so a tap does not reach them and nothing moves). A tap, key or touch
// anywhere ends it; the tap that did is not also a tap on a starter card.
import {
	computed,
	getCurrentInstance,
	nextTick,
	onBeforeUnmount,
	onMounted,
	ref,
	watch,
} from "vue";
import { phoneSceneFits } from "@jsshared/holiday/scene-phone.mjs";
import { isWhitelabeled } from "@/branding";
import { showAnnouncement } from "../announcementGate";
import { holidayTouch, wornAnswer } from "../holidayTouch";
import { showBanner } from "../noticeGate";
import { heroBox, sceneBoxes } from "../lib/holidayBoxes";
import { heroRoomPx } from "../lib/holidayRoom";
import { hasScene } from "../lib/holidayScenes";
import { resolveTheme, sizeChanged, stageEligible } from "../lib/holidayStageRules";
import { installBannerVisible } from "../lib/installBanner";
import { theme } from "../lib/theme";
import { createHolidayScene } from "../lib/useHolidayScene";

const props = defineProps({
	holdActive: { type: Boolean, default: false },
	/** Text or an attachment in the composer (a restored draft included): no scene then. */
	hasDraft: { type: Boolean, default: false },
	/** () => the composer's top in px from the top of the screen, or 0 when it is not there. */
	composerTop: { type: Function, required: true },
});

const uid = getCurrentInstance().uid;
const root = ref(null);
const hostEl = ref(null);
const hostH = ref(0);

// A top banner pushes the app down in the flow, while the art layer is fixed to the screen's
// top: it would be drawn over the banner. The ordinary welcome already shows the day, so with a
// banner up the stage is left out rather than started below it. (Over-wide on purpose: the
// update banner also yields to the hold, which ends the scene anyway.)
const bannerShown = () => installBannerVisible.value || showAnnouncement.value || showBanner.value;

// Decided once, when the screen opens: a maintenance hold or a banner that starts later ends a
// scene (see the watches below) but never turns one on.
const active = stageEligible({
	answer: holidayTouch.answer.value,
	whiteLabel: isWhitelabeled,
	holdAtMount: props.holdActive,
	bannerAtMount: bannerShown(),
	hasScene,
});

// ---- the art's theme
const darkQuery =
	typeof window.matchMedia === "function"
		? window.matchMedia("(prefers-color-scheme: dark)")
		: null;
const systemDark = ref(!!darkQuery && darkQuery.matches);
const onSystemTheme = (event) => (systemDark.value = event.matches);
const artTheme = computed(() => resolveTheme(theme.value, systemDark.value));

// ---- measuring: boxes in px relative to the art layer (which starts at the screen top)
const piece = (name) => (root.value ? root.value.querySelector(`[data-hs="${name}"]`) : null);

function measureHost() {
	hostH.value = Math.max(0, Math.round(props.composerTop() || 0));
}

function getBoxes() {
	const base = hostEl.value.getBoundingClientRect();
	const rel = (el) => {
		const r = el.getBoundingClientRect();
		return { x: r.left - base.left, y: r.top - base.top, w: r.width, h: r.height };
	};
	const mark = rel(piece("mark"));
	const heading = rel(piece("heading"));
	const cardsEl = piece("cards");
	return sceneBoxes({
		host: { w: base.width, h: base.height },
		hero: heroBox(mark, heading),
		mark,
		heading,
		cards: cardsEl ? rel(cardsEl) : null,
	});
}

// Does the scene fit this screen? Asked before any art is loaded or a showing is spent, and
// again before a replay. The layer's height is read again first (a rotation, the keyboard, a tab
// that was hidden when the screen opened): the layer follows on the next render, before the
// art import settles.
function fits() {
	if (!hostEl.value || !piece("mark") || !piece("heading")) return false;
	measureHost();
	const boxes = getBoxes();
	return phoneSceneFits({
		design: holidayTouch.answer.value.design,
		W: boxes.host.w,
		H: hostH.value,
		markTopPx: boxes.mark.y,
		wornTopPx: heroRoomPx(wornAnswer.value, props.holdActive),
	});
}

// ---- the scene
const prefersReduced = () =>
	typeof window.matchMedia === "function" &&
	window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const scene = active
	? createHolidayScene({
			answer: () => holidayTouch.answer.value,
			host: () => hostEl.value,
			getBoxes,
			fits,
			composerEmpty: () => !props.hasDraft,
			reduced: prefersReduced,
			idPrefix: `jvs${uid}`,
			fontsReady: () =>
				document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve(),
	  })
	: null;

// What the view's hero and starters read. Inactive, everything is the ordinary welcome.
const slotProps = computed(() => ({
	active,
	mark: scene ? scene.mark : null,
	showDay: scene ? scene.text.swapped : true,
	instant: scene ? scene.text.instant : true,
	cardsClass: scene && scene.cardsHidden.value ? "jvs-cards-hidden" : "",
	cardsStyle: scene ? { "--fade": `${scene.fadeMs.value}ms` } : undefined,
	onMarkTap: () => scene && scene.replay(),
	guardCardTap,
}));

/** A click on a starter card that the tap ending the scene started is not a pick. */
function guardCardTap(event) {
	if (!scene || !scene.takeEndingTap()) return;
	event.stopPropagation();
	event.preventDefault();
}

// Why the scene is or is not playing, readable from the console (window.__holidayTouch).
if (scene)
	watch(scene.reason, (reason) => holidayTouch.setSceneReason(reason), { immediate: true });

// ---- what ends a scene: a draft, the hold, a touch or key anywhere (never intercepted), a
// change of the screen's size. Leaving the screen stops it (onBeforeUnmount).
if (scene) {
	watch(
		() => props.hasDraft,
		(draft) => draft && scene.settle("early")
	);
	watch(
		() => props.holdActive,
		(hold) => hold && scene.settle("early")
	);
	watch(bannerShown, (shown) => shown && scene.settle("early"));
}

// Enter or Space on the mark also starts the click that would replay it.
function startsMarkClick(event) {
	const mark = piece("mark");
	if (!mark || !(event.target instanceof Node) || !mark.contains(event.target)) return false;
	return event.key === "Enter" || event.key === " ";
}

// A resize event is not always a resize: browsers fire one for the safe area or the bar at
// start-up with the window the same size. The window must really change, from the size last
// seen, to end a scene; before it starts, a change only re-measures (the room is asked again
// once the art is loaded), so it never costs the day's scene.
const viewportSize = () => ({ w: window.innerWidth, h: window.innerHeight });
let viewport = viewportSize();
function onWindowResize() {
	measureHost();
	const now = viewportSize();
	const moved = sizeChanged(viewport, now);
	viewport = now;
	const phase = scene.phase.value;
	if (moved && (phase === "playing" || phase === "replaying")) scene.settle("early");
}

const endEarly = () => scene.settle("early");
const POINTER = { capture: true, passive: true };
const ENDERS = [
	["pointerdown", () => scene.onPointerDown(), POINTER],
	["keydown", (event) => scene.onKeyDown(startsMarkClick(event)), POINTER],
	["touchmove", endEarly, POINTER],
	["wheel", endEarly, POINTER],
	["resize", onWindowResize, { passive: true }],
];

// ---- the page moves under a playing scene: the starters arriving, the web font swapping in.
// That is not the person doing anything, so the scene keeps playing and reads the boxes again.
// Watched: the hero, the greeting and the starter cards, by their rect (1 px of tolerance).
const sizes = new Map();
let observer = null;
const sizeOf = (el) => {
	const r = el.getBoundingClientRect();
	return { w: r.width, h: r.height };
};
function relayoutNow() {
	const phase = scene.phase.value;
	if (phase !== "playing" && phase !== "replaying") return;
	if (!hostEl.value || !piece("mark") || !piece("heading")) return;
	scene.relayout(getBoxes());
}
function onPieceResize(entries) {
	let moved = false;
	for (const { target } of entries) {
		const now = sizeOf(target);
		if (sizeChanged(sizes.get(target), now)) moved = true;
		sizes.set(target, now);
	}
	if (moved) nextTick(relayoutNow); // after the page has laid out the new sizes
}
function watchPieces() {
	if (typeof ResizeObserver !== "function") return;
	observer = new ResizeObserver(onPieceResize);
	for (const name of ["hero", "heading", "cards"]) {
		const el = piece(name);
		if (!el) continue;
		sizes.set(el, sizeOf(el));
		observer.observe(el);
	}
}

onMounted(async () => {
	if (!scene) return;
	if (darkQuery) darkQuery.addEventListener("change", onSystemTheme);
	for (const [type, handler, options] of ENDERS) window.addEventListener(type, handler, options);
	measureHost();
	await nextTick(); // the art layer has its height before the room check reads it
	watchPieces();
	scene.begin();
});
onBeforeUnmount(() => {
	if (!scene) return;
	if (darkQuery) darkQuery.removeEventListener("change", onSystemTheme);
	for (const [type, handler, options] of ENDERS)
		window.removeEventListener(type, handler, options);
	if (observer) observer.disconnect();
	scene.stop();
});
</script>

<style>
/* Not scoped: the classes go on the view's own starter cards (through the slot). */
.jvs-root {
	display: contents;
}
/* Above the plain content, below the bar buttons and the hero (the view gives those z 2). */
.jvs-art {
	position: fixed;
	top: 0;
	left: 0;
	right: 0;
	z-index: 1;
	overflow: hidden;
	pointer-events: none;
}
.jvs-host {
	position: absolute;
	inset: 0;
}
/* visibility, not display: nothing moves, and a tap or Tab does not reach what is hidden. */
.jvs-cards-hidden {
	opacity: 0;
	visibility: hidden;
}
[data-hs="cards"] {
	transition: opacity var(--fade, 0ms) ease;
}
.jvs-cards-hidden[data-hs="cards"] {
	transition: none;
}
@media (prefers-reduced-motion: reduce) {
	[data-hs="cards"] {
		transition: none !important;
	}
}
</style>
