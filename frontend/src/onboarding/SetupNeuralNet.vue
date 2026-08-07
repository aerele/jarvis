<template>
	<!-- Root ref: readColors() reads the Jarvis CSS vars (--text-3, --surface,
		 --surface-3) via getComputedStyle on THIS element, not document.documentElement.
		 Those vars are inline-applied on an ancestor (.jv-ob-root's paletteVars in
		 OnboardingView.vue), not on :root, so they only resolve correctly once
		 inherited down to a node inside that scope - this component's root div.

		 Both this div and the canvas fill via absolute + inset-0 (NOT h-full on
		 the div): the parent .jv-ob-setup-net uses min-height, and percentage
		 heights don't resolve against min-height, so h-full collapsed the canvas
		 to 0px and nothing drew. Do not change this to h-full. -->
	<div ref="rootEl" class="absolute inset-0">
		<canvas ref="canvasEl" class="absolute inset-0 block h-full w-full"></canvas>
	</div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, watch } from "vue";
import { BRAND_STAR_PATH } from "@/lib/brand";

const props = defineProps({
	dark: { type: Boolean, default: false },
});

const rootEl = ref(null);
const canvasEl = ref(null);

// ---- ported verbatim from the approved preview
// (setup-animation-preview.html): 12 ERP module nodes drifting/wandering
// around a central Jarvis core, connected by spoke + ring edges, with glowing
// pulses streaming from modules into the core. Reduced-motion renders one
// calm static frame. ----
const MODS = [
	"Sales",
	"Purchase",
	"Accounts",
	"Stock",
	"Manufacturing",
	"Projects",
	"CRM",
	"HR",
	"Payroll",
	"Assets",
	"Compliance",
	"Quality",
];
const STAR = new Path2D(BRAND_STAR_PATH);

let ctx = null;
let W = 0,
	H = 0,
	dpr = 1;
let core = { x: 0, y: 0 };
let nodes = [];
let cssRx = 0,
	cssRy = 0;
let reduced = false;
let mq = null;
const C = {};

let pulses = [],
	nextSpoke = 1.7,
	nextRing = 2.2,
	coreFlash = 0,
	t0 = null,
	raf = 0;

/** Hex to rgba(), so the brand tokens can be used at partial opacity for the
 *  edges, node rings and halo without a second hard-coded copy of each colour. */
function alpha(hex, a) {
	let h = String(hex || "")
		.trim()
		.replace("#", "");
	if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
	const n = parseInt(h, 16);
	if (h.length !== 6 || Number.isNaN(n)) return `rgba(110,139,255,${a})`;
	return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function readColors() {
	const el = rootEl.value;
	if (!el) return;
	const cs = getComputedStyle(el);
	C.label = cs.getPropertyValue("--text-3").trim() || "#83838b";
	C.nodeFill = cs.getPropertyValue("--surface").trim() || "#fff";
	C.nodeStroke = cs.getPropertyValue("--surface-3").trim() || "#ececef";
	// The brand pair comes from the tokens in main.css, never from literals
	// here. design.md's brand-asset exception allows this illustration to keep
	// the brand gradient only "provided they honor prefers-reduced-motion and
	// read their colors from tokens, not hard-coded rgba" - and until now this
	// file carried its own copies of #6e8bff/#8b5cf6 plus a spread of
	// rgba(110,139,255,...) literals, so a brand refresh would have landed
	// everywhere except the one screen that is nothing but brand.
	C.brand1 = cs.getPropertyValue("--brand-1").trim() || "#6e8bff";
	C.brand2 = cs.getPropertyValue("--brand-2").trim() || "#8b5cf6";
}

function layout() {
	const cv = canvasEl.value;
	if (!cv) return;
	const r = cv.getBoundingClientRect();
	W = r.width;
	H = r.height;
	dpr = Math.min(2, window.devicePixelRatio || 1);
	cv.width = Math.round(W * dpr);
	cv.height = Math.round(H * dpr);
	ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
	core = { x: W / 2, y: H / 2 };
	cssRx = Math.min(W * 0.33, 300);
	cssRy = Math.min(H * 0.34, 150);
	nodes = MODS.map((label, i) => {
		const a = -Math.PI / 2 + i * ((2 * Math.PI) / MODS.length);
		return {
			label,
			a,
			bx: core.x + Math.cos(a) * cssRx,
			by: core.y + Math.sin(a) * cssRy,
			phase: Math.random() * Math.PI * 2,
			phase2: Math.random() * Math.PI * 2,
			seed: Math.random(),
		};
	});
}

function reset() {
	pulses = [];
	nextSpoke = 1.7;
	nextRing = 2.2;
	coreFlash = 0;
	t0 = null;
}

function clamp(x) {
	return x < 0 ? 0 : x > 1 ? 1 : x;
}

// organic wander: two frequencies per axis so nodes drift around their slot
// rather than bobbing in place. Slow + moderate amplitude = calm, alive.
function pos(i, el) {
	const n = nodes[i];
	if (reduced) return { x: n.bx, y: n.by };
	const x = n.bx + Math.sin(el * 0.5 + n.phase) * 11 + Math.cos(el * 0.29 + n.phase2) * 6;
	const y = n.by + Math.cos(el * 0.43 + n.phase) * 10 + Math.sin(el * 0.24 + n.phase2) * 5;
	return { x, y };
}

function draw(ts) {
	if (t0 == null) t0 = ts;
	const el = (ts - t0) / 1000;
	ctx.clearRect(0, 0, W, H);
	const P = nodes.map((_, i) => pos(i, el));

	// --- spokes (draw-in over intro), then ring edges ---
	ctx.lineWidth = 1.1;
	nodes.forEach((n, i) => {
		const tA = 0.35 + i * 0.08,
			dp = reduced ? 1 : clamp((el - tA) / 0.55);
		if (dp <= 0) return;
		const p = P[i],
			ex = core.x + (p.x - core.x) * dp,
			ey = core.y + (p.y - core.y) * dp;
		ctx.strokeStyle = alpha(C.brand1, 0.16);
		ctx.beginPath();
		ctx.moveTo(core.x, core.y);
		ctx.lineTo(ex, ey);
		ctx.stroke();
	});
	const ra = reduced ? 1 : clamp((el - 1.4) / 0.8);
	if (ra > 0) {
		ctx.strokeStyle = alpha(C.brand1, 0.1 * ra);
		for (let i = 0; i < nodes.length; i++) {
			const a = P[i],
				b = P[(i + 1) % nodes.length];
			ctx.beginPath();
			ctx.moveTo(a.x, a.y);
			ctx.lineTo(b.x, b.y);
			ctx.stroke();
		}
	}

	// --- pulses ---
	if (!reduced) {
		const dt = 1 / 60;
		if (el > nextSpoke) {
			pulses.push({
				type: "spoke",
				i: (Math.random() * nodes.length) | 0,
				t: 0,
				sp: 0.8 + Math.random() * 0.5,
			});
			nextSpoke = el + 0.2 + Math.random() * 0.18;
		}
		if (el > nextRing) {
			pulses.push({
				type: "ring",
				i: (Math.random() * nodes.length) | 0,
				t: 0,
				sp: 0.5 + Math.random() * 0.4,
			});
			nextRing = el + 0.7 + Math.random() * 0.5;
		}
		for (let k = pulses.length - 1; k >= 0; k--) {
			const pu = pulses[k];
			pu.t += dt * pu.sp;
			let x, y;
			if (pu.type === "spoke") {
				const p = P[pu.i];
				x = p.x + (core.x - p.x) * pu.t;
				y = p.y + (core.y - p.y) * pu.t;
			} else {
				const a = P[pu.i],
					b = P[(pu.i + 1) % nodes.length];
				x = a.x + (b.x - a.x) * pu.t;
				y = a.y + (b.y - a.y) * pu.t;
			}
			if (pu.t >= 1) {
				if (pu.type === "spoke") coreFlash = 1;
				pulses.splice(k, 1);
				continue;
			}
			const g = ctx.createRadialGradient(x, y, 0, x, y, 9);
			g.addColorStop(0, alpha(C.brand2, 0.9));
			g.addColorStop(1, alpha(C.brand2, 0));
			ctx.fillStyle = g;
			ctx.beginPath();
			ctx.arc(x, y, 9, 0, 6.283);
			ctx.fill();
			ctx.fillStyle = "rgba(255,255,255,0.95)";
			ctx.beginPath();
			ctx.arc(x, y, 1.7, 0, 6.283);
			ctx.fill();
		}
	}

	// --- module nodes + labels ---
	ctx.font = "600 11px Inter, system-ui, sans-serif";
	nodes.forEach((n, i) => {
		const tA = 0.35 + i * 0.08,
			ap = reduced ? 1 : clamp((el - tA) / 0.45);
		if (ap <= 0) return;
		const p = P[i];
		ctx.globalAlpha = ap;
		// node
		ctx.beginPath();
		ctx.arc(p.x, p.y, 6.5, 0, 6.283);
		ctx.fillStyle = C.nodeFill;
		ctx.fill();
		ctx.lineWidth = 1.6;
		ctx.strokeStyle = alpha(C.brand1, 0.55);
		ctx.stroke();
		ctx.beginPath();
		ctx.arc(p.x, p.y, 2.4, 0, 6.283);
		ctx.fillStyle = alpha(C.brand1, 0.9);
		ctx.fill();
		// label radially outward
		const lx = p.x + Math.cos(n.a) * 17,
			ly = p.y + Math.sin(n.a) * 17;
		const cx = Math.cos(n.a);
		ctx.textAlign = Math.abs(cx) < 0.35 ? "center" : cx > 0 ? "left" : "right";
		ctx.textBaseline = "middle";
		ctx.fillStyle = C.label;
		ctx.fillText(n.label, lx, ly);
		ctx.globalAlpha = 1;
	});

	// --- core: pulsing halo + gradient disc + star ---
	if (coreFlash > 0) coreFlash = Math.max(0, coreFlash - (1 / 60) * 2.2);
	const breathe = reduced ? 0.5 : 0.5 + 0.5 * Math.sin(el * 1.6);
	const haloR = 34 + breathe * 9 + coreFlash * 16;
	const hg = ctx.createRadialGradient(core.x, core.y, 8, core.x, core.y, haloR);
	hg.addColorStop(0, alpha(C.brand2, 0.28 + coreFlash * 0.35));
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
	layout();
	reset();
	if (reduced) {
		// one static calm frame
		raf = requestAnimationFrame((ts) => {
			t0 = ts - 9999;
			draw(ts);
			cancelAnimationFrame(raf);
		});
	} else {
		raf = requestAnimationFrame(draw);
	}
}

let ro = null;
let started = false;

// This component lives inside a v-show container, so it MOUNTS hidden
// (display:none -> 0x0) and getBoundingClientRect reads 0 in onMounted; a window
// resize never fires on the v-show reveal. A ResizeObserver DOES fire when the
// element goes 0 -> visible, so we (re)layout then. First real size starts the
// animation (plays the intro); later resizes just re-lay-out. Reduced-motion has
// no loop, so it redraws its single static frame on any real-size change.
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

// Theme toggle changes the CSS vars this component reads (label/node colors);
// re-read them so labels stay legible. The edge/pulse/core colors are fixed
// brand rgba values, unaffected by theme.
watch(
	() => props.dark,
	() => {
		readColors();
	}
);
</script>
