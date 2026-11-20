import { ref, computed, onMounted, onBeforeUnmount } from "vue";

// A draggable, keyboard-operable split between a fixed-width rail and its pane
// (jarvis-admin-v2#759). Unlike components/doc/Resizer.vue (mouse only, a side
// panel with no keyboard path) this is a WAI-ARIA window splitter: the caller
// binds `separatorProps` to a `role="separator"` element and gets pointer drag
// with capture, ArrowLeft/Right/Home/End, double-click reset, and a width
// persisted per key in localStorage.
//
// The width applies from `lg` up only. Below it the rail stacks above the pane,
// so the handle is hidden by CSS and every handler here also refuses to act
// (`isWide`), keeping a stale pointer or key event inert.
export const LG_QUERY = "(min-width: 1024px)";

export function useResizableRail({
	storageKey,
	defaultWidth = 360,
	minWidth = 260,
	maxWidth = 640,
	maxFraction = 0.6,
	step = 16,
	// the board element: the max width is a fraction of ITS width
	hostEl = ref(null),
} = {}) {
	// Before mount the host is unknown; fall back to the window so the first
	// paint is clamped by the same numbers (the stylesheet's percentage max-width
	// caps it either way, so nothing jumps).
	const hostWidth = ref(typeof window === "undefined" ? 1280 : window.innerWidth);
	const measureHost = () => {
		hostWidth.value = hostEl.value?.clientWidth || window.innerWidth;
		// the layout may have flipped to stacked mid-drag: stop, the handle is gone
		if (dragging.value && !isWide()) endDrag();
	};

	const maxNow = computed(() =>
		Math.max(minWidth, Math.min(maxWidth, Math.round(hostWidth.value * maxFraction)))
	);
	const clamp = (w) => Math.min(maxNow.value, Math.max(minWidth, w));

	function readStored() {
		try {
			const n = Number(localStorage.getItem(storageKey));
			// null/""/garbage/0/negative/Infinity all mean "no usable preference"
			return Number.isFinite(n) && n > 0 ? n : defaultWidth;
		} catch {
			return defaultWidth;
		}
	}
	function persist(value) {
		try {
			if (value === defaultWidth) localStorage.removeItem(storageKey);
			else localStorage.setItem(storageKey, String(value));
		} catch {
			// private mode / quota: the width still applies for this page view
		}
	}

	const dragging = ref(false);

	// the preference as stored; the effective width re-clamps it as the board resizes
	const preferred = ref(readStored());
	const width = computed(() => clamp(preferred.value));

	const isWide = () =>
		typeof window === "undefined" || !window.matchMedia || window.matchMedia(LG_QUERY).matches;

	function set(w, save = true) {
		preferred.value = clamp(Math.round(w));
		if (save) persist(preferred.value);
	}

	let startX = 0;
	let startWidth = 0;

	function onPointerDown(e) {
		if (!isWide() || (e.button !== undefined && e.button !== 0)) return;
		// capture first and swallow a throw (stale pointer id): a failure must not leave
		// the drag flag or the body classes set with nothing to end them
		try {
			e.currentTarget.setPointerCapture?.(e.pointerId);
		} catch {
			// the drag still works while the pointer stays over the handle
		}
		startX = e.clientX;
		startWidth = width.value;
		dragging.value = true;
		document.body.classList.add("select-none", "cursor-col-resize");
		e.preventDefault();
	}
	function onPointerMove(e) {
		if (!dragging.value) return;
		set(startWidth + (e.clientX - startX), false);
	}
	function endDrag(e) {
		if (!dragging.value) return;
		dragging.value = false;
		document.body.classList.remove("select-none", "cursor-col-resize");
		try {
			e?.currentTarget?.releasePointerCapture?.(e.pointerId);
		} catch {
			// capture already released (pointercancel)
		}
		persist(preferred.value);
	}

	function onKeydown(e) {
		if (!isWide()) return;
		let next;
		if (e.key === "ArrowLeft") next = width.value - step;
		else if (e.key === "ArrowRight") next = width.value + step;
		else if (e.key === "Home") next = minWidth;
		else if (e.key === "End") next = maxNow.value;
		else return;
		e.preventDefault();
		set(next);
	}
	function reset() {
		if (!isWide()) return;
		set(defaultWidth);
	}

	// The host changes width without the window doing so (the app sidebar collapses),
	// so watch the element; no ResizeObserver (jsdom, old browsers) falls back to the window.
	let observer = null;
	let mq = null;
	const onWideChange = () => {
		if (dragging.value && !isWide()) endDrag();
	};
	onMounted(() => {
		measureHost();
		if (typeof ResizeObserver !== "undefined" && hostEl.value) {
			observer = new ResizeObserver(measureHost);
			observer.observe(hostEl.value);
		} else {
			window.addEventListener("resize", measureHost);
		}
		mq = window.matchMedia?.(LG_QUERY) || null;
		mq?.addEventListener?.("change", onWideChange);
	});
	onBeforeUnmount(() => {
		observer?.disconnect();
		observer = null;
		window.removeEventListener("resize", measureHost);
		mq?.removeEventListener?.("change", onWideChange);
		document.body.classList.remove("select-none", "cursor-col-resize");
	});

	const separatorProps = computed(() => ({
		role: "separator",
		"aria-orientation": "vertical",
		"aria-label": "Resize list",
		"aria-valuemin": minWidth,
		"aria-valuemax": maxNow.value,
		"aria-valuenow": width.value,
		"aria-valuetext": `${width.value} pixels`,
		tabindex: 0,
		onPointerdown: onPointerDown,
		onPointermove: onPointerMove,
		onPointerup: endDrag,
		onPointercancel: endDrag,
		onLostpointercapture: endDrag,
		onKeydown,
		onDblclick: reset,
	}));

	return { width, dragging, separatorProps, measureHost };
}
