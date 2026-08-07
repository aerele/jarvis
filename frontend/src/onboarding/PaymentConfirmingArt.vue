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
 * Coins and banknotes drift inward and are absorbed into the Jarvis mark,
 * which pulses as each one lands. It renders during the window where the
 * customer has paid on the admin-hosted page and the bench is asking the
 * control plane whether that payment actually landed.
 *
 * WHY THIS EXISTS ALONGSIDE SetupNeuralNet, rather than replacing it or being
 * folded into it: the two illustrate different things and never appear at the
 * same time. This one is value arriving (payment confirming). SetupNeuralNet
 * is a workspace being assembled (provisioning). Sharing one asset across both
 * would mean the same picture claims two different things, which is precisely
 * the class of overstatement jarvis#708/#709 were about. design.md's
 * brand-asset exception was amended to name both, with the one-illustration-
 * per-screen rule written into it.
 *
 * DELIBERATELY NOT EMOJI. The request floated "money notes emoji". Emoji are
 * rendered by the platform font, so the same screen ships a different picture
 * on macOS, Windows and Android; they cannot take a token colour, so they
 * cannot honour the brand-asset exception's "read colours from tokens" rule;
 * they do not adapt to theme; and at the sizes used here they land as raster
 * blobs beside a vector mark. design.md 3.9 is also explicit that emoji are
 * never UI. These are drawn as vectors instead, which themes correctly, scales
 * to any DPR, and reads as money at a glance.
 *
 * COLOUR. Coins are --money-coin, notes are --money-note (see main.css). Both
 * are deliberately off the semantic ramps: --green means success and --amber
 * means warning, and this screen renders while the payment is NOT yet
 * confirmed. Gold leads and green is the minority tone, so the screen never
 * reads as a green "paid" tick before it is true. The status is carried by the
 * copy, never by the colour.
 *
 * Reduced motion renders one calm static frame: the same composition, fully
 * drawn, with nothing travelling. Same contract as SetupNeuralNet and
 * JvSpinner.
 */
import { ref, onMounted, onBeforeUnmount, watch } from "vue";
import { BRAND_STAR_PATH } from "@/lib/brand";

const props = defineProps({
	dark: { type: Boolean, default: false },
});

const rootEl = ref(null);
const canvasEl = ref(null);

const STAR = new Path2D(BRAND_STAR_PATH);

/** How many pieces are in flight at once. Small on purpose: this is a calm
 *  confirmation, not a payout animation. */
const FLIGHT_COUNT = 9;
/** Seconds a piece takes to travel from the rim to the core, before jitter. */
const TRAVEL_SECONDS = 3.4;

let ctx = null;
let W = 0,
	H = 0,
	dpr = 1;
let core = { x: 0, y: 0 };
let ringRx = 0,
	ringRy = 0;
let reduced = false;
let mq = null;
const C = {};

let pieces = [];
let coreFlash = 0,
	t0 = null,
	raf = 0;

function readColors() {
	const el = rootEl.value;
	if (!el) return;
	const cs = getComputedStyle(el);
	// Tokens, never literals - the brand-asset exception requires it.
	C.coin = cs.getPropertyValue("--money-coin").trim() || "#c9922b";
	C.note = cs.getPropertyValue("--money-note").trim() || "#5a9e73";
	C.brand1 = cs.getPropertyValue("--brand-1").trim() || "#6e8bff";
	C.brand2 = cs.getPropertyValue("--brand-2").trim() || "#8b5cf6";
	C.surface = cs.getPropertyValue("--surface").trim() || "#ffffff";
}

/** Hex to rgba(). Kept local so the component never needs a colour library. */
function alpha(hex, a) {
	let h = String(hex || "")
		.trim()
		.replace("#", "");
	if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
	const n = parseInt(h, 16);
	if (h.length !== 6 || Number.isNaN(n)) return `rgba(201,146,43,${a})`;
	return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function clamp(x) {
	return x < 0 ? 0 : x > 1 ? 1 : x;
}

/**
 * A piece starts at a random point on the rim and travels to the core. `phase`
 * spreads the initial population across the journey so the first frame already
 * looks mid-flight instead of firing all seven from the edge at once.
 */
function makePiece(i, phase) {
	const a = Math.random() * Math.PI * 2;
	return {
		a,
		// Alternating with a bias to coins: gold leads, green is the accent.
		kind: i % 3 === 1 ? "note" : "coin",
		t: phase,
		sp: 1 / (TRAVEL_SECONDS * (0.8 + Math.random() * 0.45)),
		spin: (Math.random() - 0.5) * 1.6,
		// A gentle tangential bow so pieces arc in rather than falling straight.
		bow: (Math.random() - 0.5) * 0.5,
		wob: Math.random() * Math.PI * 2,
	};
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
	ringRx = Math.min(W * 0.42, 300);
	ringRy = Math.min(H * 0.44, 190);
	return true;
}

function reset() {
	// Spread the starting population evenly along the path.
	pieces = Array.from({ length: FLIGHT_COUNT }, (_, i) =>
		makePiece(i, (i / FLIGHT_COUNT) * 0.92)
	);
	coreFlash = 0;
	t0 = null;
}

/** Position of a piece at journey fraction t (0 rim, 1 core). */
function pieceAt(p) {
	// Ease-in: pieces accelerate slightly as they are drawn in.
	const e = p.t * p.t * 0.45 + p.t * 0.55;
	const rx = ringRx * (1 - e);
	const ry = ringRy * (1 - e);
	const bow = p.bow * (1 - e) * 1.1;
	const wob = reduced ? 0 : Math.sin(p.wob + p.t * 5) * 3 * (1 - e);
	return {
		x: core.x + Math.cos(p.a + bow) * rx + wob,
		y: core.y + Math.sin(p.a + bow) * ry,
	};
}

function drawCoin(x, y, r, rot, a) {
	ctx.save();
	ctx.translate(x, y);
	// Squash on rotation so it reads as a disc turning in space, not a ball.
	ctx.scale(Math.max(0.32, Math.abs(Math.cos(rot))), 1);
	ctx.globalAlpha = a;
	ctx.beginPath();
	ctx.arc(0, 0, r, 0, 6.283);
	ctx.fillStyle = alpha(C.coin, 0.92);
	ctx.fill();
	ctx.lineWidth = Math.max(1, r * 0.16);
	ctx.strokeStyle = alpha(C.coin, 1);
	ctx.stroke();
	// Inner rim: enough to read as a coin, not enough to become detail noise.
	ctx.beginPath();
	ctx.arc(0, 0, r * 0.54, 0, 6.283);
	ctx.strokeStyle = alpha(C.surface, 0.55);
	ctx.lineWidth = Math.max(0.8, r * 0.14);
	ctx.stroke();
	ctx.restore();
}

function drawNote(x, y, r, rot, a) {
	const w = r * 2.5,
		h = r * 1.45;
	ctx.save();
	ctx.translate(x, y);
	ctx.rotate(rot * 0.5);
	ctx.globalAlpha = a;
	const rr = Math.min(3, h * 0.28);
	ctx.beginPath();
	// Rounded rect, drawn by hand: roundRect is not in every supported engine.
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
	ctx.fillStyle = alpha(C.note, 0.9);
	ctx.fill();
	// The band across the middle is what makes a green rectangle read as a note.
	ctx.beginPath();
	ctx.arc(0, 0, h * 0.26, 0, 6.283);
	ctx.strokeStyle = alpha(C.surface, 0.6);
	ctx.lineWidth = Math.max(0.8, h * 0.1);
	ctx.stroke();
	ctx.restore();
}

function draw(ts) {
	if (t0 == null) t0 = ts;
	const el = (ts - t0) / 1000;
	const dt = 1 / 60;
	ctx.clearRect(0, 0, W, H);

	// --- pieces in flight ---
	pieces.forEach((p, i) => {
		if (!reduced) {
			p.t += dt * p.sp;
			if (p.t >= 1) {
				coreFlash = 1;
				pieces[i] = makePiece(i, 0);
				return;
			}
		}
		const pos = pieceAt(p);
		// Fade in off the rim, fade out as it is absorbed by the core.
		const fadeIn = clamp(p.t / 0.12);
		const fadeOut = clamp((1 - p.t) / 0.18);
		const a = fadeIn * fadeOut;
		if (a <= 0.01) return;
		// Shrink as it approaches, so absorption reads as depth not deletion.
		const r = (8.6 - 2.9 * p.t) * (0.85 + (i % 3) * 0.1);
		const rot = reduced ? 0.6 : el * p.spin + p.wob;
		// A soft trail behind each piece: motion without a hard streak.
		const gl = ctx.createRadialGradient(pos.x, pos.y, 0, pos.x, pos.y, r * 2.6);
		gl.addColorStop(0, alpha(p.kind === "coin" ? C.coin : C.note, 0.2 * a));
		gl.addColorStop(1, alpha(p.kind === "coin" ? C.coin : C.note, 0));
		ctx.fillStyle = gl;
		ctx.beginPath();
		ctx.arc(pos.x, pos.y, r * 2.6, 0, 6.283);
		ctx.fill();
		if (p.kind === "coin") drawCoin(pos.x, pos.y, r, rot, a);
		else drawNote(pos.x, pos.y, r, rot, a);
	});

	// --- core: the Jarvis mark, same construction as SetupNeuralNet so the two
	//     illustrations are visibly the same family ---
	if (coreFlash > 0) coreFlash = Math.max(0, coreFlash - dt * 2.2);
	const breathe = reduced ? 0.5 : 0.5 + 0.5 * Math.sin(el * 1.6);
	const haloR = 32 + breathe * 8 + coreFlash * 15;
	const hg = ctx.createRadialGradient(core.x, core.y, 8, core.x, core.y, haloR);
	hg.addColorStop(0, alpha(C.brand2, 0.26 + coreFlash * 0.32));
	hg.addColorStop(1, alpha(C.brand2, 0));
	ctx.fillStyle = hg;
	ctx.beginPath();
	ctx.arc(core.x, core.y, haloR, 0, 6.283);
	ctx.fill();

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
	reset();
	if (reduced) {
		// One calm static frame: pieces sit spread along the path, nothing moves.
		requestAnimationFrame((ts) => {
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

// The theme flip changes --surface, which this reads for the coin rim and the
// note band. Re-read so those stay legible on the dark card.
watch(
	() => props.dark,
	() => {
		readColors();
	}
);
</script>
