// Onboarding permission/confirmation promise (Plan 01, review round-2 P1-05).
//
// The tour used to promise "Asks before it changes anything" — false the moment
// a conversation has auto_apply on, and misleading even by default because it
// omits the destructive-op carve-out. The approved wording below is DERIVED from
// the real default policy (Jarvis Conversation.auto_apply default 0 = confirm
// every write; destructive ops park regardless), and permissionCopy.spec.js
// binds the two together so a future policy flip can't silently leave the tour
// over-promising again.

// Jarvis Conversation.auto_apply default, from the DocType JSON:
//   {"fieldname":"auto_apply","fieldtype":"Check","default":"0", ...
//    "Default 0 (confirm every write)."}
export const DEFAULT_AUTO_APPLY = 0;
// Destructive ops (delete/cancel/amend/send_email) ALWAYS park, even when
// auto_apply is on — see the same field's description.
export const DESTRUCTIVE_ALWAYS_CONFIRMS = true;

export const PERMISSION_TOUR_COPY =
	"By default, shows proposed changes and asks you to confirm before writing; " +
	"destructive actions always require confirmation.";

// The copy the tour must show for a given default policy. Only the shipped
// default (confirm-first + destructive-always-confirm) may claim the approved
// promise; any weaker default has to describe itself honestly instead.
export function permissionCopyFor(policy = {}) {
	const autoApply = Number(policy.autoApply || 0);
	const destructiveAlwaysConfirms = policy.destructiveAlwaysConfirms !== false;
	if (autoApply === 0 && destructiveAlwaysConfirms) return PERMISSION_TOUR_COPY;
	if (destructiveAlwaysConfirms) {
		return "Applies changes automatically; destructive actions always require confirmation.";
	}
	return "May change things automatically. Review your automation settings.";
}
