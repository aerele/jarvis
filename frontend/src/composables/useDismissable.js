import { onBeforeUnmount, watch } from "vue";

/**
 * Dismiss an open dropdown/menu on an outside click, a focusout past the
 * root, or the Escape key.
 *
 * Used by the composer's ModelEffortPicker (persona now lives inside it) so the
 * click-outside + Escape + focusout + listener-cleanup logic lives in one
 * place instead of being re-implemented per component.
 *
 * @param {import('vue').Ref<HTMLElement|null>} rootRef     element that defines "inside"
 * @param {import('vue').Ref<boolean>} open                 the open-state ref to watch
 * @param {() => void} [onDismiss]                           runs on dismissal; defaults to
 *                                                           closing `open`. Pass a custom fn
 *                                                           when closing also clears sub-state.
 * @param {import('vue').Ref<HTMLElement|null>} [triggerRef] the trigger button, refocused on
 *                                                           Escape so keyboard and
 *                                                           screen-reader users land back where
 *                                                           they opened the menu from. Not used
 *                                                           for outside-click/focusout dismissal,
 *                                                           since focus there is already moving
 *                                                           somewhere on purpose.
 *
 * Listeners are attached only while `open` is true and removed on close and on
 * unmount. The click listener uses capture (true) so it fires before inner
 * handlers stop propagation, matching the pre-extraction behaviour.
 */
export function useDismissable(rootRef, open, onDismiss, triggerRef) {
	const dismiss =
		onDismiss ||
		(() => {
			open.value = false;
		});

	function onDocClick(e) {
		if (rootRef.value && !rootRef.value.contains(e.target)) dismiss();
	}
	function onKey(e) {
		if (e.key === "Escape") {
			// ChatView's global key handler (onGlobalKey) opens with
			// `if (e.defaultPrevented) return;` so this must claim the key,
			// otherwise Escape would also fall through to it and cancel
			// dictation in the same keystroke that closes the menu.
			e.preventDefault();
			dismiss();
			triggerRef?.value?.focus();
		}
	}
	function onFocusOut(e) {
		// relatedTarget is also null when focus moves to a non-focusable node
		// inside the menu (separators, labels), not just when it leaves the
		// document entirely, so treating that as "outside" would dismiss a
		// menu the user just clicked into. Outside clicks are still covered by
		// the capture-phase click listener above, and a real tab-out always
		// supplies a relatedTarget.
		if (!e.relatedTarget) return;
		if (rootRef.value && !rootRef.value.contains(e.relatedTarget)) dismiss();
	}

	watch(open, (isOpen) => {
		if (isOpen) {
			document.addEventListener("click", onDocClick, true);
			document.addEventListener("keydown", onKey);
			rootRef.value?.addEventListener("focusout", onFocusOut);
		} else {
			document.removeEventListener("click", onDocClick, true);
			document.removeEventListener("keydown", onKey);
			rootRef.value?.removeEventListener("focusout", onFocusOut);
		}
	});
	onBeforeUnmount(() => {
		document.removeEventListener("click", onDocClick, true);
		document.removeEventListener("keydown", onKey);
		rootRef.value?.removeEventListener("focusout", onFocusOut);
	});
}
