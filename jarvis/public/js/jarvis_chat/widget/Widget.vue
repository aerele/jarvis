<template>
	<!-- Draggable, edge-snapping Jarvis shortcut FAB, floating on every ERP Desk
	     page. Tapping it opens the side chat panel in place; on a narrow
	     viewport (where a 400px panel would be most of the screen) it falls
	     back to navigating to the chat SPA. -->
	<div class="jvw-root" :class="{ 'jvw-root--dark': isDark }">
		<button
			type="button"
			ref="fabEl"
			class="jvw-fab"
			:class="{
				'jvw-fab--snapping': snapping,
				'jvw-fab--dragging': dragging,
				'jvw-fab--faded': faded && !dragging,
				'jvw-fab--dock-left': side === 'left',
			}"
			:style="fabStyle"
			:aria-label="panelOpen ? `Close ${brandName}` : `Ask ${brandName}`"
			:aria-expanded="panelOpen ? 'true' : 'false'"
			@click="onFabClick"
			@pointerdown="onPointerDown"
			@pointermove="onPointerMove"
			@pointerup="onPointerUp"
			@pointercancel="onPointerCancel"
			@pointerenter="wake"
			@focus="wake"
		>
			<!-- Grip dots: the drag affordance. design.md 1.3 forbids hover
			     motion, so this fades in on OPACITY alone — nothing moves. -->
			<span class="jvw-grip" aria-hidden="true"><i></i><i></i><i></i></span>
			<img v-if="brandLogoUrl" :src="brandLogoUrl" class="jvw-fab-img" alt="" />
			<svg v-else viewBox="0 0 24 24" width="24" height="24" fill="#fff">
				<path d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z" />
			</svg>
		</button>

		<Panel
			ref="panelRef"
			:open="panelOpen"
			:context="effectiveContext"
			:layout="panelBox"
			@close="closePanel"
			@open-full="openFull"
			@dismiss-context="contextDismissed = true"
		/>
	</div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import { FULL_CHAT_URL, conversationUrl, PANEL_MIN_VIEWPORT_PX } from "./config.mjs";
import { contextFromRoute } from "./desk_context.mjs";
import { panelLayout } from "./panel_anchor.mjs";
import { isDarkNow, watchTheme } from "./desk_theme.mjs";
import * as fabPos from "./fab_position.mjs";
import Panel from "./Panel.vue";

// ---- FAB: draggable, edge-snapping, idle-fading launcher button.
// fab_position.mjs owns the pure geometry/drag/idle-timer math (unit tested);
// this component only owns the DOM refs, localStorage and pointer events. ----
const fabEl = ref(null);
const side = ref("right");
const yRatio = ref(1);
const fabXY = ref({ x: 0, y: 0 });
const dragging = ref(false);
const snapping = ref(false);
const faded = ref(false);

let dragSession = null;
let suppressClick = false;
let idleTimer = null;
let snapTimeoutHandle = null;

// Access gate: desk boot sets this once for the session (see Task B).
const hasAccess = Boolean(window.frappe?.boot?.jarvis_has_access);
// Whitelabel FAB label + mark (set_jarvis_boot); blank => Jarvis defaults.
const brandName = (window.frappe?.boot?.jarvis_agent_name || "").trim() || "Jarvis";
const brandLogoUrl = (window.frappe?.boot?.jarvis_brand_logo_url || "").trim();

// ---- Side chat panel: open state and the Desk record it is looking at. ----
const panelRef = ref(null);
const isDark = ref(false);
let unwatchTheme = null;
const panelOpen = ref(false);
const deskContext = ref(null);
const contextDismissed = ref(false);

// Dismissing the chip suppresses context for the current page only; a new
// route is a new subject, so the dismissal does not carry over.
const effectiveContext = computed(() => (contextDismissed.value ? null : deskContext.value));

// The window hangs off the FAB, so it re-lays-out on every drag frame and on
// resize. fabXY is already reactive, which is what makes the panel travel with
// the launcher instead of being stranded across the screen from it.
const viewportTick = ref(0);
const panelBox = computed(() => {
	viewportTick.value; // re-run on resize / orientation change
	const topInset = readCssPx(document.documentElement, "--navbar-height", 48);
	return panelLayout(
		{ x: fabXY.value.x, y: fabXY.value.y, size: fabPos.FAB_SIZE },
		{ vw: window.innerWidth, vh: window.innerHeight, top: topInset }
	);
});

function readDeskContext() {
	const route = (window.frappe && frappe.get_route && frappe.get_route()) || [];
	let filters = null;
	try {
		filters = window.frappe?.query_report?.get_filter_values?.() || null;
	} catch (e) {
		filters = null; // report not loaded yet
	}
	deskContext.value = contextFromRoute(route, { filters });
	contextDismissed.value = false;
}

function closePanel() {
	panelOpen.value = false;
	fabEl.value?.focus();
}

function openFull() {
	window.location.assign(conversationUrl(panelRef.value?.convId));
}

const fabStyle = computed(() => ({
	transform: `translate3d(${fabXY.value.x}px, ${fabXY.value.y}px, 0)`,
}));

function readCssPx(el, prop, fallback) {
	if (!el) return fallback;
	const n = parseFloat(getComputedStyle(el).getPropertyValue(prop));
	return Number.isFinite(n) ? n : fallback;
}

function getViewport() {
	const topInset = readCssPx(document.documentElement, "--navbar-height", 48) + 8;
	// --jvw-safe-bottom is declared on .jvw-root (env(safe-area-inset-bottom, 0px));
	// read it off the FAB itself since it lives inside .jvw-root and inherits it.
	const safeBottom = readCssPx(fabEl.value, "--jvw-safe-bottom", 0);
	return {
		vw: window.innerWidth,
		vh: window.innerHeight,
		topInset,
		bottomInset: 22 + safeBottom,
		edgeInset: fabPos.EDGE_INSET,
		fabSize: fabPos.FAB_SIZE,
	};
}

// Renders the persisted side/ratio dock spot. animate:true flips on the CSS
// snap transition for SNAP_MS, then clears it.
function applyPosition({ animate = false } = {}) {
	const vp = getViewport();
	fabXY.value = {
		x: fabPos.xForSide(side.value, vp),
		y: fabPos.ratioToY(yRatio.value, vp),
	};
	if (snapTimeoutHandle) {
		window.clearTimeout(snapTimeoutHandle);
		snapTimeoutHandle = null;
	}
	if (animate) {
		snapping.value = true;
		snapTimeoutHandle = window.setTimeout(() => {
			snapping.value = false;
			snapTimeoutHandle = null;
		}, fabPos.SNAP_MS);
	} else {
		snapping.value = false;
	}
}

// Setup-time init (synchronous — no flash): resolve the persisted dock spot
// from localStorage before the first render so the FAB never jumps from a
// default position to the saved one.
{
	let savedRaw = null;
	try {
		savedRaw = localStorage.getItem(fabPos.STORAGE_KEY);
	} catch (e) {
		savedRaw = null;
	}
	const resolved = fabPos.resolvePosition(fabPos.parseSavedPosition(savedRaw), getViewport());
	side.value = resolved.side;
	yRatio.value = resolved.yRatio;
	applyPosition({ animate: false });
}

function wake() {
	faded.value = false;
	idleTimer?.poke();
}

// Document-level activity also counts as "not idle": today the FAB only woke
// on FAB-local pointerenter/focus/pointerdown, so moving the mouse anywhere
// else on the page left it faded and feeling disabled. Throttled to at most
// one poke() per ~1s (mousemove fires far more often than that) — when
// already faded, skip the throttle and restore instantly instead.
let lastActivityPoke = 0;
function onDocumentActivity() {
	if (faded.value) {
		wake();
		return;
	}
	const now = Date.now();
	if (now - lastActivityPoke < 1000) return;
	lastActivityPoke = now;
	idleTimer?.poke();
}

function onPointerDown(e) {
	wake();
	suppressClick = false;
	if (e.button !== 0) return;
	dragSession = fabPos.dragStart(fabXY.value.x, fabXY.value.y, e.clientX, e.clientY);
	fabEl.value?.setPointerCapture?.(e.pointerId);
}

function onPointerMove(e) {
	if (!dragSession) return;
	dragSession = fabPos.dragMove(dragSession, e.clientX, e.clientY, fabPos.TAP_THRESHOLD_PX);
	dragging.value = dragSession.dragging;
	if (!dragSession.dragging) return; // still under the tap threshold
	const vp = getViewport();
	const x = dragSession.startFabX + (e.clientX - dragSession.startPx);
	const y = fabPos.clampY(dragSession.startFabY + (e.clientY - dragSession.startPy), vp);
	fabXY.value = { x, y };
}

function onPointerUp(e) {
	fabEl.value?.releasePointerCapture?.(e.pointerId);
	if (!dragSession) return;
	const vp = getViewport();
	const result = fabPos.dragEnd(dragSession, vp);
	dragSession = null;
	dragging.value = false;
	wake(); // re-arm the idle timer now that the drag (which blocked it) is over
	if (result.tap) return; // native click follows; onFabClick handles it
	suppressClick = true;
	side.value = result.side;
	yRatio.value = result.yRatio;
	try {
		localStorage.setItem(
			fabPos.STORAGE_KEY,
			fabPos.serializePosition({ side: result.side, yRatio: result.yRatio })
		);
	} catch (e) {
		/* localStorage unavailable */
	}
	applyPosition({ animate: true });
}

function onPointerCancel(e) {
	fabEl.value?.releasePointerCapture?.(e.pointerId);
	if (!dragSession) return;
	dragSession = null;
	dragging.value = false;
	wake(); // re-arm the idle timer now that the drag (which blocked it) is over
	applyPosition({ animate: true }); // revert to the last committed spot
}

function onFabClick() {
	if (suppressClick) {
		suppressClick = false;
		return;
	}
	wake();
	if (!hasAccess) {
		window.location.assign("/jarvis-no-access");
		return;
	}
	// Below the threshold a 400px panel is most of the screen, so fall back to
	// the full SPA rather than designing a third layout for it.
	if (window.innerWidth < PANEL_MIN_VIEWPORT_PX) {
		window.location.assign(FULL_CHAT_URL);
		return;
	}
	if (!panelOpen.value) readDeskContext();
	panelOpen.value = !panelOpen.value;
}

// Re-clamps the FAB into the (possibly resized) dockable band; ratio-based
// storage means this is enough to keep it on-screen after a viewport change.
function onResize() {
	applyPosition({ animate: false });
	viewportTick.value++; // re-anchor the panel to the new viewport
}

onMounted(() => {
	// The Desk's dark flag is read in JS, not CSS: this component's
	// `:global([data-theme=dark]) .jvw-root` compiled to a bare
	// `[data-theme=dark]` rule, so its custom properties landed on <html> and
	// were overridden by .jvw-root's own light values. The accent never
	// changed in dark mode.
	isDark.value = isDarkNow();
	unwatchTheme = watchTheme((d) => {
		isDark.value = d;
	});

	// Setup-time applyPosition() ran before fabEl was live, so getViewport()
	// couldn't read the real --jvw-safe-bottom off it and fell back to 0. Now
	// that the DOM ref exists, correct the position so notched devices don't
	// keep the pre-mount fallback spot.
	applyPosition({ animate: false });

	idleTimer = fabPos.createIdleTimer({
		delayMs: fabPos.IDLE_FADE_MS,
		onIdle: () => {
			if (!dragging.value) faded.value = true;
		},
	});
	window.addEventListener("resize", onResize);
	window.addEventListener("orientationchange", onResize);
	document.addEventListener("mousemove", onDocumentActivity, { passive: true });
	document.addEventListener("touchstart", onDocumentActivity, { passive: true });
	document.addEventListener("keydown", onDocumentActivity, { passive: true });

	// The record under discussion changes as the user moves around the Desk, and
	// the panel deliberately stays open across that — so re-read the context
	// rather than closing. The chip updates live.
	readDeskContext();
	if (window.frappe?.router?.on) frappe.router.on("change", readDeskContext);
});

onBeforeUnmount(() => {
	unwatchTheme?.();
	window.removeEventListener("resize", onResize);
	window.removeEventListener("orientationchange", onResize);
	document.removeEventListener("mousemove", onDocumentActivity);
	document.removeEventListener("touchstart", onDocumentActivity);
	document.removeEventListener("keydown", onDocumentActivity);
	idleTimer?.dispose();
	if (snapTimeoutHandle) {
		window.clearTimeout(snapTimeoutHandle);
		snapTimeoutHandle = null;
	}
});
</script>

<style scoped>
/* Scoped tokens for the FAB (it lives in <body>). */
.jvw-root {
	/* Brand gradient from the Jarvis Side Chat design board (a recorded
	   divergence from design.md's near-black chrome, scoped to this widget). */
	--accent: #6a56e8;
	--accent-grad: linear-gradient(140deg, #8b7cf7, #6a56e8);
	--jvw-safe-bottom: env(safe-area-inset-bottom, 0px);
	font-family: "Inter", system-ui, -apple-system, sans-serif;
}

/* Follow the Desk theme. Frappe's theme switcher stamps data-theme="dark" on
   <html>, which is an ancestor of this body-mounted widget: the accent becomes
   the indigo brand blue (the SPA's theme.js DARK_VARS accent) so the FAB stays
   visible against dark surfaces. */
.jvw-root--dark {
	--accent: #8b7cf7;
	--accent-grad: linear-gradient(140deg, #9d90ff, #7a68f0);
}

/* ---- launcher bubble ---- */
.jvw-fab {
	width: 54px;
	height: 54px;
	border-radius: 16px;
	background: var(--accent-grad);
	border: none;
	cursor: grab;
	display: flex;
	align-items: center;
	justify-content: center;
	box-shadow: 0 10px 26px -6px rgba(106, 86, 232, 0.55);
	position: fixed;
	left: 0;
	top: 0;
	z-index: 1121;
	touch-action: none;
	will-change: transform;
	transition: opacity 0.25s ease;
}
.jvw-fab-img {
	width: 24px;
	height: 24px;
	object-fit: cover;
	border-radius: 7px;
}
.jvw-fab:hover {
	filter: brightness(1.06);
}
.jvw-fab:focus-visible {
	outline: 2px solid var(--accent);
	outline-offset: 3px;
}
.jvw-fab--snapping {
	transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.25s ease;
}
.jvw-fab--dragging {
	transition: none;
	cursor: grabbing;
}
.jvw-fab--faded {
	opacity: 0.4;
}

/* ---- drag affordance ----
   design.md 1.3 forbids hover motion, and 5 lists hover-lift and pulse as
   anti-patterns to remove. So draggability is signalled without anything
   moving at rest: the grip fades in on OPACITY, the cursor changes, and the
   press uses the same scale(0.98) TabButtons already uses.

   The scale is applied to the FAB's CHILDREN, never the FAB itself: .jvw-fab
   carries the drag position in an inline `transform`, so scaling it here would
   snap the button back to the origin mid-press. */
.jvw-grip {
	position: absolute;
	left: 7px;
	top: 50%;
	transform: translateY(-50%);
	display: flex;
	flex-direction: column;
	gap: 2.5px;
	opacity: 0;
	transition: opacity 0.12s ease;
	pointer-events: none;
}
/* Keep the grip on the edge facing into the page, not the one against the
   viewport edge the FAB is snapped to. */
.jvw-fab--dock-left .jvw-grip {
	left: auto;
	right: 7px;
}
.jvw-grip i {
	display: block;
	width: 2.5px;
	height: 2.5px;
	border-radius: 999px;
	background: #fff;
}
.jvw-fab:hover .jvw-grip,
.jvw-fab:focus-visible .jvw-grip,
.jvw-fab--dragging .jvw-grip {
	opacity: 0.55;
}

@media (prefers-reduced-motion: no-preference) {
	.jvw-fab > svg {
		transition: transform 0.12s ease;
	}
	.jvw-fab:active > svg,
	.jvw-fab--dragging > svg {
		transform: scale(0.98);
	}
}

@media (prefers-reduced-motion: reduce) {
	.jvw-fab,
	.jvw-fab--snapping {
		transition: opacity 0.2s ease;
	}
	.jvw-grip {
		transition: none;
	}
}
</style>
