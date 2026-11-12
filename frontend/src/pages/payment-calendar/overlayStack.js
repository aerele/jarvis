import { inject, onBeforeUnmount, onMounted, provide, watch } from "vue";

// Escape order of the page (C8): the highest number closes first.
export const OVERLAY_PRIORITY = Object.freeze({
	aux: 50,
	details: 40,
	picker: 20,
	company: 10,
});

const STACK_KEY = Symbol("pcal-overlay-stack");

// The open overlays of one page. The page owns the single document keydown listener,
// so Escape never reaches two overlays at once.
export class OverlayStack {
	constructor() {
		this.entries = [];
		this.handleKeydown = (event) => {
			// Escape that confirms an IME composition is not a request to close.
			if (event.key === "Escape" && !event.isComposing && this.closeTop())
				event.preventDefault();
		};
	}

	register(priority, close) {
		const entry = { priority, close };
		this.entries.push(entry);
		return () => {
			this.entries = this.entries.filter((item) => item !== entry);
		};
	}

	// Highest priority wins; among equals the most recently registered one.
	closeTop() {
		const top = this.entries.reduce(
			(best, entry) => (!best || entry.priority >= best.priority ? entry : best),
			null
		);
		top?.close();
		return !!top;
	}
}

export function provideOverlayStack() {
	const stack = new OverlayStack();
	onMounted(() => document.addEventListener("keydown", stack.handleKeydown));
	onBeforeUnmount(() => document.removeEventListener("keydown", stack.handleKeydown));
	provide(STACK_KEY, stack);
	return stack;
}

// `isOpen` is a getter or a ref-like source for watch(); `close` runs when Escape picks this overlay.
export function useOverlay(isOpen, priority, close) {
	const stack = inject(STACK_KEY, null);
	if (!stack) return;
	let unregister = null;
	const stop = () => {
		unregister?.();
		unregister = null;
	};
	watch(
		isOpen,
		(open) => {
			stop();
			if (open) unregister = stack.register(priority, close);
		},
		{ immediate: true }
	);
	onBeforeUnmount(stop);
}
