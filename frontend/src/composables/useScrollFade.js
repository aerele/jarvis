// macOS-style overlay scrollbar for popover/dropdown scroll regions. The
// scrollbar track is permanently invisible; a scroll event marks the element
// as `.is-scrolling` (revealing the thumb via the `.jv-scroll-fade` CSS in
// main.css) and a timer removes that class ~800ms after scrolling stops.
//
// Exposed as a Vue custom directive rather than a ref-based composable so it
// drops onto ANY scrollable element with a single `v-scroll-fade` attribute
// — including elements repeated by v-for (each gets its own listener/timer),
// which a single-ref composable can't handle without extra per-item wiring.
// Vue 3.2+ auto-registers a `<script setup>` import named `vXxx` as the
// directive `v-xxx`, so no global registration in main.js is needed.
const HIDE_DELAY_MS = 800;
const timers = new WeakMap();

function handleScroll(el) {
	el.classList.add("is-scrolling");
	const prev = timers.get(el);
	if (prev) clearTimeout(prev);
	timers.set(
		el,
		setTimeout(() => {
			el.classList.remove("is-scrolling");
			timers.delete(el);
		}, HIDE_DELAY_MS)
	);
}

export const vScrollFade = {
	mounted(el) {
		el.classList.add("jv-scroll-fade");
		el.__jvScrollFadeHandler = () => handleScroll(el);
		el.addEventListener("scroll", el.__jvScrollFadeHandler, { passive: true });
	},
	unmounted(el) {
		el.removeEventListener("scroll", el.__jvScrollFadeHandler);
		delete el.__jvScrollFadeHandler;
		const t = timers.get(el);
		if (t) clearTimeout(t);
		timers.delete(el);
	},
};
