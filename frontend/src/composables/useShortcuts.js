// Global keyboard shortcuts (DESIGN-V3 §3.1) - lifts Helpdesk's normalization:
//   - `meta: true` means ⌘ on Mac and Ctrl everywhere else
//   - shortcuts are suppressed while the user is typing (input / textarea /
//     contenteditable / inside [role=dialog] or [role=menu]) - EXCEPT chords
//     that carry a ctrl/meta/alt modifier: pressing Ctrl+Shift+O with the
//     caret in the composer is a command, not typing (parity: New Chat must
//     work from the chat composer, which holds focus almost always).
// ⌘K IS bound here (via AppShell): JarvisCommandPalette is built on plain
// Dialog and owns no keys itself (the stock CommandPalette - which bound ⌘K
// internally, old DA-06 - never mounted its Dialog subtree in 0.1.278).
import { onMounted, onBeforeUnmount } from "vue";

const isMac =
	typeof navigator !== "undefined" &&
	/Mac|iPod|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

function isTyping(e) {
	const el = e.target;
	if (!el || !(el instanceof Element)) return false;
	const tag = el.tagName;
	return (
		tag === "INPUT" ||
		tag === "TEXTAREA" ||
		el.isContentEditable ||
		!!el.closest("[role=dialog]") ||
		!!el.closest("[role=menu]")
	);
}

// bindings: [{ key, meta?, ctrl?, shift?, alt?, handler }]
//   meta: true  → cmd on Mac, ctrl elsewhere (HD normalization)
//   ctrl: true  → the literal Ctrl key on every platform
export function useShortcuts(bindings) {
	function matches(e, b) {
		if (e.key.toLowerCase() !== String(b.key).toLowerCase()) return false;
		const wantMeta = !!b.meta;
		const wantCtrl = !!b.ctrl;
		// Normalize: meta:true means ctrl on non-Mac.
		const needCmd = wantMeta && isMac;
		const needCtrl = wantCtrl || (wantMeta && !isMac);
		if (needCmd !== e.metaKey) return false;
		if (needCtrl !== e.ctrlKey) return false;
		if (!!b.shift !== e.shiftKey) return false;
		if (!!b.alt !== e.altKey) return false;
		return true;
	}

	function onKeydown(e) {
		if (e.defaultPrevented) return;
		for (const b of bindings) {
			if (!matches(e, b)) continue;
			const hasModifier = b.ctrl || b.meta || b.alt;
			if (!hasModifier && isTyping(e)) return;
			e.preventDefault();
			b.handler(e);
			return;
		}
	}

	onMounted(() => window.addEventListener("keydown", onKeydown));
	onBeforeUnmount(() => window.removeEventListener("keydown", onKeydown));
}
