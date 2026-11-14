import { onMounted, onUnmounted, ref } from "vue";

/**
 * useNarrow - true while the viewport is at most `maxWidth` px wide (default 640, the
 * breakpoint SupportListPage and ActivityTab already drop columns at). A list uses it
 * to stack its columns into one card cell instead of squeezing six columns to a letter
 * each or scrolling sideways.
 */
export function useNarrow(maxWidth = 640) {
	const narrow = ref(false);
	let mq = null;
	const onChange = (e) => (narrow.value = e.matches);
	onMounted(() => {
		if (typeof window === "undefined" || !window.matchMedia) return;
		mq = window.matchMedia(`(max-width: ${maxWidth}px)`);
		narrow.value = mq.matches;
		mq.addEventListener("change", onChange);
	});
	onUnmounted(() => mq && mq.removeEventListener("change", onChange));
	return narrow;
}
