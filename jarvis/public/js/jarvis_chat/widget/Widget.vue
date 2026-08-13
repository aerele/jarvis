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
				'jvw-fab--peek': autoPeek,
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
			<svg v-if="!brandLogoUrl" viewBox="0 0 24 24" width="24" height="24" fill="#fff">
				<path d="M12 2.5 L14 10 L21.5 12 L14 14 L12 21.5 L10 14 L2.5 12 L10 10 Z" />
			</svg>
			<!-- Blink face: DUPLICATED from JarvisMark.vue's jv-face/jv-eye + PEEK
			     block, by necessity not choice — this widget cannot import from
			     frontend/src or use JarvisMark itself (see panel_readiness.mjs for
			     why). Keep this markup, the CSS below, and the idle-blink timing in
			     ./Widget.vue's <script> in sync with JarvisMark.vue by hand. Hover
			     and focus reveal it like JarvisMark's hoverPeek; jvw-fab--peek
			     (autoPeek below) blinks it on its own while resting, like
			     JarvisMark's idlePeek. A whitelabel logo gets no face, same as
			     JarvisMark's img branch. -->
			<span v-if="!brandLogoUrl" class="jvw-face" aria-hidden="true">
				<i class="jvw-eye"></i><i class="jvw-eye"></i>
			</span>
		</button>

		<!-- Dimming backdrop for the expand-into-the-big-chat handoff: fades the
		     Desk behind the panel as it grows to fullscreen, so the motion reads
		     as going full. Inert otherwise. -->
		<div
			class="jvw-backdrop"
			:class="{ 'jvw-backdrop--show': leaving }"
			aria-hidden="true"
		></div>
		<Panel
			ref="panelRef"
			:open="panelOpen"
			:context="effectiveContext"
			:layout="panelBox"
			:leaving="leaving"
			@close="closePanel"
			@open-full="openFull"
			@dismiss-context="contextDismissed = true"
			@resize="onPanelResize"
			@resize-commit="onPanelResizeCommit"
		/>
	</div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import { FULL_CHAT_URL, conversationUrl, PANEL_MIN_VIEWPORT_PX } from "./config.mjs";
import { contextFromRoute } from "./desk_context.mjs";
import { panelLayout } from "./panel_anchor.mjs";
import * as panelSize from "./panel_size.mjs";
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

// ---- Idle blink: DUPLICATED from JarvisMark.vue's idlePeek timer, by
// necessity not choice (see the jvw-face comment in <template> for why this
// file can't just import JarvisMark). Keep the constants and behavior in
// sync with JarvisMark.vue by hand. Distinct from idleTimer above, which
// fades the whole FAB on page-wide inactivity; this one blinks the face on
// its own regardless of that fade.
const IDLE_PEEK_INTERVAL_MS = 8000;
const IDLE_PEEK_HOLD_MS = 1800; // one jvw-blink cycle, then back to resting
const autoPeek = ref(false);
let idlePeekTimeoutId = null;
let idlePeekHoldTimeoutId = null;

function prefersReducedMotion() {
	return Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)").matches);
}

function scheduleIdlePeek() {
	idlePeekTimeoutId = window.setTimeout(() => {
		autoPeek.value = true;
		idlePeekHoldTimeoutId = window.setTimeout(() => {
			autoPeek.value = false;
		}, IDLE_PEEK_HOLD_MS);
		scheduleIdlePeek();
	}, IDLE_PEEK_INTERVAL_MS);
}

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
// The user's saved window size (null => the shipped default). Loaded from
// localStorage synchronously below so the first render already has it, and fed
// to panelLayout, which floors it at the default and caps it to the viewport.
const prefSize = ref(null);
// Set true while the panel expands to fullscreen just before we hand off to the
// full web chat (resized-to-fullscreen), so the handoff reads as the panel
// growing INTO the big chat rather than a hard cut.
const leaving = ref(false);
const panelBox = computed(() => {
	viewportTick.value; // re-run on resize / orientation change
	const topInset = readCssPx(document.documentElement, "--navbar-height", 48);
	// Handoff to the big chat: aim the panel at the full content area so the root
	// animates open to fullscreen (Panel's .jvp-root--expanding does the easing).
	if (leaving.value) {
		return {
			left: 0,
			top: topInset,
			width: window.innerWidth,
			height: Math.max(0, window.innerHeight - topInset),
			side: side.value,
		};
	}
	return panelLayout(
		{ x: fabXY.value.x, y: fabXY.value.y, size: fabPos.FAB_SIZE },
		{ vw: window.innerWidth, vh: window.innerHeight, top: topInset },
		prefSize.value
	);
});

// Live drag: adopt the new size so the panel re-lays-out each frame (panelLayout
// re-clamps, so this can never paint a sub-default or off-screen panel).
function onPanelResize(size) {
	prefSize.value = size;
}

// Drag released: persist the choice per browser, mirroring the FAB position.
function onPanelResizeCommit() {
	if (!prefSize.value) return;
	// Resized to (near) the whole screen: the mini panel is the wrong tool at
	// that size, so hand off to the full web chat instead of persisting a panel
	// that blankets the Desk. Expand the panel to fullscreen first (with a
	// dimming backdrop) so it reads as growing INTO the full chat, and do NOT
	// persist the fullscreen size (the panel should reopen at its normal size).
	const box = panelBox.value;
	if (box && box.width >= window.innerWidth * 0.9 && box.height >= window.innerHeight * 0.85) {
		leaving.value = true;
		// Navigate just after the 0.32s expand finishes, so the panel has visibly
		// grown into the full chat before the page swaps.
		setTimeout(openFull, 360);
		return;
	}
	try {
		localStorage.setItem(panelSize.STORAGE_KEY, panelSize.serializeSize(prefSize.value));
	} catch (e) {
		/* localStorage unavailable */
	}
}

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

const fabStyle = computed(() => {
	const style = {
		transform: `translate3d(${fabXY.value.x}px, ${fabXY.value.y}px, 0)`,
	};
	if (brandLogoUrl) {
		// Painted on the button itself rather than as an <img> child: a
		// background is clipped by the border-radius by definition, so a logo
		// of any aspect ratio can never bleed past the rounded corners.
		style.backgroundImage = `url("${encodeURI(brandLogoUrl)}")`;
		style.backgroundSize = "cover";
		style.backgroundPosition = "center";
		style.backgroundRepeat = "no-repeat";
	}
	return style;
});

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

// Restore the saved panel size the same way — synchronously, before first paint
// — so a grown panel never flashes at the default size on open.
{
	let savedRaw = null;
	try {
		savedRaw = localStorage.getItem(panelSize.STORAGE_KEY);
	} catch (e) {
		savedRaw = null;
	}
	prefSize.value = panelSize.parseSavedSize(savedRaw);
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
	if (!brandLogoUrl && !prefersReducedMotion()) scheduleIdlePeek();
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
	if (idlePeekTimeoutId) window.clearTimeout(idlePeekTimeoutId);
	if (idlePeekHoldTimeoutId) window.clearTimeout(idlePeekHoldTimeoutId);
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
/* Dimming backdrop behind the panel during the expand-into-the-big-chat handoff.
   Covers the Desk (under the panel, over the page) and fades in as the panel
   grows to fullscreen, so the motion reads as going full. */
.jvw-backdrop {
	position: fixed;
	inset: 0;
	z-index: 1028; /* just under the panel's 1029 */
	background: rgba(18, 15, 40, 0.34);
	opacity: 0;
	pointer-events: none;
	transition: opacity 0.32s ease;
}
.jvw-root--dark .jvw-backdrop {
	background: rgba(0, 0, 0, 0.5);
}
.jvw-backdrop--show {
	opacity: 1;
}
@media (prefers-reduced-motion: reduce) {
	.jvw-backdrop {
		transition: none;
	}
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
	overflow: hidden;
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

.jvw-fab:hover {
	filter: brightness(1.06);
}
.jvw-fab:focus-visible {
	/* A ring that HUGS the rounded tile. outline + outline-offset drew a
	   detached square that looked misaligned against a full-bleed logo. */
	outline: none;
	box-shadow: 0 10px 26px -6px rgba(106, 86, 232, 0.55), 0 0 0 2px #fff, 0 0 0 4px var(--accent);
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

/* ---- blink face ----
   DUPLICATED from JarvisMark.vue's PEEK block (jv-face/jv-eye + jvm-blink),
   by necessity not choice — see the jvw-face comment in <template>. This is a
   deliberate, scoped divergence from design.md 1.3 (no hover motion) the same
   way the accent gradient above already is: the mark's face reveal already
   ships as a sanctioned exception elsewhere (UserMenu.vue's sidebar brand
   mark), so hover/focus revealing it here keeps the FAB consistent with the
   rest of the brand mark rather than introducing a new pattern. Keep this
   block, the template markup, and the idle-blink timing in <script> in sync
   with JarvisMark.vue by hand. */
.jvw-face {
	position: absolute;
	inset: 0;
	display: flex;
	align-items: center;
	justify-content: center;
	gap: 10px;
	opacity: 0;
	transition: opacity 0.25s ease;
}
.jvw-eye {
	width: 7px;
	height: 11px;
	background: #fff;
	border-radius: 999px;
}
.jvw-fab:hover > svg,
.jvw-fab:focus-visible > svg,
.jvw-fab--peek > svg {
	opacity: 0;
}
.jvw-fab:hover .jvw-face,
.jvw-fab:focus-visible .jvw-face,
.jvw-fab--peek .jvw-face {
	opacity: 1;
}

@keyframes jvw-blink {
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

@media (prefers-reduced-motion: no-preference) {
	.jvw-fab:hover .jvw-eye,
	.jvw-fab:focus-visible .jvw-eye,
	.jvw-fab--peek .jvw-eye {
		animation: jvw-blink 1.8s ease-in-out infinite;
	}
	.jvw-fab > svg,
	.jvw-face {
		transition: transform 0.12s ease, opacity 0.25s ease;
	}
	.jvw-fab:active > svg,
	.jvw-fab--dragging > svg,
	.jvw-fab:active .jvw-face,
	.jvw-fab--dragging .jvw-face {
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
