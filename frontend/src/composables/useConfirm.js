import { ref } from "vue";

// App-wide confirmation dialog — a promise-based, drop-in replacement for the
// native window.confirm(). A SINGLE <ConfirmDialog> mounted in the app shell
// (components/shell/ConfirmDialog.vue) renders from this shared state, so any
// component can:
//
//   import { useConfirm } from "@/composables/useConfirm"
//   const { confirm } = useConfirm()
//   if (await confirm({ title: "Remove model?", message: "…", danger: true,
//                       confirmLabel: "Remove" })) { …destructive action… }
//
// confirm() resolves true when the user confirms and false on Cancel / backdrop
// click / Escape. This was extracted from ChatView's former inline confirmDialog
// so there is one implementation, not several.
//
// WHICH CONFIRM TO USE:
//   • This useConfirm() — on the custom jv- surfaces (chat, settings, AI models,
//     onboarding). It renders with the jv- design tokens so it looks native there.
//   • frappe-ui's confirmDialog({ onConfirm }) — on the frappe-ui-styled list/
//     detail pages (macros, skills, files, wiki, agents), which are built from
//     frappe-ui components and match its dialog. Don't cross the streams.

// The live request, or null when no dialog is open. The mounted ConfirmDialog is
// the only reader; call sites never touch it directly.
export const confirmState = ref(null); // { title, message, confirmLabel, cancelLabel, danger }
let _resolve = null;

export function confirm(opts = {}) {
	// One dialog at a time. If one is already open, DON'T clobber what the user is
	// looking at — the extra request resolves false (treated as not confirmed) and
	// the visible dialog keeps its own awaiter. (Two concurrent confirms only happen
	// from programmatic paths; a modal blocks the user from triggering a second.)
	if (_resolve) return Promise.resolve(false);
	return new Promise((resolve) => {
		_resolve = resolve;
		confirmState.value = {
			title: opts.title || "Are you sure?",
			message: opts.message || "",
			confirmLabel: opts.confirmLabel || "Confirm",
			cancelLabel: opts.cancelLabel || "Cancel",
			// General-purpose: neutral (primary) by default. Destructive call sites MUST
			// pass danger:true to get the red confirm button — don't assume deletes
			// default to danger (they do not here). Deliberately opt-in, unlike the old
			// ChatView confirmDialog which defaulted danger true.
			danger: opts.danger === true,
		};
	});
}

// Called by <ConfirmDialog> when the user resolves the dialog (button, backdrop,
// or Escape). Safe to call when nothing is open.
export function settleConfirm(val) {
	confirmState.value = null;
	const r = _resolve;
	_resolve = null;
	if (r) r(val);
}

export function useConfirm() {
	return { confirm };
}
