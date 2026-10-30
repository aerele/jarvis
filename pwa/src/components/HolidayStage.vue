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
// which this is the phone twin of.
//
// The scene-day welcome on the phone. It owns the scene (lib/useHolidayScene.js), the art
// layer and what ends a scene; the new-chat view keeps its own hero and starter markup and
// gets the scene's state through the default slot. It finds the pieces to measure by their
// data-hs attributes: the mark ("mark"), the greeting ("heading") and the starter cards
// ("cards", absent when they are not shown).
//
// While a scene may still play, the greeting is the person's normal one and the starters are
// hidden (visibility, so a tap does not reach them and nothing moves). The hero never moves.
// With no room for the scene on this screen (landscape, a short phone) it rests at once and no
// showing is recorded. A tap, key or touch anywhere ends it; the tap that did is not also a
// tap on a starter card.
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
import { holidayTouch, wornAnswer } from "../holidayTouch";
import { heroBox, sceneBoxes } from "../lib/holidayBoxes";
import { heroRoomPx } from "../lib/holidayRoom";
import { hasScene } from "../lib/holidayScenes";
import { resolveTheme, stageEligible } from "../lib/holidayStageRules";
import { theme } from "../lib/theme";
import { createHolidayScene } from "../lib/useHolidayScene";

const props = defineProps({
	/** The person's ordinary greeting ("Good morning, Priya."). */
	greeting: { type: String, required: true },
	/** The hero's words today ({heading, leadup}). */
	words: { type: Object, required: true },
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

// Decided once, when the screen opens: a maintenance hold that starts later ends a scene (see
// the watch below) but never turns one on.
const active = stageEligible({
	answer: holidayTouch.answer.value,
	whiteLabel: isWhitelabeled,
	holdAtMount: props.holdActive,
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

// Does the scene fit this screen? Asked before any art is loaded or a showing is spent.
function fits() {
	if (!hostEl.value || !piece("mark") || !piece("heading")) return false;
	const boxes = getBoxes();
	return phoneSceneFits({
		design: holidayTouch.answer.value.design,
		W: boxes.host.w,
		H: boxes.host.h,
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
}
const endEarly = () => scene.settle("early");
const POINTER = { capture: true, passive: true };
const ENDERS = [
	["pointerdown", () => scene.onPointerDown(), POINTER],
	["keydown", endEarly, POINTER],
	["touchmove", endEarly, POINTER],
	["wheel", endEarly, POINTER],
	["resize", endEarly, { passive: true }],
];

onMounted(async () => {
	if (!scene) return;
	if (darkQuery) darkQuery.addEventListener("change", onSystemTheme);
	for (const [type, handler, options] of ENDERS) window.addEventListener(type, handler, options);
	measureHost();
	await nextTick(); // the art layer has its height before the room check reads it
	scene.begin();
});
onBeforeUnmount(() => {
	if (!scene) return;
	if (darkQuery) darkQuery.removeEventListener("change", onSystemTheme);
	for (const [type, handler, options] of ENDERS)
		window.removeEventListener(type, handler, options);
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
