<template>
	<!-- Root ref: readColors() reads the money/brand tokens and the jv-* palette
		 via getComputedStyle on THIS element, not document.documentElement. The
		 jv-* vars are inline-applied on an ancestor (.jv-ob-root's paletteVars in
		 OnboardingView.vue), so they only resolve once inherited down to a node
		 inside that scope - this component's root div.

		 Both this div and the canvas fill via absolute + inset-0 (NOT h-full on
		 the div): the parent uses min-height, and percentage heights don't
		 resolve against min-height, so h-full collapses the canvas to 0px and
		 nothing draws. Same trap as SetupNeuralNet. Do not change this. -->
	<div ref="rootEl" class="absolute inset-0">
		<canvas ref="canvasEl" class="absolute inset-0 block h-full w-full"></canvas>
	</div>
</template>

<script setup>
/**
 * PaymentConfirmingArt - the payment-confirming illustration.
 *
 * Banknotes travel along a hairline rail into the Jarvis mark, which pulses as
 * each one lands. It renders during the window where the customer has paid on
 * the admin-hosted page and the bench is asking the control plane whether that
 * payment actually landed.
 *
 * WHY THIS EXISTS ALONGSIDE SetupNeuralNet, rather than replacing it or being
 * folded into it: the two illustrate different things and never appear at the
 * same time. This one is value arriving (payment confirming). SetupNeuralNet is
 * a workspace being assembled (provisioning). Sharing one asset across both
 * would mean the same picture claims two different things, which is precisely
 * the class of overstatement jarvis#708/#709 were about. design.md's
 * brand-asset exception was amended to name both, with the
 * one-illustration-per-screen rule written into it.
 *
 * NOTES, NOT COINS. Circles read as a slot machine, which is the last thing to
 * put on the screen that appears immediately after taking someone's money.
 * Rectangular notes on a rail read as a transfer.
 *
 * NOT EMOJI. Emoji are drawn by the platform font, so the same screen would
 * ship a different picture on macOS, Windows and Android; they cannot take a
 * token colour, so they cannot satisfy the brand-asset exception's "read
 * colours from tokens" condition; and they do not adapt to theme. design.md
 * 3.9 is also explicit that emoji are never UI. The rupee glyph here is drawn
 * as canvas text in white on the note, so it themes with everything else.
 *
 * COLOUR. --money-note / --money-note-bd (see main.css), deliberately off the
 * semantic success ramp: --green means success, and this screen renders while
 * the payment is NOT yet confirmed. The status is carried by the copy, never by
 * the colour.
 *
 * Reduced motion renders one calm static frame: the notes at rest along the
 * rail, and no halo. Same contract as SetupNeuralNet and JvSpinner.
 */
import { ref, onMounted, onBeforeUnmount, watch } from "vue";
import { BRAND_STAR_PATH } from "@/lib/brand";

const props = defineProps({
	dark: { type: Boolean, default: false },
});

const rootEl = ref(null);
const canvasEl = ref(null);

const STAR = new Path2D(BRAND_STAR_PATH);

/** Note geometry, in CSS px. */
const NOTE_W = 22;
const NOTE_H = 13;
const NOTE_R = 3;
/** Tilted so a note reads as in-motion even in the static reduced-motion frame. */
const NOTE_TILT = (-7 * Math.PI) / 180;

/** One note's full journey, and the gap between consecutive departures. Four
 *  notes at 0.6s spacing over a 2.8s loop keeps roughly three visible at once,
 *  evenly spread: a steady arrival, not a burst. */
const LOOP_SECONDS = 2.8;
const STAGGER_SECONDS = 0.6;
const NOTE_COUNT = 4;

let ctx = null;
let W = 0,
	H = 0,
	dpr = 1;
let core = { x: 0, y: 0 };
let railHalf = 0;
let reduced = false;
let mq = null;
const C = {};

let notes = [];
let coreFlash = 0,
	t0 = null,
	raf = 0;

function readColors() {
	const el = rootEl.value;
	if (!el) return;
	const cs = getComputedStyle(el);
	// Tokens, never literals - the brand-asset exception requires it.
	C.note = cs.getPropertyValue("--money-note").trim() || "#1f8a54";
	C.noteBd = cs.getPropertyValue("--money-note-bd").trim() || "#34a76a";
	C.brand1 = cs.getPropertyValue("--brand-1").trim() || "#6e8bff";
	C.brand2 = cs.getPropertyValue("--brand-2").trim() || "#8b5cf6";
	C.rail = cs.getPropertyValue("--border-2").trim() || "#dfdfe4";
}

/** Hex to rgba(). Kept local so the component never needs a colour library. */
function alpha(hex, a) {
	let h = String(hex || "")
		.trim()
		.replace("#", "");
	if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
	const n = parseInt(h, 16);
	if (h.length !== 6 || Number.isNaN(n)) return `rgba(31,138,84,${a})`;
	return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function clamp(x) {
	return x < 0 ? 0 : x > 1 ? 1 : x;
}

/**
 * Notes alternate sides so value visibly converges ON the mark rather than
 * streaming past it, which is what a single-direction rail looks like once the
 * mark sits centred. Still one rail.
 */
function buildNotes() {
	notes = Array.from({ length: NOTE_COUNT }, (_, i) => ({
		side: i % 2 === 0 ? -1 : 1,
		// Seconds into the loop at which this note departs.
		offset: i * STAGGER_SECONDS,
	}));
}

function layout() {
	const cv = canvasEl.value;
	if (!cv) return false;
	const r = cv.getBoundingClientRect();
	W = r.width;
	H = r.height;
	if (W < 2 || H < 2) return false;
	dpr = Math.min(2, window.devicePixelRatio || 1);
	cv.width = Math.round(W * dpr);
	cv.height = Math.round(H * dpr);
	ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
	core = { x: W / 2, y: H / 2 };
	railHalf = Math.min(W * 0.42, 320);
	return true;
}

/** Journey fraction for note `n` at elapsed time `el`: 0 at the rail end, 1 at
 *  the mark. In reduced motion the notes do not advance; they are parked at
 *  their departure offsets instead (see draw). */
function progress(n, el) {
	const local = (el - n.offset) % LOOP_SECONDS;
	return local < 0 ? (local + LOOP_SECONDS) / LOOP_SECONDS : local / LOOP_SECONDS;
}

function drawNote(x, y, a) {
	if (a <= 0.01) return;
	ctx.save();
	ctx.translate(x, y);
	ctx.rotate(NOTE_TILT);
	ctx.globalAlpha = a;

	const w = NOTE_W,
		h = NOTE_H,
		rr = NOTE_R;
	ctx.beginPath();
	// Rounded rect by hand: roundRect is not in every engine this ships to.
	ctx.moveTo(-w / 2 + rr, -h / 2);
	ctx.lineTo(w / 2 - rr, -h / 2);
	ctx.quadraticCurveTo(w / 2, -h / 2, w / 2, -h / 2 + rr);
	ctx.lineTo(w / 2, h / 2 - rr);
	ctx.quadraticCurveTo(w / 2, h / 2, w / 2 - rr, h / 2);
	ctx.lineTo(-w / 2 + rr, h / 2);
	ctx.quadraticCurveTo(-w / 2, h / 2, -w / 2, h / 2 - rr);
	ctx.lineTo(-w / 2, -h / 2 + rr);
	ctx.quadraticCurveTo(-w / 2, -h / 2, -w / 2 + rr, -h / 2);
	ctx.closePath();
	ctx.fillStyle = C.note;
	ctx.fill();
	ctx.lineWidth = 1;
	ctx.strokeStyle = C.noteBd;
	ctx.stroke();

	// The rupee mark, drawn as text so it inherits nothing from the platform.
	ctx.fillStyle = "#ffffff";
	ctx.font = "700 8px Inter, system-ui, sans-serif";
	ctx.textAlign = "center";
	ctx.textBaseline = "middle";
	ctx.fillText("₹", 0, 0.5);

	ctx.restore();
}

function draw(ts) {
	if (t0 == null) t0 = ts;
	const el = (ts - t0) / 1000;
	const dt = 1 / 60;
	ctx.clearRect(0, 0, W, H);

	// --- the rail: one hairline through the mark, faded at both ends so it
	//     reads as a track that emerges and dissolves rather than a cut line ---
	const rg = ctx.createLinearGradient(core.x - railHalf, 0, core.x + railHalf, 0);
	rg.addColorStop(0, alpha(C.rail, 0));
	rg.addColorStop(0.28, alpha(C.rail, 0.9));
	rg.addColorStop(0.72, alpha(C.rail, 0.9));
	rg.addColorStop(1, alpha(C.rail, 0));
	ctx.strokeStyle = rg;
	ctx.lineWidth = 1;
	ctx.beginPath();
	ctx.moveTo(core.x - railHalf, core.y);
	ctx.lineTo(core.x + railHalf, core.y);
	ctx.stroke();

	// --- notes ---
	notes.forEach((n) => {
		let t;
		if (reduced) {
			// Parked along the rail: a still frame of the same composition, never a
			// blank stage. Spread across the middle of the journey rather than
			// mapped straight from the departure offsets, because that put the
			// first note at t=1 - exactly on the core, hidden behind the mark - so
			// the static frame showed one note fewer than the animation.
			t = 0.18 + (n.offset / LOOP_SECONDS) * 0.62;
		} else {
			t = progress(n, el);
			// Arrival: pulse the mark exactly once as the note is absorbed.
			if (n.last != null && t < n.last) coreFlash = 1;
			n.last = t;
		}
		const x = core.x + n.side * railHalf * (1 - t);
		// Fade in early off the rail end, fade out as the mark takes it.
		const a = clamp(t / 0.14) * clamp((1 - t) / 0.16);
		drawNote(x, core.y, reduced ? 1 : a);
	});

	// --- core: the Jarvis mark, same construction as SetupNeuralNet so the two
	//     illustrations are visibly the same family ---
	if (!reduced) {
		if (coreFlash > 0) coreFlash = Math.max(0, coreFlash - dt * 2.2);
		const breathe = 0.5 + 0.5 * Math.sin(el * 1.6);
		const haloR = 30 + breathe * 7 + coreFlash * 15;
		const hg = ctx.createRadialGradient(core.x, core.y, 8, core.x, core.y, haloR);
		hg.addColorStop(0, alpha(C.brand2, 0.24 + coreFlash * 0.32));
		hg.addColorStop(1, alpha(C.brand2, 0));
		ctx.fillStyle = hg;
		ctx.beginPath();
		ctx.arc(core.x, core.y, haloR, 0, 6.283);
		ctx.fill();
	}

	const R = 22;
	const dg = ctx.createLinearGradient(core.x - R, core.y - R, core.x + R, core.y + R);
	dg.addColorStop(0, C.brand1);
	dg.addColorStop(1, C.brand2);
	ctx.fillStyle = dg;
	ctx.beginPath();
	ctx.arc(core.x, core.y, R, 0, 6.283);
	ctx.fill();

	ctx.save();
	const s = (2 * R * 0.82) / 24;
	ctx.translate(core.x - R * 0.82, core.y - R * 0.82);
	ctx.scale(s, s);
	ctx.fillStyle = "#fff";
	ctx.fill(STAR);
	ctx.restore();

	raf = requestAnimationFrame(draw);
}

function start() {
	if (!canvasEl.value) return;
	cancelAnimationFrame(raf);
	readColors();
	if (!layout()) return;
	buildNotes();
	coreFlash = 0;
	t0 = null;
	if (reduced) {
		raf = requestAnimationFrame((ts) => {
			t0 = ts;
			draw(ts);
			cancelAnimationFrame(raf);
		});
	} else {
		raf = requestAnimationFrame(draw);
	}
}

let ro = null;
let started = false;

// Mirrors SetupNeuralNet: this can mount inside a hidden container, where
// getBoundingClientRect reads 0 and no window resize ever fires on reveal. A
// ResizeObserver DOES fire on 0 -> visible, so (re)layout there. Reduced motion
// has no loop, so it redraws its single frame on any real-size change.
function measure() {
	const el = rootEl.value;
	if (!el) return;
	const r = el.getBoundingClientRect();
	if (r.width < 2 || r.height < 2) return;
	if (!started || reduced) {
		started = true;
		start();
	} else {
		readColors();
		layout();
	}
}

function onReducedMotionChange(e) {
	reduced = e.matches;
	if (started) start();
}

onMounted(() => {
	const cv = canvasEl.value;
	if (!cv) return;
	ctx = cv.getContext("2d");
	mq = window.matchMedia("(prefers-reduced-motion: reduce)");
	reduced = mq.matches;
	mq.addEventListener("change", onReducedMotionChange);
	ro = new ResizeObserver(measure);
	ro.observe(rootEl.value);
	window.addEventListener("resize", measure);
	measure();
});

onBeforeUnmount(() => {
	cancelAnimationFrame(raf);
	if (ro) {
		ro.disconnect();
		ro = null;
	}
	window.removeEventListener("resize", measure);
	if (mq) mq.removeEventListener("change", onReducedMotionChange);
});

// The theme flip changes the money and rail tokens this reads. Re-read them, and
// redraw immediately when there is no animation loop to do it on the next frame.
watch(
	() => props.dark,
	() => {
		readColors();
		if (started && reduced) start();
	}
);
</script>
