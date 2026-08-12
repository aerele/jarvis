// Promise-based "copy this chat into your new ticket?" prompt — same shared-
// state pattern as useConfirm.js (a single mounted dialog driven by module-scope
// state), kept as its own composable rather than a 3rd useConfirm() button
// because this one also renders a preview of what would be copied, and its
// resolution has three distinct meanings (yes-this-once / no-this-once /
// stop-asking), not a plain boolean.
import { ref } from "vue";

// { preview: string } while open, null when closed. The mounted dialog is the
// only reader; call sites never touch it directly.
export const copyPromptState = ref(null);
let _resolve = null;

// Resolves "yes" | "no" | "dontask". One at a time, same reasoning as
// useConfirm: a second call while one is open resolves "no" rather than
// clobbering what the user is looking at.
export function promptSupportCopy({ preview } = {}) {
	if (_resolve) return Promise.resolve("no");
	return new Promise((resolve) => {
		_resolve = resolve;
		copyPromptState.value = { preview: preview || "" };
	});
}

export function settleSupportCopyPrompt(val) {
	copyPromptState.value = null;
	const r = _resolve;
	_resolve = null;
	if (r) r(val);
}

export function useSupportCopyPrompt() {
	return { promptSupportCopy };
}
