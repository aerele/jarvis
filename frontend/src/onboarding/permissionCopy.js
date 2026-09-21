// Onboarding permission/confirmation promise (Plan 01, review round-2 P1-05).
//
// The tour promises "shows proposed changes and asks you to confirm before
// writing; destructive actions always require confirmation." As of the
// action-card overhaul EVERY real change asks - admin Auto-Apply was removed, so
// there is no per-conversation default that can weaken this - which makes the
// promise unconditionally true. permissionCopy.spec.js binds the tour copy to the
// policy below so a future weakening can't silently make the promise false again
// (as the old "Asks before it changes anything" wording did once auto_apply
// existed).

// Destructive ops (delete/cancel/amend) are always braked - they require
// confirmation in every mode. Kept as an explicit invariant the tour copy leans on.
export const DESTRUCTIVE_ALWAYS_CONFIRMS = true;

export const PERMISSION_TOUR_COPY =
	"By default, shows proposed changes and asks you to confirm before writing; " +
	"destructive actions always require confirmation.";

// The copy the tour must show for a given policy. With Auto-Apply removed the only
// axis left is the destructive carve-out; the confirm-first default is structural,
// so any policy that still always-confirms destructive ops may claim the approved
// promise, and only a (hypothetical) weakening has to describe itself honestly.
export function permissionCopyFor(policy = {}) {
	const destructiveAlwaysConfirms = policy.destructiveAlwaysConfirms !== false;
	if (destructiveAlwaysConfirms) return PERMISSION_TOUR_COPY;
	return "May change things automatically. Review your automation settings.";
}
